"""Scan-level delivery: context build, single-scan fit, triage, and the scan task pool.

Split verbatim out of :mod:`isar.session.driver` (P2.6b) so the driver orders batch stages rather
than implementing them. `build_context` and `fit_one` generalize the two gold-check `run_pipeline`
recipes - same analysis calls, same order - with the inputs (zero, resolution, slit, prior,
guidelines) derived from card + state instead of hardcoded.
"""
from __future__ import annotations

import copy
import logging
import time
from collections import deque
from concurrent.futures import (FIRST_COMPLETED, Future, ProcessPoolExecutor,
                                wait)
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import numpy as np

from isar.analyze import errors, fast_pass, loop, select, verdict
from isar.analyze.evidence import EVIDENCE_REVISION
from isar.analyze import gold as gold_mod
from isar.analyze.components import CentralQE, ConstantBackground, Elastic, PhononDHO, QuasiElastic
from isar.analyze.context import ScanContext, effective_exposure, select_exposure
from isar.analyze.descriptor import describe_spectrum
from isar.analyze.phonon import EPS_OMEGA0, _phonon_for
from isar.analyze.prior import PriorSettings, mode_bounds, width_ceiling
from isar.analyze.progress import ProgressFn, emitter, relay
from isar.schema.card import Card
from isar.schema.scan import Flag, exposure_for
from isar.schema.measurement import (MeasurementArena, campaign_provenance,
                                     current_lattice_epoch)
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    INTERVAL_SCHEMA,
    PROBABILITY_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    semantics_fingerprint,
    validate_for_write,
)
from isar.schema.steering import IntensityMeasure, ParamConstraints, evidence_sink_for
from isar.session import branch_assignment as BA
from isar.session import (
    branch_model, branch_tracking, calibrations, guidelines as guide_mod, session_prior,
)
from isar.session import deep_delivery, predictive_artifact
from isar.session.outcome import FitOutcome
from isar.session.predictive_artifact import model_manifest_of as _model_manifest_of
from isar.session.state import ScanEntry

log = logging.getLogger(__name__)

# Steering Phase 2c: apply the low-q branch soft-constraint when the dispersion prior predicts a
# phonon within this many resolution FWHM of the elastic line (the merging regime where Γ/A go
# unidentifiable). Above it the peak is well separated and the fit is left unconstrained.
LOW_Q_FWHM_FACTOR = 2.0
# Error tiers that run the ordinary physical-fit frequentist interval (MINOS + parametric bootstrap
# + ω₀ profile). "bayes" is a strict superset of "full" — everything full does, plus the
# nested-sampling posterior on the selected label. That single-fit Bayesian interval is
# diagnostics-only; pooled-master certification separately uses the mixed predictive interval.
FULL_TIERS = ("full", "bayes")


class AnalysisWorkIncomplete(RuntimeError):
    """The soft analysis deadline stopped dispatch before the canonical worklist completed."""


# ===========================================================================
# context + single-scan fit (the gold-check recipe, generalized)
# ===========================================================================
def build_context(record, detector_id, card: Card, calres, prior, g: guide_mod.Guidelines,
                  *, scan_number=None, q=None, T=None):
    """A ScanContext with the monitor-short mask and the guideline fit-window mask folded in.

    Returns ``(ctx, n_used, region_reasons)`` or ``(None, n_used, region_reasons)`` when too few
    points survive (undersampled non-result). P5: only ``ctx.mask`` is ever narrowed — the raw
    intensity arrays are untouched. ``region_reasons`` records the guideline masking provenance
    (empty when no ``fit_range``/``exclude_regions`` applied)."""
    ctx = ScanContext.from_scan(
        record, detector_id, card, center=calres.center,
        gauss_fwhm=calres.gauss_fwhm, lorentz_fwhm=calres.lorentz_fwhm,
        prior_omega=(prior.omega0 if prior else None),
        resolution_curve=getattr(calres, "curve", None))
    mask = np.asarray(ctx.mask, dtype=bool)
    norm = _norm_of(record, detector_id)
    if norm.counting_mode == "monitor" and float(norm.preset) > 0:
        mon = np.asarray(norm.monitor, dtype=float)
        mask = mask & (mon >= g.monitor_frac * float(norm.preset))

    # Guideline fit-window mask (P5: mask only; intensity never touched).
    region_reasons: list = []
    if scan_number is not None:
        fit_range, excl = g.mask_regions_for(scan_number=scan_number, q=q, T=T)
        x = np.asarray(ctx.x, dtype=float)
        if fit_range is not None:
            lo, hi = float(fit_range[0]), float(fit_range[1])
            keep = (x >= lo) & (x <= hi)
            dropped = int((mask & ~keep).sum())
            mask = mask & keep
            if dropped:
                region_reasons.append(f"fit_range [{lo:g}, {hi:g}] masked {dropped} points")
        for r in excl:
            lo, hi = float(r[0]), float(r[1])
            inside = (x >= lo) & (x <= hi)
            dropped = int((mask & inside).sum())
            mask = mask & ~inside
            if dropped:
                region_reasons.append(f"exclude_region [{lo:g}, {hi:g}] masked {dropped} points")

    ctx = replace(ctx, mask=mask)
    n_used = int(mask.sum())
    if n_used < g.min_fit_points:
        return None, n_used, region_reasons
    curve = ctx.resolution_curve
    if curve is not None:
        # The table is used as given (zero beyond it): a fit range that needs more is flagged, not extended.
        xu = ctx.x_used
        lo, hi = float(xu.min()) - ctx.center, float(xu.max()) - ctx.center
        if not curve.covers(lo, hi):
            msg = (f"resolution_range_exceeded: {detector_id}: the fit range [{lo:.1f}, {hi:.1f}] meV about "
                   f"the zero leaves the resolution table [{curve.range[0]:.1f}, {curve.range[1]:.1f}] meV; "
                   f"the table is used as given")
            region_reasons.append(msg)
            if hasattr(calres, "add_flag"):
                calres.add_flag(Flag("resolution_range_exceeded", "warning", msg))
    return ctx, n_used, region_reasons


def _norm_of(rec, det=None):
    """The normalization that applies to one spectrum: its own exposure where it carries it
    (``exposure_for``), else the record's. Stub records without spectra fall back to the record's."""
    sp = (getattr(rec, "spectra", None) or {}).get(det)
    if sp is not None and (getattr(sp, "monitor", None) is not None or getattr(sp, "time", None) is not None):
        return exposure_for(rec, det)
    return getattr(rec, "normalization", None)


# In-window detector counts floor for trusting the delta-method HESSE ω₀ interval. Calibrated on
# synthetic peaks: HESSE σ tracks the bootstrap half-width (ratio ≈ 1.0, slightly conservative) down
# to ~100 counts for a well-separated peak — the real HESSE-failure regime is Γ→0 (near-elastic /
# broad), which `gamma_at_bound` already gates. So this is only a sparsity floor against fit
# instability at genuinely few counts; kept low (200) to trust weaker peaks too — a q that converges
# on a trusted HESSE interval is re-checked by the full bootstrap in the certify pass anyway, so this
# gate need not be perfect, only avoid the degenerate ultra-sparse case.
_HESSE_MIN_COUNTS = 200.0


def _in_window_counts(ctx) -> float:
    """Total detector counts over the fit window (the kept points)."""
    try:
        counts = np.asarray(ctx.counts, dtype=float)
        mask = np.asarray(ctx.mask, dtype=bool)
        return float(counts[mask].sum())
    except Exception:  # noqa: BLE001 — a malformed ctx just fails the counts gate
        return 0.0


def _fit_prior_settings(fit):
    """The prior settings ``fit`` recorded (its ``fit_semantics`` recipe); ``None``: the recipe default."""
    recipe = (getattr(fit, "fit_semantics", None) or {}).get("prior_recipe") or {}
    return PriorSettings(**recipe["settings"]) if "settings" in recipe else None


def _mode_hesse_trusted(mode: dict, *, covariance_ok: bool, counts_ok: bool, settings=None) -> bool:
    """Whether ONE fitted mode's delta-method HESSE σ(ω₀) can be trusted.

    The fit-level ``hesse_trusted`` (:func:`_hesse_interval_and_trust`) is the primary mode's
    verdict, and its Γ-at-bound test rails on ANY mode. That is right for the primary and wrong
    for every other mode: on the optic reference path at h = 2.1 the fit found the optic at
    6.3 meV (Γ = 0.1, σ = 0.02 meV, 40σ) beside a near-elastic second mode whose Γ sat at the
    floor, and the target — selected by the tracker, certified on its own HESSE chain — inherited
    the rival's distrust for eleven scans (2026-09-08). A mode's trust is its own: the fit's
    covariance and counts (shared), its own Γ off both bounds, its own σ finite and positive.
    """
    from isar.analyze.phonon import _gamma_at_bound

    sig = mode.get("omega0_err")
    gamma = mode.get("gamma")
    try:
        sig_ok = sig is not None and np.isfinite(float(sig)) and float(sig) > 0.0
        gamma_ok = gamma is not None and np.isfinite(float(gamma)) \
            and not _gamma_at_bound([gamma], [mode.get("omega0")] if mode.get("omega0") is not None else None,
                                    settings=settings)
    except (TypeError, ValueError):
        return False
    return bool(covariance_ok and counts_ok and sig_ok and gamma_ok)


def _hesse_interval_and_trust(best, ctx) -> tuple:
    """(interval, trusted) for the fast tier.

    ``interval`` is the delta-method ω₀ ± 1σ_HESSE (0.68, matching the bootstrap level), or None
    when the HESSE σ is not a finite positive number (a non-positive-definite covariance on the
    derived ω₀). ``trusted`` additionally requires the covariance to be valid, Γ off its zero-bound,
    and enough in-window counts — the conditions under which HESSE σ ≈ the bootstrap half-width."""
    if best is None:
        return None, False
    w0, sig = best.omega0, best.omega0_err_hesse
    if w0 is None or sig is None or not np.isfinite(w0) or not np.isfinite(sig) or sig <= 0:
        return None, False           # non-PD / degenerate → no usable HESSE interval
    interval = (float(w0 - sig), float(w0 + sig))
    trusted = (bool(getattr(best, "covariance_ok", False))
               and not bool(getattr(best, "gamma_at_bound", False))
               and _in_window_counts(ctx) >= _HESSE_MIN_COUNTS)
    return interval, trusted


def _exposure_per_point(rec, ctx, det=None) -> tuple[float, str]:
    """Median per-point counting exposure over the kept points, and the counting mode.

    The portable normaliser for the fitted amplitude (steering contract 2): monitor counts (or
    seconds) per point over the fit window (``ctx.mask``). NaN when unavailable → no IntensityMeasure.
    """
    norm = _norm_of(rec, det)
    if norm is None:
        return float("nan"), ""
    # The mask is the fit-window shape authority and remains available on lightweight consumers
    # that intentionally omit the acquisition grid (for example portable intensity reconstruction).
    selected = select_exposure(norm, np.asarray(ctx.mask, dtype=bool).shape, ctx.mask)
    mode = selected.source or (getattr(norm, "counting_mode", "") or "")
    if selected.absolute is None:
        return float("nan"), mode
    try:
        a = np.asarray(selected.absolute, dtype=float)
        m = np.asarray(ctx.mask, dtype=bool)
        if a.size == m.size and m.any():
            a = a[m]
        a = a[np.isfinite(a) & (a > 0)]
        return (float(np.median(a)) if a.size else float("nan")), mode
    except Exception:  # noqa: BLE001 — a malformed normalization just yields no intensity measure
        return float("nan"), mode


def _primary_amplitude(fit) -> float | None:
    """INTRINSIC amplitude of the primary phonon (the mode nearest the reported ω₀).

    Intrinsic, not measured (H4): the branch model's log A(ω₀) trend is in the lineshape's
    intrinsic A (``amplitude_intrinsic`` = h / UnitPeak.height), so the acoustic law is literally
    A ∝ 1/ω with no occupation contamination.

    A mode record without ``amplitude_intrinsic`` yields ``None`` rather than falling back to the
    measured ``amplitude``: the two differ by (n+1), and relabelling a measured value as intrinsic
    is the mis-scaling this function exists to prevent. Every post-H4 fit persists the field, so
    the fallback only ever named a record this code should refuse to interpret."""
    modes = fit.get("modes")
    w0 = fit.get("omega0")
    if not modes or w0 is None:
        return None
    prim = min(modes, key=lambda m: abs(float(m.get("omega0", 1e18)) - float(w0)))
    a = prim.get("amplitude_intrinsic")
    return float(a) if a is not None and np.isfinite(float(a)) else None


def _intensity_measure(rec, ctx, fit, det=None) -> dict | None:
    """Portable :class:`IntensityMeasure` dict for the primary phonon (intrinsic amplitude per
    exposure, H4), or None when not computable."""
    amp = _primary_amplitude(fit)
    if amp is None:
        return None
    exposure, mode = _exposure_per_point(rec, ctx, det)
    if not np.isfinite(exposure) or exposure <= 0:
        return None
    return IntensityMeasure(
        value=amp / exposure, exposure=exposure, counting_mode=(mode or "monitor"),
        gauss_fwhm=float(ctx.gauss_fwhm), lorentz_fwhm=float(ctx.lorentz_fwhm),
        instrument=str(ctx.instrument)).to_dict()


def _effective_exposure_for(rec, ctx, fit, *, absolute_exposure=None,
                            absolute_exposure_source=None, target_center=None,
                            acquisition_target_energy=None,
                            submitted_ncount: int | None = None, det=None) -> dict:
    """One exposure definition for single scans and pooled masters."""
    target = target_center
    if target is None or not np.isfinite(target):
        target = fit.get("omega0")
    if target is None or not np.isfinite(target):
        target = acquisition_target_energy
    if target is None or not np.isfinite(target):
        target = getattr(ctx, "acoustic_omega", None)
    if target is None or not np.isfinite(target):
        target = getattr(ctx, "prior_omega", None)
    if target is None or not np.isfinite(target):
        target = 0.5 * (float(ctx.window[0]) + float(ctx.window[1]))
    exposure = absolute_exposure
    normalization_source = absolute_exposure_source
    if exposure is None and rec is not None:
        norm = _norm_of(rec, det)
        selected = select_exposure(norm, np.asarray(ctx.x).shape, ctx.mask)
        exposure = selected.absolute
        normalization_source = selected.source
    info = effective_exposure(
        ctx.x, exposure, ctx.mask, target_center=float(target),
        resolution_fwhm=float(ctx.gauss_fwhm) + float(ctx.lorentz_fwhm),
        submitted_ncount=submitted_ncount)
    info["normalization_source"] = normalization_source
    return info


def _acquisition_provenance_for(rec, card, scan_id: str) -> tuple[str, dict | None]:
    """Resolve a driven TAVI scan through machine state, else return its stable standalone ID."""
    raw = getattr(rec, "raw_header", None) or {}
    tavi = raw.get("tavi") if isinstance(raw, dict) else None
    job_id = tavi.get("job_id") if isinstance(tavi, dict) else None
    if job_id is not None:
        for item in reversed(getattr(card.state, "acquisition_provenance", None) or []):
            if str(item.get("job_id")) == str(job_id):
                acquisition_id = item.get("acquisition_id")
                if acquisition_id:
                    return str(acquisition_id), item
    return f"scan:{scan_id}", None


def _measurement_record(rec, det, q, T, sample, scan_id, card, chash, *, count_trust, reasons,
                        energy_window=None, energy_bands=None, effective_exposure=None,
                        exposure_provenance=None) -> dict:
    """The counts-authoritative record for this arena (SCENE-4 PR 4.0, work package #26).

    Built at every terminal outcome of :func:`_process_one` — a gate refusal, an undersampled
    context, or a built context — and, for the built-context case, BEFORE ``fit_one`` is even
    called, so it survives a fit crash on the same arena untouched (the hard constraint the tests
    in ``tests/session/test_measurement_record.py`` pin). ``count_trust="trusted"`` is the only
    state a scene may ever build coverage from (:mod:`isar.scene.build`); the other states
    (``"excluded"`` / ``"held_for_calibration"`` / ``"undersampled"`` / the pre-dispatch
    ``"attempted"`` stub) are recorded as attempted but never as coverage. ``chash`` is the
    delivery-generation identity (the same ``content_hash`` stamped into ``results.csv``): the
    scene adapter joins a delivered fit row to this record on ``scan_ref`` AND ``content_hash``.

    The campaign-provenance block (SCENE-4 PR 4.3, D2) is stamped VERBATIM off the acquisition
    record the driver persisted at submission — this layer never derives campaign identity, and a
    manual or pre-4.3 scan (no acquisition record) leaves all four fields ``None``.
    """
    _, acquisition = _acquisition_provenance_for(rec, card, scan_id)
    return MeasurementArena(
        scan_ref=scan_id, scan_id=scan_id, scan_number=rec.scan_number, detector=det,
        sample=sample,
        conditions=(("temperature_K", float(T)),) if T is not None else (),
        q=(tuple(float(v) for v in q) if q is not None else None),
        lattice_epoch=current_lattice_epoch(getattr(card.state, "lattice_epochs", None) or []),
        energy_window=energy_window, energy_bands=energy_bands,
        effective_exposure=effective_exposure, exposure_provenance=exposure_provenance,
        count_trust=count_trust, reasons=tuple(reasons),
        written_at=datetime.now(timezone.utc).isoformat(), content_hash=chash,
        **campaign_provenance(acquisition),
    ).to_dict()


def _build_descriptor(ctx):
    """The model-free L0 :class:`ScanDescriptor` for this scan, or None on any failure — a report
    aid + Phase-4 falsification input, never a reason to fail a fit. Built once per scan and reused
    for both the verdict (`classify`) and storage (the fit's mask/counts don't change across the
    hard-fix retry, so one object serves both)."""
    try:
        return describe_spectrum(ctx.x, ctx.counts, center=ctx.center, gauss_fwhm=ctx.gauss_fwhm,
                                 lorentz_fwhm=ctx.lorentz_fwhm, mask=ctx.mask,
                                 exposure_scale=ctx.exposure_scale)
    except Exception:  # noqa: BLE001
        return None


def _bayes_prior_for(prior, card):
    # Production posterior is deliberately uninformative (bounds-only): posterior≈likelihood,
    # so bayes-vs-bootstrap disagreement diagnoses the methods, not the prior. Campaign-
    # prediction priors are future steering work; this seam is where they plug in.
    return None


def _timing_provenance(fit) -> dict:
    """Per-method wall-time + (bayes tier only) sampler-config provenance from a fit dict. The
    ``bayes_*``/``dynesty_version`` keys are added only when the bayes tier ran (``dynesty_version``
    is set only there). Kept alongside — not replacing — the ``errors_attach_seconds`` lump.
    ``fast_pass`` is the fast pass's own timing (C-timing)."""
    prov = {"fast_pass": fit.get("fast_pass_timing"),
            "bootstrap_seconds": fit.get("bootstrap_seconds"),
            "minos_seconds": fit.get("minos_seconds"),
            "profile_seconds": fit.get("profile_seconds"),
            "bayes_seconds": fit.get("bayes_seconds"),
            "bootstrap_M_requested": fit.get("bootstrap_M_requested"),
            "bootstrap_M_effective": fit.get("bootstrap_M_effective"),
            "fit_role": fit.get("fit_role"),
            "errors_tier_requested": fit.get("errors_tier_requested"),
            "errors_tier_effective": fit.get("errors_tier_effective"),
            "escalation_reasons": list(fit.get("escalation_reasons") or ()),
            "promotion_state": fit.get("promotion_state")}
    if fit.get("dynesty_version") is not None:
        prov["bayes_n_live"] = fit.get("bayes_n_live")
        prov["bayes_seed"] = fit.get("bayes_seed")
        prov["dynesty_version"] = fit.get("dynesty_version")
    if fit.get("deep_pass_timing") is not None:
        prov["deep_pass"] = fit["deep_pass_timing"]
    return prov


def _amplitudes_of(best, by_hyp) -> dict | None:
    """The delivered fit's linear amplitudes (P0-3): ``{linear_param_name: value}`` for the winning
    structure, in ``CompositeModel.linear_names()`` order. Rebuilt from the winner's own composite
    (``by_hyp[best.label].build()``) so the keys are exactly its linear params (phonon area(s), the
    elastic/quasi-elastic area, background) and nothing non-linear (Ω_d/Γ) leaks in. Values are the
    fitted amplitudes already stored on ``best.params`` (plain Python floats). ``None`` when there is
    no delivered fit or its hypothesis is not in ``by_hyp`` (never expected — every returned label is
    an enumerated hypothesis)."""
    if best is None:
        return None
    hyp = by_hyp.get(best.label)
    if hyp is None:
        return None
    return {n: float(best.params[n]) for n in hyp.build().linear_names() if n in best.params}


def _mode_omegas(result) -> list:
    """The candidate's per-mode DERIVED ω₀ (meV), one entry per mode OF A REAL RESULT. The scene layer
    places EVERY candidate's modes in (Q,ω) space, not just the winner's (only the winner's ``modes``
    are persisted elsewhere), and it must place them on the scale everything else publishes —
    ``FitOutcome.omega0``, the branch models, the dispersion prior. The raw fitted Ω_d would be
    off-scale for every damped mode. Ascending in the clean case (``modes`` is already canonicalized
    ascending by ω₀); a mode with no usable ω₀ contributes ``None`` in its own slot — the delivery
    contract for "no number" is None, never NaN — and mode order is then preserved rather than sorted.
    ``[]`` for a phonon-less candidate (bg, elastic+bg, qe+bg) and when no result is available at all,
    in which case the row's ``n_modes`` (defaulted to 1 without a result) disagrees with this length."""
    values = []
    for m in getattr(result, "modes", None) or ():
        if not isinstance(m, dict):
            # A non-dict mode is a structural bug upstream, not a missing measurement: report it
            # rather than let it read as an unplaceable mode.
            log.warning("candidate %r: mode entry is %s, not a dict — omitting its ω₀ position",
                        getattr(result, "label", None), type(m).__name__)
            values.append(None)
            continue
        w = m.get("omega0")
        values.append(float(w) if w is not None and np.isfinite(w) else None)
    return values if None in values else sorted(values)


def _energy_bands(x, mask, center) -> list:
    """The measured energy CELLS of the kept bins, merged into runs, on the ω scale.

    ``energy_window`` is the outer envelope of these; this is what the scan actually measured. A QC
    mask, an ``exclude_region`` or a monitor-short stretch removes bins from the middle of the axis,
    and a single (lo, hi) pair cannot express that — it claims the hole as measured.

    Adjacency is STRUCTURAL, read off the original grid rather than from the kept points' spacing.
    Each kept bin covers its cell — from the midpoint to its left neighbour on the ORIGINAL axis to
    the midpoint to its right, with the end bins extending half their adjacent step outward — and two
    kept cells with no dropped bin between them share a midpoint exactly, so they merge; a dropped
    bin opens a hole exactly where it sits. There is no tunable gap threshold, and there must not be
    one: any factor times a global spacing statistic gets a non-uniform axis wrong in both
    directions. A coarse-to-fine stitched axis has no single spacing, so a threshold either shatters
    the coarse half into zero-width points or claims dropped fine bins as measured; a comb rejection
    (every other bin dropped) has a perfectly uniform kept spacing and no threshold can see the
    holes at all. Reading the grid answers both without a knob.

    A lone kept bin is a real cell of its neighbours' width, not a zero-width point: it WAS measured.
    ``[]`` when nothing was kept — no coverage at all, which is not the same claim as one degenerate
    band at an arbitrary position.
    """
    xs = np.asarray(x, dtype=float)
    keep = np.asarray(mask, dtype=bool)
    if not np.all(np.isfinite(xs)):
        # A band derived through a NaN midpoint would persist a bare NaN token in the results.csv
        # JSON cell and a footprint that covers nothing anywhere. The abscissa is instrument data,
        # so a non-finite value in it is a delivery defect to fix, not a number to guard around.
        raise ValueError(f"energy axis carries {int((~np.isfinite(xs)).sum())} non-finite value(s): "
                         "a measured footprint cannot be derived from it")
    order = np.argsort(xs, kind="stable")
    xs, keep = xs[order] - float(center), keep[order]
    if not keep.any():
        return []
    edges = _cell_edges(xs)
    idx = np.flatnonzero(keep)
    cuts = [0, *(int(i) + 1 for i in np.flatnonzero(np.diff(idx) > 1)), idx.size]
    return [[float(edges[idx[a]]), float(edges[idx[b - 1] + 1])] for a, b in zip(cuts, cuts[1:])]


def _ctx_energy_window(ctx) -> tuple | None:
    """The QC-masked signed energy window a context covers, or ``None`` when nothing survived the
    mask or the calibration zero is unusable (a scan is held until calibration exists — DESIGN
    §5 — so a non-finite centre is a broken fit, never an implied 0.0).

    Shared by the pre-fit measurement-arena record (PR 4.0 #26, built before ``fit_one`` even
    runs) and the delivered fit's own ``energy_window`` — one derivation, so the two never
    disagree about what a context measured.
    """
    if np.size(ctx.x_used) and np.isfinite(ctx.center):
        lo, hi = ctx.window
        return (lo - float(ctx.center), hi - float(ctx.center))
    return None


def _cell_edges(xs) -> np.ndarray:
    """``len(xs) + 1`` cell boundaries: the midpoints, with the ends extended half a step outward."""
    if xs.size == 1:
        # One bin and no neighbour to take a width from. Its cell is the point itself — degenerate,
        # and honestly so: nothing in the axis says how wide the measurement was.
        return np.array([xs[0], xs[0]])
    mids = 0.5 * (xs[:-1] + xs[1:])
    return np.concatenate([[xs[0] - 0.5 * (xs[1] - xs[0])], mids,
                           [xs[-1] + 0.5 * (xs[-1] - xs[-2])]])


# What each enumerated component class IS, for the scene layer's component descriptors. Matched by
# class rather than by any name string: the enumerated structure is the single authority on what a
# candidate is made of, and a label is an identifier the scene must never have to parse.
# ``None`` = not a scene component (the background is everywhere and claims nothing; the BL43LXU
# central QE is phenomenological and claims no phenomenon).
_COMPONENT_KINDS = ((PhononDHO, "mode"), (Elastic, "elastic"), (QuasiElastic, "quasielastic"),
                    (CentralQE, None), (ConstantBackground, None))
# Distinct from the legitimate ``None`` above, which means "known, and not a scene component".
_UNKNOWN_KIND = object()


def _component_descriptors(label, hyp) -> list:
    """The ordered component descriptors of one candidate, from the structure it was fitted as.

    Canonical order — modes ascending by slot, then the non-mode kinds by name — so two builds of
    the same fit persist byte-identical rows regardless of the order the catalogue happened to
    assemble the composite in. ``slot`` indexes the row's ``omegas``, which the enumeration keeps
    aligned: the ``phonon``/``phonon2`` instances are the modes, in that order.

    Raises when a candidate's hypothesis is missing or holds a component class this map does not
    know. Both are structural bugs (``select_models`` refuses a scored label with no hypothesis),
    and both would otherwise persist a candidate whose composition the scene reads as "nothing" —
    silently unpricing its physics rather than failing where the mistake is.
    """
    if hyp is None:
        raise ValueError(f"candidate {label!r} was scored with no enumerated hypothesis: its "
                         "component composition cannot be persisted, and the scene layer may not "
                         "reconstruct it from the label")
    out, slot = [], 0
    for component in hyp.components:
        cls = component if isinstance(component, type) else type(component)
        kind = next((k for base, k in _COMPONENT_KINDS if issubclass(cls, base)), _UNKNOWN_KIND)
        if kind is _UNKNOWN_KIND:
            raise ValueError(f"candidate {label!r} holds component {cls.__name__}, which declares "
                             "no scene component kind — add it to _COMPONENT_KINDS")
        if kind is None:
            continue
        if kind == "mode":
            out.append({"kind": kind, "slot": slot})
            slot += 1
        else:
            out.append({"kind": kind, "slot": None})
    return sorted(out, key=lambda d: (d["slot"] is None, d["slot"] or 0, d["kind"]))


_BOOTSTRAP_CONVERGENCE_FLOOR_V1 = 0.8
"""Minimum ``n_converged / M`` for a bootstrap interval to count as producer-sound (P0-C).

Provisional v1 threshold, to be calibrated against real coverage data in SCENE-7 — this is a
placeholder floor, not a derived number. The floor exists at all because a percentile interval
built mostly from replicates whose optimizer never converged is not a sampling distribution any
more: past some fraction of fabricated/divergent draws, the "interval" is describing the
optimizer's failure mode rather than ω₀'s uncertainty, and projecting it as trustworthy
within-candidate uncertainty would mint precision the run never earned.
"""


def _producer_soundness(item: dict, within: dict) -> dict:
    """The writer-stamped verdict on whether ``item`` (a persisted ``isar.interval_semantics/1``
    quantity) is fit-conditionally sound (P0-C / P1-H): a converged, unimodal, genuinely
    frequentist-sampling-distribution bootstrap interval. This is the ONLY authority a candidate
    slot ever carries — delivery selection plays no role, so a winner and a non-winner survivor
    with the same record get the identical verdict, and the cell cannot go stale when delivery
    later changes its mind.

    ``{"authority_basis": "producer_sound"}`` iff EVERY signal below holds; otherwise
    ``{"authority_basis": "producer_unsound", "reasons": (...)}`` with one reason code per failing
    signal, sorted for a stable, diffable persisted shape.
    """
    reasons = []
    if item.get("availability") is not True:
        reasons.append("unavailable")
    if item.get("interval_kind") != "parametric_bootstrap_percentile":
        reasons.append("not_bootstrap_percentile")
    if item.get("inference_paradigm") != "frequentist_sampling_distribution":
        reasons.append("not_frequentist_sampling_distribution")
    if within.get("bimodal"):
        reasons.append("bimodal")
    n_converged, M = within.get("n_converged"), within.get("M")
    if n_converged is None or not M:
        reasons.append("convergence_unrecorded")
    elif n_converged / M < _BOOTSTRAP_CONVERGENCE_FLOOR_V1:
        reasons.append("bootstrap_underconverged")
    if reasons:
        return {"authority_basis": "producer_unsound", "reasons": tuple(sorted(reasons))}
    return {"authority_basis": "producer_sound"}


def _mode_interval_item(item: dict | None, values, source_quantity: str, within: dict) -> dict | None:
    """Project one persisted ``isar.interval_semantics/1`` quantity item into the JSON-safe
    ``mode_intervals`` cell (SCENE-4 PR 4.2a; authority re-baselined to fit-conditional producer
    soundness, P0-C), or ``None`` when no real interval survives the writer's own availability
    flag. Every field but ``authority`` is copied straight off ``item``/``values`` — nothing here
    is derived or invented; ``authority`` is the :func:`_producer_soundness` verdict.

    Post-slice-boundary fix: ``values`` is NOT always a ``(lo, center, hi)`` triple — confirmed by
    reading every producer. The bootstrap freq_interval (``errors.bootstrap._percentile_interval``)
    and the Bayesian credible interval (``errors.bayes``, ``BayesResult.omega0_interval``) ARE
    triples; the profile interval (``errors.profile.omega0_profile_interval``) and the fast-tier
    HESSE interval (``scan_delivery._hesse_interval_and_trust``) are ``(lo, hi)`` PAIRS with no
    third, "center", entry at all. Minting one (e.g. from the row's ``omega0``) would be exactly
    the invented semantics this commit refuses — so a pair projects ``center=None`` honestly.
    """
    if item is None or values is None or not item.get("availability"):
        return None
    if len(values) not in (2, 3):
        raise ValueError(f"interval quantity {source_quantity!r} has {len(values)} value(s) "
                         f"({values!r}) — a persisted interval must be a (lo, hi) pair or a "
                         "(lo, center, hi) triple, never anything else")
    center = float(values[1]) if len(values) == 3 else None
    return {
        "low": float(values[0]), "center": center, "high": float(values[-1]),
        # Strict access (PR 4.2a review, Minor finding 3): every writer that ever builds this
        # item (``errors._interval_semantics_for``'s ``common`` dict, ``_outcome_interval_
        # semantics``'s ``unavailable()`` helper) sets ``nominal_level`` unconditionally — an
        # item reaching here without one is a corrupt record, not a legitimately-optional field,
        # and minting 0.68 for it would be exactly the invented semantics this commit refuses.
        "nominal_level": float(item["nominal_level"]),
        "interval_kind": item.get("interval_kind"),
        "inference_paradigm": item.get("inference_paradigm"),
        "source_quantity": source_quantity,
        "authority": _producer_soundness(item, within),
        "conditioning_fingerprint": item.get("conditioning_fingerprint"),
    }


def _mode_intervals_of(label, r, n_omegas) -> list:
    """The slot-aligned ``mode_intervals`` cell for one candidate row (SCENE-4 PR 4.2a: the
    per-mode interval persistence re-baseline; P0-C: interval authority made immutably
    fit-conditional).

    Sized by ``n_omegas`` — the length of the row's OWN ``omegas`` list, the same value
    ``_mode_omegas(r)`` produced for this row — never by ``n_modes``. The two legitimately
    disagree for a result-less candidate (``_mode_omegas`` docstring): ``n_modes`` defaults to 1
    with no result to read a position off, so ``omegas == ()``. Sizing off ``n_modes`` there
    would emit ``[None]`` against an empty ``omegas``, which ``CandidateRow.__post_init__``
    (mode_intervals/omegas must be equal length) then refuses.

    POPULATION RULE: only ``n_omegas == 1`` ever populates slot 0 — a multimode candidate's
    persisted record never names which slot the bootstrap tracked as its primary mode (it tracks
    one internally, ``analyze.components.CompositeModel.primary_phonon_index``, but does not
    persist which slot that was on this row), so a multimode candidate stays honestly all-``None``
    rather than guessing slot 0. Phonon-less (or result-less) candidates carry ``[]``.

    AUTHORITY (P0-C, owner ruling 2026-08-14): EVERY row — winner included — projects only its
    OWN candidate-tier ``within_model["freq_interval"]`` record, verbatim, exactly like every
    other survivor. Delivery selection (``FitOutcome.best_label``, ``selected_for_delivery``,
    ``passed_reliability_gate``) is not interval authority and never reaches this cell: those are
    outcome-level facts about which candidate the driver chose, not about whether ITS bootstrap
    interval is a trustworthy sampling distribution. Staleness dies by construction — a candidate
    row is write-once, and only outcome-level semantics ever refresh
    (``evidence_update.py``'s ``_refresh_interval_semantics``), which this cell does not depend
    on.
    """
    if n_omegas != 1:
        return [None] * n_omegas
    within = getattr(r, "within_model", None) if r is not None else None
    within = within or {}
    item = (within.get("interval_semantics") or {}).get("quantities", {}).get("freq_interval")
    values = within.get("freq_interval")
    return [_mode_interval_item(item, values, "freq_interval", within)]


def _candidate_rows(scores, fits, by_hyp) -> list | None:
    """The model-selection candidate table (P0-4): one compact row per SCORED structure. ``scores``
    is the ``ModelScore`` list the driver selects on — posterior ``P(s) = π_s·Z_s / Σ π·Z`` in the
    evidence lane, Akaike weights on the recorded AIC fallback; this only reshapes it, never
    re-ranks. The winner is the row whose ``label`` equals ``Selection.best`` /
    ``FitOutcome.best_label`` (``delta_aic == 0`` identifies it only on the fallback lane; AIC is a
    persisted diagnostic under /2).
    Non-converged fits are already excluded by ``score_models``, so every row is a usable candidate.
    ``fits`` (label → ``StructureFitResult``) supplies ``cost_per_dof``/``n_modes``/``analysis_usable``
    that ``ModelScore`` does not carry; ``by_hyp`` (label → ``Hypothesis``) supplies the ENUMERATED
    STRUCTURE each candidate was fitted as, which is the one authority on its component composition
    (``components``). All values are coerced to plain Python scalars so the row JSON-serialises
    cleanly. ``None`` when nothing converged (empty ``scores``)."""
    if not scores:
        return None
    rows = []
    for s in scores:
        r = fits.get(s.label)
        ndof = int(s.n - s.k)
        cpd = (float(r.cost_per_dof) if r is not None
               else (float(s.cost) / ndof if ndof > 0 else float("nan")))
        rows.append({
            "label": str(s.label),
            "k": int(s.k),
            "ndof": ndof,
            "cost": float(s.cost),
            # Guard non-finite cost/dof (ndof==0, or a blown-up fit) to None so the candidates JSON
            # never carries a nonstandard NaN token that a strict-JSON reader would reject.
            "cost_per_dof": (cpd if np.isfinite(cpd) else None),
            "n_modes": int(r.n_modes) if r is not None else 1,
            "omegas": (omegas := _mode_omegas(r)),
            # SCENE-4 PR 4.2a: the within-candidate ω₀ uncertainty, slot-aligned to ``omegas`` —
            # sized off ``len(omegas)``, NOT ``n_modes`` (they legitimately disagree for a
            # result-less candidate; see ``_mode_intervals_of``).
            "mode_intervals": _mode_intervals_of(s.label, r, len(omegas)),
            # SCENE-3: what this candidate is MADE OF, so the scene layer never has to recover
            # component semantics from the label text (review finding 9).
            "components": _component_descriptors(s.label, by_hyp.get(s.label)),
            "delta_aic": float(s.delta_aic),
            "akaike_weight": float(s.akaike_weight),
            "posterior_probability": (
                None if getattr(s, "posterior_probability", None) is None
                else float(s.posterior_probability)
            ),
            "log_evidence": (
                float(s.log_evidence)
                if getattr(s, "log_evidence", None) is not None
                and np.isfinite(s.log_evidence)
                else None
            ),
            "evidence_method": getattr(s, "evidence_method", None),
            "usable": bool(r.analysis_usable) if r is not None else True,
            # SCENE-1 PR-c persistence (commit D). ``at_limit`` is this candidate's per-parameter
            # boundary-pin map — what the evidence validity ladder read when it priced the row, so a
            # reader can see WHY a rung escalated. ``variant``/``identity`` are the candidate's
            # catalogue provenance (constrained variant, claimed phenomenon), both ``None`` for an
            # ordinary candidate. ``effective_constraints`` is read back from the recorded
            # fit_semantics, never re-derived: the row must show the constraints the fit actually
            # ran under. ``[]`` means "recorded, unconstrained"; ``None`` means no semantics record.
            "at_limit": (dict(r.at_limit)
                         if r is not None and getattr(r, "at_limit", None) is not None else None),
            "variant": (getattr(r, "variant", None) if r is not None else None),
            "identity": (getattr(r, "identity", None) if r is not None else None),
            "effective_constraints": (
                None if (s.fit_semantics or {}).get("effective_constraints") is None
                else list((s.fit_semantics or {})["effective_constraints"])
            ),
            "estimator_kind": (s.fit_semantics or {}).get("estimator_kind"),
            "fit_kind": (s.fit_semantics or {}).get("fit_kind"),
            "constraint_origin": (s.fit_semantics or {}).get("aggregate_constraint_origin"),
            "selection_eligibility": (s.fit_semantics or {}).get("selection_eligibility"),
            "data_likelihood_fingerprint": (s.fit_semantics or {}).get(
                "data_likelihood_fingerprint"
            ),
            "model_parameterization_fingerprint": (s.fit_semantics or {}).get(
                "model_parameterization_fingerprint"
            ),
            "constraint_fingerprint": (s.fit_semantics or {}).get(
                "constraint_fingerprint"
            ),
            "comparison_context_fingerprint": (s.fit_semantics or {}).get(
                "comparison_context_fingerprint"
            ),
            "conditioning_fingerprint": (s.fit_semantics or {}).get(
                "conditioning_fingerprint"
            ),
        })
    return rows


def _model_set_fingerprint(kind: str, payload) -> str:
    return semantics_fingerprint(f"isar.{kind}/1", payload)


_ELIGIBILITY_PRECEDENCE = {
    "eligible": 0,
    "ineligible_unapproved_conditioning": 1,
    "ineligible_soft_penalized": 2,
    "ineligible_data_dependent_conditioning": 3,
    "ineligible_incomparable_context": 4,
}


def _canonical_candidate_generation_route(route: dict) -> dict:
    """Order-invariant form of the model-set portions of a generation route."""
    canonical = copy.deepcopy(dict(route))
    for name in (
        "guideline_structure_filter",
        "labels_before_guideline",
        "offered_labels",
        "scored_labels",
    ):
        if canonical.get(name) is not None:
            canonical[name] = sorted(str(value) for value in canonical[name])
    return canonical


def _model_set_record(label, fit_semantics: dict) -> dict:
    """The structured model identity needed to distinguish equal labels in different contexts."""
    return {
        "label": str(label),
        "data_likelihood_fingerprint": fit_semantics["data_likelihood_fingerprint"],
        "model_parameterization_fingerprint": fit_semantics[
            "model_parameterization_fingerprint"
        ],
        "constraint_fingerprint": fit_semantics["constraint_fingerprint"],
        "comparison_context_fingerprint": fit_semantics[
            "comparison_context_fingerprint"
        ],
        "selection_eligibility": fit_semantics["selection_eligibility"],
    }


def _is_offered_variant(fit_semantics: dict) -> bool:
    """A deliberately-offered catalogue variant: constrained, and every constraint's origin is
    ``catalogue_variant``. Owner ruling 2026-08-09: when the analysis is looking carefully at
    close-together physics (near-zone-centre acoustic, crossings), the deliberately-offered
    rival hypothesis must not disqualify the scan by mere presence — only by winning."""
    constraints = fit_semantics.get("effective_constraints") or ()
    return bool(constraints) and all(
        isinstance(item, dict) and item.get("origin") == "catalogue_variant"
        for item in constraints
    )


def _aggregate_selection_eligibility(
    candidate_records: list[dict], *, winner: str | None = None
) -> tuple[str, list[dict]]:
    """Strongest complete normalization defect plus every candidate-specific cause.

    A NON-winning offered catalogue variant (:func:`_is_offered_variant`) contributes neither
    its eligibility nor its reasons to the aggregate — it was offered, priced, and rejected,
    which conditions nothing about the winner. A variant that WINS is not exempt: a
    hard-conditioned winner stays ineligible. Its data-likelihood fingerprint still counts in
    the incomparable-context check — the arena guarantee is about the comparison itself."""
    reason_labels: dict[str, list[str]] = {}
    eligibilities = []
    likelihoods = set()
    for candidate in candidate_records:
        label = str(candidate["label"])
        fit_semantics = candidate["fit_semantics"]
        likelihoods.add(str(fit_semantics["data_likelihood_fingerprint"]))
        if winner is not None and label != winner and _is_offered_variant(fit_semantics):
            continue
        eligibilities.append(str(fit_semantics["selection_eligibility"]))
        for reason in fit_semantics.get("selection_ineligibility_reasons") or ():
            reason_labels.setdefault(str(reason), []).append(label)
    if len(likelihoods) != 1:
        eligibilities.append("ineligible_incomparable_context")
        reason_labels.setdefault("candidate_data_likelihood_contexts_differ", []).extend(
            str(candidate["label"]) for candidate in candidate_records
        )
    aggregate = max(
        eligibilities or ["eligible"],
        key=lambda value: _ELIGIBILITY_PRECEDENCE[value],
    )
    reasons = [
        {
            "reason_code": reason,
            "candidate_labels": sorted(set(labels)),
        }
        for reason, labels in sorted(reason_labels.items())
    ]
    return aggregate, reasons


def _build_selection_semantics(
    results,
    selection,
    route,
    *,
    error_attachment_labels,
) -> dict | None:
    """Describe the exact candidate-set transitions without changing selection behavior."""
    try:
        missing = [r.label for r in results if getattr(r, "fit_semantics", None) is None]
        if missing:
            raise SemanticsConstructionError(
                "missing_candidate_fit_semantics", ",".join(str(label) for label in missing)
            )
        by_label = {r.label: r for r in results}
        offered_records = sorted(
            (
                _model_set_record(r.label, r.fit_semantics)
                for r in results
            ),
            key=lambda item: item["label"],
        )
        scored_records = sorted(
            (
                _model_set_record(score.label, score.fit_semantics)
                for score in selection.scores
            ),
            key=lambda item: item["label"],
        )
        fallback = getattr(selection, "selection_fallback", None)
        arena_fingerprint = getattr(
            getattr(selection, "evidence", None), "arena_fingerprint", None
        )
        if not selection.scores:
            selection_method, model_weight_kind = "none", "none"
            arena_fingerprint = None
        elif fallback is not None:
            selection_method, model_weight_kind = "aic_fallback", "akaike_relative_weight"
        else:
            selection_method = "laplace_evidence_selection"
            model_weight_kind = "posterior_probability"
            if not arena_fingerprint:
                raise SemanticsConstructionError("missing_evidence_arena")
        if selection_method == "laplace_evidence_selection":
            # The weight denominator is the posterior normalization: candidates with a
            # nonzero prior AND finite trusted lnZ — the same set select.py normalizes
            # over (prior 0 ⟺ non-finite lnZ by contract; the explicit finiteness guard
            # keeps a contract breach from feeding NaN into the fingerprint payload).
            denominator = sorted(
                (
                    {
                        **_model_set_record(score.label, score.fit_semantics),
                        "log_evidence": float(score.log_evidence),
                    }
                    for score in selection.scores
                    if (score.candidate_prior or 0.0) > 0.0
                    and score.log_evidence is not None
                    and np.isfinite(score.log_evidence)
                ),
                key=lambda item: item["label"],
            )
        else:
            denominator = sorted(
                (
                    {
                        **_model_set_record(score.label, score.fit_semantics),
                        "aic": float(score.aic),
                    }
                    for score in selection.scores
                ),
                key=lambda item: item["label"],
            )
        pool_labels = []
        for score in selection.scores:
            if not score.survives:
                continue
            samples = (getattr(by_label.get(score.label), "within_model", None) or {}).get(
                "omega0_samples"
            )
            if samples is not None and np.size(samples) > 0:
                pool_labels.append(str(score.label))
        error_attachment_records = sorted(
            (
                _model_set_record(label, by_label[label].fit_semantics)
                for label in error_attachment_labels
            ),
            key=lambda item: item["label"],
        )
        pool_records = sorted(
            (
                _model_set_record(label, by_label[label].fit_semantics)
                for label in pool_labels
            ),
            key=lambda item: item["label"],
        )
        component_fingerprints = {
            "offered_model_set_fingerprint": _model_set_fingerprint(
                "offered_model_set", offered_records
            ),
            "scored_model_set_fingerprint": _model_set_fingerprint(
                "scored_model_set", scored_records
            ),
            "weight_denominator_fingerprint": _model_set_fingerprint(
                "model_weight_denominator", denominator
            ),
            "error_attachment_set_fingerprint": _model_set_fingerprint(
                "error_attachment_set", error_attachment_records
            ),
            "legacy_pool_set_fingerprint": _model_set_fingerprint(
                "legacy_pool_set", pool_records
            ),
        }
        candidate_records = []
        for score in selection.scores:
            log_evidence = getattr(score, "log_evidence", None)
            if log_evidence is not None and not np.isfinite(log_evidence):
                # Dominated/invalid candidates carry NaN lnZ in ModelScore; canonical JSON
                # refuses NaN, so "no trusted number" persists as None.
                log_evidence = None
            log_evidence_err = getattr(score, "log_evidence_err", None)
            candidate_prior = getattr(score, "candidate_prior", None)
            posterior = getattr(score, "posterior_probability", None)
            candidate_records.append(
                {
                    "label": str(score.label),
                    "cost": float(score.cost),
                    "effective_parameter_count": int(score.k),
                    "aic": float(score.aic),
                    "akaike_weight": float(score.akaike_weight),
                    "log_evidence": None if log_evidence is None else float(log_evidence),
                    "log_evidence_err": (
                        None if log_evidence_err is None else float(log_evidence_err)
                    ),
                    "evidence_method": getattr(score, "evidence_method", None),
                    "evidence_validity": getattr(score, "evidence_validity", None),
                    "candidate_prior": (
                        None if candidate_prior is None else float(candidate_prior)
                    ),
                    "posterior_probability": (
                        None if posterior is None else float(posterior)
                    ),
                    "fit_semantics": score.fit_semantics,
                }
            )
        aggregate_eligibility, aggregate_reasons = _aggregate_selection_eligibility(
            candidate_records, winner=selection.best
        )
        canonical_route = _canonical_candidate_generation_route(route)
        selection_payload = {
            **component_fingerprints,
            "model_selection_revision": select.MODEL_SELECTION_REVISION,
            "selection_method": selection_method,
            "selection_fallback": fallback,
            "weight_floor": float(select.DEFAULT_WEIGHT_FLOOR),
            "candidate_generation_route": canonical_route,
            "selection_eligibility": aggregate_eligibility,
            "selection_ineligibility_reasons": aggregate_reasons,
            "candidates": sorted(candidate_records, key=lambda item: item["label"]),
        }
        if arena_fingerprint is not None:
            selection_payload["evidence_arena_fingerprint"] = str(arena_fingerprint)
            selection_payload["evidence_revision"] = EVIDENCE_REVISION
        record = {
            "schema": FIT_SELECTION_SCHEMA,
            "candidate_generation_route": route,
            "selection_method": selection_method,
            "selection_fallback": fallback,
            "model_weight_kind": model_weight_kind,
            "selection_eligibility": aggregate_eligibility,
            "selection_ineligibility_reasons": aggregate_reasons,
            **component_fingerprints,
            "model_selection_fingerprint": semantics_fingerprint(
                "isar.model_selection_semantics/2", selection_payload
            ),
            "candidate_records": candidate_records,
            "authority_scopes": authority_scopes(
                single_fit_delivery="active",
                user_diagnostic="active",
            ),
        }
        if arena_fingerprint is not None:
            record["evidence_arena_fingerprint"] = str(arena_fingerprint)
            record["evidence_revision"] = EVIDENCE_REVISION
        return validate_for_write(record, FIT_SELECTION_SCHEMA)
    except SemanticsConstructionError as exc:
        log.warning(
            "semantics_construction_failed producer=isar.analyze.select "
            "quantity=model_selection reason_code=%s",
            exc.reason_code,
        )
        return None


def _outcome_interval_semantics(
    *,
    best,
    selection,
    interval,
    omega0_profile,
    hesse_interval,
    hesse_trusted,
    bayes_interval,
    interval_reliable,
) -> dict | None:
    """Propagate producer-owned interval meaning onto the four persisted quantities."""
    try:
        source = (
            ((getattr(best, "within_model", None) or {}).get("interval_semantics") or {}).get(
                "quantities", {}
            )
            if best is not None
            else {}
        )
        fit_semantics = getattr(best, "fit_semantics", None) if best is not None else None

        def unavailable(reason: str) -> dict:
            item = {
                "availability": False,
                "nominal_level": 0.68,
                "unavailable_reason": reason,
                "calibration_target": None,
                "producer_authority_scopes": authority_scopes(),
                "authority_scopes": authority_scopes(),
            }
            if fit_semantics is not None:
                item.update(
                    {
                        "estimator_kind": fit_semantics["estimator_kind"],
                        "effective_constraints": list(
                            fit_semantics.get("effective_constraints") or []
                        ),
                        "conditioning_fingerprint": fit_semantics[
                            "conditioning_fingerprint"
                        ],
                    }
                )
            return item

        if interval is not None and source.get("freq_interval") is not None:
            interval_item = copy.deepcopy(source["freq_interval"])
        elif interval is not None and selection.pooled is not None:
            # The exact contributing set and its observed-data weights are owned by the selection
            # producer.  Reference both component-set and complete-selection fingerprints instead
            # of reverse-engineering either from the interval values.
            selection_semantics = selection.fit_selection_semantics or {}
            legacy_pool_set_fingerprint = selection_semantics.get(
                "legacy_pool_set_fingerprint"
            )
            source_model_selection_fingerprint = selection_semantics.get(
                "model_selection_fingerprint"
            )
            if (
                legacy_pool_set_fingerprint is None
                or source_model_selection_fingerprint is None
            ):
                raise SemanticsConstructionError(
                    "missing_selection_fingerprint", "legacy_pooled_interval"
                )
            interval_item = {
                "availability": True,
                "nominal_level": 0.68,
                "interval_kind": "akaike_weighted_parametric_bootstrap_pool",
                "particle_measure_kind": "parametric_bootstrap_sampling_distribution",
                "draw_measure_kind": "explicit_weight_empirical_particles",
                # The pool weights are whatever drove selection; the /2 record says which.
                # (interval_kind keeps its historical name — vocab rename is interval-schema
                # churn deferred to an owner ruling.)
                "model_weight_kind": selection_semantics["model_weight_kind"],
                "inference_paradigm": "frequentist_empirical_model_mixture",
                "legacy_pool_set_fingerprint": legacy_pool_set_fingerprint,
                "source_model_selection_fingerprint": (
                    source_model_selection_fingerprint
                ),
                "conditioning_fingerprint": semantics_fingerprint(
                    "isar.legacy_pooled_interval_conditioning/1",
                    {
                        "legacy_pool_set_fingerprint": legacy_pool_set_fingerprint,
                        "source_model_selection_fingerprint": (
                            source_model_selection_fingerprint
                        ),
                    },
                ),
                "calibration_target": None,
                "producer_authority_scopes": authority_scopes(
                    single_fit_delivery="candidate",
                    legacy_master_certification="candidate",
                    stopping="candidate",
                    user_diagnostic="active",
                ),
                "authority_scopes": authority_scopes(user_diagnostic="active"),
            }
        else:
            interval_item = unavailable("not_produced")
        interval_item["availability"] = interval is not None

        profile_item = (
            copy.deepcopy(source["profile_interval"])
            if source.get("profile_interval") is not None
            else unavailable("not_produced")
        )
        profile_item["availability"] = omega0_profile is not None

        if hesse_interval is not None and fit_semantics is None:
            raise SemanticsConstructionError("missing_candidate_fit_semantics", "hesse_interval")
        hesse_item = unavailable("not_produced")
        if hesse_interval is not None:
            hesse_item.update(
                {
                    "availability": True,
                    "interval_kind": "hesse_delta",
                    "inference_paradigm": "frequentist_asymptotic",
                    "hesse_trusted": bool(hesse_trusted),
                    "particle_measure_kind": "laplace_hesse_parameter_approximation",
                    "producer_authority_scopes": authority_scopes(
                        single_fit_delivery="candidate",
                        legacy_master_certification="candidate",
                        stopping="candidate",
                        user_diagnostic="active",
                    ),
                    "authority_scopes": authority_scopes(user_diagnostic="active"),
                }
            )

        bayes_item = (
            copy.deepcopy(source["bayes_interval"])
            if source.get("bayes_interval") is not None
            else unavailable("not_produced")
        )
        bayes_item["availability"] = bayes_interval is not None

        quantity_items = {
            "interval": interval_item,
            "omega0_profile": profile_item,
            "hesse_interval": hesse_item,
            "bayes_interval": bayes_item,
        }

        record = {
            "schema": INTERVAL_SCHEMA,
            "quantities": quantity_items,
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
        return _realize_interval_authority(
            record,
            interval=interval,
            omega0_profile=omega0_profile,
            hesse_interval=hesse_interval,
            bayes_interval=bayes_interval,
            hesse_trusted=hesse_trusted,
            interval_reliable=interval_reliable,
        )
    except (SemanticsConstructionError, KeyError) as exc:
        reason_code = (
            exc.reason_code if isinstance(exc, SemanticsConstructionError)
            else "missing_interval_semantics_field"
        )
        log.warning(
            "semantics_construction_failed producer=isar.session.driver "
            "quantity=fit_outcome_intervals reason_code=%s",
            reason_code,
        )
        return None


def _realize_interval_authority(
    record: dict | None,
    *,
    interval,
    omega0_profile,
    hesse_interval,
    bayes_interval,
    hesse_trusted,
    interval_reliable,
) -> dict | None:
    """Bind producer capability to the interval ladder's actual delivered outcome.

    Branch triage and connected tracking can replace or withdraw the scalar interval after the
    error producer has run.  Reapplying this small projection at every such boundary keeps the
    recorded authority attached to the value that survives, without reverse-inferring the
    interval's statistical kind.
    """
    if record is None:
        return None
    try:
        realized = copy.deepcopy(record)
        quantities = realized["quantities"]
        values = {
            "interval": interval,
            "omega0_profile": omega0_profile,
            "hesse_interval": hesse_interval,
            "bayes_interval": bayes_interval,
        }
        selected_quantity = None
        # A failed interval producer returns a nan-filled tuple, not None: selecting or publishing
        # one on `is not None` delivers a nan half-width that never satisfies the stopping
        # tolerance, so the q silently never converges and never escalates.
        if errors.usable_interval(interval) and interval_reliable is not False:
            selected_quantity = "interval"
        elif errors.usable_interval(omega0_profile):
            selected_quantity = "omega0_profile"
        elif errors.usable_interval(hesse_interval) and hesse_trusted:
            selected_quantity = "hesse_interval"
        certifiable = bool(
            selected_quantity is not None
            and (
                interval_reliable is not False
                or selected_quantity == "hesse_interval" and hesse_trusted
            )
        )
        for name, item in quantities.items():
            item.setdefault("producer_availability", bool(item.get("availability")))
            item["availability"] = errors.usable_interval(values.get(name))
            selected = name == selected_quantity
            item["selected_for_delivery"] = selected
            item["passed_reliability_gate"] = bool(selected and certifiable)
            if not item["availability"]:
                item["authority_scopes"] = authority_scopes()
            elif selected and certifiable:
                item["authority_scopes"] = authority_scopes(
                    single_fit_delivery="active",
                    legacy_master_certification="active",
                    stopping="active",
                    user_diagnostic="active",
                )
            elif selected:
                item["authority_scopes"] = authority_scopes(
                    single_fit_delivery="active",
                    legacy_master_certification="ineligible",
                    stopping="ineligible",
                    user_diagnostic="active",
                )
            else:
                item["authority_scopes"] = authority_scopes(
                    user_diagnostic="active"
                )
        if interval is not None and interval_reliable is False:
            quantities["interval"]["authority_scopes"] = authority_scopes(
                single_fit_delivery="inactive",
                legacy_master_certification="ineligible",
                stopping="ineligible",
                user_diagnostic="active",
            )
            quantities["interval"]["passed_reliability_gate"] = False
        if hesse_interval is not None and not hesse_trusted:
            quantities["hesse_interval"]["authority_scopes"] = authority_scopes(
                user_diagnostic="active"
            )
            quantities["hesse_interval"]["passed_reliability_gate"] = False
        realized["selected_interval_quantity"] = selected_quantity
        realized["selected_interval_certifiable"] = certifiable
        realized["authority_scopes"] = (
            copy.deepcopy(quantities[selected_quantity]["authority_scopes"])
            if selected_quantity is not None
            else authority_scopes()
        )
        return validate_for_write(realized, INTERVAL_SCHEMA)
    except (SemanticsConstructionError, KeyError) as exc:
        reason_code = (
            exc.reason_code if isinstance(exc, SemanticsConstructionError)
            else "missing_interval_semantics_field"
        )
        log.warning(
            "semantics_construction_failed producer=isar.session.driver "
            "quantity=realized_fit_outcome_intervals reason_code=%s",
            reason_code,
        )
        return None


def _refresh_interval_semantics(outcome: dict) -> None:
    """Refresh only realized authority after a delivery mutation."""
    outcome["interval_semantics"] = _realize_interval_authority(
        outcome.get("interval_semantics"),
        interval=outcome.get("interval"),
        omega0_profile=outcome.get("omega0_profile"),
        hesse_interval=outcome.get("hesse_interval"),
        bayes_interval=outcome.get("bayes_interval"),
        hesse_trusted=bool(outcome.get("hesse_trusted", False)),
        interval_reliable=outcome.get("interval_reliable", True),
    )


def _refresh_probability_semantics(
    outcome: dict,
    *,
    predictive_payload: dict | None = None,
) -> dict | None:
    """Compose declarations supplied by the confidence, assignment, tracker, and predictive producers."""
    from isar.analyze import confidence as confidence_mod

    try:
        quantities = {}
        if outcome.get("confidence") is not None:
            quantities["confidence"] = confidence_mod.probability_semantics()
        if (
            outcome.get("branch_posterior") is not None
            or outcome.get("branch_weight") is not None
            or outcome.get("assignment_entropy") is not None
        ):
            local = BA.probability_semantics(outcome.get("branch_posterior"))
            for name in ("branch_posterior", "branch_weight", "assignment_entropy"):
                if outcome.get(name) is not None:
                    quantities[name] = local[name]
        if outcome.get("target_probability") is not None or outcome.get("identity_entropy") is not None:
            tracked = branch_tracking.probability_semantics(
                outcome.get("branch_posterior"),
                target_branch=str(outcome.get("target_branch") or "acoustic"),
            )
            for name in ("target_probability", "identity_entropy"):
                if outcome.get(name) is not None:
                    quantities[name] = tracked[name]
        existing = outcome.get("probability_semantics")
        existing_model_entropy = (
            ((existing or {}).get("quantities") or {}).get("model_entropy")
            if isinstance(existing, dict)
            else None
        )
        if outcome.get("model_entropy") is not None:
            predictive_retained = (
                (predictive_payload or {}).get("predictive_retained_set_fingerprint")
            )
            if predictive_retained is not None:
                source_weight_kind = (
                    (predictive_payload or {}).get("statistical_semantics") or {}
                ).get("model_weight_kind")
                if source_weight_kind is None:
                    # An artifact carrying a retained-set fingerprint without its semantics
                    # record is malformed; fail soft like the missing-fingerprint branch below.
                    raise SemanticsConstructionError(
                        "missing_model_weight_kind", "model_entropy"
                    )
                quantities["model_entropy"] = (
                    predictive_artifact.model_entropy_probability_semantics(
                        predictive_retained,
                        source_weight_kind,
                    )
                )
            elif existing_model_entropy is not None:
                quantities["model_entropy"] = existing_model_entropy
            else:
                raise SemanticsConstructionError(
                    "missing_predictive_retained_set_fingerprint", "model_entropy"
                )
        record = {
            "schema": PROBABILITY_SCHEMA,
            "quantities": quantities,
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
        return validate_for_write(record, PROBABILITY_SCHEMA)
    except (SemanticsConstructionError, KeyError) as exc:
        reason_code = (
            exc.reason_code if isinstance(exc, SemanticsConstructionError)
            else "missing_probability_semantics_field"
        )
        log.warning(
            "semantics_construction_failed producer=isar.session.driver "
            "quantity=probability_like_outputs reason_code=%s",
            reason_code,
        )
        return None


def _auto_escalation_reasons(prelim, fits, ctx) -> list[str]:
    """Ordered, deterministic reasons why an HESSE-first fit needs full errors.

    This consumes the point-estimation objects directly.  It deliberately does not refit them:
    :func:`fit_one` can attach the authoritative error layer to the same candidates afterward.
    """
    best = fits.get(prelim.best) if prelim.best else None
    reasons = []
    if best is None or not best.analysis_usable:
        reasons.append("no_usable_best")
        return reasons
    if not best.covariance_ok or best.parameter_covariance is None:
        reasons.append("no_usable_covariance")
    _interval, trusted = _hesse_interval_and_trust(best, ctx)
    if not trusted:
        reasons.append("untrusted_hesse")

    for name, value in best.params.items():
        sigma = best.param_errors.get(name)
        if sigma is None or not np.isfinite(sigma) or float(sigma) <= 0:
            continue
        value, sigma = float(value), float(sigma)
        if name.endswith(".omega0"):
            # The energy prior's own edges (the recipe that bounded the fit), the rule the fit uses.
            lo, hi = mode_bounds([name], ctx)[0]
            if min(value - lo, hi - value) <= 2.0 * sigma:
                reasons.append("omega0_within_2sigma_of_bound")
                break
    for name, value in best.params.items():
        sigma = best.param_errors.get(name)
        if sigma is None or not np.isfinite(sigma) or float(sigma) <= 0:
            continue
        value, sigma = float(value), float(sigma)
        if name.endswith(".Gamma"):
            # the upper edge is this mode's own ceiling under the fit's recipe (recipe /7)
            omega0 = best.params.get(name[:-len("Gamma")] + "omega0")
            top = loop.GAMMA_MAX if omega0 is None else width_ceiling(
                float(omega0), settings=_fit_prior_settings(best))
            if min(value - loop.GAMMA_FLOOR, top - value) <= 2.0 * sigma:
                reasons.append("gamma_within_2sigma_of_bound")
                break
    runner_weights = sorted(
        (float(score.weight) for score in prelim.scores if score.label != prelim.best),
        reverse=True)
    if runner_weights and runner_weights[0] >= 0.10:
        reasons.append("runner_up_model_weight_ge_0.10")
    return reasons


def _damped_frequency(e0, gamma, cov2):
    """``(Ω, σ_Ω)`` for the damped frequency Ω = √(E0² − Γ²), or ``(None, None)`` when Γ ≥ E0 (an
    overdamped mode has none). ``cov2`` is the (E0, Γ) covariance block, or ``None``:
    σ² = (E0/Ω)²σ_E0² + (Γ/Ω)²σ_Γ² − 2(E0Γ/Ω²)cov(E0, Γ)."""
    if not gamma < e0:
        return None, None
    omega = float(np.sqrt(e0 * e0 - gamma * gamma))
    if cov2 is None:
        return omega, None
    d_e, d_g = e0 / omega, -gamma / omega
    var = d_e * d_e * cov2[0, 0] + d_g * d_g * cov2[1, 1] + 2.0 * d_e * d_g * cov2[0, 1]
    return omega, (float(np.sqrt(var)) if np.isfinite(var) and var > 0 else None)


def _with_peak_positions(modes, result) -> list:
    """Each mode dict with its displayed peak position: the DHO's damped frequency
    Ω_d = √(E0² − Γ²) (meV) when Γ < E0, empty when Γ ≥ E0, with its error propagated from the
    fitted (E0, Γ) covariance. ``omega0`` stays the undamped energy E0, the fitted parameter."""
    params = getattr(result, "params", None) or {}
    names = list(params)
    cov = getattr(result, "parameter_covariance", None)
    cov = np.asarray(cov, dtype=float) if cov is not None else None
    if cov is not None and cov.shape != (len(names), len(names)):
        cov = None
    prefixes = [n[: -len(".omega0")] for n in params if n.endswith(".omega0")]
    out = []
    for mode in modes:
        if not isinstance(mode, dict) or not prefixes or mode.get("omega0") is None:
            out.append(mode)
            continue
        prefix = min(prefixes, key=lambda p: (
            abs(float(params[p + ".omega0"]) - float(mode["omega0"]))
            + abs(float(params[p + ".Gamma"]) - float(mode.get("gamma") or 0.0))))
        i, j = names.index(prefix + ".omega0"), names.index(prefix + ".Gamma")
        omega, err = _damped_frequency(float(params[prefix + ".omega0"]),
                                       float(params[prefix + ".Gamma"]),
                                       cov[np.ix_([i, j], [i, j])] if cov is not None else None)
        out.append({**mode, "peak_position": omega, "peak_position_err": err})
    return out


def _finite_json(value):
    """``value`` as canonical JSON accepts it: numpy scalars as Python ones, a non-finite number as
    ``None`` (a missing value stays missing, never a NaN token)."""
    if isinstance(value, dict):
        return {str(k): _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


FAST_PASS_RECORD_SCHEMA = "isar.fast-pass/1"


def _fast_pass_record(res) -> dict:
    """The fast pass's result as persisted (C-fastpass-record, :data:`FAST_PASS_RECORD_SCHEMA`):
    what the benchmark, the deep pass and the reports read without refitting. JSON-safe, and no wall
    time: a results row must not depend on how the run was scheduled (time is C-timing's)."""
    def model(m):
        return {"model_id": m.model_id, "label": m.label, "n_peaks": m.n_peaks,
                "elastic": m.elastic, "background": m.background,
                "delta_modes": list(m.delta_modes), "ln_model_prior": m.ln_model_prior,
                "evidence_status": m.evidence_status, "ln_z": m.ln_z, "ln_like": m.ln_like,
                "ln_prior": m.ln_prior, "cost": m.cost, "k_free": m.k_free, "free": list(m.free),
                "probability": m.probability, "twin_of": m.twin_of, "n_basins": m.n_basins,
                "reason": m.reason, "posterior_iterations": m.posterior_iterations,
                "params": dict(m.params),
                "map_params": None if m.map_params is None else dict(m.map_params),
                "qe": m.qe, "on_demand": m.on_demand, "removed": list(m.removed),
                "superseded": m.superseded, "parent": m.parent}

    selected = res.selected
    return _finite_json({
        "schema": FAST_PASS_RECORD_SCHEMA, "state": res.state,
        "implementation_version": fast_pass.IMPLEMENTATION_VERSION,
        "preferred_count": res.preferred_count,
        "count_probabilities": {str(k): res.count_probabilities[k]
                                for k in sorted(res.count_probabilities)},
        "probabilities_conditional": res.probabilities_conditional,
        "close_call_alternatives": list(res.close_call_alternatives),
        "numerically_marginal": res.numerically_marginal,
        "search_limit_reason": res.search_limit_reason,
        "deep_pass_candidate": res.deep_pass_candidate,
        "selected_model_id": None if selected is None else selected.model_id,
        "selected_label": None if selected is None else selected.label,
        "arena_fingerprint": res.arena_fingerprint,
        "ln_like_saturated": res.ln_like_saturated,
        "models": [model(m) for m in res.models],
        "peaks": [asdict(p) for p in res.peaks],
        "elastic_height": res.elastic_height, "background": res.background,
        "qe": None if res.qe is None else asdict(res.qe),
        "grouped_intensity": [{"e_lo": lo, "e_hi": hi, "intensity": a, "sigma": s}
                              for lo, hi, a, s in res.grouped_intensity],
        "flags": [asdict(f) for f in res.flags],
        "flag_statistics": res.flag_statistics,
        "prior_provenance": res.prior_provenance,
        "settings": asdict(res.settings),
        "n_fits": res.n_fits, "seed_count": res.timing.get("seed_count"),
    })


def _fast_pass_timing(res) -> dict:
    """C-timing's per-spectrum entry (``ScanEntry.provenance["fast_pass"]``): the engine's own stage
    and family times, its fit count and why the search stopped."""
    t = res.timing
    return _finite_json({
        "total_seconds": t.get("total"),
        "stages": {name: t.get(name) for name in fast_pass.STAGES},
        "families": t.get("families") or {},
        "n_fits": res.n_fits, "seed_count": t.get("seed_count"),
        "stop_reason": res.search_limit_reason or "complete"})


def _structure_whitelist(structure_labels, ctx, ceiling) -> tuple:
    """``(structures, catalogue_labels, warn)`` for :func:`~isar.analyze.fast_pass.run_fast_pass`:
    the card's label whitelist (``None``: every structure), the catalogue labels up to ``ceiling``
    it narrows (``None`` without a whitelist), and the warning when it names none of them (then
    every structure is fitted, as before)."""
    if not structure_labels:
        return None, None, None
    from isar.analyze import catalogue

    offered = []
    for n in range(int(ceiling) + 1):
        for s in catalogue.enumerate_count_structures(ctx, n):
            twin = loop.without_background(s)
            offered += [str(s.label)] + ([] if twin is None else [str(twin.label)])
    if not set(structure_labels) & set(offered):
        return None, offered, (f"guideline structures {list(structure_labels)} match no "
                               f"admissible {offered}; kept all")
    return tuple(structure_labels), offered, None


def _candidate_models(res) -> dict:
    """``label -> ModelResult``: one candidate row per catalogue label with a fit. When basins share
    a label, the most probable (a priced one on a tie; then the lower basin index) speaks for it.
    A superseded model (another label of its group was priced) is no candidate."""
    rows = {}
    for m in res.models:
        if m.fit is None or m.superseded:
            continue
        kept = rows.get(m.label)
        if kept is None or (m.probability, m.ln_z is not None) > (kept.probability,
                                                                  kept.ln_z is not None):
            rows[m.label] = m
    return rows


def _selection_from_fast_pass(res, results, rows) -> select.Selection:
    """The fast pass's answer as a :class:`~isar.analyze.select.Selection`, never re-ranked
    (``select.select_models``'s 1/N prior per structure is not the fast pass's per-count prior).
    Each score is priced on its label's own Laplace evidence (its basins summed), its posterior is
    the label's fast-pass probability, and the winner is the engine's selected model. With nothing
    priced the selection is the recorded AIC lane with no winner."""
    from scipy.special import logsumexp

    from isar.analyze import evidence as evidence_mod

    basins = {}
    for m in res.models:
        if not m.superseded:              # as the fast pass's own tally: superseded models never count
            basins.setdefault(m.label, []).append(m)
    n_counts = len(res.count_probabilities)
    evidence = evidence_mod.EvidenceSet(res.arena_fingerprint)
    prior_spec = tuple(sorted((k, repr(v)) for k, v in res.prior_provenance.items()))
    best = None if res.selected is None else res.selected.label
    # Unresolved = a contender without evidence: the lane falls back to the AIC weights (no
    # posterior overwrites them below), recorded as evidence_budget_exhausted, which the scene reads
    # as an incomplete comparison (H7), never as complete.
    fallback = ("aic_all_evidence_invalid" if best is None else
                "evidence_budget_exhausted" if res.state == "unresolved" else None)
    scores = select.score_models(results)
    for s in scores:
        m = rows[s.label]
        priced = [b.ln_z for b in basins[s.label] if b.ln_z is not None]
        if m.evidence_status in ("ok", "ok_boundary") and priced:
            method = "laplace" if m.evidence_status == "ok" else "laplace_truncated"
            s.log_evidence = float(logsumexp(priced))
            s.evidence_method = method
            s.evidence_validity = {"lane": "fast_pass", "fast_pass_status": m.evidence_status,
                                   "model_id": m.model_id, "n_basins": len(priced)}
            evidence.add(evidence_mod.EvidenceRecord(
                label=s.label, log_evidence=s.log_evidence, log_evidence_err=None,
                evidence_method=method, k_free=int(m.k_free),
                arena_fingerprint=res.arena_fingerprint, prior_spec=prior_spec,
                validity=s.evidence_validity))
        else:
            s.evidence_validity = {"fast_pass_status": m.evidence_status, "reason": m.reason}
            if m.twin_of is not None:
                s.evidence_validity["twin_of"] = m.twin_of
        if fallback is None:
            s.posterior_probability = s.weight = float(sum(b.probability for b in basins[s.label]))
            s.candidate_prior = (float(np.exp(m.ln_model_prior)) / n_counts
                                 if s.log_evidence is not None and n_counts else 0.0)
            s.survives = bool(s.weight >= select.DEFAULT_WEIGHT_FLOOR)
    return select.Selection(scores, best, [] if best is None else [best], None, "unpooled",
                            evidence=evidence, selection_fallback=fallback)


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None,
            errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
            descriptor=None, structure_hint=None, bayes_opts=None,
            predictive_n_members: int | None = None, fit_role="provisional_scan",
            progress: ProgressFn | None = None):
    """The fast pass (:func:`~isar.analyze.fast_pass.run_fast_pass`, C-fastpass) on one spectrum,
    then its selection, the error tier's attachments and the verdict. Returns extracted fields.
    ``progress``: C-progress steps of the fast pass, then ``errors`` (never changes the fit).

    The search is always the fast pass: every count from 0 to the card's mode ceiling
    (:func:`guidelines.fast_pass_settings`), seeded and audited, priced by Laplace evidence; the
    card's ``peaks`` (guideline hints) are extra starts and its structure labels a whitelist (an
    empty match keeps all + warns). ``structure_hint`` (master refit) is recorded, never used: the
    full ladder fits every count anyway.

    ``errors_tier`` only decides what is attached to the selected model afterwards. ``"cheap"``:
    the covariance errors, no ω₀ interval. ``"fast"``: plus the delta-method ω₀ interval
    (``ω₀ ± 1σ``) and a ``hesse_trusted`` flag (Γ off-bound, positive-definite σ, enough counts:
    then that interval ≈ the bootstrap half-width). ``"auto"``: fast, promoted to full on a stable
    promotion trigger. ``"full"``: MINOS + parametric bootstrap + ω₀ profile on the selected model
    (the authoritative interval the stop rule consumes). ``"bayes"``: full plus the nested-sampling
    posterior. ``boot_workers`` sets the parametric-bootstrap process-pool size (1 = serial; see
    :func:`run_batch`)."""
    if errors_tier not in ("auto", "full", "fast", "cheap", "bayes"):
        raise ValueError(f"unknown errors tier: {errors_tier!r}")
    requested_tier = errors_tier
    run_full = errors_tier in FULL_TIERS  # MINOS + bootstrap + ω₀ profile (authoritative interval)
    run_fast = errors_tier in ("fast", "auto")  # HESSE-first; auto may promote below
    run_bayes = errors_tier == "bayes"   # full + nested-sampling posterior on the selected label
    settings = guide_mod.fast_pass_settings(card)
    structures, catalogue_labels, warn = _structure_whitelist(structure_labels, ctx,
                                                              settings.mode_ceiling)
    _t_two_stage = time.perf_counter()
    send = emitter(progress, "fast")
    res = fast_pass.run_fast_pass(ctx, settings=settings, peaks=tuple(peaks or ()),
                                  structures=structures, progress=relay(send))
    send("errors")
    two_stage_seconds = round(time.perf_counter() - _t_two_stage, 4)
    rows = _candidate_models(res)
    results = [m.fit for m in rows.values()]
    by_hyp = {label: m.hypothesis for label, m in rows.items()}
    offered_labels = [str(r.label) for r in results]
    candidate_generation_route = {
        "route": "fast_pass",
        "supplied_hint": None if structure_hint is None else str(structure_hint),
        "hint_accepted": False,
        "guideline_structure_filter": (
            [str(label) for label in structure_labels] if structure_labels else []
        ),
        "guideline_changed_model_set": bool(
            structures is not None and set(catalogue_labels) - set(structures)),
        "labels_before_guideline": catalogue_labels if structures is not None else offered_labels,
        "peak_hint_count": int(len(peaks)) if peaks is not None else 0,
        "multimode_route": "fast_pass",
        "offered_labels": offered_labels,
    }
    sel = _selection_from_fast_pass(res, results, rows)
    fits = {r.label: r for r in results}
    escalation_reasons = (_auto_escalation_reasons(sel, fits, ctx)
                          if errors_tier == "auto" else [])
    if errors_tier == "auto" and escalation_reasons:
        run_full = True
        run_fast = False
    effective_tier = ("full" if errors_tier == "auto" and run_full
                      else "fast" if errors_tier == "auto" else errors_tier)
    errors_attach_seconds = 0.0
    error_attachment_labels = []
    if run_full and sel.best is not None and fits[sel.best].optimizer_converged:
        _t_errors = time.perf_counter()
        hyp = by_hyp[sel.best]
        # The winner bootstraps under its own derived context, so a variant's (or a delta mode's)
        # pin holds in the resampled refits instead of quietly re-fitting a different model.
        error_attachment_labels = [str(sel.best)]
        errors.attach_batch(
            [(fits[sel.best], hyp, loop.candidate_context(hyp, ctx))], M=M, seed=seed,
            with_profile=True, bayes_label=(sel.best if run_bayes else None),
            prior=_bayes_prior_for(prior, card), boot_workers=boot_workers,
            executor=boot_executor, boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        errors_attach_seconds = round(time.perf_counter() - _t_errors, 4)
        samples = (fits[sel.best].within_model or {}).get("omega0_samples")
        if samples is not None and np.size(samples) > 0:
            # One survivor: its weight normalises away in the pool.
            sel.pooled = select.pool_across_model([(1.0, np.asarray(samples, dtype=float))])
            sel.modality = sel.pooled.modality if sel.pooled is not None else "unpooled"
    candidate_generation_route["scored_labels"] = [str(score.label) for score in sel.scores]
    fit_selection_semantics = _build_selection_semantics(
        results,
        sel,
        candidate_generation_route,
        error_attachment_labels=error_attachment_labels,
    )
    sel.fit_selection_semantics = fit_selection_semantics
    sv = verdict.classify(results, ctx, sel, prior=prior, descriptor=descriptor,
                          count_probabilities=res.count_probabilities, count_state=res.state)

    best = fits.get(sel.best) if sel.best else None
    omega0 = best.omega0 if best is not None else None
    interval = None
    if best is not None and best.within_model and best.within_model.get("freq_interval") is not None:
        interval = best.within_model["freq_interval"]
    elif sel.pooled is not None and sel.pooled.modality == "fold":
        interval = sel.pooled.interval
    # Exact ω₀ profile interval (robust to Γ→0); the driver prefers it when the bootstrap is
    # flagged unreliable below. Absent on the cheap tier (no profile ran).
    omega0_profile = None
    if best is not None and best.within_model:
        omega0_profile = best.within_model.get("profile_interval")
    reasons = list(sv.reasons)
    if warn:
        reasons.append(warn)

    # FIX 2: a phonon Γ railed out to its bound makes the derived ω₀ interval degenerate (a
    # spuriously tight bootstrap ~1e-3 meV). Flag the interval unreliable + record why, rather
    # than silently trusting it — `isar score --truth` and downstream filter on this flag.
    interval_reliable = True
    if best is not None and getattr(best, "gamma_at_bound", False) and interval is not None:
        interval_reliable = False
        reasons.append("phonon Γ at bound: ω₀ interval unreliable (resolution-limited spike, "
                       "degenerate bootstrap)")
    # Fast (HESSE) tier: no bootstrap/profile ran; report the delta-method ω₀ interval + trust gate.
    # The bootstrap `interval`/profile are absent (interval_reliable=False); the loop keys off
    # hesse_trusted instead, escalating to `full` when the asymptotic approximation does not hold.
    hesse_interval = None
    hesse_trusted = False
    if run_fast:
        hesse_interval, hesse_trusted = _hesse_interval_and_trust(best, ctx)
        interval = None
        omega0_profile = None
        interval_reliable = False
        reasons.append("fast-errors tier: HESSE ω₀ interval only "
                       + ("(trusted)" if hesse_trusted else "(untrusted → escalate to full)"))
    # Cheap quick-look tier: no interval at all, only a rough HESSE ω₀ — mark it untrustworthy so
    # the stop rule never consumes it.
    if effective_tier == "cheap":
        interval = None
        omega0_profile = None
        interval_reliable = False
        reasons.append("cheap-errors quick-look: verdict + rough HESSE ω₀ only "
                       "(no bootstrap/profile interval)")

    # Bayes-errors tier: the posterior was attached only to the selected label (to bound cost).
    # Diagnostics-only — the bootstrap `interval` above stays authoritative and nothing here touches
    # it. Extract the credible interval, evidence, and the §5.5 three-method agreement from the
    # best's within_model; a winner the attach did not reach reports not-sampled.
    bayes_interval = None
    bayes_ok = None
    bayes_ess = None
    bayes_fail_reason = None
    log_evidence = None
    log_evidence_err = None
    bayes_freq_ratio = None
    hesse_freq_ratio = None
    bayes_prior_driven = False
    bayes_fork_suspected = False
    bayes_unconstrained = False
    bayes_n_live = None
    bayes_seed = None
    dynesty_version = None
    if run_bayes:
        try:
            import dynesty as _dynesty
            dynesty_version = str(_dynesty.__version__)
        except Exception:  # noqa: BLE001 — a missing dynesty just leaves the version unrecorded
            dynesty_version = None
        bayes_n_live = (bayes_opts or {}).get("n_live")
        wm = best.within_model if (best is not None and best.within_model) else None
        if wm is not None and "bayes_ok" in wm:
            bayes_interval = tuple(wm["bayes_interval"]) if wm.get("bayes_interval") is not None else None
            bayes_ok = bool(wm.get("bayes_ok"))
            bayes_ess = wm.get("bayes_ess")
            bayes_fail_reason = wm.get("bayes_fail_reason")
            log_evidence = wm.get("log_evidence")
            log_evidence_err = wm.get("log_evidence_err")
            bayes_seed = wm.get("bayes_seed")
            ag = wm.get("agreement")
            if ag is not None:
                bayes_freq_ratio = (float(ag.bayes_freq_ratio)
                                    if np.isfinite(ag.bayes_freq_ratio) else None)
                hesse_freq_ratio = (float(ag.hesse_freq_ratio)
                                    if np.isfinite(ag.hesse_freq_ratio) else None)
                bayes_prior_driven = bool(ag.prior_driven)
                bayes_fork_suspected = bool(ag.fork_suspected)
                bayes_unconstrained = bool(ag.unconstrained)
        if bayes_ok:
            reasons.append(f"bayes-errors tier: posterior converged (ess={float(bayes_ess):.0f})")
        else:
            reasons.append("bayes-errors tier: posterior failed → frequentist fallback "
                           f"({bayes_fail_reason or 'not sampled'})")

    # Per-method wall time, summed across the error-attach survivors (each result's
    # within_model["method_seconds"]). Populated on the full/bayes tiers; 0.0 elsewhere
    # (no attach ran). The existing errors_attach_seconds lump is kept untouched.
    bootstrap_seconds = minos_seconds = profile_seconds = bayes_seconds = 0.0
    profile_method_seconds = []
    profile_batch_seconds = []
    for label in sel.survivors:
        r = fits.get(label)
        wm_r = getattr(r, "within_model", None) if r is not None else None
        msec = (wm_r or {}).get("method_seconds") or {}
        bootstrap_seconds += float(msec.get("bootstrap", 0.0))
        minos_seconds += float(msec.get("minos", 0.0))
        profile_method_seconds.append(float(msec.get("profile", 0.0)))
        if "profile_batch" in msec:
            profile_batch_seconds.append(float(msec["profile_batch"]))
        bayes_seconds += float(msec.get("bayes", 0.0))
    bootstrap_seconds = round(bootstrap_seconds, 4)
    minos_seconds = round(minos_seconds, 4)
    # attach_batch fans every survivor's profile work onto the shared pool together; its wall
    # contribution is the critical path. The executor-free compatibility path remains additive.
    profile_seconds = round((max(profile_batch_seconds) if profile_batch_seconds
                             else sum(profile_method_seconds)), 4)
    bayes_seconds = round(bayes_seconds, 4)
    best_within = best.within_model if (best is not None and best.within_model) else {}
    bootstrap_M_requested = best_within.get("M_requested", int(M))
    bootstrap_M_effective = best_within.get(
        "M_effective", best_within.get("M", 0 if not run_full else None))

    n_modes = int(best.n_modes) if best is not None else 1
    modes = list(best.modes) if (best is not None and best.modes) else None
    if modes:
        # Every mode carries its own HESSE trust (see _mode_hesse_trusted); the tracker copies the
        # SELECTED mode's onto the row, so a target certifies on its own chain, never a rival's.
        shared_cov = bool(getattr(best, "covariance_ok", False))
        shared_counts = _in_window_counts(ctx) >= _HESSE_MIN_COUNTS
        fit_settings = _fit_prior_settings(best)
        modes = [{**dict(m), "hesse_trusted": _mode_hesse_trusted(
                      dict(m), covariance_ok=shared_cov, counts_ok=shared_counts,
                      settings=fit_settings)}
                 if isinstance(m, dict) else m for m in modes]
    # GUI Phase 0 (additive, no fitting/selection logic touched): the delivered fit's linear
    # amplitudes and the model-selection candidate table. Built from the FINAL selection (``sel``)
    # and the delivered ``best`` — so an escalated re-fit (its own fit_one call) carries its own.
    amplitudes = _amplitudes_of(best, by_hyp)
    model_manifest = _model_manifest_of(best, by_hyp)
    # ADR-019 producer isolation: the predictive artifact is NOT built here. Its inputs are handed
    # to the caller, which constructs it after branch triage inside its own try/except — so a
    # predictive-construction failure can never convert this fit into a non_result, and the artifact
    # is never published before the delivery decisions that follow this function have been made.
    predictive_inputs = (
        {"best": best, "scores": sel.scores, "fits": fits, "by_hyp": by_hyp, "seed": int(seed),
         "n_members": int(predictive_n_members),
         "fit_selection_semantics": fit_selection_semantics}
        if predictive_n_members is not None else None)

    # Phase 4a — under-model falsification: the descriptor localised real signal the phonon model
    # can't fit. This is NOT an ω₀ measurement (machine contract): null the number/intervals and
    # tag descriptor_only, so the stop rule / gold / truth never count it as a delivered ω₀. The
    # descriptor peaks (stored separately) are the deliverable; the verdict routes to "unmodeled".
    omega0_err = best.omega0_err_hesse if best else None
    gamma = best.gamma if best else None
    fit_kind = best.fit_kind if best else "unconstrained"
    if sv.verdict == "signal_unmodeled":
        omega0 = omega0_err = interval = omega0_profile = hesse_interval = None
        interval_reliable = False
        fit_kind = "descriptor_only"
        # Not an ω₀ measurement → null the bayes interval/diagnostics too, same as the other intervals.
        bayes_interval = bayes_ess = None
        log_evidence = log_evidence_err = None
        bayes_freq_ratio = hesse_freq_ratio = None

    # SCENE-1 PR-a — a phonon-less winner (`elastic_only`, or a bg winner's no_signal/underpowered):
    # NOT an ω₀ measurement either. Two hard rules: (1) the delivery contract for "no number" is
    # None — the fitter's NaN would pass every `is not None` guard in the branch layer and leak
    # NaN into results.csv / report.json / exposure targeting; (2) no interval may be published,
    # because the fold-pooled fallback above is built from the *rejected* phonon-bearing survivors
    # (a phonon-less winner never carries omega0_samples), so that interval belongs to a structure
    # selection just declined — publishing it as reliable would hand the driver a certifiable
    # interval for a scan that measured no phonon.
    if best is not None and int(getattr(best, "n_modes", 1) or 0) == 0:
        omega0 = omega0_err = gamma = None
        interval = omega0_profile = hesse_interval = None
        interval_reliable = False
        hesse_trusted = False
        bayes_interval = bayes_ess = None
        log_evidence = log_evidence_err = None
        bayes_freq_ratio = hesse_freq_ratio = None

    interval_semantics = _outcome_interval_semantics(
        best=best,
        selection=sel,
        interval=interval,
        omega0_profile=omega0_profile,
        hesse_interval=hesse_interval,
        hesse_trusted=hesse_trusted,
        bayes_interval=bayes_interval,
        interval_reliable=interval_reliable,
    )
    # P0-C: candidate-row interval authority is immutably fit-conditional — every row, winner
    # included, projects only its own candidate-tier record (see ``_mode_intervals_of``). Nothing
    # about the outcome-level ``interval_semantics``/selected quantity above feeds the rows.
    candidates = _candidate_rows(sel.scores, fits, by_hyp)
    if modes:
        modes = _with_peak_positions(modes, best)

    # The QC-masked energy window this fit actually saw — the scene layer's observation footprint:
    # what a scan looked at, whether or not it found anything. Shifted onto the ω scale by the
    # calibration zero, because ``ctx.x`` is the RAW abscissa and the zero is carried separately in
    # ``ctx.center`` (the model evaluates at ``x - center``, lineshapes.convolute_voigt_dho). Without
    # the shift the window and the ω₀/omegas it must contain would be two different scales.
    energy_window = _ctx_energy_window(ctx)
    # The ORIGINAL axis plus the mask, not the kept points: cell adjacency is a fact about the
    # measurement grid, and the kept points alone cannot say which bins were dropped.
    energy_bands = _energy_bands(ctx.x, ctx.mask, ctx.center) if energy_window is not None else None

    return {
        "verdict": sv.verdict, "confidence": sv.confidence, "reasons": reasons,
        "energy_window": energy_window, "energy_bands": energy_bands,
        "omega0": omega0, "omega0_err": omega0_err,
        "interval": (tuple(interval) if interval is not None else None),
        "omega0_profile": (tuple(omega0_profile) if omega0_profile is not None else None),
        "interval_reliable": interval_reliable,
        "hesse_interval": (tuple(hesse_interval) if hesse_interval is not None else None),
        "hesse_trusted": hesse_trusted,
        "modality": sel.modality, "best_label": sel.best,
        # The catalogue identity the WINNING candidate claimed (SCENE-1 PR-c commit E), and the
        # disagreement diagnostic the branch layer fills in once branch assignment has run
        # (:func:`_record_identity_ba_disagreement`). Both are records only.
        "evidence_identity": getattr(best, "identity", None) if best is not None else None,
        "identity_ba_disagreement": None,
        # The WINNER's per-parameter boundary-pin map (SCENE-1 PR-c commit D). Persisted because a
        # railed parameter is what invalidates the Laplace curvature and what makes the reported
        # interval untrustworthy — the reader must not have to re-fit to learn which one railed.
        "at_limit": (dict(best.at_limit)
                     if best is not None and getattr(best, "at_limit", None) is not None else None),
        "gamma": gamma,
        "cost_per_dof": (best.cost_per_dof if best else None),
        "n_modes": n_modes, "modes": modes,
        "amplitudes": amplitudes, "candidates": candidates,
        "model_manifest": model_manifest,
        "fit_selection_semantics": fit_selection_semantics,
        "interval_semantics": interval_semantics,
        "predictive_inputs": predictive_inputs,
        "fit_kind": fit_kind,
        "gamma_at_bound": bool(getattr(best, "gamma_at_bound", False)) if best else False,
        "fit_role": str(fit_role),
        "errors_tier_requested": requested_tier,
        "errors_tier_effective": effective_tier,
        "escalation_reasons": escalation_reasons,
        "promotion_state": ("completed" if escalation_reasons else
                            "not_required" if requested_tier == "auto" else "not_requested"),
        "two_stage_seconds": two_stage_seconds,
        "errors_attach_seconds": errors_attach_seconds,
        # The fast pass's result (C-fastpass-record; no wall time) and its timing (C-timing,
        # provenance only).
        "fast_pass": {**_fast_pass_record(res), "exposure_basis": {
            "source": ctx.exposure_source, "reference": ctx.exposure_reference}},
        "fast_pass_timing": _fast_pass_timing(res),
        # Bayes-errors tier diagnostics (None/False/0.0 on the other tiers).
        "bayes_interval": (tuple(bayes_interval) if bayes_interval is not None else None),
        "bayes_ok": bayes_ok,
        "bayes_ess": bayes_ess,
        "bayes_fail_reason": bayes_fail_reason,
        "log_evidence": log_evidence,
        "log_evidence_err": log_evidence_err,
        "bayes_freq_ratio": bayes_freq_ratio,
        "hesse_freq_ratio": hesse_freq_ratio,
        "bayes_prior_driven": bayes_prior_driven,
        "bayes_fork_suspected": bayes_fork_suspected,
        "bayes_unconstrained": bayes_unconstrained,
        "bootstrap_seconds": bootstrap_seconds,
        "minos_seconds": minos_seconds,
        "profile_seconds": profile_seconds,
        "bayes_seconds": bayes_seconds,
        "bootstrap_M_requested": bootstrap_M_requested,
        "bootstrap_M_effective": bootstrap_M_effective,
        # Provenance-only (not FitOutcome fields): the sampler config for the run record.
        "bayes_n_live": bayes_n_live,
        "bayes_seed": bayes_seed,
        "dynesty_version": dynesty_version,
    }


def _process_one_task(args):
    """Top-level (picklable) entry point for a scan-pool worker: unpack plain data and fit.

    On Windows spawn the worker re-imports this module; every argument is picklable (verified:
    ScanRecord, Card, Guidelines, RunCalibration). No shared session/card mutation happens here —
    the returned :class:`ScanEntry` carries everything; the parent applies ledger + prior mutations
    serially in submission order."""
    (rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
     M, errors_tier, seed, boot_workers, boot_adaptive, bayes_opts) = args
    return _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                        chash, ghash, M=M, seed=seed, errors_tier=errors_tier,
                        boot_workers=boot_workers, boot_adaptive=boot_adaptive,
                        bayes_opts=bayes_opts)


@dataclass(frozen=True)
class ScanStart:
    """What a scan's start point (``_run_tasks(start_scan=)``) hands the fit stage, per input task in
    order: a task tuple to fit, a terminal :class:`ScanEntry` (never fitted), a :class:`Reused` entry, or
    None (carried: no result this run); and the scan's frozen calibration view (None: the run's)."""
    items: list
    runcal: "calibrations.RunCalibration | None" = None


@dataclass(frozen=True)
class Reused:
    """A checkpointed entry taken as this run's result: ``on_finish`` sees it, ``on_complete`` never
    (its measurement record and checkpoint exist already)."""
    entry: ScanEntry


def condition_entry(rec, det, q, T, sample, scan_id, card, chash, ghash, reason) -> ScanEntry:
    """A unit the user declared out (an excluded scan, a dead analyzer): an ``excluded`` entry made in the
    parent, never fitted, with the measurement record a card exclusion gets."""
    return ScanEntry(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                     status="excluded", content_hash=chash, guidelines_hash=ghash, reasons=[reason],
                     measurement_record=_measurement_record(rec, det, q, T, sample, scan_id, card, chash,
                                                            count_trust="excluded", reasons=[reason]))


def _scan_groups(tasks) -> list[list]:
    """The tasks split by scan, in order. They are built per record, then per detector, so a scan's
    tasks are consecutive; a scan met twice is a caller bug, refused."""
    groups: list[tuple[str, list]] = []
    for task in tasks:
        key = task[5].rsplit(":", 1)[0]
        if groups and groups[-1][0] == key:
            groups[-1][1].append(task)
            continue
        if any(k == key for k, _g in groups):
            raise ValueError(f"the fit units of scan {key} are not contiguous")
        groups.append((key, [task]))
    return [g for _k, g in groups]


def _run_tasks(tasks, card, g, runcal, exclusions, *, M, errors_tier, boot_workers,
               use_pool, scan_workers, boot_executor=None, boot_adaptive=False,
               bayes_opts=None, on_complete=None, deadline_gate=None, deep=None,
               deep_executor=None, start_scan=None, on_finish=None) -> list:
    """Fit the scan-detectors, serially or over a process pool, preserving submission order.

    ``start_scan(group)`` (one scan's consecutive task tuples) runs in the parent when the scan's first
    unit is about to be handed to a worker and returns a :class:`ScanStart`; without it every task is
    fitted against ``runcal``. Each result goes to ``on_finish`` when it completes, then to
    ``on_complete`` in submission order; a carried unit's output is None and reaches neither.

    ``deep`` (a :class:`~isar.session.deep_delivery.DeepRequest`) adds the deep pass after each
    fast-pass fit, serial path only: ``deep_executor`` is its one pool (not picklable).

    ``boot_executor`` (a shared bootstrap pool) is threaded only through the SERIAL path — an
    executor is not picklable and must never enter a scan-pool worker (nested pools). The pool path
    only runs when ``boot_workers == 1`` (Layer A owns the workers), so it never wants a shared pool.
    """
    if not tasks:
        return []
    if deep is not None and use_pool:
        raise ValueError("the deep pass fits its scans serially (use_pool=False)")
    n = len(tasks)
    groups = _scan_groups(tasks)
    begin = start_scan or (lambda group: ScanStart(list(group)))

    def stop(done):
        raise AnalysisWorkIncomplete(f"analysis deadline reached after {done}/{n} scan fit units")

    def view(st):
        return runcal if st.runcal is None else st.runcal

    def finish(item):
        entry = item.entry if isinstance(item, Reused) else item
        if entry is not None and on_finish is not None:
            on_finish(entry)

    def complete(item):
        if item is None or isinstance(item, Reused):
            return None if item is None else item.entry
        if on_complete is not None:
            on_complete(item)
        return item

    if not use_pool:
        out = []
        for group in groups:
            st = None
            for i in range(len(group)):
                # One deadline wave per unit fitted; a scan is never started after the deadline.
                if (st is None or isinstance(st.items[i], tuple)) and deadline_gate is not None \
                        and not deadline_gate.begin_wave():
                    stop(len(out))
                if st is None:
                    st = begin(group)
                item = st.items[i]
                if isinstance(item, tuple):
                    rec, det, q, T, sample, scan_id, chash, ghash, seed = item
                    item = _process_one(rec, det, q, T, sample, scan_id, card, g, view(st), exclusions,
                                        chash, ghash, M=M, seed=seed, errors_tier=errors_tier,
                                        boot_workers=boot_workers, boot_executor=boot_executor,
                                        boot_adaptive=boot_adaptive, bayes_opts=bayes_opts,
                                        **({"deep": deep, "deep_executor": deep_executor}
                                           if deep is not None else {}))
                finish(item)
                out.append(complete(item))
        return out

    # Lazy refill: a unit is handed to the pool only when a worker is free, so a scan's start point
    # (and the saves it reads) comes as late as its turn; no worker idles while units remain. Under a
    # deadline, bounded waves let the soft stop end *new* work without killing active processes.
    # ``on_complete`` follows submission order, so cached-unit reduction stays canonical either way.
    max_workers = min(scan_workers, n)
    no_deadline = deadline_gate is None or deadline_gate.deadline is None
    units = [(group, i) for group in groups for i in range(len(group))]
    nxt = 0
    started: dict[int, ScanStart] = {}
    pending: deque = deque()          # (future, is a pool future), in submission order
    done_order: deque = deque()       # futures as they complete (their done callbacks)
    reported: set = set()             # futures whose on_finish ran
    out = []

    def submit_next(ex) -> int:
        nonlocal nxt
        group, i = units[nxt]
        nxt += 1
        if i == 0:
            started[id(group)] = begin(group)
        st = started[id(group)]
        item = st.items[i]
        if isinstance(item, tuple):
            rec, det, q, T, sample, scan_id, chash, ghash, seed = item
            fut = ex.submit(_process_one_task, (rec, det, q, T, sample, scan_id, card, g, view(st), exclusions,
                                                chash, ghash, M, errors_tier, seed, boot_workers,
                                                boot_adaptive, bayes_opts))
        else:                                 # already decided in the parent: a completed stand-in
            fut = Future()
            fut.set_result(item)
        fut.add_done_callback(done_order.append)
        pending.append((fut, isinstance(item, tuple)))
        return int(isinstance(item, tuple))

    def running() -> int:
        return sum(1 for f, pooled in pending if pooled and not f.done())

    def report(f):
        if f not in reported:
            reported.add(f)
            finish(f.result())

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        while True:
            if no_deadline:
                while nxt < len(units) and running() < max_workers:
                    submit_next(ex)
            elif not pending and nxt < len(units):
                if not deadline_gate.begin_wave():
                    stop(len(out))
                submitted = 0
                while nxt < len(units) and submitted < max_workers:
                    submitted += submit_next(ex)
            if not pending:
                break
            live = [f for f, _pooled in pending if not f.done()]
            if live:
                wait(live, return_when=FIRST_COMPLETED)
            while done_order:
                report(done_order.popleft())
            while pending and pending[0][0].done():
                f, _pooled = pending.popleft()
                report(f)                     # its done callback may not have run yet
                out.append(complete(f.result()))
    return out


# Phase A branch assignment: near a zone centre the acoustic ω₀ is unresolvable BY POSITION (below
# BA.RESOLVABLE_FWHM_FACTOR × resolution FWHM the merged Stokes/anti-Stokes blob is shape-identical to
# an elastic peak). There it is measured by the constrained (hard Γ + height) fit, or reported as
# branch_continuation.
# The dispersion prior narrows the Ω_d IC grid to ω₀±1.5 (ic.PRIOR_HALF_WIDTH). A COLD acoustic belief
# predicts ω₀≈0 with a large σ; narrowing to [0,1.5] there would miss the resolvable acoustic (1.4+) and
# deadlock the bootstrap. So the prior narrows the IC only when it is CONFIDENT (σ ≤ this); otherwise the
# IC stays wide (the residual-triggered 2-mode fit surfaces the real modes) while acoustic_omega still
# selects which mode is the QoI.
_PRIOR_NARROW_SIGMA = 0.6


def _dispersion_period(card) -> float | None:
    """The card's reduced-zone fold period (``[dispersion] zone_period``), or None when the
    branch-assignment posterior is not configured (⇒ pre-steering behaviour, unchanged)."""
    d = getattr(card.policy, "dispersion", None) or {}
    p = d.get("zone_period")
    try:
        return float(p) if p else None
    except (TypeError, ValueError):
        return None


def _campaign_target(card) -> tuple[str, str, tuple[float, float]]:
    state = getattr(card, "state", None)
    target = getattr(state, "campaign_target", None) or {}
    branch = str(target.get("branch") or "acoustic")
    label = str(target.get("label") or branch)
    energy = target.get("energy_search_meV") or [-2.0, 7.0]
    return branch, label, (float(energy[0]), float(energy[1]))


def _campaign_hypotheses(card) -> list[dict]:
    state = getattr(card, "state", None)
    snapshot = getattr(state, "hypothesis_snapshot", None) or {}
    value = snapshot.get("hypotheses") or []
    return [dict(item) for item in value if isinstance(item, dict)]


def _target_fold_anchor_count(card, *, sample: str, branch: str) -> int:
    """Count trusted target-branch anchors while accepting pre-target legacy cards."""
    state = getattr(card, "state", None)
    points = getattr(state, "dispersion_prior", None) or []
    return sum(1 for point in points
               if point.get("sample") == sample and point.get("branch_id") == branch
               and point.get("modality") == "fold")


def _assign_and_triage(fit, descriptor, ctx, *, card, sample, T, q, t_round, near_gamma):
    """Preserve a completed fit as unordered evidence for the tracked branch (Phase A). Returns
    override fields ``{deliver, branch_id, weight, entropy, resolvable, selected_energy}`` plus the
    candidate identity, or ``None`` when the posterior is off.

    The authoritative branch label is NOT assigned here: it is assigned only after every pooled
    master has updated, using the whole objective path in both directions
    (:func:`isar.session.evidence_update._apply_tracked_branch`). Per-q feature selection — picking
    the feature nearest/lowest/highest in energy at this q and calling it the target — was deleted
    with the retired ``target_selector`` values; the EIG planner maps a DECLARED branch, so no
    compiled campaign can ask for it."""
    period = _dispersion_period(card)
    if period is None:
        return None
    target_branch, _target_label, _energy_search = _campaign_target(card)
    modes = fit.get("modes")
    if not modes and fit.get("omega0") is not None:
        modes = [{"omega0": fit["omega0"], "omega0_err": fit.get("omega0_err"),
                  "gamma": fit.get("gamma"), "amplitude": 0.0}]
    feats = BA.features_from(modes or [], descriptor, gauss_fwhm=ctx.gauss_fwhm)
    if not feats:
        return {"deliver": bool(fit.get("omega0") is not None), "branch_id": None,
                "weight": None, "entropy": None, "resolvable": None,
                "selected_energy": fit.get("omega0")}
    return {"deliver": True, "branch_id": None, "weight": None, "entropy": None,
            "resolvable": None, "selected_energy": fit.get("omega0"),
            "candidate_id": f"{target_branch}:tracked_branch",
            "candidate_state": "provisional"}


def _record_identity_ba_disagreement(fit, branch_id):
    """Record — never act on — a winning candidate whose catalogue identity contradicts the branch
    assignment (SCENE-1 PR-c commit E).

    Two independent claims about what was measured: evidence selection's identity-conditioned
    winner, and branch assignment's posterior. BA keeps driving delivery until SCENE-6, so this
    touches no verdict, confidence, stopping or steering decision; it exists so the disagreement
    rate is measurable before the axes are ever allowed to swap authority. ``None`` whenever
    either side is silent (the production catalogue's every scan today) or the two agree.
    """
    identity = fit.get("evidence_identity")
    disagreement = None
    if identity is not None and branch_id is not None and str(identity) != str(branch_id):
        disagreement = {"evidence_identity": str(identity), "branch_id": str(branch_id)}
    fit["identity_ba_disagreement"] = disagreement
    return disagreement


def _apply_branch_triage(fit, descriptor, ctx, *, card, sample, T, q, t_round, near_gamma,
                         master: bool):
    """Run branch assignment + honest triage over ``fit`` and apply its verdict (Phase A). Shared by
    the per-scan (:func:`_finish_outcome`) and pooled-master (:func:`_fit_master_group`) paths.

    Returns branch assignment, candidate posterior/state, and secondary features — all
    None when the posterior is off or the fit is a ``signal_unmodeled`` descriptor result. When the
    triage declines delivery it mutates ``fit`` in place: flips the verdict to ``branch_continuation``,
    nulls the ω₀ fields, and appends a reason (worded per ``master``). It always writes the
    records-only ``identity_ba_disagreement`` diagnostic (:func:`_record_identity_ba_disagreement`)
    — the one place where the winner's catalogue identity and the assigned branch are both known."""
    branch_id = assignment_entropy = branch_weight = resolvable = None
    branch_posterior = candidate_id = candidate_state = selected_energy = None
    secondary_features = None
    if fit.get("verdict") != "signal_unmodeled":
        triage = _assign_and_triage(fit, descriptor, ctx, card=card, sample=sample, T=T, q=q,
                                    t_round=t_round, near_gamma=near_gamma)
        if triage is not None:
            branch_id = triage.get("branch_id")
            assignment_entropy = triage.get("entropy")
            branch_weight = triage.get("weight")
            resolvable = triage.get("resolvable")
            secondary_features = triage.get("secondary_points") or None
            branch_posterior = triage.get("posterior")
            selected_energy = triage.get("selected_energy")
            candidate_id = triage.get("candidate_id")
            candidate_state = triage.get("candidate_state")
            if triage.get("deliver") is False and fit.get("omega0") is not None:
                target_branch, _target_label, _energy = _campaign_target(card)
                if master:
                    fit["reasons"].append(
                        f"branch triage (master): not a resolvable {target_branch} delivery "
                        f"(assigned {branch_id}); reported branch_continuation")
                else:
                    fit["reasons"].append(
                        f"branch triage: not a resolvable {target_branch} delivery (assigned {branch_id}); "
                        f"reported as branch_continuation, ω₀ withheld")
                fit["verdict"] = "branch_continuation"
                fit["omega0"] = fit["omega0_err"] = fit["interval"] = None
                fit["omega0_profile"] = fit["hesse_interval"] = None
                fit["bayes_interval"] = None
                fit["interval_reliable"] = False
    _record_identity_ba_disagreement(fit, branch_id)
    _refresh_interval_semantics(fit)
    return (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
            branch_posterior, candidate_id, candidate_state, selected_energy)


def _gate_scan(rec, det, card, g, runcal, exclusions, base) -> tuple:
    """Exclusion + calibration gate. Returns ``(entry, calres)``.

    ``entry`` is a terminal :class:`ScanEntry` (``excluded`` or ``held_for_calibration``) when the
    scan is gated — ``calres`` is None in the exclusion case; otherwise ``entry`` is None and
    ``calres`` is the resolved calibration for the fit path. (calres is returned rather than
    re-resolved by the caller so ``calibrations.resolve`` is still called exactly once.)"""
    # Exclusions (guideline / seed / state) — never fitted.
    reason = g.excluded(rec.scan_number)
    if reason is None and rec.scan_number in exclusions:
        reason = "excluded (card exclusions_seed / state)"
    if reason is not None:
        return ScanEntry(status="excluded", reasons=[reason], **base), None

    # Calibration gate.
    calres = calibrations.resolve(rec, det, card, runcal)
    if calres.held:
        return (ScanEntry(status="held_for_calibration", reasons=[calres.held_reason],
                          provenance=calres.provenance(), **base),
                calres)
    return None, calres


def _apply_low_q_rescue(ctx, prior, card, sample, T, rec, near_gamma, region_reasons, det=None):
    """Steering Phase 2c — LOW-q rescue: near the zone centre the acoustic peak merges with the
    elastic line and Γ/A go unidentifiable. If the dispersion prior predicts a phonon within
    LOW_Q_FWHM_FACTOR·resolution of the elastic line AND a branch model exists, softly pin Γ and A
    to the branch (contract 1: penalty enters the objective only) and let ω₀ float. Off for
    well-separated peaks and whenever no branch/prior is available (⇒ no behavior change there).

    Returns the (possibly constraint-augmented) ctx; appends the applied-constraint note to
    ``region_reasons`` in place."""
    if prior is not None and np.isfinite(prior.omega0) \
            and prior.omega0 < LOW_Q_FWHM_FACTOR * ctx.gauss_fwhm:
        pred = branch_model.branch_for(card.state, sample=sample, T=T, omega0=float(prior.omega0))
        if pred is not None:
            exposure, _mode = _exposure_per_point(rec, ctx, det)
            exp = exposure if np.isfinite(exposure) else 0.0
            # The branch predicts an intrinsic A; the fit parameter is the height, so convert at the
            # predicted ω₀ (clamped to the fit's own energy floor) and Γ, with this scan's phonon.
            recipe = _phonon_for(ctx)
            phonon_comp = recipe() if isinstance(recipe, type) else recipe
            per_a = phonon_comp.unit_peak({"omega0": max(float(prior.omega0), EPS_OMEGA0),
                                           "Gamma": float(pred.gamma)}, ctx).height
            # Near Γ AND in-anchor: the CONSTRAINED recipe (hard Γ + height from 1/ω; ω₀ is then
            # pinned by position only — T-1, TODO.md DEFECT-10). Elsewhere the ordinary soft rescue
            # (the height stays soft, absorbing monitor / focusing-asymmetry drift).
            if near_gamma and pred.in_anchor_range:
                cons = pred.hard_constraints(exp, per_a)
                note = (f"near-Γ constrained fit (hard Γ={pred.gamma:.2f} meV + height from 1/ω; "
                        f"ω₀ pinned by position only)")
            else:
                cons = pred.soft_constraints(exp, per_a)
                note = (f"low-q branch soft-constraint applied (Γ≈{pred.gamma:.2f}±{pred.gamma_sigma:.2f}, "
                        f"A from 1/ω{'' if pred.in_anchor_range else '; extrapolated'})")
            if not cons.is_empty():
                ctx = replace(ctx, constraints=cons)
                region_reasons.append(note)
    return ctx


def _resolve_prior_and_context(rec, det, q, T, sample, scan_id, card, g, calres, base):
    """Prior lookup + context construction + descriptor building.

    Returns the working set ``(ctx, prior, prior_source, n_neighbors, near_gamma, t_round, labels,
    peaks, descriptor, region_reasons)``, or a terminal ``non_result`` :class:`ScanEntry` when the
    scan is undersampled after guideline masking."""
    # Prior: user expectation beats the auto prior.
    period = _dispersion_period(card)
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    prior = g.expectation_for(scan_number=rec.scan_number, q=q, T=T)
    prior_source = "user"
    n_neighbors = None
    if prior is None:
        target_branch, _target_label, _energy_search = _campaign_target(card)
        prior = session_prior.prior_for(card.state, sample=sample, T=T, q=q,
                                        exclude_scan_id=scan_id, period=period, t_round=t_round,
                                        branch_id=target_branch)
        prior_source = "auto" if prior is not None else None
        if prior is not None:
            n_neighbors = sum(1 for p in card.state.dispersion_prior
                              if p.get("sample") == sample and p.get("scan_id") != scan_id)

    # Context + undersampled guard (min_fit_points re-checked AFTER guideline region masking).
    labels = g.structure_labels_for(scan_number=rec.scan_number, q=q, T=T)
    peaks = g.peaks_for(scan_number=rec.scan_number, q=q, T=T)
    ctx, n_used, region_reasons = build_context(
        rec, det, card, calres, prior, g, scan_number=rec.scan_number, q=q, T=T)
    if ctx is None:
        rs = [f"undersampled: {n_used} < {g.min_fit_points} kept points"] + region_reasons
        return ScanEntry(status="non_result", reasons=rs,
                         provenance=calres.provenance(), **base)

    # Phase A: hand the fit the branch-assignment acoustic ω₀(q) prediction so a multi-mode fit reports
    # the ACOUSTIC mode as primary (not nearest-any-prior / largest-amplitude). Narrow the IC grid with
    # it only when the belief is CONFIDENT — a cold, uncertain ~0 prediction must not over-narrow the
    # search and miss the resolvable acoustic (the bootstrap deadlock).
    target_branch, _target_label, energy_search = _campaign_target(card)
    target_anchors = _target_fold_anchor_count(card, sample=sample, branch=target_branch)
    established = target_branch == "acoustic" or target_anchors >= branch_model.MIN_ANCHORS
    if target_branch == "optic" and not established:
        ctx = replace(ctx, acoustic_omega=None, prior_omega=None)
    elif period is not None and prior is not None and np.isfinite(prior.omega0):
        confident = float(prior.omega0_sigma or 1e9) <= _PRIOR_NARROW_SIGMA
        ctx = replace(ctx, acoustic_omega=float(prior.omega0),
                      prior_omega=(float(prior.omega0) if confident else None))

    near_gamma = (target_branch == "acoustic" and prior is not None and np.isfinite(prior.omega0)
                  and prior.omega0 < BA.RESOLVABLE_FWHM_FACTOR * ctx.gauss_fwhm)
    if target_branch == "acoustic":
        ctx = _apply_low_q_rescue(ctx, prior, card, sample, T, rec, near_gamma, region_reasons, det)

    # L0 descriptor: built once here (mask/counts are now fixed) and reused for the verdict's
    # under-model falsification (Phase 4a) AND storage — including through the hard-fix retry below.
    descriptor = _build_descriptor(ctx)
    return (ctx, prior, prior_source, n_neighbors, near_gamma, t_round, labels, peaks,
            descriptor, region_reasons)


def _hard_fix_escalation(fit, ctx, prior, card, sample, T, labels, peaks, descriptor,
                         region_reasons, *, M, seed, errors_tier, boot_workers, boot_executor,
                         boot_adaptive, bayes_opts=None):
    """Steering Phase 3b — hard-fix escalation: if the (soft / Stokes-only) fit STILL rails Γ to its
    bound (true low-count degeneracy — Γ un-identifiable even under the soft prior) and the branch
    predicts Γ within its anchor range, re-fit with Γ frozen to the branch value (k reduced,
    tagged branch_conditional). Only in-range (never hard-fix an extrapolated branch); adopted
    only if it converges and no longer rails. A soft A constraint, if any, is preserved.

    Returns the adopted fit dict (the escalated ``hfit`` when accepted, else the original ``fit``);
    appends its provenance note to ``region_reasons`` in place.

    Skipped when Γ is ALREADY hard-constrained (the near-Γ branch_conditional path): gamma_at_bound is
    computed from the fitted Γ value regardless of constraints, so a frozen Γ can still read at-bound —
    but re-freezing an already-frozen Γ rescues nothing, and rebuilding constraints from soft-only here
    would silently drop the existing hard phonon.h intensity constraint."""
    target_branch, _target_label, _energy_search = _campaign_target(card)
    already_hard_gamma = (ctx.constraints is not None
                          and "phonon.Gamma" in (ctx.constraints.hard or {}))
    if (target_branch == "acoustic" and fit.get("gamma_at_bound")
            and prior is not None and np.isfinite(prior.omega0)
            and not already_hard_gamma):
        pred = branch_model.branch_for(card.state, sample=sample, T=T, omega0=float(prior.omega0))
        if pred is not None and pred.in_anchor_range and pred.gamma > 0:
            soft = dict(ctx.constraints.soft) if ctx.constraints is not None else {}
            origins = dict(ctx.constraints.origins) if ctx.constraints is not None else {}
            soft.pop("phonon.Gamma", None)                 # Γ becomes hard, not soft
            origins.pop("phonon.Gamma", None)
            origins["phonon.Gamma"] = "data_dependent_rescue"
            hctx = replace(ctx, constraints=ParamConstraints(
                soft=soft,
                hard={"phonon.Gamma": float(pred.gamma)},
                origins=origins,
            ))
            try:
                hfit = fit_one(hctx, card, prior=prior, M=M, seed=seed, structure_labels=labels,
                               peaks=peaks, errors_tier=errors_tier, boot_workers=boot_workers,
                               boot_executor=boot_executor, boot_adaptive=boot_adaptive,
                               descriptor=descriptor, bayes_opts=bayes_opts)
            except Exception as exc:  # noqa: BLE001 — escalation is best-effort; keep the original fit
                log.warning("hard-fix escalation fit failed (%s): %s", type(exc).__name__, exc)
                hfit = None
            if hfit is not None and hfit.get("omega0") is not None \
                    and not hfit.get("gamma_at_bound"):
                fit = hfit
                region_reasons.append(
                    f"hard-fix escalation: Γ frozen to branch {pred.gamma:.2f} meV "
                    f"(branch_conditional; soft prior alone left Γ at bound)")
    return fit


def _finish_outcome(fit, descriptor, ctx, rec, det, q, T, sample, card, g, calres, prior,
                    prior_source, n_neighbors, near_gamma, t_round, region_reasons, base,
                    *, M, seed, t_fit0) -> ScanEntry:
    """Branch assignment + outcome/provenance construction → the terminal ``fitted`` ScanEntry."""
    # Phase A — Bayesian branch assignment + honest triage. Decide whether this fit's ω₀ is a
    # deliverable ACOUSTIC measurement or must abstain, and tag the branch it was assigned to so an
    # optic/spurious feature can never seed or be scored as the acoustic branch. Skipped for
    # signal_unmodeled (already a non-ω₀ descriptor result) and when the posterior is off (period None).
    (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
     branch_posterior, candidate_id, candidate_state, selected_energy) = _apply_branch_triage(
        fit, descriptor, ctx, card=card, sample=sample, T=T, q=q, t_round=t_round,
        near_gamma=near_gamma, master=False)

    fit["reasons"].extend(region_reasons)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    # omega_window demotion (report-level; verdict unchanged).
    if (g.omega_window is not None and disposition == "resolved" and fit["omega0"] is not None
            and not (g.omega_window[0] <= fit["omega0"] <= g.omega_window[1])):
        disposition = "flagged"
        fit["reasons"].append(f"omega0 {fit['omega0']:.2f} outside user window {g.omega_window}")

    prior_used = None
    if prior is not None:
        prior_used = {"omega0": prior.omega0, "sigma": prior.omega0_sigma,
                      "n_neighbors": n_neighbors, "source": prior_source}

    target_branch, target_label, _energy_search = _campaign_target(card)
    acquisition_id, acquisition = _acquisition_provenance_for(rec, card, base["scan_id"])
    acquisition_target = (acquisition or {}).get("acquisition_target_energy")
    submitted_ncount = (acquisition or {}).get("submitted_ncount")
    exposure_info = _effective_exposure_for(
        rec, ctx, fit, target_center=selected_energy,
        acquisition_target_energy=acquisition_target, submitted_ncount=submitted_ncount, det=det)
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=sample, prior_used=prior_used, calib=calres.summary(),
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        amplitudes=fit.get("amplitudes"), candidates=fit.get("candidates"),
        model_manifest=fit.get("model_manifest"),
        interval_reliable=fit.get("interval_reliable", True),
        omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        intensity=_intensity_measure(rec, ctx, fit, det),
        descriptor=(descriptor.to_dict() if descriptor is not None else None),
        fit_kind=fit.get("fit_kind", "unconstrained"),
        branch_id=branch_id, assignment_entropy=assignment_entropy,
        resolvable=resolvable, branch_weight=branch_weight,
        secondary_features=secondary_features,
        branch_posterior=branch_posterior, candidate_id=candidate_id,
        candidate_state=candidate_state,
        bayes_interval=fit.get("bayes_interval"), bayes_ok=fit.get("bayes_ok"),
        bayes_ess=fit.get("bayes_ess"), bayes_fail_reason=fit.get("bayes_fail_reason"),
        log_evidence=fit.get("log_evidence"), log_evidence_err=fit.get("log_evidence_err"),
        bayes_freq_ratio=fit.get("bayes_freq_ratio"), hesse_freq_ratio=fit.get("hesse_freq_ratio"),
        bayes_prior_driven=fit.get("bayes_prior_driven", False),
        bayes_fork_suspected=fit.get("bayes_fork_suspected", False),
        bayes_unconstrained=fit.get("bayes_unconstrained", False),
        target_branch=target_branch, target_label=target_label,
        effective_exposure=exposure_info.get("value"), exposure_provenance=exposure_info,
        acquisition_id=acquisition_id,
        search_energy_lo=(acquisition or {}).get("search_energy_lo"),
        search_energy_hi=(acquisition or {}).get("search_energy_hi"),
        fit_selection_semantics=fit.get("fit_selection_semantics"),
        interval_semantics=fit.get("interval_semantics"),
        at_limit=fit.get("at_limit"),
        energy_window=fit.get("energy_window"), energy_bands=fit.get("energy_bands"),
        evidence_identity=fit.get("evidence_identity"),
        identity_ba_disagreement=fit.get("identity_ba_disagreement"),
        fast_pass=fit["fast_pass"], deep_pass=fit.get("deep_pass"),
        evidence_sink=evidence_sink_for(getattr(rec, "raw_header", None),
                                        getattr(card.state, "acquisition_provenance", None)))
    outcome.probability_semantics = _refresh_probability_semantics(outcome.__dict__)

    # NB: the running dispersion prior (session_prior.add_point) is fed by the PARENT in
    # submission order after the fit returns — start-of-batch seeding, so a parallel batch is
    # bit-identical to a serial one. This worker never mutates shared card/session state.
    return ScanEntry(status="fitted", reasons=outcome.reasons,
                     detectors={det: outcome.to_dict()},
                     provenance={**calres.provenance(),
                                 "M": M, "seed": seed, "prior_source": prior_source,
                                 "fit_seconds": round(time.perf_counter() - t_fit0, 4),
                                 "two_stage_seconds": fit.get("two_stage_seconds"),
                                 "errors_attach_seconds": fit.get("errors_attach_seconds"),
                                 **_timing_provenance(fit)}, **base)


def _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                 chash, ghash, *, M, seed, errors_tier="full", boot_workers=1,
                 boot_executor=None, boot_adaptive=False, bayes_opts=None, deep=None,
                 deep_executor=None, progress: ProgressFn | None = None) -> ScanEntry:
    _t_fit0 = time.perf_counter()
    base = dict(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                content_hash=chash, guidelines_hash=ghash)

    gated, calres = _gate_scan(rec, det, card, g, runcal, exclusions, base)
    if gated is not None:
        return replace(gated, measurement_record=_measurement_record(
            rec, det, q, T, sample, scan_id, card, chash,
            count_trust=gated.status, reasons=gated.reasons))

    working = _resolve_prior_and_context(rec, det, q, T, sample, scan_id, card, g, calres, base)
    if isinstance(working, ScanEntry):
        return replace(working, measurement_record=_measurement_record(
            rec, det, q, T, sample, scan_id, card, chash,
            count_trust="undersampled", reasons=working.reasons))
    (ctx, prior, prior_source, n_neighbors, near_gamma, t_round, labels, peaks,
     descriptor, region_reasons) = working

    # Counts are authoritative from here on: the arena reached a built ScanContext, so it is
    # TRUSTED — recorded BEFORE fit_one is even attempted (PR 4.0 #26 hard constraint), so a fit
    # crash below still leaves this record behind. Same band derivation the delivered fit uses
    # (_ctx_energy_window / _energy_bands), so the two never disagree about what was measured.
    #
    # _ctx_energy_window / _energy_bands can raise (a non-finite energy axis, review finding 9:
    # _energy_bands rejects a non-finite value anywhere in ctx.x, even outside the mask): guarded
    # here so a single broken axis yields a terminal non_result WITH its measurement record
    # (energy fields None, the error recorded as a reason), never a batch abort — the same
    # posture as the fit_one except-block just below.
    try:
        _energy_window = _ctx_energy_window(ctx)
        _energy_bands_kept = (_energy_bands(ctx.x, ctx.mask, ctx.center)
                              if _energy_window is not None else None)
    except ValueError as exc:
        measurement_record = _measurement_record(
            rec, det, q, T, sample, scan_id, card, chash, count_trust="trusted",
            reasons=[f"energy window error ({type(exc).__name__}): {exc}"] + region_reasons)
        return ScanEntry(status="non_result",
                         reasons=[f"energy window error ({type(exc).__name__}): {exc}"]
                                 + region_reasons,
                         provenance=calres.provenance(), **base,
                         measurement_record=measurement_record)
    _acq_id, _acq = _acquisition_provenance_for(rec, card, scan_id)
    _exposure_info = _effective_exposure_for(
        rec, ctx, {}, acquisition_target_energy=(_acq or {}).get("acquisition_target_energy"),
        submitted_ncount=(_acq or {}).get("submitted_ncount"), det=det)
    measurement_record = _measurement_record(
        rec, det, q, T, sample, scan_id, card, chash, count_trust="trusted",
        reasons=region_reasons,
        energy_window=_energy_window, energy_bands=_energy_bands_kept,
        effective_exposure=_exposure_info.get("value"), exposure_provenance=_exposure_info)

    try:
        fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, structure_labels=labels, peaks=peaks,
                      errors_tier=errors_tier, boot_workers=boot_workers,
                      boot_executor=boot_executor, boot_adaptive=boot_adaptive, descriptor=descriptor,
                      bayes_opts=bayes_opts, progress=progress)
    except Exception as exc:  # noqa: BLE001 — one unfittable scan must not kill the batch
        log.warning("%s: fit error", scan_id, exc_info=True)
        return ScanEntry(status="non_result",
                         reasons=[f"fit error ({type(exc).__name__}): {exc}"] + region_reasons,
                         provenance=calres.provenance(), **base,
                         measurement_record=measurement_record)

    fast_fit = fit
    fit = _hard_fix_escalation(fit, ctx, prior, card, sample, T, labels, peaks, descriptor,
                               region_reasons, M=M, seed=seed, errors_tier=errors_tier,
                               boot_workers=boot_workers, boot_executor=boot_executor,
                               boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
    if deep is not None:
        # A hard-fix escalation replaced the fit by one on a constrained context: refused.
        record, timing = deep_delivery.deliver(
            fit, ctx, deep, executor=deep_executor,
            refuse="hard-fix escalation replaced the fast-pass fit" if fit is not fast_fit else None,
            progress=progress)
        fit["deep_pass"], fit["deep_pass_timing"] = _finite_json(record), _finite_json(timing)

    entry = _finish_outcome(fit, descriptor, ctx, rec, det, q, T, sample, card, g, calres, prior,
                            prior_source, n_neighbors, near_gamma, t_round, region_reasons, base,
                            M=M, seed=seed, t_fit0=_t_fit0)
    return replace(entry, measurement_record=measurement_record)

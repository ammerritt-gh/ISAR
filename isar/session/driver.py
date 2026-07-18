"""The pipeline driver (UNIFICATION_DESIGN §2.5) — parse → reduce → route → calibrate → fit → collate.

`run_batch` is the whole pipeline behind `isar run`: idempotent (re-parses everything, re-fits only
new / content-drifted / guideline-drifted / previously-held scans), context-updates before
measurements, calibration-gated fits seeded by the prior/guidelines, verdict + confidence per scan,
results written to the ledger + `results.csv`.

`build_context` and `fit_one` generalize the two gold-check `run_pipeline` recipes — same analysis
calls, same order — with the inputs (zero, resolution, slit, prior, guidelines) derived from card +
state instead of hardcoded. Nothing in `isar/analyze/` is modified (§4).
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from isar.analyze import errors, loop, select, verdict
from isar.analyze import gold as gold_mod
from isar.analyze.context import ScanContext, effective_exposure, normalized_exposure
from isar.analyze.descriptor import describe_spectrum
from isar.analyze.structures import enumerate_multimode, enumerate_structures
from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.schema.card import Card
from isar.schema.hypotheses import preset as hypothesis_preset
from isar.schema.steering import IntensityMeasure, ParamConstraints
from isar.session import branch_assignment as BA
from isar.session import branch_model, calibrations, guidelines as guide_mod, ingest, router, session_prior

# Steering Phase 2c: apply the low-q branch soft-constraint when the dispersion prior predicts a
# phonon within this many resolution FWHM of the elastic line (the merging regime where Γ/A go
# unidentifiable). Above it the peak is well separated and the fit is left unconstrained.
LOW_Q_FWHM_FACTOR = 2.0
# Error tiers that run the authoritative frequentist interval (MINOS + parametric bootstrap + ω₀
# profile). "bayes" is a strict superset of "full" — everything full does, plus the nested-sampling
# posterior on the prelim-best label (diagnostics-only; the bootstrap interval stays authoritative).
FULL_TIERS = ("full", "bayes")
from isar.session import unit_data
from isar.session.state import ScanEntry, SessionState, content_hash
from isar.session.timing import NullTimer
from isar.session.workspace import Workspace

log = logging.getLogger(__name__)


# ===========================================================================
# per-scan outcome
# ===========================================================================
@dataclass
class FitOutcome:
    detector_id: str
    verdict: str
    disposition: str
    confidence: float
    omega0: float | None
    omega0_err: float | None
    interval: tuple | None
    modality: str | None
    gamma: float | None
    gamma_provisional: bool
    best_label: str | None
    cost_per_dof: float | None
    q: list
    T: float
    sample: str
    prior_used: dict | None
    calib: dict
    reasons: list = field(default_factory=list)
    n_modes: int = 1
    modes: list | None = None    # per-mode dicts (canonicalized ascending by ω₀) when n_modes > 1
    # FIX 2: False when a phonon Γ railed out to its bound → the reported ω₀ interval is
    # degenerate/untrustworthy (see fit_one). Defaulted True for back-compat with older ledgers.
    interval_reliable: bool = True
    # Exact 1-D profile-likelihood interval on ω₀ (errors/profile.omega0_profile_interval). Unlike
    # the parametric-bootstrap `interval` it stays well-defined when Γ→0 (the bound corner where the
    # bootstrap collapses), so the driver prefers it when `interval_reliable` is False.
    omega0_profile: tuple | None = None
    # Fast (HESSE) tier: the delta-method ω₀ ± 1σ_HESSE interval and whether it is trusted as the
    # convergence interval (Γ off-bound + PD covariance + counts). None/False on other tiers.
    hesse_interval: tuple | None = None
    hesse_trusted: bool = False
    # A "master" fit of the combine-before-divide pool of all scans at this (Q,T): pooled=True with
    # n_members = how many scans were summed. Per-scan fits leave these at their defaults.
    pooled: bool = False
    n_members: int | None = None
    # Member scan labels of a pooled master (chain of custody): bare native scan numbers as strings,
    # qualified `experiment:scan:detector` ONLY for a scan number that repeats within this member list
    # (a multi-detector merge). Order preserves ms.members. None on per-scan fits and older ledgers.
    members: list | None = None
    # Portable, provenance-bearing intensity for the primary phonon (steering contract 2). A dict
    # (IntensityMeasure.to_dict) or None; carried onto the dispersion prior so a later scan can
    # predict expected counts. Absent on masters (no single-record normalization) and older ledgers.
    intensity: dict | None = None
    # Model-free L0 description of the spectrum (ScanDescriptor.to_dict): peaks + significance,
    # independent of the phonon model. Attached to every scan; nothing decides on it yet (the Phase-4
    # falsification gate compares its peak count to the fitted mode count). None on older ledgers.
    descriptor: dict | None = None
    # Fit provenance (steering contract 4): unconstrained | soft_prior | branch_conditional |
    # stokes_only | descriptor_only. Anything but "unconstrained" is not an ordinary phonon fit.
    fit_kind: str = "unconstrained"
    # Phase A Bayesian branch assignment: which dispersion branch this scan's ω₀ was assigned to
    # (acoustic | optic | new | ...), the Shannon entropy of that assignment, whether the acoustic ω₀
    # was position-resolvable (ω₀ ≳ 1 FWHM), and the soft assignment weight fed to the branch model.
    # None on older ledgers / when the branch-assignment posterior is off (no dispersion.zone_period).
    branch_id: str | None = None
    assignment_entropy: float | None = None
    resolvable: bool | None = None
    branch_weight: float | None = None
    # Rival-branch anchor candidates from THIS delivered scan's other features (each
    # {omega0, branch_id, weight}) — banked onto the dispersion prior by the parent so an optic
    # belief can form and claim its own features at later q. None/empty on abstains + older ledgers.
    secondary_features: list | None = None
    # Bayes-errors tier (--errors bayes): the nested-sampling posterior on ω₀ + its §5.5
    # three-method (HESSE / bootstrap / posterior) cross-check. Diagnostics-only — the bootstrap
    # `interval` stays authoritative; certification/confidence never read these. All defaulted for
    # back-compat with older ledgers and the non-bayes tiers.
    bayes_interval: tuple | None = None   # (lo, median, hi) 68% credible, diagnostics-only
    bayes_ok: bool | None = None          # None = tier didn't run bayes; False = ran, failed gate
    bayes_ess: float | None = None
    bayes_fail_reason: str | None = None
    log_evidence: float | None = None
    log_evidence_err: float | None = None
    bayes_freq_ratio: float | None = None
    hesse_freq_ratio: float | None = None
    bayes_prior_driven: bool = False
    bayes_fork_suspected: bool = False
    bayes_unconstrained: bool = False
    # GUI Phase 0 (P0-3 / P0-4), additive. ``amplitudes``: the DELIVERED fit's linear amplitudes
    # ({linear_param_name: value} for the winning structure — phonon area(s) ``…A``, elastic
    # ``elastic.E``, quasi-elastic ``qe.QE``, background ``bg.BG``), the same linear params the
    # fitter solved, kept so a params-only curve evaluator can rebuild the model without re-solving
    # NNLS against counts (report.audit.reconstruct_curve does the latter today). ``candidates``: one
    # compact row per SCORED structure ({label, k, ndof, cost, cost_per_dof, n_modes, delta_aic,
    # akaike_weight, usable}) — the fit inspector's standing answer to "what did ISAR choose and
    # why". Rows come straight from ``select.score_models`` (AIC = cost + 2k; Akaike weights), so the
    # winner (delta_aic == 0) is exactly ``best_label``; non-converged fits are absent. Both None on
    # older ledgers, when there is no delivered fit, or when nothing converged.
    amplitudes: dict | None = None
    candidates: list | None = None
    target_branch: str = "acoustic"
    target_label: str = "acoustic"
    effective_exposure: float | None = None
    exposure_provenance: dict | None = None
    branch_posterior: dict | None = None
    candidate_id: str | None = None
    candidate_state: str | None = None
    acquisition_id: str | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["interval"] = list(self.interval) if self.interval is not None else None
        d["omega0_profile"] = list(self.omega0_profile) if self.omega0_profile is not None else None
        d["hesse_interval"] = list(self.hesse_interval) if self.hesse_interval is not None else None
        d["bayes_interval"] = list(self.bayes_interval) if self.bayes_interval is not None else None
        return d


@dataclass
class RunSummary:
    outcomes: list = field(default_factory=list)     # (scan_number, FitOutcome)
    counts: dict = field(default_factory=dict)       # status/disposition tallies
    gaps: list = field(default_factory=list)
    context_lines: list = field(default_factory=list)
    n_fitted_this_run: int = 0     # scans that reached the fit/non-result stage this run
    n_reprocessed: int = 0         # scans (re)processed this run, any outcome (incl. excluded/held)
    collation: object = None


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
        prior_omega=(prior.omega0 if prior else None))
    mask = np.asarray(ctx.mask, dtype=bool)
    norm = record.normalization
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
    return ctx, n_used, region_reasons


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


def _exposure_per_point(rec, ctx) -> tuple[float, str]:
    """Median per-point counting exposure over the kept points, and the counting mode.

    The portable normaliser for the fitted amplitude (steering contract 2): monitor counts (or
    seconds) per point over the fit window (``ctx.mask``). NaN when unavailable → no IntensityMeasure.
    """
    norm = getattr(rec, "normalization", None)
    if norm is None:
        return float("nan"), ""
    mode = getattr(norm, "counting_mode", "") or ""
    arr = norm.monitor if mode == "monitor" else norm.time
    try:
        a = np.asarray(arr, dtype=float)
        m = np.asarray(ctx.mask, dtype=bool)
        if a.size == m.size and m.any():
            a = a[m]
        a = a[np.isfinite(a) & (a > 0)]
        return (float(np.median(a)) if a.size else float("nan")), mode
    except Exception:  # noqa: BLE001 — a malformed normalization just yields no intensity measure
        return float("nan"), mode


def _primary_amplitude(fit) -> float | None:
    """Fitted amplitude of the primary phonon (the mode nearest the reported ω₀)."""
    modes = fit.get("modes")
    w0 = fit.get("omega0")
    if not modes or w0 is None:
        return None
    prim = min(modes, key=lambda m: abs(float(m.get("omega0", 1e18)) - float(w0)))
    a = prim.get("amplitude")
    return float(a) if a is not None else None


def _intensity_measure(rec, ctx, fit) -> dict | None:
    """Portable :class:`IntensityMeasure` dict for the primary phonon, or None when not computable."""
    amp = _primary_amplitude(fit)
    if amp is None:
        return None
    exposure, mode = _exposure_per_point(rec, ctx)
    if not np.isfinite(exposure) or exposure <= 0:
        return None
    return IntensityMeasure(
        value=amp / exposure, exposure=exposure, counting_mode=(mode or "monitor"),
        gauss_fwhm=float(ctx.gauss_fwhm), lorentz_fwhm=float(ctx.lorentz_fwhm),
        instrument=str(ctx.instrument)).to_dict()


def _effective_exposure_for(rec, ctx, fit, *, absolute_exposure=None,
                            submitted_ncount: int | None = None) -> dict:
    """One exposure definition for single scans and pooled masters."""
    target = fit.get("omega0")
    if target is None or not np.isfinite(target):
        target = getattr(ctx, "acoustic_omega", None)
    if target is None or not np.isfinite(target):
        target = getattr(ctx, "prior_omega", None)
    if target is None or not np.isfinite(target):
        target = 0.5 * (float(ctx.window[0]) + float(ctx.window[1]))
    exposure = absolute_exposure
    if exposure is None and rec is not None:
        norm = getattr(rec, "normalization", None)
        if norm is not None:
            exposure = getattr(norm, "monitor", None)
            if exposure is None:
                exposure = getattr(norm, "time", None)
    return effective_exposure(
        ctx.x, exposure, ctx.mask, target_center=float(target),
        resolution_fwhm=float(ctx.gauss_fwhm) + float(ctx.lorentz_fwhm),
        submitted_ncount=submitted_ncount)


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
    is set only there). Kept alongside — not replacing — the ``errors_attach_seconds`` lump."""
    prov = {"bootstrap_seconds": fit.get("bootstrap_seconds"),
            "minos_seconds": fit.get("minos_seconds"),
            "profile_seconds": fit.get("profile_seconds"),
            "bayes_seconds": fit.get("bayes_seconds")}
    if fit.get("dynesty_version") is not None:
        prov["bayes_n_live"] = fit.get("bayes_n_live")
        prov["bayes_seed"] = fit.get("bayes_seed")
        prov["dynesty_version"] = fit.get("dynesty_version")
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


def _candidate_rows(scores, fits) -> list | None:
    """The model-selection candidate table (P0-4): one compact row per SCORED structure. ``scores``
    is ``select.score_models``'s output (the exact ranking the driver selects on — AIC = cost + 2k,
    Akaike weights ``w_i = exp(-Δ_i/2)/Σ exp(-Δ_j/2)``); this only reshapes it, never re-ranks, so
    the winner (``delta_aic == 0``) equals ``Selection.best`` / ``FitOutcome.best_label``.
    Non-converged fits are already excluded by ``score_models``, so every row is a usable candidate.
    ``fits`` (label → ``StructureFitResult``) supplies ``cost_per_dof``/``n_modes``/``analysis_usable``
    that ``ModelScore`` does not carry. All values are coerced to plain Python scalars so the row
    JSON-serialises cleanly. ``None`` when nothing converged (empty ``scores``)."""
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
            "delta_aic": float(s.delta_aic),
            "akaike_weight": float(s.weight),
            "usable": bool(r.analysis_usable) if r is not None else True,
        })
    return rows


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None,
            errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
            descriptor=None, structure_hint=None, bayes_opts=None):
    """Two-stage structure fit (§ multiplicity), optionally narrowed by guideline labels, then
    select + verdict. Mirrors docs/phase3_gold*_check.run_pipeline; returns extracted fields.

    ``peaks`` (guideline hints, ≥2 forces the 2-mode offer and seeds the greedy IC) are threaded
    into :func:`loop.fit_structures_two_stage`. The whitelist narrows the union of 1- and 2-mode
    labels (``stokes2+bg`` etc. pass through) — an empty match keeps all + warns.

    ``structure_hint`` (a previous best_label, master-refit only; card policy
    ``[fitting].reuse_master_structure``) offers the structure-skip shortcut: fit only that
    structure + the no-phonon null instead of the full enumeration when the gates permit
    (:func:`loop.fit_structures_two_stage_hinted`). ``None`` (every other path) is the unchanged
    full enumeration. The returned ``structure_skipped`` reports whether the shortcut was taken.

    ``errors_tier`` selects the uncertainty depth. ``"full"`` (default, unchanged) runs MINOS +
    parametric bootstrap + ω₀ profile — the authoritative interval the stop rule consumes.
    ``"fast"`` runs the MIGRAD+HESSE fit only and reports the delta-method ω₀ interval
    (``ω₀ ± 1σ_HESSE``) plus a ``hesse_trusted`` flag: in the asymptotic regime (Γ off-bound,
    positive-definite HESSE σ, enough counts) that interval ≈ the bootstrap half-width, so the
    driver can converge on it without paying for a bootstrap; untrusted → escalate to ``full``.
    ``"cheap"`` is the quick-look tier: HESSE only, no interval at all (verdict + rough ω₀ to steer
    acquisition, ~an order of magnitude faster). ``boot_workers`` sets the parametric-bootstrap
    process-pool size (1 = serial; see :func:`run_batch`)."""
    run_full = errors_tier in FULL_TIERS  # MINOS + bootstrap + ω₀ profile (authoritative interval)
    run_fast = errors_tier == "fast"     # HESSE ω₀ interval + trust flag, no bootstrap (fast loop)
    run_bayes = errors_tier == "bayes"   # full + nested-sampling posterior on the prelim-best label
    _t_two_stage = time.perf_counter()
    results, structure_skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=run_full, peaks=peaks, structure_hint=structure_hint)
    two_stage_seconds = round(time.perf_counter() - _t_two_stage, 4)
    # by_hyp spans both stages so errors.attach can find any surviving label's hypothesis.
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}
    by_hyp.update({h.label: h for h in enumerate_multimode(ctx)})

    warn = None
    if structure_labels:
        kept = [r for r in results if r.label in structure_labels]
        if kept:
            results = kept
        else:
            warn = (f"guideline structures {list(structure_labels)} match no admissible "
                    f"{[r.label for r in results]}; kept all")

    fits = {r.label: r for r in results}

    prelim = select.select_models(results, stamp=False)
    errors_attach_seconds = 0.0
    if run_full:
        _t_errors = time.perf_counter()
        for label in prelim.survivors:
            r = fits.get(label)
            if r is not None and r.optimizer_converged and label in by_hyp:
                errors.attach(r, by_hyp[label], ctx, M=M, seed=seed, with_profile=True,
                              with_bayes=(run_bayes and label == prelim.best),
                              prior=_bayes_prior_for(prior, card),
                              boot_workers=boot_workers, boot_executor=boot_executor,
                              boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        errors_attach_seconds = round(time.perf_counter() - _t_errors, 4)
    sel = select.select_models(results)
    sv = verdict.classify(results, ctx, sel, prior=prior, descriptor=descriptor)

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
    if errors_tier == "cheap":
        interval = None
        omega0_profile = None
        interval_reliable = False
        reasons.append("cheap-errors quick-look: verdict + rough HESSE ω₀ only "
                       "(no bootstrap/profile interval)")

    # Bayes-errors tier: the posterior was attached only to the prelim-best label (to bound cost).
    # Diagnostics-only — the bootstrap `interval` above stays authoritative and nothing here touches
    # it. Extract the credible interval, evidence, and the §5.5 three-method agreement from the final
    # best's within_model. If the final selection moved off the prelim-best label the posterior was
    # sampled on the wrong (or an absent) model → report it as not-sampled rather than mislabel it.
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
        if sel.best != prelim.best:
            bayes_ok = False
            bayes_fail_reason = "not sampled (best changed after error attach)"
        elif wm is not None and "bayes_ok" in wm:
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
    for label in prelim.survivors:
        r = fits.get(label)
        wm_r = getattr(r, "within_model", None) if r is not None else None
        msec = (wm_r or {}).get("method_seconds") or {}
        bootstrap_seconds += float(msec.get("bootstrap", 0.0))
        minos_seconds += float(msec.get("minos", 0.0))
        profile_seconds += float(msec.get("profile", 0.0))
        bayes_seconds += float(msec.get("bayes", 0.0))
    bootstrap_seconds = round(bootstrap_seconds, 4)
    minos_seconds = round(minos_seconds, 4)
    profile_seconds = round(profile_seconds, 4)
    bayes_seconds = round(bayes_seconds, 4)

    n_modes = int(best.n_modes) if best is not None else 1
    modes = list(best.modes) if (best is not None and best.modes) else None
    # GUI Phase 0 (additive, no fitting/selection logic touched): the delivered fit's linear
    # amplitudes and the model-selection candidate table. Built from the FINAL selection (``sel``)
    # and the delivered ``best`` — so an escalated re-fit (its own fit_one call) carries its own.
    amplitudes = _amplitudes_of(best, by_hyp)
    candidates = _candidate_rows(sel.scores, fits)

    # Phase 4a — under-model falsification: the descriptor localised real signal the phonon model
    # can't fit. This is NOT an ω₀ measurement (machine contract): null the number/intervals and
    # tag descriptor_only, so the stop rule / gold / truth never count it as a delivered ω₀. The
    # descriptor peaks (stored separately) are the deliverable; the verdict routes to "unmodeled".
    omega0_err = best.omega0_err_hesse if best else None
    fit_kind = best.fit_kind if best else "unconstrained"
    if sv.verdict == "signal_unmodeled":
        omega0 = omega0_err = interval = omega0_profile = hesse_interval = None
        interval_reliable = False
        fit_kind = "descriptor_only"
        # Not an ω₀ measurement → null the bayes interval/diagnostics too, same as the other intervals.
        bayes_interval = bayes_ess = None
        log_evidence = log_evidence_err = None
        bayes_freq_ratio = hesse_freq_ratio = None

    return {
        "verdict": sv.verdict, "confidence": sv.confidence, "reasons": reasons,
        "omega0": omega0, "omega0_err": omega0_err,
        "interval": (tuple(interval) if interval is not None else None),
        "omega0_profile": (tuple(omega0_profile) if omega0_profile is not None else None),
        "interval_reliable": interval_reliable,
        "hesse_interval": (tuple(hesse_interval) if hesse_interval is not None else None),
        "hesse_trusted": hesse_trusted,
        "modality": sel.modality, "best_label": sel.best,
        "gamma": (best.gamma if best else None),
        "cost_per_dof": (best.cost_per_dof if best else None),
        "n_modes": n_modes, "modes": modes,
        "amplitudes": amplitudes, "candidates": candidates,
        "fit_kind": fit_kind,
        "gamma_at_bound": bool(getattr(best, "gamma_at_bound", False)) if best else False,
        "two_stage_seconds": two_stage_seconds,
        "errors_attach_seconds": errors_attach_seconds,
        "structure_skipped": structure_skipped,
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
        # Provenance-only (not FitOutcome fields): the sampler config for the run record.
        "bayes_n_live": bayes_n_live,
        "bayes_seed": bayes_seed,
        "dynesty_version": dynesty_version,
    }


# ===========================================================================
# the batch loop
# ===========================================================================
def _sample_of(record, card: Card) -> str:
    return (card.policy.sample or {}).get("name") or record.experiment_id or ""


def _exclusion_ids(card: Card) -> set:
    ids = set(card.policy.exclusions_seed or [])
    for e in (card.state.exclusions or []):
        if isinstance(e, dict) and "scan" in e:
            ids.add(e["scan"])
    return ids


# ===========================================================================
# worker-budget resolution (performance §; owner approved up to 30 workers)
# ===========================================================================
def _resolve_workers(cli_workers=None) -> int:
    """Scan-level worker count: explicit ``--workers`` (>0) wins, then ``ISAR_WORKERS``, then a
    default of ``min(30, cpu-2)`` (leave two threads for the OS / this parent process)."""
    for src in (cli_workers, os.environ.get("ISAR_WORKERS")):
        if src is None or src == "":
            continue
        try:
            n = int(src)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    cpu = os.cpu_count() or 1
    return max(1, min(30, cpu - 2))


def _resolve_boot_workers(default_n) -> int:
    """Parametric-bootstrap worker count. ``ISAR_BOOTSTRAP_WORKERS`` (>0) overrides; otherwise the
    caller's ``default_n`` (1 inside a scan-pool worker to avoid nested oversubscription; the full
    budget when scans run serially — e.g. the drive loop's one-scan-per-``isar run``)."""
    env = os.environ.get("ISAR_BOOTSTRAP_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return max(1, int(default_n))


# Bayes-tier (--errors bayes) nested-sampling knobs. Production defaults are deliberately modest to
# bound the per-scan cost of the posterior; overridable per-run via CLI (--bayes-nlive etc.) or env.
# n_live=250 caveat (2026-07-11 bench, docs/stats/bench_error_methods.py): on very-high-count scans
# 250 gave narrower credible intervals than the step-7-validated 400 despite passing the ESS gate
# (mvp_zone_test16 scans 469/470); elsewhere 250≈400 at ~1.6x less cost. Acceptable while the
# posterior is diagnostics-only — a coverage campaign (score --truth --interval bayes) must gate any
# promotion of this interval beyond diagnostics.
_BAYES_DEFAULTS = {"n_live": 250, "dlogz": 0.5, "max_iter": 30_000}
_BAYES_ENV = {"n_live": "ISAR_BAYES_NLIVE", "dlogz": "ISAR_BAYES_DLOGZ", "max_iter": "ISAR_BAYES_MAXITER"}
_BAYES_CASTS = {"n_live": int, "dlogz": float, "max_iter": int}


def _resolve_bayes_opts(cli_opts=None) -> dict:
    """Resolve the bayes-tier sampler options with precedence CLI > env > defaults. Returns a plain
    picklable dict ``{n_live, dlogz, max_iter}`` threaded through the fit task tuples to
    ``errors.attach(..., bayes_opts=…)``. Mirrors :func:`_resolve_boot_workers`'s env/override style."""
    cli_opts = cli_opts or {}
    out = dict(_BAYES_DEFAULTS)
    for key, env_name in _BAYES_ENV.items():
        env = os.environ.get(env_name)
        if env not in (None, ""):
            try:
                out[key] = _BAYES_CASTS[key](env)
            except (TypeError, ValueError):
                pass
    for key, cast in _BAYES_CASTS.items():
        v = cli_opts.get(key)
        if v is not None:
            out[key] = cast(v)
    return out


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


def _run_tasks(tasks, card, g, runcal, exclusions, *, M, errors_tier, boot_workers,
               use_pool, scan_workers, boot_executor=None, boot_adaptive=False,
               bayes_opts=None) -> list:
    """Fit the fresh scan-detectors, serially or over a process pool, preserving submission order.

    ``boot_executor`` (a shared bootstrap pool) is threaded only through the SERIAL path — an
    executor is not picklable and must never enter a scan-pool worker (nested pools). The pool path
    only runs when ``boot_workers == 1`` (Layer A owns the workers), so it never wants a shared pool.
    """
    arg_list = [(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
                 M, errors_tier, seed, boot_workers, boot_adaptive, bayes_opts)
                for (rec, det, q, T, sample, scan_id, chash, ghash, seed) in tasks]
    if not arg_list:
        return []
    if not use_pool:
        out = []
        for a in arg_list:
            (rec, det, q, T, sample, scan_id, card_, g_, runcal_, exclusions_, chash, ghash,
             M_, tier_, seed, bw, adaptive_, bopts_) = a
            out.append(_process_one(rec, det, q, T, sample, scan_id, card_, g_, runcal_,
                                    exclusions_, chash, ghash, M=M_, seed=seed, errors_tier=tier_,
                                    boot_workers=bw, boot_executor=boot_executor,
                                    boot_adaptive=adaptive_, bayes_opts=bopts_))
        return out
    max_workers = min(scan_workers, len(arg_list))
    # map preserves order → results align with `tasks`, keeping prior-seeding + row order deterministic.
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_process_one_task, arg_list))


def run_batch(workspace: Workspace, data_paths, *, force=False, llm=False, M=200,
              detectors=None, formats=("html", "markdown"), workers=None,
              errors_tier="full", bayes_cli=None, timer=None) -> RunSummary:
    timer = timer or NullTimer()
    with timer.stage("workspace_load"):
        card, session = workspace.load()
    for p in map(str, data_paths):
        if p not in session.data_paths:
            session.data_paths.append(p)

    with timer.stage("ingest_parse") as _m_ingest:
        plan = ingest.discover(data_paths, card)
        records = ingest.parse_all(plan, card)
        _m_ingest["n_records"] = len(records)
    # Basename -> resolved absolute source path, so run's provenance source_file matches ingest's.
    source_by_name = {Path(f).name: str(Path(f).resolve()) for f in plan.measurement_files}

    # Reduce (both no-ops where not applicable; QC never raises).
    with timer.stage("reduce_qc"):
        for rec in records:
            combine.reduce_record(rec, card)
            qc.qc_record(rec, card)

    # File-manifest bookkeeping.
    for f in plan.measurement_files + plan.companion_files:
        rp = str(Path(f).resolve())
        try:
            stat = Path(f).stat()
            # Merge, not replace: ingest may have written richer rows (n_scans/scan_ids) for these
            # keys — preserve them so an `isar ingest` before an `isar run` survives the run.
            session.files.setdefault(rp, {}).update(
                {"size": stat.st_size, "mtime": stat.st_mtime,
                 "format": "companion" if f in plan.companion_files else "measurement"})
        except OSError as exc:
            log.warning("run: could not stat %s (%s); manifest row skipped", f, exc)

    # Context updates BEFORE measurements (calibration/alignment/environment).
    with timer.stage("route_context"):
        routed = router.route(records)
        ctx_lines = router.apply_context_updates(
            routed, card, session, companion_files=plan.companion_files)

        # Optional LLM adjudication (off critical path; degrade, never stall).
        if llm or (card.policy.adjudication or {}).get("enabled"):
            ctx_lines += _maybe_adjudicate(routed.measurements, card)

    # Per-unit data files for the GUI inspector, exactly as `isar ingest` writes them — so a run-only
    # workspace (never ingested) still has a counts plot per unit. Reduced+QC'd records; failures are
    # logged and skipped, never fatal. (unit_data.write_unit_data mirrors the ingest contract.)
    _unit_data_dir = getattr(workspace, "root", None)
    if _unit_data_dir is not None:
        _unit_data_dir = Path(_unit_data_dir) / "data"
        for rec in routed.measurements:
            for det_id in rec.spectra:
                try:
                    unit_data.write_unit_data(rec, det_id, _unit_data_dir)
                except Exception as exc:  # noqa: BLE001 — one bad unit must never fail the run
                    log.warning("run: failed to write unit data for %s:%s:%s (%s); continuing",
                                rec.experiment_id, rec.scan_number, det_id, exc)

    runcal = calibrations.calibration_view(card)
    g = guide_mod.resolve(card)
    fit_dets = guide_mod.fit_detectors(g, card, cli_detectors=_split_csv(detectors))
    exclusions = _exclusion_ids(card)
    seed_base = 6000

    summary = RunSummary(gaps=ingest.file_gaps(records), context_lines=ctx_lines)

    # --- Pass 1: split fresh scan-detectors (to fit) from carried ones, preserving report order. ---
    from isar.session.state import canonical_hash
    tasks = []          # (rec, det, q, T, sample, scan_id, chash, ghash, seed)
    plan = []           # ordered: ("carry", scan, outcome) | ("task", scan, det, task_idx)
    for rec in routed.measurements:
        scan = rec.scan_number
        chash = content_hash(rec)
        for det in fit_dets:
            if det not in rec.spectra:
                continue
            sp = rec.spectra[det]
            q = [float(v) for v in (sp.q if sp.q is not None else [0.0, 0.0, 0.0])]
            T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
            sample = _sample_of(rec, card)
            scan_id = f"{rec.experiment_id}:{scan}:{det}"

            gsig = guide_mod.guidelines_signature(g, scan_number=scan, q=q, T=T)
            ghash = canonical_hash(gsig)

            prev = session.get(scan_id)
            fresh = (prev is None or prev.content_hash != chash
                     or prev.guidelines_hash != ghash or prev.status.startswith("held")
                     or prev.status == "ingested" or force)
            if not fresh:
                # Unchanged & already resolved: keep the ledger entry, re-list its outcome for the report.
                if prev.detectors.get(det):
                    plan.append(("carry", scan, _outcome_from_entry(prev, det)))
                continue
            task_idx = len(tasks)
            tasks.append((rec, det, q, T, sample, scan_id, chash, ghash, seed_base + scan))
            plan.append(("task", scan, det, task_idx))

    # --- Fit the fresh scans (parallel across scans when it pays; §performance). ---
    # Only the `full` tier bootstraps. When a full batch is small enough that the Layer-A scan pool
    # would idle cores (each per-scan bootstrap forced serial), bypass Layer A: fit serially in the
    # parent and funnel every bootstrap (scans + masters) through ONE shared pool at full budget
    # (the workorder — one spawn, not N). Large full reprocess batches (tasks > workers, where Layer
    # A already saturates) and the fast/cheap tiers (no bootstrap) keep the Layer-A parallel-fit path.
    scan_workers = _resolve_workers(workers)
    is_full = errors_tier in FULL_TIERS
    bayes_opts = _resolve_bayes_opts(bayes_cli)
    use_workorder = is_full and len(tasks) <= scan_workers
    if use_workorder:
        use_pool = False
        boot_workers = _resolve_boot_workers(scan_workers)
    else:
        use_pool = scan_workers > 1 and len(tasks) > 1
        boot_workers = 1 if use_pool else _resolve_boot_workers(scan_workers)
    boot_adaptive = is_full
    master_boot_workers = _resolve_boot_workers(scan_workers)

    shared_pool = None
    if is_full and boot_workers > 1 and not use_pool:
        shared_pool = ProcessPoolExecutor(max_workers=boot_workers)   # scans + masters, one spawn
    try:
        with timer.stage("fit_scans", n_tasks=len(tasks), use_pool=use_pool,
                         boot_workers=boot_workers) as _m_fit:
            entries = _run_tasks(tasks, card, g, runcal, exclusions, M=M, errors_tier=errors_tier,
                                 boot_workers=boot_workers, use_pool=use_pool,
                                 scan_workers=scan_workers, boot_executor=shared_pool,
                                 boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
            _m_fit["per_task"] = [
                {"scan_id": e.scan_id,
                 "seconds": e.provenance.get("fit_seconds"),
                 "two_stage_seconds": e.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": e.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": e.provenance.get("bootstrap_seconds"),
                 "minos_seconds": e.provenance.get("minos_seconds"),
                 "profile_seconds": e.provenance.get("profile_seconds"),
                 "bayes_seconds": e.provenance.get("bayes_seconds")}
                for e in entries if e.provenance.get("fit_seconds") is not None]
        _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                          workspace, force=force, M=M, seed_base=seed_base, errors_tier=errors_tier,
                          master_boot_workers=master_boot_workers, boot_executor=shared_pool,
                          boot_adaptive=boot_adaptive, scan_workers=scan_workers,
                          bayes_opts=bayes_opts, timer=timer, source_by_name=source_by_name)
    finally:
        if shared_pool is not None:
            shared_pool.shutdown()
    return summary


def _store_branch_models(state, t_round: float) -> None:
    """Build the cross-q branch(es) from the (now complete) dispersion prior and persist them into
    ``state.branch_models`` — one per distinct ``(sample, t_bin)`` — so the DRIVE (a separate,
    crash-prone process that cannot import :mod:`isar.session`) reads the prediction durably from
    ``card.state.json``. Replace-by-key so a re-run refreshes rather than accretes; best-effort (a
    build failure never fails the batch). ``build_branch`` returns ``None`` below ``MIN_ANCHORS``."""
    points = state.dispersion_prior or []
    keys: list[tuple] = []
    for p in points:
        sample = p.get("sample")
        tb = p.get("t_bin")
        if tb is None and p.get("T") is not None:
            tb = round(float(p["T"]) / t_round) * t_round if t_round else float(p["T"])
        if sample is None or tb is None:
            continue
        key = (sample, float(tb))
        if key not in keys:
            keys.append(key)
    for sample, tb in keys:
        try:
            bm = branch_model.build_branch(points, sample=sample, t_bin=tb)
        except Exception:   # noqa: BLE001
            bm = None
        if bm is None:
            continue
        bid = bm.get("branch_id", branch_model.BRANCH_ID)
        state.branch_models = [
            m for m in (state.branch_models or [])
            if not (m.get("sample") == sample
                    and abs(float(m.get("t_bin", -1e18)) - float(tb)) < 1e-9
                    and m.get("branch_id") == bid)]
        state.branch_models.append(bm)


def _store_branch_beliefs(state, t_round: float, *, period: float | None,
                          hypotheses: list[dict] | None = None) -> None:
    """Persist the data/prior ingredients of every branch belief for the Qt-free drive/viewers."""
    if period is None:
        state.branch_belief = []
        state.branch_belief_models = []
        return
    points = state.dispersion_prior or []
    keys: list[tuple[str, float]] = []
    for point in points:
        sample = point.get("sample")
        tb = point.get("t_bin", point.get("T"))
        if sample is None or tb is None:
            continue
        tb = round(float(tb) / t_round) * t_round if t_round else float(tb)
        key = (str(sample), float(tb))
        if key not in keys:
            keys.append(key)
    if not keys and hypotheses:
        # A prior-only snapshot is useful but has no sample/T identity until the first scan lands.
        state.branch_belief = []
        state.branch_belief_models = []
        return
    rows: list[dict] = []
    for sample, tb in keys:
        beliefs = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                                   t_round=t_round, hypotheses=hypotheses)
        for branch_id, belief in sorted(beliefs.items()):
            anchors = [{"u": float(u), "omega0": float(y), "weight": float(w)}
                       for u, y, w in zip(belief.us, belief.ys, belief.ws)]
            rows.append({"sample": sample, "t_bin": tb, "branch_id": branch_id,
                         "period": float(period), "anchors": anchors,
                         "n_anchors": belief.n_anchors,
                         "parameter_prior": dict(belief.parameter_prior or {}),
                         "label_probabilities": {}})
    state.branch_belief_models = rows
    target = (getattr(state, "hypothesis_snapshot", None) or {}).get("target_hypothesis")
    point_rows: list[dict] = []
    for point in sorted(points, key=lambda item: (
            str(item.get("sample", "")), float(item.get("t_bin", item.get("T", 0.0))),
            tuple(float(value) for value in (item.get("q") or ())),
            str(item.get("scan_id", "")))):
        branch_id = str(point.get("branch_id") or BA.ACOUSTIC)
        probability = min(max(float(point.get("weight", 1.0)), 0.0), 1.0)
        probabilities = {branch_id: probability, BA.NEW: 1.0 - probability}
        entropy = -sum(value * math.log(value) for value in probabilities.values() if value > 0.0)
        point_rows.append({
            "sample": point.get("sample"), "t_bin": point.get("t_bin", point.get("T")),
            "q": list(point.get("q") or []), "omega0": point.get("omega0"),
            "sigma": BA.SIGMA_SLACK / math.sqrt(max(probability, 1e-9)),
            "branch_id": branch_id, "label_probabilities": probabilities,
            "assignment_entropy": entropy,
            "p_is_target": probability if branch_id == target else 0.0,
            "resolvable": None, "scan_id": point.get("scan_id")})
    state.branch_belief = point_rows


def _update_hypothesis_mismatch(state, t_round: float, *, period: float | None,
                                hypotheses: list[dict] | None = None) -> None:
    """Persist O11's prior-vs-data-only posterior-predictive mismatch assessment."""
    snapshot = getattr(state, "hypothesis_snapshot", None) or {}
    target = snapshot.get("target_hypothesis")
    policy = snapshot.get("steering") or {}
    threshold = float(policy.get("mismatch_z_threshold", 3.0))
    hypothesis = next((h for h in (hypotheses or []) if h.get("name") == target), None)
    if period is None or hypothesis is None:
        state.hypothesis_mismatch = {}
        return
    target_points = [p for p in (state.dispersion_prior or []) if p.get("branch_id") == target]
    groups: dict[tuple[str, float], list[dict]] = {}
    for point in target_points:
        sample_value = point.get("sample")
        tb_value = point.get("t_bin", point.get("T"))
        if sample_value is None or tb_value is None:
            continue
        tb = round(float(tb_value) / t_round) * t_round if t_round else float(tb_value)
        groups.setdefault((str(sample_value), float(tb)), []).append(point)
    eligible = {key: rows for key, rows in groups.items() if len(rows) >= 2}
    if not eligible:
        state.hypothesis_mismatch = {
            "active": False, "evaluated": False, "target_hypothesis": target,
            "comparison": "prior_vs_data_only_posterior_predictive",
            "reason": "fewer_than_two_real_anchors", "threshold_z": threshold,
            "fingerprint": snapshot.get("fingerprint")}
        return
    sample, tb = max(sorted(eligible), key=lambda key: (len(eligible[key]), key))
    points = sorted(eligible[(sample, tb)], key=lambda point: (
        tuple(float(value) for value in (point.get("q") or ())),
        str(point.get("scan_id", ""))))
    belief = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                              t_round=t_round, hypotheses=hypotheses).get(target)
    prior_only = BA.BranchBelief(target, np.array([]), np.array([]), np.array([]),
                                 parameter_prior=hypothesis)
    comparisons = []
    for u in sorted(set(float(value) for value in belief.us)):
        prior_mu, prior_sigma = prior_only.predict(u)
        data_mu, data_sigma = belief.predict_data_only(u)
        denom = math.sqrt(prior_sigma ** 2 + data_sigma ** 2)
        z = abs(prior_mu - data_mu) / max(denom, 1e-12)
        comparisons.append({"u": u, "prior_mean": prior_mu, "prior_sigma": prior_sigma,
                            "data_mean": data_mu, "data_sigma": data_sigma, "z": z})
    worst = max(comparisons, key=lambda row: row["z"])
    active = bool(worst["z"] >= threshold)
    state.hypothesis_mismatch = {
        "active": active, "evaluated": True, "target_hypothesis": target,
        "comparison": "prior_vs_data_only_posterior_predictive",
        "max_z": worst["z"], "threshold_z": threshold,
        "evaluated_points": len(comparisons), "worst": worst,
        "sample": sample, "t_bin": tb,
        "point_ids": [str(point.get("scan_id", "")) for point in points],
        "fingerprint": snapshot.get("fingerprint"),
        "message": (f"declared {target} hypothesis disagrees with data-only branch prediction"
                    if active else "declared hypothesis remains compatible with data-only prediction")}


def _core_provenance(rec, det, source_by_name=None) -> dict:
    """The ingest-parity core provenance keys for one (record, detector), reused from the Phase-2
    helper so `isar run` on a workspace that was never `isar ingest`-ed still carries per-unit q, T,
    the target #Q, command, calibration state, etc. — the same keys the GUI's per-unit q display and
    scan-group #Q labels read. Absent an ingest, this is the ONLY place those keys get stamped.

    ``source_by_name`` maps a record's source basename → its resolved absolute path (so run's
    source_file matches ingest's absolute form); missing → the parser's recorded name."""
    sp = rec.spectra.get(det)
    q = [float(v) for v in sp.q] if sp is not None and sp.q is not None else None
    T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
    core = {"T": T, "q": q, **unit_data._provenance_extras(rec)}
    fname = getattr(getattr(rec, "source", None), "file", None)
    src = None
    if source_by_name and fname:
        src = source_by_name.get(fname) or source_by_name.get(Path(fname).name)
    src = src or fname
    if src is not None:
        core["source_file"] = src
    return core


def _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                      workspace, *, force, M, seed_base, errors_tier, master_boot_workers,
                      boot_executor, boot_adaptive, scan_workers=1, bayes_opts=None, timer=None,
                      source_by_name=None):
    """Pass 2 (ledger + prior) → collate → master fits → tally → write. Split out so the shared
    bootstrap pool from :func:`run_batch` stays open across the whole full-tier batch."""
    timer = timer or NullTimer()
    # --- Pass 2: apply ledger + prior mutations serially, in submission order (determinism). ---
    # Prior seeding is START-OF-BATCH: a scan is fit against the dispersion prior as of the batch
    # start, never a same-batch neighbour's fresh point (this is what makes serial == parallel).
    # The re-fit idempotency makes it self-healing across runs — a later `isar run` re-seeds from
    # the now-complete prior only for scans that actually drifted.
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    # scan_id -> (record, detector) for stamping ingest-parity core provenance onto fitted entries.
    rec_by_scan_id: dict[str, tuple] = {}
    for rec in records:
        for det_id in rec.spectra:
            rec_by_scan_id[f"{rec.experiment_id}:{rec.scan_number}:{det_id}"] = (rec, det_id)
    for item in plan:
        if item[0] == "carry":
            _, scan, outcome = item
            summary.outcomes.append((scan, outcome))
            continue
        _, scan, det, task_idx = item
        entry = entries[task_idx]
        # Stamp per-unit core provenance (q/T/source_file/q_target/…) the fitter never sets, and
        # MERGE under any existing (e.g. ingest-stamped) provenance so an ingest-then-run workspace
        # keeps ingest-only keys (ingested_at) while run values win where both are set. Absent an
        # ingest, this is what gives the GUI its per-unit q and scan-group #Q labels in the fit path.
        rd = rec_by_scan_id.get(entry.scan_id)
        if rd is not None:
            core = _core_provenance(rd[0], rd[1], source_by_name)
            entry.provenance = {**core, **(entry.provenance or {})}
        prev = session.get(entry.scan_id)
        if prev is not None and prev.provenance:
            entry.provenance = {**prev.provenance, **(entry.provenance or {})}
        session.put(entry)
        summary.n_reprocessed += 1
        summary.n_fitted_this_run += 1 if entry.status in ("fitted", "non_result") else 0
        oc = entry.detectors.get(det)
        if oc:
            g_err = None
            _modes, _w0 = oc.get("modes"), oc.get("omega0")
            if _modes and _w0 is not None:
                _pm = min(_modes, key=lambda m: abs(float(m.get("omega0", 1e18)) - float(_w0)))
                g_err = _pm.get("gamma_err")
            # A Phase-A triaged acoustic delivery carries its own hygiene (assignment posterior's
            # P/dominance/entropy gates), so it may seed from an unpooled quicklook — without this
            # the acoustic prior only fills from late fold productions while optic banking (below)
            # fills immediately, and the occupancy prior lets the optic outvote a still-anchorless
            # acoustic for its own features (the v15 mid-band deadlock). Forks stay excluded.
            session_prior.add_point(card.state, scan_id=entry.scan_id, sample=oc.get("sample"),
                                    T=oc.get("T"), q=oc.get("q"), omega0=oc.get("omega0"),
                                    modality=oc.get("modality"), verdict=oc.get("verdict"),
                                    gamma=oc.get("gamma"), gamma_err=g_err,
                                    intensity=oc.get("intensity"),
                                    fit_kind=oc.get("fit_kind"), branch_id=oc.get("branch_id"),
                                    weight=oc.get("branch_weight"), t_round=t_round,
                                    require_fold=True)
            # Rival-branch anchors from this DELIVERED scan's other features (optic banking):
            # stored under suffixed scan_ids; purge first so a refit with fewer secondaries
            # leaves no stale points behind.
            card.state.dispersion_prior = [p for p in card.state.dispersion_prior
                                           if not str(p.get("scan_id", "")).startswith(f"{entry.scan_id}#")]
            for i, sf in enumerate(oc.get("secondary_features") or []):
                session_prior.add_point(card.state, scan_id=f"{entry.scan_id}#{sf['branch_id']}{i}",
                                        sample=oc.get("sample"), T=oc.get("T"), q=oc.get("q"),
                                        omega0=sf.get("omega0"), modality=oc.get("modality"),
                                        # A banked rival anchor is an unconstrained DETECTION of the
                                        # other branch (secondary_branch_points contract) — it must
                                        # not inherit the primary fit's constrained fit_kind.
                                        verdict=oc.get("verdict"), fit_kind="unconstrained",
                                        branch_id=sf.get("branch_id"), weight=sf.get("weight"),
                                        t_round=t_round, require_fold=False)
            summary.outcomes.append((scan, FitOutcome(**oc) if not isinstance(oc, FitOutcome) else oc))

    # Persist the cross-q branch(es) from the now-complete prior into card.state so the DRIVE — a
    # separate, crash-prone process that cannot import isar.session — reads the prediction durably from
    # card.state.json (steering Phase 5b-live).
    _store_branch_models(card.state, t_round)
    _store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    _update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))

    # Collate (all records, whole-experiment view).
    with timer.stage("collate"):
        try:
            summary.collation = collate_mod.collate(records, card)
        except Exception as exc:  # noqa: BLE001 — collation is a report aid, never fail the batch on it
            summary.context_lines.append(f"collation skipped: {exc}")

    # Fit the merged "master scan" per (Q,T) group (combine-before-divide pooling), when the card
    # opts in with collation.merge == "merge". These pooled fits are what let a campaign accumulate
    # statistics by repeating a measurement; s30 (merge="separate") produces none. Never fatal.
    with timer.stage("fit_masters") as _m_masters:
        try:
            # Masters run after the scan phase; their bootstraps take the full worker budget and
            # reuse the batch's shared pool when one is open (else _fit_masters opens its own).
            # Masters fit serially w.r.t. each other; only their refits hit the pool.
            per_master: list = []
            n_masters = _fit_masters(summary.collation, records, card, runcal, g, session,
                                     M=M, seed_base=seed_base, errors_tier=errors_tier, force=force,
                                     boot_workers=master_boot_workers, boot_executor=boot_executor,
                                     boot_adaptive=boot_adaptive, scan_workers=scan_workers,
                                     bayes_opts=bayes_opts, per_master=per_master)
            _m_masters["n_masters"] = n_masters
            _m_masters["per_master"] = per_master
            if n_masters:
                summary.context_lines.append(f"fit {n_masters} merged master scan(s)")
        except Exception as exc:  # noqa: BLE001 — master fitting is additive; a failure never kills the batch
            summary.context_lines.append(f"master fitting skipped: {exc}")

    summary.counts = _tally(session)
    session.last_run = {"counts": summary.counts,
                        "scan_gaps": [f"{e}: {a}->{b}" for (e, a, b) in summary.gaps],
                        "n_fitted_this_run": summary.n_fitted_this_run}

    with timer.stage("write_results"):
        _write_results_csv(workspace, session)
    with timer.stage("session_save"):
        workspace.save(card, session)


# Synthetic scan_number range for master (pooled) ledger entries so they sort AFTER real scans and
# never collide with a real scan_number. The real identity is the scan_id + member-set content_hash.
_MASTER_SCAN_BASE = 9_000_000


def _member_labels(members) -> list[str]:
    """Chain-of-custody labels for a pooled master's members (MergedSpectrum.members tuples
    ``(experiment_id, scan_number, detector_id)``). A bare native scan number as a string, EXCEPT a
    scan number that repeats within this member list (multi-detector merge), which is qualified
    ``experiment:scan:detector`` to stay unambiguous. Preserves ``ms.members`` order."""
    counts = Counter(m[1] for m in members)
    return [str(m[1]) if counts[m[1]] == 1 else f"{m[0]}:{m[1]}:{m[2]}" for m in members]


def _fit_master_group_task(args):
    """Top-level (picklable) entry point for a master-group pool worker: fit one merged group.

    Bootstrap is forced serial inside the child (``boot_workers=1``, no shared pool) — Layer A owns
    the process workers, the same trade-off as the scan pool in :func:`_run_tasks`, and the seeded
    bootstrap is invariant to worker count / where the refits run (errors.bootstrap). The returned
    :class:`ScanEntry` carries its own timings in ``provenance``; the parent applies ``session.put``
    serially afterward in sorted-group order. Every argument is picklable (ms/grp are plain collation
    dataclasses, member_rec a ScanRecord, card/g/runcal as the scan pool already ships)."""
    (ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number, chash,
     M, seed, errors_tier, boot_adaptive, structure_hint, bayes_opts) = args
    # Pass structure_hint only when set: with the reuse policy off (default) hint is None and this
    # call is byte-identical to the pre-B2 call (keeps the "change nothing when off" guarantee).
    hint_kw = {"structure_hint": structure_hint} if structure_hint is not None else {}
    return _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                             scan_number, chash, M=M, seed=seed, errors_tier=errors_tier,
                             boot_workers=1, boot_executor=None, boot_adaptive=boot_adaptive,
                             bayes_opts=bayes_opts, **hint_kw)


def _fit_masters(collation, records, card, runcal, g, session, *, M, seed_base,
                 errors_tier="full", force=False, boot_workers=1, boot_executor=None,
                 boot_adaptive=False, scan_workers=1, bayes_opts=None, per_master=None) -> int:
    """Fit the merged master scan for each (Q,T) group; returns how many were (re)fit this run.

    Runs only when the card opts in (``collation.merge == "merge"``). Each group's pooled
    ``MergedSpectrum`` (combine-before-divide ΣN counts) is fit through the same ``fit_one`` path as a
    single scan and stored as a ``master:<key>`` ledger entry marked ``pooled=True``. Idempotent on the
    member SET: the entry's ``content_hash`` is a hash of ``ms.members``, so a master refits exactly
    when a member scan arrives or drifts (``collate`` re-runs over all records every batch, so masters
    cannot piggyback on the per-scan freshness flags). Never fatal — a bad group yields a
    ``non_result`` entry, never a crash.

    When more than one group needs refitting and the scan-worker budget allows, the group fits run in
    parallel over an order-preserving ``ProcessPoolExecutor.map`` (bootstrap forced serial in each
    child, same Layer-A trade-off as the scan pool); ``session.put`` is still applied serially in the
    original sorted-group order afterward, so the result is bit-identical to the serial path. A single
    refit (the normal-iteration case) keeps the in-process serial path with the shared bootstrap pool.
    """
    if collation is None or getattr(collation, "merge", "separate") != "merge":
        return 0
    from isar.session.state import canonical_hash

    rec_by_scan = {rec.scan_number: rec for rec in records}
    # Card policy opt-in (default OFF): reuse an invalidated group's previous fitted structure as a
    # cold-refit hint, skipping the full enumeration when the gates permit. OFF ⇒ hint is always
    # None ⇒ nothing below the fit call changes (see fit_structures_two_stage_hinted).
    _pol = getattr(card, "policy", None)
    reuse_structure = bool((getattr(_pol, "fitting", None) or {}).get("reuse_master_structure", False))
    # --- Refit list first: apply the membership-hash skip unchanged (carry path untouched). ---
    # idx is the ORIGINAL sorted-group enumeration position (NOT the refit-list position) so the
    # per-group seed `seed_base + _MASTER_SCAN_BASE + idx` and synthetic scan_number stay stable.
    refits = []     # (idx, grp, ms, master_id, chash, member_rec, member_det, structure_hint)
    for idx, grp in enumerate(sorted(collation.groups, key=lambda gg: str(gg.key))):
        ms = getattr(grp, "merged", None)
        if ms is None or ms.n_members < 1:
            continue
        master_id = "master:" + "_".join(str(k) for k in grp.key)
        chash = canonical_hash([list(m) for m in ms.members])
        prev = session.get(master_id)               # read BEFORE the refit overwrites the ledger
        if not force and prev is not None and prev.content_hash == chash:
            # Member set unchanged → carry the prior master. Backfill the member provenance onto an
            # older ledger entry that predates this field: the content-hash match proves the member
            # set is byte-identical, so `_member_labels(ms.members)` is exact — pure bookkeeping, no
            # refit. In-place mutation persists (session.get returns the live ledger ref).
            for oc in prev.detectors.values():
                if oc.get("pooled") and "members" not in oc:
                    oc["members"] = _member_labels(ms.members)
            if prev.provenance and "members" not in prev.provenance:
                prev.provenance["members"] = [list(m) for m in ms.members]
            continue
        member_scan, member_det = ms.members[0][1], ms.members[0][2]
        # Structure hint: the previous entry's best_label, iff it was a completed (fitted) master
        # and the reuse policy is on. Anything else (cold group, held/non_result, policy off) → None.
        hint = None
        if reuse_structure and prev is not None and prev.status == "fitted":
            hint = (prev.detectors.get(member_det or "merged") or {}).get("best_label")
        refits.append((idx, grp, ms, master_id, chash, rec_by_scan.get(member_scan), member_det,
                       hint))
    if not refits:
        return 0

    # --- Fit the refits: parallel across groups when it pays, else serial in-process. ---
    if len(refits) > 1 and scan_workers > 1:
        # Parallel: bootstrap forced serial in each child (boot_workers=1, no shared pool). map
        # preserves order → entries[k] aligns with refits[k], keeping the serial ledger order.
        arg_list = [(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                     _MASTER_SCAN_BASE + idx, chash, M, seed_base + _MASTER_SCAN_BASE + idx,
                     errors_tier, boot_adaptive, hint, bayes_opts)
                    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in refits]
        max_workers = min(scan_workers, len(arg_list))
        with ProcessPoolExecutor(max_workers=max_workers) as ex_pool:
            entries = list(ex_pool.map(_fit_master_group_task, arg_list))
    else:
        # Serial (normal iteration, ≤1 refit, or single worker) — hot path unchanged. Reuse the
        # batch's shared bootstrap pool when given; else open one for the master pass (only the full
        # tier bootstraps, so a pool is pointless otherwise).
        own_pool = None
        ex = boot_executor
        if ex is None and errors_tier in FULL_TIERS and boot_workers > 1:
            own_pool = ProcessPoolExecutor(max_workers=boot_workers)
            ex = own_pool
        try:
            entries = [
                _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                                  _MASTER_SCAN_BASE + idx, chash, M=M,
                                  seed=seed_base + _MASTER_SCAN_BASE + idx, errors_tier=errors_tier,
                                  boot_workers=boot_workers, boot_executor=ex,
                                  boot_adaptive=boot_adaptive, bayes_opts=bayes_opts,
                                  # hint kwarg only when set → off-path call is byte-identical.
                                  **({"structure_hint": hint} if hint is not None else {}))
                for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in refits]
        finally:
            if own_pool is not None:
                own_pool.shutdown()

    # Apply ledger mutations + timing entries serially, in the original sorted-group order.
    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint), entry in zip(refits, entries):
        session.put(entry)
        if per_master is not None:
            per_master.append(
                {"master_id": master_id,
                 "seconds": entry.provenance.get("fit_seconds"),
                 "two_stage_seconds": entry.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": entry.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": entry.provenance.get("bootstrap_seconds"),
                 "minos_seconds": entry.provenance.get("minos_seconds"),
                 "profile_seconds": entry.provenance.get("profile_seconds"),
                 "bayes_seconds": entry.provenance.get("bayes_seconds")})
    return len(refits)


def _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number,
                      chash, *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None,
                      boot_adaptive=False, structure_hint=None, bayes_opts=None) -> ScanEntry:
    """Fit one merged group's pooled spectrum → a ``pooled=True`` ScanEntry (or a non_result).

    ``structure_hint`` (the group's previous fitted best_label, opt-in via card policy
    ``[fitting].reuse_master_structure``) offers the structure-skip shortcut; an entry produced via
    an accepted hint is stamped ``provenance["structure_skipped"] = True``."""
    _t_fit0 = time.perf_counter()
    det = member_det or "merged"
    base = dict(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                content_hash=chash, guidelines_hash="")
    q = list(grp.key_fields.get("q") or [0.0, 0.0, 0.0])
    T_disp = grp.key_fields.get("T")
    T = float(T_disp) if T_disp is not None else float("nan")
    if member_rec is None:
        return ScanEntry(status="non_result", reasons=[f"{master_id}: no member record for resolution"],
                         **base)
    try:
        calres = calibrations.resolve(member_rec, member_det, card, runcal)
    except Exception as exc:  # noqa: BLE001
        return ScanEntry(status="non_result", reasons=[f"{master_id}: resolution failed ({exc})"], **base)

    x = np.asarray(ms.energy, dtype=float)
    counts = np.asarray(ms.intensity, dtype=float)
    mask = np.asarray(ms.mask, dtype=bool)
    raw_monitor = getattr(ms, "monitor", None)
    monitor = None if raw_monitor is None else np.asarray(raw_monitor, dtype=float)
    exposure_scale, _reference_exposure = ((None, None) if monitor is None
                                           else normalized_exposure(monitor, mask))
    if exposure_scale is not None:
        mask = mask & (exposure_scale > 0)
    if int(mask.sum()) < g.min_fit_points:
        return ScanEntry(status="non_result",
                         reasons=[f"{master_id}: undersampled pool ({int(mask.sum())} < "
                                  f"{g.min_fit_points} kept points)"], **base)
    # Pooled spectrum → ScanContext directly (bypasses build_context: a merged group has no single
    # ScanRecord/normalization; MergedSpectrum.mask already encodes monitor validity).
    sample = str(grp.key_fields.get("sample", ""))
    period = _dispersion_period(card)
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    target_branch, target_label, energy_search = _campaign_target(card)
    prior = (session_prior.prior_for(card.state, sample=sample, T=T, q=q, period=period,
                                     t_round=t_round, branch_id=target_branch)
             if period is not None else None)
    prior_ao = float(prior.omega0) if (prior is not None and np.isfinite(prior.omega0)) else None
    target_anchors = _target_fold_anchor_count(card, sample=sample, branch=target_branch)
    established = target_branch == "acoustic" or target_anchors >= branch_model.MIN_ANCHORS
    # A cold optic target has no numeric anchor.  Keep both the IC and primary-mode choice broad;
    # Bayesian assignment below will select a candidate without forcing the upper-energy mode.
    ao = prior_ao if established else None
    # Narrow the IC only when the belief is confident (see _PRIOR_NARROW_SIGMA); else keep the acoustic
    # mode hint but a wide IC so the pooled fit still finds the resolvable acoustic.
    ic_ao = (prior_ao if (established and prior_ao is not None
                          and float(prior.omega0_sigma or 1e9) <= _PRIOR_NARROW_SIGMA) else None)
    ctx = ScanContext(x=x, counts=counts, mask=mask, T=T, center=calres.center,
                      gauss_fwhm=calres.gauss_fwhm, lorentz_fwhm=calres.lorentz_fwhm,
                      instrument=card.policy.instrument, energy_status=card.policy.energy_status,
                      prior_omega=ic_ao, quasielastic_fwhm=card.policy.quasielastic_fwhm,
                      acoustic_omega=ao, exposure_scale=exposure_scale)
    near_gamma = (target_branch == "acoustic" and ao is not None
                  and ao < BA.RESOLVABLE_FWHM_FACTOR * calres.gauss_fwhm)
    descriptor = _build_descriptor(ctx)
    try:
        fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, errors_tier=errors_tier,
                      boot_workers=boot_workers, boot_executor=boot_executor,
                      boot_adaptive=boot_adaptive, descriptor=descriptor,
                      structure_hint=structure_hint, bayes_opts=bayes_opts)
    except Exception as exc:  # noqa: BLE001 — one unfittable pool must not kill the batch (N=16 wrinkle)
        return ScanEntry(status="non_result",
                         reasons=[f"{master_id}: fit error ({type(exc).__name__}): {exc}"], **base)

    # Phase A: the pooled master runs the SAME branch triage as a per-scan fit — the acoustic hint
    # (ctx.acoustic_omega) makes primary_phonon_index report the acoustic mode (not the brighter optic),
    # and the triage withholds a non-acoustic/unresolvable pool as branch_continuation. Without this a
    # boundary master delivers the optic as the acoustic ω₀.
    (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
     branch_posterior, candidate_id, candidate_state) = _apply_branch_triage(
        fit, descriptor, ctx, card=card, sample=sample, T=T, q=q, t_round=t_round,
        near_gamma=near_gamma, master=True)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    exposure_info = _effective_exposure_for(None, ctx, fit, absolute_exposure=monitor)
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=str(grp.key_fields.get("sample", "")), prior_used=None,
        calib=calres.summary(), reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        amplitudes=fit.get("amplitudes"), candidates=fit.get("candidates"),
        interval_reliable=fit.get("interval_reliable", True), omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        pooled=True, n_members=int(ms.n_members), members=_member_labels(ms.members),
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
        effective_exposure=exposure_info.get("value"), exposure_provenance=exposure_info)
    entry = ScanEntry(status="fitted", **base)
    entry.detectors[det] = outcome.to_dict()
    entry.provenance = {"pooled": True, "n_members": int(ms.n_members),
                        "members": [list(m) for m in ms.members],
                        "fit_seconds": round(time.perf_counter() - _t_fit0, 4),
                        "two_stage_seconds": fit.get("two_stage_seconds"),
                        "errors_attach_seconds": fit.get("errors_attach_seconds"),
                        **_timing_provenance(fit)}
    if fit.get("structure_skipped"):
        entry.provenance["structure_skipped"] = True
    return entry


# Phase A branch assignment: near a zone centre the acoustic ω₀ is unresolvable BY POSITION (below
# BA.RESOLVABLE_FWHM_FACTOR × resolution FWHM the merged Stokes/anti-Stokes blob is shape-identical to
# an elastic peak). There it is measured by the constrained (hard Γ+A / brightness) fit, or reported as
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


def _campaign_selector(card) -> tuple[str, float | None]:
    target = getattr(getattr(card, "state", None), "campaign_target", None) or {}
    selector = str(target.get("selector") or "lowest_energy")
    energy = target.get("target_energy_meV")
    return selector, (float(energy) if energy is not None else None)


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
    """Bayesian branch assignment + honest triage over a completed fit (Phase A). Returns override
    fields ``{deliver, branch_id, weight, entropy, resolvable}`` or ``None`` when the posterior is off.

    Delivers the acoustic ω₀ when it is a confident, dominant, *resolvable* feature — OR when a
    converged near-Γ **constrained** (branch_conditional) fit measured it (the brightness constraint
    supplies identifiability where position cannot). Otherwise the scan is NOT an acoustic delivery:
    the caller nulls ω₀ and reports ``branch_continuation``, tagging the fit's assigned branch so an
    optic/spurious feature can never seed or be scored as the acoustic branch."""
    period = _dispersion_period(card)
    if period is None:
        return None
    t_bin = round(float(T) / t_round) * t_round if t_round else float(T)
    target_branch, _target_label, energy_search = _campaign_target(card)
    hypotheses = _campaign_hypotheses(card)
    if target_branch != BA.ACOUSTIC and not any(h.get("name") == target_branch for h in hypotheses):
        # Transition tolerance for old campaign state: create a broad shipped hypothesis, never an
        # assignment or anchor.  Its zero occupancy remains weak evidence in the posterior.
        hypotheses.append(hypothesis_preset(target_branch, energy_search).to_dict())
    beliefs = BA.build_beliefs(card.state.dispersion_prior, sample=sample, t_bin=t_bin,
                               period=period, t_round=t_round,
                               hypotheses=hypotheses)
    modes = fit.get("modes")
    if not modes and fit.get("omega0") is not None:
        modes = [{"omega0": fit["omega0"], "omega0_err": fit.get("omega0_err"),
                  "gamma": fit.get("gamma"), "amplitude": 0.0}]
    feats = BA.features_from(modes or [], descriptor, gauss_fwhm=ctx.gauss_fwhm)
    if not feats:
        return {"deliver": bool(fit.get("omega0") is not None), "branch_id": None,
                "weight": None, "entropy": None, "resolvable": None}
    u = BA.reduced_q(q, period)
    if target_branch != BA.ACOUSTIC:
        # A fitted mode and the model-free descriptor commonly report the same physical peak.  Do
        # branch assignment from the fitted modes when they exist: counting both representations as
        # rival features splits dominance and can demote an otherwise clean pooled optic fit.  The
        # descriptor remains the honest fallback when no model mode survived.
        target_feats = [feature for feature in feats if feature.source == "mode"] or feats
        target_post = BA.assignment_posterior(target_feats, beliefs, u)
        selector, target_energy = _campaign_selector(card)
        if target_energy is not None:
            target_post = BA.condition_on_target_energy(
                target_feats, target_post, target_branch, target_energy=target_energy,
                sigma=max(float(ctx.gauss_fwhm), BA.SIGMA_SLACK))
        qoi = BA.selected_branch_qoi(
            target_feats, target_post, target_branch, gauss_fwhm=ctx.gauss_fwhm,
            selector=selector, target_energy=target_energy, energy_search=energy_search)
        ent = float(qoi["entropy"]) if np.isfinite(qoi["entropy"]) else None
        aligned = fit.get("omega0") is not None and qoi.get("omega0") is not None \
            and abs(float(fit["omega0"]) - float(qoi["omega0"])) <= 0.5 * ctx.gauss_fwhm
        deliver = BA.deliver_branch(qoi) and aligned
        posterior = qoi.get("posterior")
        assigned = max(posterior, key=posterior.get) if posterior else None
        candidate_id = f"{target_branch}:{u:.6f}:{selector}"
        return {"deliver": bool(deliver), "branch_id": assigned,
                "weight": float(qoi.get("p_branch", 0.0)), "entropy": ent,
                "resolvable": bool(qoi.get("resolvable")), "posterior": posterior,
                "candidate_id": candidate_id,
                "candidate_state": ("accepted" if deliver else "provisional")}
    post = BA.assignment_posterior(feats, beliefs, u)
    qoi = BA.acoustic_qoi(feats, post, gauss_fwhm=ctx.gauss_fwhm)
    ent = float(qoi["entropy"]) if np.isfinite(qoi["entropy"]) else None
    # Bounded-continuity guard: apply it only once the belief is ESTABLISHED (≥ MIN_ANCHORS real
    # anchors). During bootstrap the belief has few anchors and MUST be allowed to deliver outward to
    # seed itself; a hard guard there deadlocks the whole prior. Once established, a lone feature far
    # past the anchored range (a zone-boundary optic) is withheld. The near-Γ constrained fit is exempt.
    acoustic_belief = beliefs.get(BA.ACOUSTIC)
    established = acoustic_belief is not None and acoustic_belief.n_anchors >= branch_model.MIN_ANCHORS
    extrap_ok = (not established) or acoustic_belief.deliverable(u)
    constrained_ok = (near_gamma and fit.get("fit_kind") == "branch_conditional"
                      and fit.get("omega0") is not None and not fit.get("gamma_at_bound")
                      and qoi["p_acoustic"] >= BA.P_ACOUSTIC_MIN)
    if constrained_ok or (BA.deliver_acoustic(qoi) and extrap_ok):
        # Rival-branch banking: a DELIVERED scan's clearly-non-acoustic higher features seed the
        # optic belief, so at the next q the optic claims its own kin instead of splitting the
        # acoustic dominance (the post-Phase-A abstention deadlock at mid-band q).
        return {"deliver": True, "branch_id": "acoustic", "weight": float(qoi["p_acoustic"]),
                "entropy": ent, "resolvable": bool(qoi["resolvable"]),
                "secondary_points": BA.secondary_branch_points(feats, post, qoi,
                                                               gauss_fwhm=ctx.gauss_fwhm)}
    bid = "optic"
    if fit.get("omega0") is not None:
        bid, _ = BA.assign_label(fit["omega0"], fit.get("omega0_err") or 0.3 * ctx.gauss_fwhm,
                                 beliefs, u, gauss_fwhm=ctx.gauss_fwhm)
    return {"deliver": False, "branch_id": bid, "weight": 0.0, "entropy": ent,
            "resolvable": bool(qoi["resolvable"])}


def _apply_branch_triage(fit, descriptor, ctx, *, card, sample, T, q, t_round, near_gamma,
                         master: bool):
    """Run branch assignment + honest triage over ``fit`` and apply its verdict (Phase A). Shared by
    the per-scan (:func:`_finish_outcome`) and pooled-master (:func:`_fit_master_group`) paths.

    Returns branch assignment, candidate posterior/state, and secondary features — all
    None when the posterior is off or the fit is a ``signal_unmodeled`` descriptor result. When the
    triage declines delivery it mutates ``fit`` in place: flips the verdict to ``branch_continuation``,
    nulls the ω₀ fields, and appends a reason (worded per ``master``)."""
    branch_id = assignment_entropy = branch_weight = resolvable = None
    branch_posterior = candidate_id = candidate_state = None
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
    return (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
            branch_posterior, candidate_id, candidate_state)


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
                          provenance={"resolution_version": calres.resolution_version}, **base),
                calres)
    return None, calres


def _apply_low_q_rescue(ctx, prior, card, sample, T, rec, near_gamma, region_reasons):
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
            exposure, _mode = _exposure_per_point(rec, ctx)
            exp = exposure if np.isfinite(exposure) else 0.0
            # Near Γ AND in-anchor: the CONSTRAINED recipe (hard Γ+A → the blob's Bose-weighted
            # brightness pins ω₀ where its position, 0.34 FWHM from the origin, cannot). Elsewhere the
            # ordinary soft rescue (A stays soft, absorbing monitor / focusing-asymmetry drift).
            if near_gamma and pred.in_anchor_range:
                cons = pred.hard_constraints(exp)
                note = (f"near-Γ constrained fit (hard Γ={pred.gamma:.2f} meV + A from 1/ω; "
                        f"brightness pins ω₀)")
            else:
                cons = pred.soft_constraints(exp)
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
                         provenance={"resolution_version": calres.resolution_version}, **base)

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
        ctx = _apply_low_q_rescue(ctx, prior, card, sample, T, rec, near_gamma, region_reasons)

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
    would silently drop the existing hard phonon.A brightness constraint."""
    target_branch, _target_label, _energy_search = _campaign_target(card)
    already_hard_gamma = (ctx.constraints is not None
                          and "phonon.Gamma" in (ctx.constraints.hard or {}))
    if (target_branch == "acoustic" and fit.get("gamma_at_bound")
            and prior is not None and np.isfinite(prior.omega0)
            and not already_hard_gamma):
        pred = branch_model.branch_for(card.state, sample=sample, T=T, omega0=float(prior.omega0))
        if pred is not None and pred.in_anchor_range and pred.gamma > 0:
            soft = dict(ctx.constraints.soft) if ctx.constraints is not None else {}
            soft.pop("phonon.Gamma", None)                 # Γ becomes hard, not soft
            hctx = replace(ctx, constraints=ParamConstraints(
                soft=soft, hard={"phonon.Gamma": float(pred.gamma)}))
            try:
                hfit = fit_one(hctx, card, prior=prior, M=M, seed=seed, structure_labels=labels,
                               peaks=peaks, errors_tier=errors_tier, boot_workers=boot_workers,
                               boot_executor=boot_executor, boot_adaptive=boot_adaptive,
                               descriptor=descriptor, bayes_opts=bayes_opts)
            except Exception:  # noqa: BLE001 — escalation is best-effort; keep the original fit
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
     branch_posterior, candidate_id, candidate_state) = _apply_branch_triage(
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
    exposure_info = _effective_exposure_for(rec, ctx, fit)
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=sample, prior_used=prior_used, calib=calres.summary(),
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        amplitudes=fit.get("amplitudes"), candidates=fit.get("candidates"),
        interval_reliable=fit.get("interval_reliable", True),
        omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        intensity=_intensity_measure(rec, ctx, fit),
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
        effective_exposure=exposure_info.get("value"), exposure_provenance=exposure_info)

    # NB: the running dispersion prior (session_prior.add_point) is fed by the PARENT in
    # submission order after the fit returns — start-of-batch seeding, so a parallel batch is
    # bit-identical to a serial one. This worker never mutates shared card/session state.
    return ScanEntry(status="fitted", reasons=outcome.reasons,
                     detectors={det: outcome.to_dict()},
                     provenance={"resolution_version": calres.resolution_version,
                                 "M": M, "seed": seed, "prior_source": prior_source,
                                 "fit_seconds": round(time.perf_counter() - t_fit0, 4),
                                 "two_stage_seconds": fit.get("two_stage_seconds"),
                                 "errors_attach_seconds": fit.get("errors_attach_seconds"),
                                 **_timing_provenance(fit)}, **base)


def _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                 chash, ghash, *, M, seed, errors_tier="full", boot_workers=1,
                 boot_executor=None, boot_adaptive=False, bayes_opts=None) -> ScanEntry:
    _t_fit0 = time.perf_counter()
    base = dict(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                content_hash=chash, guidelines_hash=ghash)

    gated, calres = _gate_scan(rec, det, card, g, runcal, exclusions, base)
    if gated is not None:
        return gated

    working = _resolve_prior_and_context(rec, det, q, T, sample, scan_id, card, g, calres, base)
    if isinstance(working, ScanEntry):
        return working
    (ctx, prior, prior_source, n_neighbors, near_gamma, t_round, labels, peaks,
     descriptor, region_reasons) = working

    try:
        fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, structure_labels=labels, peaks=peaks,
                      errors_tier=errors_tier, boot_workers=boot_workers,
                      boot_executor=boot_executor, boot_adaptive=boot_adaptive, descriptor=descriptor,
                      bayes_opts=bayes_opts)
    except Exception as exc:  # noqa: BLE001 — one unfittable scan must not kill the batch
        return ScanEntry(status="non_result",
                         reasons=[f"fit error ({type(exc).__name__}): {exc}"] + region_reasons,
                         provenance={"resolution_version": calres.resolution_version}, **base)

    fit = _hard_fix_escalation(fit, ctx, prior, card, sample, T, labels, peaks, descriptor,
                               region_reasons, M=M, seed=seed, errors_tier=errors_tier,
                               boot_workers=boot_workers, boot_executor=boot_executor,
                               boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)

    return _finish_outcome(fit, descriptor, ctx, rec, det, q, T, sample, card, g, calres, prior,
                           prior_source, n_neighbors, near_gamma, t_round, region_reasons, base,
                           M=M, seed=seed, t_fit0=_t_fit0)


# ===========================================================================
# helpers
# ===========================================================================
def _split_csv(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _outcome_from_entry(entry: ScanEntry, det: str) -> FitOutcome:
    return FitOutcome(**entry.detectors[det])


def _maybe_adjudicate(measurements, card) -> list[str]:
    try:
        from isar.reduce import adjudicate
        cfg = adjudicate.adjudication_config(card)
        client = adjudicate.make_client(cfg)
        adjudicate.adjudicate_records(measurements, card, client)
        return ["LLM adjudication: applied"]
    except Exception as exc:  # noqa: BLE001 — degrade, never stall the batch (DESIGN §9)
        return [f"LLM adjudication skipped (no consult): {exc}"]


def _tally(session: SessionState) -> dict:
    counts = {"by_status": {}, "by_disposition": {}}
    for e in session.scans.values():
        counts["by_status"][e.status] = counts["by_status"].get(e.status, 0) + 1
        for oc in e.detectors.values():
            d = oc.get("disposition")
            if d:
                counts["by_disposition"][d] = counts["by_disposition"].get(d, 0) + 1
    return counts


_CSV_COLUMNS = [
    "scan", "detector", "sample", "T", "h", "k", "l", "status", "verdict", "disposition",
    "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
    "omega0_profile_lo", "omega0_profile_hi", "hesse_lo", "hesse_hi", "hesse_trusted",
    "bayes_lo", "bayes_med", "bayes_hi", "bayes_ok", "bayes_ess", "bayes_fail_reason",
    "bayes_freq_ratio", "hesse_freq_ratio", "bayes_prior_driven", "bayes_fork_suspected",
    "bayes_unconstrained", "log_evidence", "log_evidence_err",
    "pooled", "n_members", "members",
    "gamma", "gamma_provisional", "n_modes", "secondary_omega0", "secondary_omega0_err",
    "best_model", "cost_per_dof", "fit_kind", "branch_id", "assignment_entropy", "resolvable",
    "descriptor_n_peaks", "descriptor_peaks",
    "zero", "zero_source", "gauss_fwhm",
    "lorentz_fwhm", "resolution_source", "resolution_version", "prior_omega", "prior_sigma",
    "prior_n", "prior_source", "reasons",
    # GUI Phase 0 (P0-3/P0-4), appended so older readers/CSVs are unaffected. JSON-encoded strings:
    # the amplitudes dict and the model-selection candidate table.
    "amplitudes", "candidates",
    # Additive atomic identity: scan numbers are not globally unique across experiments, so new
    # consumers must route detector data using this full ledger key. Appended for old positional CSV
    # consumers, which continue seeing every existing column in its original order.
    "scan_id", "target_branch", "target_label",
    # Dynamic-exposure / branch-candidate provenance (additive v2 columns).
    "effective_exposure", "exposure_provenance", "branch_posterior",
    "candidate_id", "candidate_state", "acquisition_id",
]


def _descriptor_summary(oc: dict) -> tuple:
    """(`n_peaks`, compact `"E@σ; …"` string) from the stored descriptor, else (None, None).
    Surfaces the model-free deliverable on results.csv (Phase 4a) — load-bearing for
    ``signal_unmodeled`` scans, whose ω₀ is deliberately absent."""
    desc = oc.get("descriptor")
    if not desc:
        return None, None
    peaks = desc.get("peaks") or []
    compact = "; ".join(f"{float(p.get('energy', 0.0)):.2f}@{float(p.get('significance', 0.0)):.1f}σ"
                        for p in peaks)
    return len(peaks), (compact or None)


def _secondary_mode(oc: dict) -> tuple:
    """The (omega0, omega0_err) of the non-primary mode for a 2-mode fit, else (None, None).

    Primary = the mode whose ω₀ matches the reported scalar ``omega0``; secondary is the other.
    """
    if (oc.get("n_modes") or 1) != 2:
        return None, None
    modes = oc.get("modes") or []
    if len(modes) != 2:
        return None, None
    prim = oc.get("omega0")
    others = [m for m in modes if m.get("omega0") != prim]
    sec = others[0] if others else modes[-1]
    return sec.get("omega0"), sec.get("omega0_err")


def _write_results_csv(workspace: Workspace, session: SessionState) -> Path:
    workspace.report_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.report_dir / "results.csv"
    rows = []
    for e in sorted(session.scans.values(), key=lambda e: e.scan_number):
        if not e.detectors:
            rows.append({"scan_id": e.scan_id, "scan": e.scan_number, "status": e.status,
                         "reasons": "; ".join(e.reasons)})
            continue
        for det, oc in e.detectors.items():
            q = oc.get("q") or [None, None, None]
            iv = oc.get("interval")
            piv = oc.get("omega0_profile")
            hiv = oc.get("hesse_interval")
            biv = oc.get("bayes_interval")
            cal = oc.get("calib") or {}
            pr = oc.get("prior_used") or {}
            # Member provenance (chain of custody): pooled masters join their member labels with ";".
            # Unpooled → empty. A pooled master carrying the field must satisfy n_members == len(members)
            # (raise otherwise — the invariant is load-bearing for the diagnostic evidence chain). A
            # pooled master WITHOUT the field (old ledger, group absent from this batch's collation so
            # the backfill never ran) is tolerated: empty cell, no raise.
            members = oc.get("members")
            if oc.get("pooled") and members is not None and len(members) != int(oc.get("n_members") or -1):
                raise ValueError(
                    f"master scan {e.scan_number}: n_members={oc.get('n_members')} != "
                    f"len(members)={len(members)} — member provenance corrupt")
            rows.append({
                "scan_id": e.scan_id, "scan": e.scan_number, "detector": det, "sample": oc.get("sample"),
                "target_branch": oc.get("target_branch", "acoustic"),
                "target_label": oc.get("target_label", oc.get("target_branch", "acoustic")),
                "effective_exposure": oc.get("effective_exposure"),
                "exposure_provenance": (json.dumps(oc.get("exposure_provenance"), sort_keys=True)
                                        if oc.get("exposure_provenance") is not None else None),
                "branch_posterior": (json.dumps(oc.get("branch_posterior"), sort_keys=True)
                                     if oc.get("branch_posterior") is not None else None),
                "candidate_id": oc.get("candidate_id"), "candidate_state": oc.get("candidate_state"),
                "acquisition_id": oc.get("acquisition_id"),
                "T": oc.get("T"), "h": q[0], "k": q[1], "l": q[2], "status": e.status,
                "verdict": oc.get("verdict"), "disposition": oc.get("disposition"),
                "confidence": oc.get("confidence"), "omega0": oc.get("omega0"),
                "omega0_err": oc.get("omega0_err"),
                "interval_lo": (iv[0] if iv else None), "interval_hi": (iv[-1] if iv else None),
                "interval_reliable": oc.get("interval_reliable", True),
                "omega0_profile_lo": (piv[0] if piv else None),
                "omega0_profile_hi": (piv[-1] if piv else None),
                "hesse_lo": (hiv[0] if hiv else None), "hesse_hi": (hiv[-1] if hiv else None),
                "hesse_trusted": oc.get("hesse_trusted"),
                "bayes_lo": (biv[0] if biv else None),
                "bayes_med": (biv[1] if biv else None),
                "bayes_hi": (biv[-1] if biv else None),
                "bayes_ok": oc.get("bayes_ok"), "bayes_ess": oc.get("bayes_ess"),
                "bayes_fail_reason": oc.get("bayes_fail_reason"),
                "bayes_freq_ratio": oc.get("bayes_freq_ratio"),
                "hesse_freq_ratio": oc.get("hesse_freq_ratio"),
                "bayes_prior_driven": oc.get("bayes_prior_driven"),
                "bayes_fork_suspected": oc.get("bayes_fork_suspected"),
                "bayes_unconstrained": oc.get("bayes_unconstrained"),
                "log_evidence": oc.get("log_evidence"),
                "log_evidence_err": oc.get("log_evidence_err"),
                "pooled": oc.get("pooled") or None, "n_members": oc.get("n_members"),
                "members": (";".join(members) if members else None),
                "gamma": oc.get("gamma"), "gamma_provisional": oc.get("gamma_provisional"),
                "n_modes": oc.get("n_modes") or 1,
                "secondary_omega0": _secondary_mode(oc)[0],
                "secondary_omega0_err": _secondary_mode(oc)[1],
                "best_model": oc.get("best_label"), "cost_per_dof": oc.get("cost_per_dof"),
                "fit_kind": oc.get("fit_kind"),
                "branch_id": oc.get("branch_id"),
                "assignment_entropy": oc.get("assignment_entropy"),
                "resolvable": oc.get("resolvable"),
                "descriptor_n_peaks": _descriptor_summary(oc)[0],
                "descriptor_peaks": _descriptor_summary(oc)[1],
                "zero": cal.get("center"), "zero_source": cal.get("zero_source"),
                "gauss_fwhm": cal.get("gauss_fwhm"), "lorentz_fwhm": cal.get("lorentz_fwhm"),
                "resolution_source": cal.get("resolution_source"),
                "resolution_version": cal.get("resolution_version"),
                "prior_omega": pr.get("omega0"), "prior_sigma": pr.get("sigma"),
                "prior_n": pr.get("n_neighbors"), "prior_source": pr.get("source"),
                "reasons": "; ".join(oc.get("reasons") or []),
                # JSON-encoded (or None when absent) so the flat CSV can carry the structured fields;
                # readers.read_results_csv json.loads them back. sort_keys keeps the cell stable.
                "amplitudes": (json.dumps(oc.get("amplitudes"), sort_keys=True)
                               if oc.get("amplitudes") is not None else None),
                "candidates": (json.dumps(oc.get("candidates"), sort_keys=True)
                               if oc.get("candidates") is not None else None),
            })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in _CSV_COLUMNS})
    return path

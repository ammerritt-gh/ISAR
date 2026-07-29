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
import copy
import hashlib
import json
import logging
import math
import os
import time
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

import numpy as np

from isar.analyze import errors, loop, select, verdict
from isar.analyze import gold as gold_mod
from isar.analyze.context import (
    ScanContext, effective_exposure, normalized_exposure, select_exposure,
)
from isar.analyze.descriptor import describe_spectrum
from isar.analyze.structures import enumerate_multimode, enumerate_structures
from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.predictive import PREDICTIVE_CONTRACT
from isar.predictive import json_fingerprint as _json_fingerprint
from isar.schema.card import Card
from isar.schema.hypotheses import preset as hypothesis_preset
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    INTERVAL_SCHEMA,
    PROBABILITY_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    compact_for_csv,
    semantics_fingerprint,
    validate_for_write,
)
from isar.schema.steering import (AUXILIARY_TRACK_NODE, CANDIDATE_NOT_DETECTED, CANONICAL_MASTER,
                                  IntensityMeasure, ParamConstraints, evidence_sink_for,
                                  realized_outcome_for)
from isar.session import branch_assignment as BA
from isar.session import branch_model, branch_tracking, calibrations, guidelines as guide_mod, ingest, router, session_prior
from isar.session import predictive_artifact
from isar.session.predictive_artifact import model_manifest_of as _model_manifest_of

# Steering Phase 2c: apply the low-q branch soft-constraint when the dispersion prior predicts a
# phonon within this many resolution FWHM of the elastic line (the merging regime where Γ/A go
# unidentifiable). Above it the peak is well separated and the fit is left unconstrained.
LOW_Q_FWHM_FACTOR = 2.0
# Error tiers that run the ordinary physical-fit frequentist interval (MINOS + parametric bootstrap
# + ω₀ profile). "bayes" is a strict superset of "full" — everything full does, plus the
# nested-sampling posterior on the prelim-best label. That single-fit Bayesian interval is
# diagnostics-only; pooled-master certification separately uses the mixed predictive interval.
FULL_TIERS = ("full", "bayes")
from isar.session import unit_data
from isar.session.state import ScanEntry, SessionState, canonical_hash, content_hash
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
    # Phase A normalized branch-assignment scoring: which dispersion branch this scan's ω₀ was
    # assigned to (acoustic | optic | new | ...), the Shannon entropy over assignment weights,
    # whether the acoustic ω₀ was position-resolvable (ω₀ ≳ 1 FWHM), and the soft assignment weight
    # fed to the branch model. These are probability-like scores, not calibrated posterior
    # probabilities. None on older ledgers / when assignment is off (no dispersion.zone_period).
    branch_id: str | None = None
    assignment_entropy: float | None = None
    resolvable: bool | None = None
    branch_weight: float | None = None
    # Rival-branch anchor candidates from THIS delivered scan's other features (each
    # {omega0, branch_id, weight}) — banked onto the dispersion prior by the parent so an optic
    # belief can form and claim its own features at later q. None/empty on abstains + older ledgers.
    secondary_features: list | None = None
    # Bayes-errors tier (--errors bayes): the nested-sampling posterior on ω₀ + its §5.5
    # three-method (HESSE / bootstrap / posterior) cross-check. `bayes_interval` is diagnostics-only
    # as a single-fit quantity; pooled-master predictive construction may use accepted nested
    # particles inside its separate legacy mixed interval. All defaulted for back-compat with older
    # ledgers and the non-bayes tiers.
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
    # Declarative, versioned description of the delivered model.  This is the read-only viewer's
    # reconstruction contract: registered component kinds + prefixes + the exact fitted parameter
    # vector, never a pickled Python object or import path.
    model_manifest: dict | None = None
    target_branch: str = "acoustic"
    target_label: str = "acoustic"
    effective_exposure: float | None = None
    exposure_provenance: dict | None = None
    branch_posterior: dict | None = None
    candidate_id: str | None = None
    candidate_state: str | None = None
    acquisition_id: str | None = None
    # Where this scan's evidence is allowed to go (isar.schema.steering.EVIDENCE_SINKS). Absent or
    # unrecognised means ``canonical_master``, so hand-loaded data and every pre-existing ledger read
    # as ordinary evidence. Anything else was acquired to answer a narrower question and is barred from
    # the (Q,T) pool and from certification.
    evidence_sink: str = CANONICAL_MASTER
    # The searched energy window of an auxiliary detection probe. Persisted so a trustworthy clean null
    # (``disposition == no_signal`` under an ``auxiliary_track_node`` sink) becomes tracker negative
    # evidence -- a ``null_window`` relaxing the missing-branch pull at this q -- instead of a vanished
    # result. ``None`` on ordinary deliveries and on ledgers predating this field.
    search_energy_lo: float | None = None
    search_energy_hi: float | None = None
    # Withheld target delivery (see _withhold_tracked_delivery). ``target_delivery_eligible`` gates
    # certification/model/prior downstream; the other two carry the identity conclusion that survives
    # the withheld number, and the reason a reader can act on. Declared here because the ledger
    # rehydrates through this dataclass -- an undeclared key is logged and dropped on every carry.
    target_delivery_eligible: bool = True
    identity_branch_id: str | None = None
    delivery_withheld_reason: str | None = None
    # Additive tracked-branch contract. Every fitted mode remains an unordered candidate; the joint
    # path tracker records which one is currently selected without erasing earlier alternatives.
    branch_candidates: list | None = None
    selected_candidate_index: int | None = None
    track_state: str | None = None
    track_revision: int | None = None
    # The tracker's local identity verdict, retained even when the scalar selection is withdrawn.
    # ``track_state`` says whether this q is connected to the confirmed trajectory; these say whether
    # its branch label is decisive *here*.  Acquisition needs the difference to tell an undecided
    # label (more counting helps) from a decided label behind a path gap (more counting cannot).
    # ``None`` means a ledger predating these columns, i.e. unknown — never "clear".
    target_probability: float | None = None
    identity_entropy: float | None = None
    local_identity_clear: bool | None = None
    # A reduced-q origin the tracker infers rather than measures: unresolvable by construction, so
    # it carries no certifiable interval and is excluded from precision coverage.
    structural_endpoint_status: str | None = None
    is_reduced_q_origin: bool = False
    spectrum_ref: str | None = None
    predictive_ref: str | None = None
    # The authority-bound target projection resolved after triage + the connected tracker (ADR-019).
    # Written by :func:`_resolve_target_projections`, never by the fitter.
    target_projection_ref: str | None = None
    master_revision: str | None = None
    model_interval: tuple | None = None
    model_robust: bool | None = None
    model_entropy: float | None = None
    gamma_at_bound: bool = False
    linewidth_state: str | None = None
    pool_consistent: bool | None = None
    consistency_statistic: float | None = None
    consistency_pvalue: float | None = None
    consistency_alpha: float | None = None
    consistency_seed: int | None = None
    # STAT-1 additive provenance. Full records persist in session.json; results.csv receives compact
    # projections. Missing/legacy provenance is ``None`` and never invalidates the scientific result.
    fit_selection_semantics: dict | None = None
    interval_semantics: dict | None = None
    probability_semantics: dict | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["interval"] = list(self.interval) if self.interval is not None else None
        d["omega0_profile"] = list(self.omega0_profile) if self.omega0_profile is not None else None
        d["hesse_interval"] = list(self.hesse_interval) if self.hesse_interval is not None else None
        d["bayes_interval"] = list(self.bayes_interval) if self.bayes_interval is not None else None
        d["model_interval"] = list(self.model_interval) if self.model_interval is not None else None
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
    analysis_incomplete: bool = False  # checkpointed work exists; no ledger/results generation published


class AnalysisWorkIncomplete(RuntimeError):
    """The soft analysis deadline stopped dispatch before the canonical worklist completed."""


@dataclass
class _AnalysisDeadline:
    """Shared wave gate for scan and master fits in one top-level runner invocation."""

    deadline: float | None
    force_initial_wave: bool = False
    waves_started: int = 0

    def begin_wave(self) -> bool:
        now = time.monotonic()
        permitted = (self.deadline is None or now < self.deadline
                     or (self.force_initial_wave and self.waves_started == 0))
        if permitted:
            self.waves_started += 1
        return permitted

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return max(self.deadline - time.monotonic(), 0.0)


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
                            absolute_exposure_source=None, target_center=None,
                            acquisition_target_energy=None,
                            submitted_ncount: int | None = None) -> dict:
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
        norm = getattr(rec, "normalization", None)
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
            "estimator_kind": (s.fit_semantics or {}).get("estimator_kind"),
            "fit_kind": (s.fit_semantics or {}).get("fit_kind"),
            "constraint_origin": (s.fit_semantics or {}).get("aggregate_constraint_origin"),
            "selection_eligibility": (s.fit_semantics or {}).get("selection_eligibility"),
            "conditioning_fingerprint": (s.fit_semantics or {}).get(
                "conditioning_fingerprint"
            ),
        })
    return rows


def _model_set_fingerprint(kind: str, payload) -> str:
    return semantics_fingerprint(f"isar.{kind}/1", payload)


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
        offered_labels = [str(r.label) for r in results]
        scored_labels = [str(score.label) for score in selection.scores]
        denominator = [
            {"label": str(score.label), "aic": float(score.aic)}
            for score in selection.scores
        ]
        pool_labels = []
        by_label = {r.label: r for r in results}
        for score in selection.scores:
            if not score.survives:
                continue
            samples = (getattr(by_label.get(score.label), "within_model", None) or {}).get(
                "omega0_samples"
            )
            if samples is not None and np.size(samples) > 0:
                pool_labels.append(str(score.label))
        component_fingerprints = {
            "offered_model_set_fingerprint": _model_set_fingerprint(
                "offered_model_set", offered_labels
            ),
            "scored_model_set_fingerprint": _model_set_fingerprint(
                "scored_model_set", scored_labels
            ),
            "weight_denominator_fingerprint": _model_set_fingerprint(
                "akaike_weight_denominator", denominator
            ),
            "error_attachment_set_fingerprint": _model_set_fingerprint(
                "error_attachment_set", list(error_attachment_labels)
            ),
            "legacy_pool_set_fingerprint": _model_set_fingerprint(
                "legacy_pool_set", pool_labels
            ),
        }
        candidate_records = []
        for score in selection.scores:
            candidate_records.append(
                {
                    "label": str(score.label),
                    "cost": float(score.cost),
                    "effective_parameter_count": int(score.k),
                    "aic": float(score.aic),
                    "akaike_weight": float(score.weight),
                    "fit_semantics": score.fit_semantics,
                }
            )
        selection_payload = {
            **component_fingerprints,
            "ordinary_aic_revision": select.MODEL_SELECTION_REVISION,
            "weight_floor": float(select.DEFAULT_WEIGHT_FLOOR),
            "candidates": [
                {
                    "label": item["label"],
                    "cost": item["cost"],
                    "effective_parameter_count": item["effective_parameter_count"],
                    "akaike_weight": item["akaike_weight"],
                }
                for item in candidate_records
            ],
        }
        record = {
            "schema": FIT_SELECTION_SCHEMA,
            "candidate_generation_route": route,
            "model_weight_kind": "akaike_relative_weight" if selection.scores else "none",
            **component_fingerprints,
            "model_selection_fingerprint": semantics_fingerprint(
                "isar.model_selection_semantics/1", selection_payload
            ),
            "candidate_records": candidate_records,
            "authority_scopes": authority_scopes(
                single_fit_delivery="active",
                user_diagnostic="active",
            ),
        }
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
                "model_weight_kind": "akaike_relative_weight",
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
                "authority_scopes": authority_scopes(
                    single_fit_delivery="active",
                    legacy_master_certification="active",
                    stopping="active",
                    user_diagnostic="active",
                ),
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
                    "authority_scopes": authority_scopes(
                        single_fit_delivery="active",
                        legacy_master_certification=("active" if hesse_trusted else "inactive"),
                        stopping=("active" if hesse_trusted else "inactive"),
                        user_diagnostic="active",
                    ),
                }
            )

        bayes_item = (
            copy.deepcopy(source["bayes_interval"])
            if source.get("bayes_interval") is not None
            else unavailable("not_produced")
        )
        bayes_item["availability"] = bayes_interval is not None
        record = {
            "schema": INTERVAL_SCHEMA,
            "quantities": {
                "interval": interval_item,
                "omega0_profile": profile_item,
                "hesse_interval": hesse_item,
                "bayes_interval": bayes_item,
            },
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
        return validate_for_write(record, INTERVAL_SCHEMA)
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
                quantities["model_entropy"] = (
                    predictive_artifact.model_entropy_probability_semantics(
                        predictive_retained
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

    x_max = float(np.max(ctx.x_used))
    for name, value in best.params.items():
        sigma = best.param_errors.get(name)
        if sigma is None or not np.isfinite(sigma) or float(sigma) <= 0:
            continue
        value, sigma = float(value), float(sigma)
        if name.endswith(".Omega_d"):
            if min(value - loop.EPS_OMEGA_D, x_max - value) <= 2.0 * sigma:
                reasons.append("omega_d_within_2sigma_of_bound")
                break
    for name, value in best.params.items():
        sigma = best.param_errors.get(name)
        if sigma is None or not np.isfinite(sigma) or float(sigma) <= 0:
            continue
        value, sigma = float(value), float(sigma)
        if name.endswith(".Gamma"):
            if min(value - loop.GAMMA_FLOOR, loop.GAMMA_MAX - value) <= 2.0 * sigma:
                reasons.append("gamma_within_2sigma_of_bound")
                break
    runner_weights = sorted(
        (float(score.weight) for score in prelim.scores if score.label != prelim.best),
        reverse=True)
    if runner_weights and runner_weights[0] >= 0.10:
        reasons.append("runner_up_akaike_weight_ge_0.10")
    return reasons


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None,
            errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
            descriptor=None, structure_hint=None, bayes_opts=None,
            predictive_n_members: int | None = None, fit_role="provisional_scan"):
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
    acquisition, ~an order of magnitude faster). ``"auto"`` first performs that same HESSE point
    estimate, then attaches full errors to the *same candidate objects* only when a stable promotion
    trigger fires. ``boot_workers`` sets the parametric-bootstrap process-pool size (1 = serial; see
    :func:`run_batch`)."""
    if errors_tier not in ("auto", "full", "fast", "cheap", "bayes"):
        raise ValueError(f"unknown errors tier: {errors_tier!r}")
    requested_tier = errors_tier
    run_full = errors_tier in FULL_TIERS  # MINOS + bootstrap + ω₀ profile (authoritative interval)
    run_fast = errors_tier in ("fast", "auto")  # HESSE-first; auto may promote below
    run_bayes = errors_tier == "bayes"   # full + nested-sampling posterior on the prelim-best label
    _t_two_stage = time.perf_counter()
    # Candidate enumeration is point-estimation only. Full-tier MINOS is authoritative only after
    # the preliminary AIC pass and runs once per survivor in errors.attach_batch below.
    results, structure_skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, peaks=peaks, structure_hint=structure_hint,
        executor=boot_executor)
    two_stage_seconds = round(time.perf_counter() - _t_two_stage, 4)
    labels_before_guideline = [str(r.label) for r in results]
    # by_hyp spans both stages so errors.attach can find any surviving label's hypothesis.
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}
    by_hyp.update({h.label: h for h in enumerate_multimode(ctx)})

    warn = None
    guideline_changed_set = False
    if structure_labels:
        kept = [r for r in results if r.label in structure_labels]
        if kept:
            guideline_changed_set = [r.label for r in kept] != [r.label for r in results]
            results = kept
        else:
            warn = (f"guideline structures {list(structure_labels)} match no admissible "
                    f"{[r.label for r in results]}; kept all")

    if structure_skipped:
        multimode_route = "not_evaluated_structure_hint"
    elif any(int(getattr(r, "n_modes", 1)) > 1 for r in results):
        multimode_route = "guideline_forced" if peaks is not None and len(peaks) >= 2 \
            else "residual_triggered"
    else:
        multimode_route = "not_offered"
    candidate_generation_route = {
        "route": "structure_hint_shortcut" if structure_skipped else "full_two_stage",
        "supplied_hint": None if structure_hint is None else str(structure_hint),
        "hint_accepted": bool(structure_skipped),
        "guideline_structure_filter": (
            [str(label) for label in structure_labels] if structure_labels else []
        ),
        "guideline_changed_model_set": bool(guideline_changed_set),
        "labels_before_guideline": labels_before_guideline,
        "peak_hint_count": int(len(peaks)) if peaks is not None else 0,
        "multimode_route": multimode_route,
        "offered_labels": [str(r.label) for r in results],
    }

    fits = {r.label: r for r in results}

    prelim = select.select_models(results, stamp=False)
    escalation_reasons = (_auto_escalation_reasons(prelim, fits, ctx)
                          if errors_tier == "auto" else [])
    if errors_tier == "auto" and escalation_reasons:
        run_full = True
        run_fast = False
    effective_tier = ("full" if errors_tier == "auto" and run_full
                      else "fast" if errors_tier == "auto" else errors_tier)
    errors_attach_seconds = 0.0
    error_attachment_labels = []
    if run_full:
        _t_errors = time.perf_counter()
        survivor_items = []
        for label in prelim.survivors:
            r = fits.get(label)
            if r is not None and r.optimizer_converged and label in by_hyp:
                survivor_items.append((r, by_hyp[label], ctx))
        error_attachment_labels = [str(item[0].label) for item in survivor_items]
        errors.attach_batch(
            survivor_items, M=M, seed=seed, with_profile=True,
            bayes_label=(prelim.best if run_bayes else None),
            prior=_bayes_prior_for(prior, card), boot_workers=boot_workers,
            executor=boot_executor, boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        errors_attach_seconds = round(time.perf_counter() - _t_errors, 4)
    sel = select.select_models(results)
    candidate_generation_route["scored_labels"] = [str(score.label) for score in sel.scores]
    fit_selection_semantics = _build_selection_semantics(
        results,
        sel,
        candidate_generation_route,
        error_attachment_labels=error_attachment_labels,
    )
    sel.fit_selection_semantics = fit_selection_semantics
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
    if effective_tier == "cheap":
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
    profile_method_seconds = []
    profile_batch_seconds = []
    for label in prelim.survivors:
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
    # GUI Phase 0 (additive, no fitting/selection logic touched): the delivered fit's linear
    # amplitudes and the model-selection candidate table. Built from the FINAL selection (``sel``)
    # and the delivered ``best`` — so an escalated re-fit (its own fit_one call) carries its own.
    amplitudes = _amplitudes_of(best, by_hyp)
    model_manifest = _model_manifest_of(best, by_hyp)
    candidates = _candidate_rows(sel.scores, fits)
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
    fit_kind = best.fit_kind if best else "unconstrained"
    if sv.verdict == "signal_unmodeled":
        omega0 = omega0_err = interval = omega0_profile = hesse_interval = None
        interval_reliable = False
        fit_kind = "descriptor_only"
        # Not an ω₀ measurement → null the bayes interval/diagnostics too, same as the other intervals.
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
    )

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
        "bootstrap_M_requested": bootstrap_M_requested,
        "bootstrap_M_effective": bootstrap_M_effective,
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
    budget when scans run serially — e.g. the drive loop's one-scan-per-``isar run``). Note the
    asymmetry: the env always wins over ``default_n``, so a caller passing a deliberate serial
    default (e.g. ``1``) is silently overridden under pytest (where the repo-root conftest sets
    ``ISAR_BOOTSTRAP_WORKERS``) — don't call this expecting ``default_n=1`` to force serial."""
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
               bayes_opts=None, on_complete=None, deadline_gate=None) -> list:
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
            if deadline_gate is not None and not deadline_gate.begin_wave():
                raise AnalysisWorkIncomplete(
                    f"analysis deadline reached after {len(out)}/{len(arg_list)} scan fit units")
            (rec, det, q, T, sample, scan_id, card_, g_, runcal_, exclusions_, chash, ghash,
             M_, tier_, seed, bw, adaptive_, bopts_) = a
            entry = _process_one(rec, det, q, T, sample, scan_id, card_, g_, runcal_,
                                 exclusions_, chash, ghash, M=M_, seed=seed, errors_tier=tier_,
                                 boot_workers=bw, boot_executor=boot_executor,
                                 boot_adaptive=adaptive_, bayes_opts=bopts_)
            if on_complete is not None:
                on_complete(entry)
            out.append(entry)
        return out
    max_workers = min(scan_workers, len(arg_list))
    # Dispatch bounded waves.  map preserves order within each wave, so cached-unit reduction remains
    # canonical while the soft deadline can stop *new* work without killing active processes.
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        out = []
        for offset in range(0, len(arg_list), max_workers):
            if deadline_gate is not None and not deadline_gate.begin_wave():
                raise AnalysisWorkIncomplete(
                    f"analysis deadline reached after {len(out)}/{len(arg_list)} scan fit units")
            wave = arg_list[offset:offset + max_workers]
            for entry in ex.map(_process_one_task, wave):
                if on_complete is not None:
                    on_complete(entry)
                out.append(entry)
        return out


def run_batch(workspace: Workspace, data_paths, *, force=False, llm=False, M=200, scan_M=None,
              detectors=None, formats=("html", "markdown"), workers=None,
              errors_tier="full", scan_errors_tier=None, master_errors_tier=None,
              bayes_cli=None, timer=None,
              certification_manifest=None, analysis_allowance_seconds=None,
              finish_initial_wave=False) -> RunSummary:
    timer = timer or NullTimer()
    scan_M = 50 if scan_M is None else int(scan_M)
    if scan_M <= 0 or int(M) <= 0:
        raise ValueError("M and scan_M must be positive")
    scan_tier = errors_tier if scan_errors_tier is None else str(scan_errors_tier)
    master_tier = errors_tier if master_errors_tier is None else str(master_errors_tier)
    with timer.stage("workspace_load"):
        card, session = workspace.load()
    checkpoint_store = None
    if certification_manifest is not None:
        from isar.session.certification import CheckpointStore
        checkpoint_store = CheckpointStore(certification_manifest)
    master_certification = bool(
        checkpoint_store is not None and checkpoint_store.is_master_certification)
    if master_certification and force:
        raise ValueError("force=True is invalid for isar.certification/2 master-only runs")
    if master_certification:
        unresolved = unresolved_current_promotions(workspace.root, session)
        if unresolved:
            ids = ", ".join(str(record.get("promotion_id")) for record in unresolved)
            raise RuntimeError(
                f"certification refused: unresolved current master promotion(s): {ids}")
    deadline_gate = _AnalysisDeadline(
        None if analysis_allowance_seconds is None
        else time.monotonic() + max(float(analysis_allowance_seconds), 0.0),
        force_initial_wave=bool(finish_initial_wave))
    if not master_certification:
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
    if not master_certification:
        for f in plan.measurement_files + plan.companion_files:
            rp = str(Path(f).resolve())
            try:
                stat = Path(f).stat()
                # Merge, not replace: ingest may have written richer rows (n_scans/scan_ids) for
                # these keys — preserve them across an ordinary ingest-then-run workflow.
                session.files.setdefault(rp, {}).update(
                    {"size": stat.st_size, "mtime": stat.st_mtime,
                     "format": "companion" if f in plan.companion_files else "measurement"})
            except OSError as exc:
                log.warning("run: could not stat %s (%s); manifest row skipped", f, exc)

    # Context updates BEFORE measurements (calibration/alignment/environment).
    with timer.stage("route_context"):
        routed = router.route(records)
        ctx_lines = ([] if master_certification else router.apply_context_updates(
            routed, card, session, companion_files=plan.companion_files))

        # Optional LLM adjudication (off critical path; degrade, never stall).
        if not master_certification and (llm or (card.policy.adjudication or {}).get("enabled")):
            ctx_lines += _maybe_adjudicate(routed.measurements, card)

    # Per-unit data files for the GUI inspector, exactly as `isar ingest` writes them — so a run-only
    # workspace (never ingested) still has a counts plot per unit. Reduced+QC'd records; failures are
    # logged and skipped, never fatal. (unit_data.write_unit_data mirrors the ingest contract.)
    _unit_data_dir = getattr(workspace, "root", None)
    if _unit_data_dir is not None and not master_certification:
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

    if master_certification:
        fit_options = dict(checkpoint_store.manifest.get("fit_options") or {})
        return _run_master_certification(
            workspace, card, session, records, runcal, g, checkpoint_store,
            summary=summary, M=int(fit_options.get("M", M)), errors_tier=errors_tier,
            workers=_resolve_workers(workers),
            bayes_opts=dict(fit_options.get("bayes_opts") or _resolve_bayes_opts(bayes_cli)),
            deadline_gate=deadline_gate, timer=timer)

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

    # Ordinary deadline-bounded work uses the same immutable/checkpointed reduction contract as final
    # certification.  The fingerprint freezes every parsed record plus policy/state and the canonical
    # scan/master worklist; worker count and the remaining allowance may change harmlessly on resume.
    if checkpoint_store is None and analysis_allowance_seconds is not None:
        from isar.session.certification import CheckpointStore, create_analysis_manifest

        def _file_sha256(path):
            candidate = Path(path)
            return hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None

        master_worklist = []
        try:
            preview = collate_mod.collate(records, card)
            for group in sorted(getattr(preview, "groups", ()), key=lambda item: str(item.key)):
                merged = getattr(group, "merged", None)
                if merged is None:
                    continue
                master_worklist.append({
                    "key": list(group.key),
                    "members": [list(member) for member in merged.members],
                })
        except Exception as exc:  # noqa: BLE001 — the ordinary run keeps collation best-effort
            log.warning("could not preview master worklist for analysis checkpoints: %s", exc)

        manifest_path = create_analysis_manifest(workspace.root, {
            "tier": errors_tier,
            **({"scan_errors_tier": scan_tier} if scan_errors_tier is not None else {}),
            **({"master_errors_tier": master_tier} if master_errors_tier is not None else {}),
            "force": bool(force),
            "M": int(M),
            "scan_M": int(scan_M),
            "inputs": {
                "policy_sha256": _file_sha256(workspace.root / "card.policy.toml"),
                "state_sha256": _file_sha256(workspace.root / "card.state.json"),
                "records": [
                    {"experiment_id": rec.experiment_id, "scan_number": rec.scan_number,
                     "content_hash": content_hash(rec)}
                    for rec in records
                ],
            },
            "seeds": {"scan_seed_base": seed_base, "scan_rule": "6000 + scan_number",
                      "master_rule": "6000 + canonical_group_index",
                      "error_attachment_default": 12345,
                      "bayes_seed_offset": 1_000_003},
            "worklist": {
                "scans": [
                    {"unit_key": f"scan:{task[5]}", "content_hash": task[6],
                     "guidelines_hash": task[7], "seed": task[8]}
                    for task in tasks
                ],
                "masters": master_worklist,
            },
        })
        checkpoint_store = CheckpointStore(manifest_path)
        summary.context_lines.append(
            f"analysis checkpoints: {manifest_path.parent.relative_to(workspace.root)}")

    # --- Fit the fresh scans (parallel across scans when it pays; §performance). ---
    # Only the `full` tier bootstraps. When a full batch is small enough that the Layer-A scan pool
    # would idle cores (each per-scan bootstrap forced serial), bypass Layer A: fit serially in the
    # parent and funnel every bootstrap (scans + masters) through ONE shared pool at full budget
    # (the workorder — one spawn, not N). Large full reprocess batches (tasks > workers, where Layer
    # A already saturates) and the fast/cheap tiers (no bootstrap) keep the Layer-A parallel-fit path.
    scan_workers = _resolve_workers(workers)
    is_full = scan_tier in FULL_TIERS
    bayes_opts = _resolve_bayes_opts(bayes_cli)
    use_workorder = is_full and len(tasks) <= scan_workers
    if use_workorder:
        use_pool = False
        boot_workers = _resolve_boot_workers(scan_workers)
    else:
        use_pool = scan_workers > 1 and len(tasks) > 1
        boot_workers = 1 if use_pool else _resolve_boot_workers(scan_workers)
    # ``auto`` may promote after its HESSE-first point estimate; promoted bootstrap uses the same
    # deterministic adaptive rule as an explicitly full fit.
    scan_boot_adaptive = is_full or scan_tier == "auto"
    master_boot_adaptive = master_tier in FULL_TIERS or master_tier == "auto"
    master_boot_workers = _resolve_boot_workers(scan_workers)

    shared_pool = None
    if is_full and boot_workers > 1 and not use_pool:
        shared_pool = ProcessPoolExecutor(max_workers=boot_workers)   # scans + masters, one spawn
    try:
        with timer.stage("fit_scans", n_tasks=len(tasks), use_pool=use_pool,
                         boot_workers=boot_workers) as _m_fit:
            cached: dict[int, ScanEntry] = {}
            remaining = []
            remaining_indices = []
            if checkpoint_store is not None:
                for task_index, task in enumerate(tasks):
                    scan_id, chash, ghash = task[5], task[6], task[7]
                    entry = checkpoint_store.load(
                        f"scan:{scan_id}", content_hash=chash, guidelines_hash=ghash)
                    if entry is not None:
                        cached[task_index] = entry
                    else:
                        remaining.append(task)
                        remaining_indices.append(task_index)
            else:
                remaining = list(tasks)
                remaining_indices = list(range(len(tasks)))

            def checkpoint_entry(entry):
                if checkpoint_store is not None:
                    checkpoint_store.save(f"scan:{entry.scan_id}", entry)

            fresh_entries = _run_tasks(
                remaining, card, g, runcal, exclusions, M=scan_M, errors_tier=scan_tier,
                boot_workers=boot_workers, use_pool=use_pool,
                scan_workers=scan_workers, boot_executor=shared_pool,
                boot_adaptive=scan_boot_adaptive, bayes_opts=bayes_opts,
                on_complete=checkpoint_entry, deadline_gate=deadline_gate)
            cached.update(zip(remaining_indices, fresh_entries))
            entries = [cached[index] for index in range(len(tasks))]
            _m_fit["per_task"] = [
                {"scan_id": e.scan_id,
                 "seconds": e.provenance.get("fit_seconds"),
                 "two_stage_seconds": e.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": e.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": e.provenance.get("bootstrap_seconds"),
                 "minos_seconds": e.provenance.get("minos_seconds"),
                 "profile_seconds": e.provenance.get("profile_seconds"),
                 "bayes_seconds": e.provenance.get("bayes_seconds")}
                | {"bootstrap_M_requested": e.provenance.get("bootstrap_M_requested"),
                   "bootstrap_M_effective": e.provenance.get("bootstrap_M_effective")}
                for e in entries if e.provenance.get("fit_seconds") is not None]
        completed_master_keys: list[str] = []
        _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                          workspace, force=force, M=M, seed_base=seed_base, errors_tier=master_tier,
                          master_boot_workers=master_boot_workers, boot_executor=shared_pool,
                          boot_adaptive=master_boot_adaptive, scan_workers=scan_workers,
                          bayes_opts=bayes_opts, timer=timer, source_by_name=source_by_name,
                          checkpoint_store=checkpoint_store,
                          completed_master_keys=completed_master_keys,
                          deadline_gate=deadline_gate)
        if checkpoint_store is not None:
            checkpoint_store.mark_complete(
                unit_keys=[f"scan:{entry.scan_id}" for entry in entries] + completed_master_keys)
    except AnalysisWorkIncomplete as exc:
        summary.analysis_incomplete = True
        summary.context_lines.append(f"checkpointed incomplete analysis: {exc}")
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
        complete = point.get("branch_posterior")
        probabilities = None
        if isinstance(complete, dict):
            usable = {}
            for key, value in complete.items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(numeric) and numeric >= 0.0:
                    usable[str(key)] = numeric
            total = sum(usable.values())
            if usable and total > 0.0:
                probabilities = {key: value / total for key, value in usable.items()}
        if probabilities is None:
            # Compatibility for points persisted before complete vectors were additive.
            probabilities = {branch_id: probability, BA.NEW: 1.0 - probability}
        entropy = -sum(value * math.log(value) for value in probabilities.values() if value > 0.0)
        point_rows.append({
            "sample": point.get("sample"), "t_bin": point.get("t_bin", point.get("T")),
            "q": list(point.get("q") or []), "omega0": point.get("omega0"),
            "sigma": BA.SIGMA_SLACK / math.sqrt(max(probability, 1e-9)),
            "branch_id": branch_id, "label_probabilities": probabilities,
            "assignment_entropy": entropy,
            "p_is_target": float(probabilities.get(target, 0.0)) if target is not None else 0.0,
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


def _tracked_selector(card) -> bool:
    return _campaign_selector(card)[0] == "tracked_branch"


def _mode_candidates(scan_id: str, detector: str, outcome: dict) -> list[dict]:
    """Materialise every fitted mode with its own uncertainty and provenance.

    Full profile/bootstrap/Bayes intervals currently belong to the fit's scalar primary mode. They
    are copied only to that matching candidate; a secondary receives its own HESSE interval, never
    the primary mode's interval (which would be numerically tidy and scientifically wrong).
    """
    # A tracking revision may have rewritten the backwards-compatible scalar omega0 to a secondary
    # mode. The previously materialised table is immutable fit evidence and remembers which mode
    # actually owned the fitter's profile/bootstrap/Bayes intervals. Reuse it on carry/no-op reruns;
    # a genuine refit replaces the whole outcome and arrives without this table.
    stored = outcome.get("branch_candidates")
    if isinstance(stored, list) and stored:
        rows = []
        for candidate in stored:
            if not isinstance(candidate, dict):
                continue
            row = dict(candidate)
            # Assignment probabilities are derived state, never fit evidence.  Keeping them in the
            # next Viterbi input would let yesterday's assignment masquerade as a new local label.
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                row.pop(field, None)
            rows.append(row)
        if rows:
            return rows
    modes = list(outcome.get("modes") or [])
    if not modes and outcome.get("omega0") is not None:
        modes = [{"omega0": outcome.get("omega0"), "omega0_err": outcome.get("omega0_err"),
                  "gamma": outcome.get("gamma"), "amplitude": None}]
    primary = outcome.get("omega0")
    rows = []
    for index, mode in enumerate(modes):
        energy = mode.get("omega0")
        if energy is None:
            continue
        try:
            energy = float(energy)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(energy):
            continue
        error = mode.get("omega0_err")
        try:
            error = float(error)
        except (TypeError, ValueError):
            error = None
        if error is not None and (not np.isfinite(error) or error <= 0.0):
            error = None
        is_primary = primary is not None and abs(float(primary) - energy) <= 1e-6
        hesse = ([energy - error, energy + error] if error is not None else None)
        row = {
            "candidate_ref": f"{scan_id}:{detector}:mode:{index}", "source_scan_id": scan_id,
            "detector": detector, "mode_index": index, "energy_meV": energy,
            "omega0_err": error, "sigma_meV": error, "gamma": mode.get("gamma"),
            "gamma_err": mode.get("gamma_err"), "amplitude": mode.get("amplitude"),
            "interval": (list(outcome.get("interval")) if is_primary and outcome.get("interval") else None),
            "profile_interval": (list(outcome.get("omega0_profile"))
                                 if is_primary and outcome.get("omega0_profile") else None),
            "hesse_interval": (list(outcome.get("hesse_interval"))
                               if is_primary and outcome.get("hesse_interval") else hesse),
            "bayes_interval": (list(outcome.get("bayes_interval"))
                               if is_primary and outcome.get("bayes_interval") else None),
            "reliable": bool(outcome.get("interval_reliable", True)),
            "pooled": bool(outcome.get("pooled")), "n_members": outcome.get("n_members"),
            "fit_kind": outcome.get("fit_kind"), "model": outcome.get("best_label"),
            "verdict": outcome.get("verdict"), "modality": outcome.get("modality"),
        }
        rows.append(row)
    return rows


def _path_index(q: list, path: list[list[float]], tolerance: float = 1e-5) -> int | None:
    if not q or not path:
        return None
    distances = [math.dist([float(value) for value in q], point) for point in path]
    index = int(np.argmin(distances))
    return index if distances[index] <= tolerance else None


# A clean null answers "no rival in this window" but does not measure detection power, so its evidence
# of absence is real but not certain. Until PR3 computes a predicted detection power per probe, a
# trustworthy clean null relaxes the missing-branch pull by this bounded, conservative fraction rather
# than fully cancelling it -- the null moves the posterior without ever claiming the branch cannot exist.
CLEAN_NULL_DETECTION_SENSITIVITY = 0.5


def _clean_null_window(outcome: dict) -> tuple[float, float, float] | None:
    """The (lo, hi, sensitivity) negative-evidence window for a trustworthy clean-null auxiliary probe.

    A clean null searched [search_energy_lo, search_energy_hi] and found no mode there; the tracker
    reads this as "the branch predicted in this window is confirmed absent, not merely unmeasured"
    (:func:`branch_tracking._null_relaxation`). Returns ``None`` for anything that is not a trustworthy
    clean null -- a broken analysis, a detection, or a probe with no recorded window -- so a failure can
    never masquerade as evidence of absence.
    """
    if outcome.get("evidence_sink") != AUXILIARY_TRACK_NODE:
        return None
    if realized_outcome_for(outcome.get("disposition"),
                            has_candidate=bool(outcome.get("branch_candidates"))) != CANDIDATE_NOT_DETECTED:
        return None
    lo, hi = outcome.get("search_energy_lo"), outcome.get("search_energy_hi")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
        return None
    return (lo, hi, CLEAN_NULL_DETECTION_SENSITIVITY)


def _tracked_authorities(session: SessionState, path: list[list[float]]) -> tuple[list[dict], dict]:
    """Return one authoritative fit per path point and all outcome references.

    Only a published pooled master may be an identity authority.  Physical fits remain in ``refs`` so
    the GUI can display their candidate tables, but they never fill a gap in the authoritative track
    and never vote on a master assignment.  Internal consistency ranks masters here rather than
    excluding them — an inconsistent pool is still fitted evidence of *which modes exist*, and the
    gates that consistency does own live elsewhere (see the ``pool_consistent`` comment below).

    A scan acquired as an ``auxiliary_track_node`` probe is the one exception, and a deliberately
    narrow one.  It was fired to decide *which mode* a candidate is, not to deliver a better ω₀, so it
    is never pooled and never certification-eligible; its fitted modes join the observation at its path
    index as extra candidates tagged ``evidence_source="auxiliary_probe"``.  It can therefore change the
    identity assigned at that point and can never supply the point's ω₀ or become its ``authority``.
    It merges only where a pooled master already holds the index: alone it would be the *sole* identity
    evidence at that point, which is precisely what the rule above exists to prevent.
    """
    by_index: dict[int, list[tuple]] = {}
    auxiliary_by_index: dict[int, list[dict]] = {}
    null_windows_by_index: dict[int, list[tuple]] = {}
    refs: dict[str, tuple] = {}
    for order, entry in enumerate(session.scans.values()):
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None
            key = f"{entry.scan_id}:{detector}"
            refs[key] = (entry, detector, outcome)
            index = _path_index(outcome.get("q") or [], path)
            # A clean null from an auxiliary probe fits no mode, so it never becomes a candidate; it is
            # negative evidence that the branch predicted in its searched window is absent, carried to
            # the tracker as a ``null_window`` that relaxes (never forces) that branch's missing pull.
            null_window = _clean_null_window(outcome)
            if index is not None and null_window is not None:
                null_windows_by_index.setdefault(index, []).append(null_window)
                continue
            if (index is not None and candidates
                    and outcome.get("evidence_sink") == AUXILIARY_TRACK_NODE):
                for candidate in candidates:
                    tagged = dict(candidate)
                    tagged["evidence_source"] = "auxiliary_probe"
                    try:
                        search_window = branch_tracking.normalize_energy_domain(
                            (outcome.get("search_energy_lo"), outcome.get("search_energy_hi")))
                    except ValueError:
                        search_window = ()
                    if search_window:
                        tagged["authorized_energy_window_meV"] = list(search_window[0])
                    auxiliary_by_index.setdefault(index, []).append(tagged)
                continue
            if index is not None and candidates and outcome.get("pooled"):
                exposure = outcome.get("effective_exposure")
                try:
                    exposure = float(exposure)
                except (TypeError, ValueError):
                    exposure = 0.0
                # A ``pool_consistent is False`` master's members disagree on ω₀, but the acoustic mode
                # is still fitted — so its candidate is valid IDENTITY/connectivity evidence and must
                # not be dropped. Dropping it emptied the q's observation, broke the tracked trajectory,
                # and cascaded every confirmed downstream point to ``blocked`` (AP8). It stays out of
                # the certifiable interval (stopping.py ``_master_hw``) and the dispersion prior
                # (``_rebuild_*_prior``) independently, so keeping it here cannot certify a bad ω₀ or
                # pollute the prior. When a q holds both a consistent and an inconsistent master the
                # rank prefers the consistent one rather than excluding the other.
                consistent = outcome.get("pool_consistent") is not False
                rank = (int(consistent), int(outcome.get("n_members") or 0), exposure, order)
                by_index.setdefault(index, []).append((rank, key, candidates))
    observations = []
    for index in range(len(path)):
        choices = by_index.get(index) or []
        null_windows = null_windows_by_index.get(index) or []
        if not choices:
            # Any auxiliary probe at this index is deliberately dropped: see the docstring. It stays
            # in ``refs`` for display and its beliefs update elsewhere, but it does not become the
            # identity evidence for a point no master has measured. A clean-null probe is the one
            # exception that leaves a trace here: its negative evidence rides on ``null_windows``.
            observation = {"candidates": [], "authority": None}
        else:
            _rank, key, candidates = max(choices, key=lambda item: item[0])
            _entry, _detector, outcome = refs[key]
            merged = [dict(candidate) for candidate in candidates]
            merged.extend(auxiliary_by_index.get(index) or [])
            observation = {"candidates": merged, "authority": key,
                           "gauss_fwhm": (outcome.get("gauss_fwhm")
                                          or (outcome.get("calib") or {}).get("gauss_fwhm"))}
        # Only present when a clean null was recorded here: a campaign that never used the auxiliary
        # channel must produce observations byte-identical to the pre-channel ones, so its identity
        # signature is unperturbed.
        if null_windows:
            observation["null_windows"] = null_windows
        observations.append(observation)
    return observations, refs


def _tracked_authority_signature(
        observations: list[dict], *, target_branch: str, period: float | None,
        hypotheses: list[dict], entropy_stop: float, maximum_bridge_steps: int,
        energy_domain_meV=None,
        scorer_revision: int = branch_tracking.BRANCH_TRACKING_SCORER_REVISION,
) -> str:
    """Stable identity input; derived probabilities are deliberately excluded."""
    authorities = []
    for observation in observations:
        candidates = []
        for candidate in observation.get("candidates") or []:
            fitted = dict(candidate)
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                fitted.pop(field, None)
            candidates.append(fitted)
        authority = {
            "authority": observation.get("authority"),
            "gauss_fwhm": observation.get("gauss_fwhm"),
            "candidates": candidates,
        }
        # Negative evidence is part of the identity basis: a new clean null changes the question being
        # asked at this q, so an unchanged basis (Increment C) cannot re-offer the same probe. Added only
        # when present, so a campaign with no probes hashes byte-identically to the pre-channel signature.
        if observation.get("null_windows"):
            authority["null_windows"] = observation["null_windows"]
        authorities.append(authority)
    return canonical_hash({
        "authorities": authorities, "target_branch": target_branch,
        "period": period, "hypotheses": hypotheses,
        "assignment_entropy_stop_nats": float(entropy_stop),
        "maximum_bridge_steps": int(maximum_bridge_steps),
        "energy_domain_meV": [
            list(interval) for interval in branch_tracking.normalize_energy_domain(energy_domain_meV)],
        "scorer_revision": int(scorer_revision),
    })


def _materialize_branch_candidates(session: SessionState) -> None:
    """Persist unordered fitted-mode evidence independently of target delivery.

    Local branch triage may deliberately clear the scalar omega0.  Keeping the immutable candidate
    table means the viewer and a later connected tracker can still see what was actually fitted.
    """
    for entry in session.scans.values():
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None


AUXILIARY_EVIDENCE_SOURCE = "auxiliary_probe"


def _is_auxiliary_candidate(candidate: dict) -> bool:
    """Whether this fitted mode came from an auxiliary probe rather than the point's own master.

    The tag is applied in :func:`_tracked_authorities` when the probe's modes are merged onto the
    canonical observation, and absence of it means canonical.
    """
    return (candidate or {}).get("evidence_source") == AUXILIARY_EVIDENCE_SOURCE


def _select_mode(outcome: dict, candidate: dict, candidate_index: int, assignment: dict,
                 revision: int, target_branch: str = "optic") -> None:
    if _is_auxiliary_candidate(candidate):
        # Defence in depth behind _apply_tracked_branch's check. An auxiliary probe is never pooled
        # and never certification-eligible, so copying its energy and intervals onto a pooled master
        # builds a row that certifies the target from exposure the target window never spent.
        raise ValueError(
            f"auxiliary candidate {candidate.get('candidate_ref')!r} cannot supply a target "
            "delivery; withhold the delivery instead (see _withhold_tracked_delivery)")
    outcome["selected_candidate_index"] = int(candidate_index)
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = candidate.get("candidate_ref")
    outcome["omega0"] = candidate.get("energy_meV")
    outcome["omega0_err"] = candidate.get("omega0_err")
    outcome["gamma"] = candidate.get("gamma")
    outcome["interval"] = candidate.get("interval")
    outcome["omega0_profile"] = candidate.get("profile_interval")
    outcome["hesse_interval"] = candidate.get("hesse_interval")
    outcome["bayes_interval"] = candidate.get("bayes_interval")
    outcome["interval_reliable"] = bool(candidate.get("reliable", False))
    probabilities = dict(candidate.get("probabilities") or {})
    outcome["branch_id"] = target_branch
    outcome["branch_posterior"] = probabilities
    outcome["branch_weight"] = float(probabilities.get(target_branch, 0.0))
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # Auxiliary withholding is a temporary delivery gate, not a durable property of the q. A later
    # canonical selection can immediately reuse the fit's preserved model delivery without a refit.
    outcome["target_delivery_eligible"] = True
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _carry_local_identity(outcome: dict, assignment: dict) -> None:
    """Carry the tracker's local identity verdict onto the outcome, selected or not.

    These survive :func:`_clear_tracked_selection` on purpose.  That helper withdraws the *scalar
    selection* so nothing stale is certifiable, but acquisition needs the local identity evidence
    precisely when the selection is absent — that is how it tells an undecided label (count more)
    from a decided label behind a path gap (counting cannot help).
    """
    outcome["target_probability"] = assignment.get("target_probability")
    outcome["identity_entropy"] = assignment.get("identity_entropy")
    clear = assignment.get("local_identity_clear")
    outcome["local_identity_clear"] = None if clear is None else bool(clear)


def _clear_tracked_selection(outcome: dict, assignment: dict, revision: int) -> None:
    """Persist a missing/ambiguous target without leaving an old scalar selection certifiable."""
    outcome["selected_candidate_index"] = None
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = None
    outcome["branch_id"] = None
    outcome["branch_posterior"] = None
    outcome["branch_weight"] = None
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # A newly ambiguous/missing assignment also withdraws any earlier auxiliary-only identity
    # conclusion. Keep a pre-existing delivery gate closed until a canonical selection explicitly
    # re-enables it, but do not keep claiming a branch identity the tracker no longer confirms.
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    # The immutable branch_candidates table remains the complete fit evidence and can restore these
    # mode-specific values after a later reassignment.  The scalar compatibility view must abstain.
    for field in ("omega0", "omega0_err", "gamma", "interval", "omega0_profile",
                  "hesse_interval", "bayes_interval"):
        outcome[field] = None
    outcome["interval_reliable"] = False
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _withhold_tracked_delivery(outcome: dict, candidate: dict, assignment: dict, revision: int,
                               target_branch: str) -> None:
    """Record a confirmed identity whose only supporting mode came from an auxiliary probe.

    This is neither a selection nor an ambiguity. The tracker reached a conclusion — that is what the
    probe was fired to buy, and discarding it would waste the beam time — but the mode it concluded
    from lives in a scan that is never pooled and never certification-eligible. So the *answer* is
    kept and the *number* is withheld: this point has no canonical measurement of the branch it has
    now identified, and the honest next action is an ordinary target scan at that energy.

    ``branch_id`` is deliberately NOT set. On a master that field means "this row supplies the target
    delivery", and every reader predating this state would take it at face value. The conclusion is
    carried by ``identity_branch_id`` instead, alongside the local-identity fields acquisition already
    reads when a selection is absent.
    """
    _clear_tracked_selection(outcome, assignment, revision)
    outcome["target_delivery_eligible"] = False
    outcome["identity_branch_id"] = target_branch
    outcome["delivery_withheld_reason"] = "auxiliary_only_selection"
    # Preserve the fit's model delivery. The eligibility flag gates both scalar and model paths, and a
    # later canonical selection can therefore restore certification without an unnecessary refit.
    log.warning(
        "withholding target delivery at q=%s: the tracked %s identity rests on auxiliary candidate "
        "%r, which is never pooled and never certification-eligible. Identity retained; this point "
        "still needs a canonical target measurement.",
        outcome.get("q"), target_branch, candidate.get("candidate_ref"))


def _rebuild_tracked_prior(card, session: SessionState, track: dict, observations: list[dict],
                           refs: dict, *, t_round: float) -> None:
    source_ids = {key.rsplit(":", 1)[0] for row in observations
                  for key in [row.get("authority")] if key}
    # Replace only derived points sourced from fits participating in this tracked campaign. Raw fit
    # evidence remains in the ledger/candidate tables and reassignment history remains in the track.
    card.state.dispersion_prior = [point for point in (card.state.dispersion_prior or [])
                                   if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    revision = int(track.get("revision", 0))
    for assignment, observation in zip(track.get("assignments") or [], observations):
        if assignment.get("track_state") != "confirmed" or not observation.get("authority"):
            continue
        _entry, _detector, outcome = refs[observation["authority"]]
        if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                or not _master_prior_admissible(_entry, outcome)
                or outcome.get("pool_consistent") is False):
            continue
        for branch in ("optic", "acoustic"):
            candidate_index = assignment.get(f"{branch}_candidate_index")
            if candidate_index is None:
                continue
            candidate = assignment["candidates"][candidate_index]
            if _is_auxiliary_candidate(candidate):
                # The prior accepts only clean single-answer folds from pooled masters. This candidate
                # was fitted by a probe that is not pooled at all, and the outcome gating this loop is
                # the *canonical* authority — so without this check the auxiliary energy would enter
                # the running dispersion prior wearing the master's fold provenance.
                continue
            probability = float((candidate.get("probabilities") or {}).get(branch, 0.0))
            if probability < branch_tracking.CONFIRMED_PROBABILITY:
                continue
            session_prior.add_point(
                card.state,
                scan_id=f"{candidate['source_scan_id']}#tracked:{branch}",
                sample=outcome.get("sample"), T=outcome.get("T"), q=outcome.get("q"),
                omega0=candidate.get("energy_meV"), modality="fold", verdict="resolved",
                gamma=candidate.get("gamma"), gamma_err=candidate.get("gamma_err"),
                intensity=(outcome.get("intensity") if branch == "optic" else None),
                fit_kind=outcome.get("fit_kind"), branch_id=branch, weight=probability,
                branch_posterior=candidate.get("probabilities"),
                assignment_entropy=assignment.get("assignment_entropy"), t_round=t_round,
                require_fold=True)
    for point in card.state.dispersion_prior:
        if "#tracked:" in str(point.get("scan_id", "")):
            point["track_revision"] = revision


def _authorized_tracking_domain(target: dict, observations: list[dict]):
    """Normalized union of policy- and provenance-authorized tracking windows.

    Candidate energies never contribute to this union. An auxiliary candidate with missing or malformed
    acquisition provenance remains visible evidence, but gains no support outside the declared target
    and identity bands.
    """
    intervals = []
    target_band = target.get("energy_search_meV")
    if target_band is None:
        intervals.extend(branch_tracking.normalize_energy_domain(None))
    else:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(target_band))
        except ValueError as exc:
            raise ValueError(
                f"malformed persisted energy_search_meV tracking authorization: {target_band!r}"
            ) from exc
    identity_band = target.get("identity_search_meV")
    if identity_band is not None:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(identity_band))
        except ValueError:
            log.warning("ignoring malformed persisted identity_search_meV tracking authorization: %r",
                        identity_band)
    for observation in observations:
        for candidate in observation.get("candidates") or []:
            if not _is_auxiliary_candidate(candidate):
                continue
            window = candidate.get("authorized_energy_window_meV")
            if window is None:
                continue
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(window))
            except ValueError:
                log.warning("ignoring malformed auxiliary candidate tracking authorization: %r", window)
        for null_window in observation.get("null_windows") or []:
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(null_window[:2]))
            except (TypeError, ValueError):
                log.warning("ignoring malformed auxiliary null-window tracking authorization: %r",
                            null_window)
    return branch_tracking.normalize_energy_domain(intervals)


def _apply_tracked_branch(session: SessionState, card, *, t_round: float) -> dict | None:
    if not _tracked_selector(card):
        return None
    target = getattr(card.state, "campaign_target", None) or {}
    path = target.get("path") or []
    if not path:
        log.warning("tracked_branch requested without a persisted objective path; assignment deferred")
        return None
    path = [[float(value) for value in q] for q in path]
    observations, refs = _tracked_authorities(session, path)
    policy = (getattr(card.state, "hypothesis_snapshot", None) or {}).get("steering") or {}
    previous = getattr(card.state, "branch_tracks", None) or {}
    target_branch = _campaign_target(card)[0]
    period = _dispersion_period(card)
    hypotheses = _campaign_hypotheses(card)
    entropy_stop = float(policy.get("assignment_entropy_stop_nats", 0.2))
    maximum_bridge_steps = int(policy.get("maximum_bridge_steps", 1))
    energy_domain = _authorized_tracking_domain(target, observations)
    authority_signature = _tracked_authority_signature(
        observations, target_branch=target_branch, period=period, hypotheses=hypotheses,
        entropy_stop=entropy_stop, maximum_bridge_steps=maximum_bridge_steps,
        energy_domain_meV=energy_domain,
        scorer_revision=branch_tracking.BRANCH_TRACKING_SCORER_REVISION)
    if (previous.get("authoritative_evidence_signature") == authority_signature
            and previous.get("target_branch") == target_branch
            and previous.get("assignments") is not None):
        track = previous
    else:
        track = branch_tracking.track_path(
            path, observations, period=period, hypotheses=hypotheses,
            target_branch=target_branch, previous=previous,
            assignment_entropy_stop_nats=entropy_stop,
            maximum_bridge_steps=maximum_bridge_steps,
            energy_domain_meV=energy_domain)
        track["authoritative_evidence_signature"] = authority_signature
    revision = int(track["revision"])
    for assignment, observation in zip(track["assignments"], observations):
        key = observation.get("authority")
        selected = assignment.get("selected_candidate_index")
        if not key:
            continue
        _entry, _detector, outcome = refs[key]
        outcome["branch_candidates"] = assignment["candidates"]
        # A MAP state is still only an assignment hypothesis.  Ambiguous/provisional/blocked target
        # rows remain candidate evidence and must not publish a scalar target delivery.
        if selected is None or assignment.get("track_state") != "confirmed":
            _clear_tracked_selection(outcome, assignment, revision)
            for _other_key, (_other_entry, _other_detector, other) in refs.items():
                if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                    continue
                local_assignment = dict(assignment)
                local_assignment["track_state"] = "individual"
                _clear_tracked_selection(other, local_assignment, revision)
            continue
        candidate = assignment["candidates"][selected]
        if _is_auxiliary_candidate(candidate):
            _withhold_tracked_delivery(outcome, candidate, assignment, revision, target_branch)
        else:
            _select_mode(outcome, candidate, selected, assignment, revision, target_branch)
        # Individual fits at the same q remain muted evidence but follow the current identity for
        # backwards-compatible scalar columns, using their own nearest fitted mode. Referenced against
        # the selected candidate's energy rather than ``outcome["omega0"]``: identical on the delivery
        # path, and still the identity the tracker concluded when the delivery was withheld.
        reference_energy = float(candidate["energy_meV"])
        for _other_key, (_other_entry, _other_detector, other) in refs.items():
            if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                continue
            candidates = other.get("branch_candidates") or []
            if not candidates:
                continue
            nearest = min(range(len(candidates)), key=lambda index: abs(
                float(candidates[index]["energy_meV"]) - reference_energy))
            local_assignment = dict(assignment)
            local_assignment["track_state"] = "individual"
            _select_mode(other, candidates[nearest], nearest, local_assignment, revision,
                         target_branch)
    _rebuild_tracked_prior(card, session, track, observations, refs, t_round=t_round)
    card.state.branch_tracks = track
    return track


def _rebuild_pooled_prior(card, session: SessionState, session_prior, *, t_round: float) -> None:
    """Rebuild the non-tracked running dispersion prior from pooled masters only."""
    if _tracked_selector(card):
        return
    source_ids = set(session.scans)
    card.state.dispersion_prior = [
        point for point in (card.state.dispersion_prior or [])
        if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    for entry in sorted(session.scans.values(), key=lambda item: item.scan_number):
        for outcome in entry.detectors.values():
            if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                    or not _master_prior_admissible(entry, outcome)
                    or outcome.get("pool_consistent") is False):
                continue
            session_prior.add_point(
                card.state, scan_id=entry.scan_id, sample=outcome.get("sample"),
                T=outcome.get("T"), q=outcome.get("q"), omega0=outcome.get("omega0"),
                modality="fold", verdict=outcome.get("verdict"), gamma=outcome.get("gamma"),
                intensity=outcome.get("intensity"), fit_kind=outcome.get("fit_kind"),
                branch_id=outcome.get("branch_id"), weight=outcome.get("branch_weight"),
                branch_posterior=outcome.get("branch_posterior"),
                assignment_entropy=outcome.get("assignment_entropy"), t_round=t_round,
                require_fold=True)


def _master_prior_admissible(entry: ScanEntry, outcome: dict) -> bool:
    """Full errors or a trusted HESSE master may seed the prior; cheap/untrusted fits may not."""
    provenance = entry.provenance or {}
    tier = provenance.get("errors_tier_effective") or provenance.get(
        "analysis_tier_effective") or provenance.get("analysis_tier")
    # Additive compatibility: ledgers predating tier provenance already passed the resolved-fold
    # gate and must not silently lose their prior points merely by being reopened on newer code.
    if tier is None:
        return True
    if tier in FULL_TIERS:
        return True
    return tier == "fast" and bool(outcome.get("hesse_trusted"))


def _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                      workspace, *, force, M, seed_base, errors_tier, master_boot_workers,
                      boot_executor, boot_adaptive, scan_workers=1, bayes_opts=None, timer=None,
                      source_by_name=None, checkpoint_store=None, completed_master_keys=None,
                      deadline_gate=None):
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
            summary.outcomes.append((scan, _coerce_fit_outcome(oc)))

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
                                     data_dir=workspace.root / "data",
                                     M=M, seed_base=seed_base, errors_tier=errors_tier, force=force,
                                     boot_workers=master_boot_workers, boot_executor=boot_executor,
                                     boot_adaptive=boot_adaptive, scan_workers=scan_workers,
                                     bayes_opts=bayes_opts, per_master=per_master,
                                     checkpoint_store=checkpoint_store,
                                     completed_master_keys=completed_master_keys,
                                     deadline_gate=deadline_gate)
            _m_masters["n_masters"] = n_masters
            _m_masters["per_master"] = per_master
            if n_masters:
                summary.context_lines.append(f"fit {n_masters} merged master scan(s)")
        except Exception as exc:  # noqa: BLE001 — ordinary master fitting is additive
            # Certification must fail closed: suppressing a corrupt/mismatched checkpoint here
            # would let the runner publish a superficially complete revision with a missing unit.
            if checkpoint_store is not None:
                raise
            summary.context_lines.append(f"master fitting skipped: {exc}")

    if _campaign_hypotheses(card):
        _materialize_branch_candidates(session)
    _rebuild_pooled_prior(card, session, session_prior, t_round=t_round)
    # Identity assignment is deliberately after pooled-master updates. It reads the complete
    # candidate field, may revise earlier labels, then rebuilds the connected fold-only prior.
    track = _apply_tracked_branch(session, card, t_round=t_round)
    # ADR-019: only now is the target authoritative — triage has had its say and the tracker has
    # reassigned or cleared every selection. The projection is resolved against `/2`'s stored
    # per-draw mode energies, so a track revision changes it without any refit.
    _resolve_target_projections(session, workspace.root / "data")
    if track is not None:
        summary.context_lines.append(
            f"branch track revision {track['revision']}: "
            f"{len(track['frontiers']['confirmed_indices'])} connected point(s)")
        if track["frontiers"].get("frontier_blocked"):
            summary.context_lines.append("branch_ambiguity; frontier_blocked")

    # Persist cross-q views only after the authoritative prior has been rebuilt.
    _store_branch_models(card.state, t_round)
    _store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    _update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))

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


_MASTER_FIT_CONTRACT = "isar.master_fit/2"


def _master_fit_fingerprint(ms, card, calres, g, prior, *, errors_tier, M, seed,
                            boot_adaptive, bayes_opts) -> str:
    """Fingerprint the complete numerical input closure of one pooled-master fit."""
    from isar.session.certification import master_fit_fingerprint

    state = getattr(card, "state", None)
    target = getattr(state, "campaign_target", None) or {}
    frozen_state = {
        "dispersion_prior": getattr(state, "dispersion_prior", None),
        "branch_models": getattr(state, "branch_models", None),
        "branch_belief": getattr(state, "branch_belief", None),
        "branch_belief_models": getattr(state, "branch_belief_models", None),
        "branch_tracks": getattr(state, "branch_tracks", None),
        "hypothesis_snapshot": getattr(state, "hypothesis_snapshot", None),
        "campaign_target": target,
    }
    return master_fit_fingerprint(
        spectrum_digest=collate_mod.merged_spectrum_digest(ms),
        policy=card.policy,
        resolved_guidelines={
            "min_fit_points": g.min_fit_points,
            "monitor_frac": g.monitor_frac,
        },
        resolved_calibration=calres.summary(), frozen_state=frozen_state,
        errors_tier=errors_tier, M=M, seed=seed,
        boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)


def _persist_master_predictive(data_dir, payload: dict) -> str:
    """Blob-first, content-addressed publication of one predictive ensemble."""
    return _persist_content_addressed(data_dir, "master_predictive", payload)


def _persist_content_addressed(data_dir, subdirectory: str, payload: dict) -> str:
    """Blob-first, content-addressed publication of one JSON side artifact."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(data_dir) / subdirectory
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.json"
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                log.warning("could not remove incomplete predictive artifact %s: %s",
                            temporary, cleanup_error)
            raise
    return target.relative_to(Path(data_dir).parent).as_posix()


def _predicted_master_analysis_seconds(payload: dict) -> float:
    """Deterministic seconds estimate for ranking future master-analysis work.

    Observed fit wall time depends on worker placement and host load, so persisting it inside a
    content-addressed scientific artifact made serial and parallel reductions hash differently.
    The stable proxy scales with the plausible-model parameter dimensions and evaluated energy
    bins—the dominant repeatable work dimensions—while remaining explicitly an estimate.
    """
    complexity = 0
    for model in payload.get("models") or ():
        n_parameters = max(len(model.get("parameter_names") or ()), 1)
        expected = model.get("expected_counts") or ()
        n_points = len(expected[0]) if expected and isinstance(expected[0], list) else 1
        complexity += n_parameters * max(n_points, 1)
    return round(max(0.1, 0.1 + 0.00015 * complexity), 4)


_PROMOTION_CANDIDATE_IDENTITY_FIELDS = (
    "label",
    "k",
    "ndof",
    "cost",
    "cost_per_dof",
    "n_modes",
    "delta_aic",
    "akaike_weight",
    "usable",
)


def _promotion_candidate_identity(candidates) -> list[dict] | None:
    """Pre-STAT-1 numerical candidate projection used by promotion identity."""
    if candidates is None:
        return None
    return [
        {
            key: row.get(key)
            for key in _PROMOTION_CANDIDATE_IDENTITY_FIELDS
            if key in row
        }
        for row in candidates
    ]


def _record_master_promotion(data_dir, entry: ScanEntry, *, source_revision: str,
                             M: int, seed: int) -> None:
    """Persist and publish a synchronous auto-tier promotion for this exact master revision."""
    provenance = entry.provenance or {}
    reasons = list(provenance.get("escalation_reasons") or ())
    if (provenance.get("errors_tier_requested") != "auto"
            or provenance.get("errors_tier_effective") != "full" or not reasons):
        return
    from isar.session.promotions import PromotionKey, PromotionStore

    outcomes = [outcome for outcome in entry.detectors.values() if outcome.get("pooled")]
    # Candidate rows gained additive STAT-1 classifications.  Promotion identity remains the
    # pre-STAT-1 numerical contract: content-addressed semantics may change without reminting the
    # scientific retry for an otherwise identical master fit.
    candidates = outcomes[0].get("candidates") if outcomes else None
    scientific_candidates = _promotion_candidate_identity(candidates)
    point_fingerprint = _json_fingerprint({
        "master_id": entry.scan_id,
        "source_revision": source_revision,
        "best_label": outcomes[0].get("best_label") if outcomes else None,
        "candidates": scientific_candidates,
    })
    key = PromotionKey(
        master_id=entry.scan_id, source_revision=source_revision,
        spectrum_digest=str(provenance.get("spectrum_digest") or ""),
        point_fingerprint=point_fingerprint, tier="full", M=int(M), seed=int(seed))
    store = PromotionStore(Path(data_dir).parent)
    store.begin(key, escalation_reasons=reasons)
    store.complete(key, asdict(entry))
    record = store.resolve_publication(key, current_revision=source_revision)
    if record["status"] == "published":
        store.supersede_other_attempts(key)
    entry.provenance.update({
        "promotion_id": key.digest,
        "promotion_state": record["status"],
        "promotion_source_revision": source_revision,
        "promotion_publication_outcome": record["publication_outcome"],
        "point_fit_fingerprint": point_fingerprint,
    })


def current_master_revisions(session: SessionState) -> dict[str, str]:
    """Return the authoritative published content revision for every pooled master."""
    return {
        master_id: str(entry.content_hash)
        for master_id, entry in session.scans.items()
        if master_id.startswith("master:") and entry.content_hash
    }


def unresolved_current_promotions(workspace_root, session: SessionState) -> list[dict]:
    """Reconcile stale attempts and return promotions blocking a terminal/certification action."""
    from isar.session.promotions import PromotionStore
    return PromotionStore(workspace_root).reconcile(current_master_revisions(session))


def _fit_master_group_task(args):
    """Top-level (picklable) entry point for a master-group pool worker: fit one merged group.

    Bootstrap is forced serial inside the child (``boot_workers=1``, no shared pool) — Layer A owns
    the process workers, the same trade-off as the scan pool in :func:`_run_tasks`, and the seeded
    bootstrap is invariant to worker count / where the refits run (errors.bootstrap). The returned
    :class:`ScanEntry` carries its own timings in ``provenance``; the parent applies ``session.put``
    serially afterward in sorted-group order. Every argument is picklable (ms/grp are plain collation
    dataclasses, member_rec a ScanRecord, card/g/runcal as the scan pool already ships)."""
    (ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number, chash,
     M, seed, errors_tier, boot_adaptive, structure_hint, bayes_opts, *role_tail) = args
    fit_role = role_tail[0] if role_tail else "master"
    # Pass structure_hint only when set: with the reuse policy off (default) hint is None and this
    # call is byte-identical to the pre-B2 call (keeps the "change nothing when off" guarantee).
    hint_kw = {"structure_hint": structure_hint} if structure_hint is not None else {}
    role_kw = {"fit_role": fit_role} if fit_role != "master" else {}
    return _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                             scan_number, chash, M=M, seed=seed, errors_tier=errors_tier,
                             boot_workers=1, boot_executor=None, boot_adaptive=boot_adaptive,
                             bayes_opts=bayes_opts, **role_kw, **hint_kw)


def _stamp_master_analysis(entry: ScanEntry, *, errors_tier: str,
                           fit_fingerprint: str | None,
                           spectrum_digest: str | None) -> ScanEntry:
    entry.provenance = {
        **(entry.provenance or {}),
        "analysis_tier": errors_tier,
        "analysis_tier_effective": (entry.provenance or {}).get(
            "errors_tier_effective", errors_tier),
        "master_fit_contract": _MASTER_FIT_CONTRACT,
        # The predictive artifact this master was produced under. The carry path in
        # :func:`_fit_masters` requires the current value, so a contract bump regenerates the
        # artifact from an unchanged pool on the next run instead of carrying a stale one.
        "predictive_contract": PREDICTIVE_CONTRACT,
    }
    if fit_fingerprint is not None:
        entry.provenance["master_fit_fingerprint"] = fit_fingerprint
    if spectrum_digest is not None:
        entry.provenance["spectrum_digest"] = spectrum_digest
    return entry


def _certification_entry_reusable(entry: ScanEntry | None, *, content_hash_value: str,
                                  spectrum_digest: str, fit_fingerprint: str,
                                  requested_tier: str) -> bool:
    """Integrity and scientific-completion gate shared by workspace/checkpoint reuse."""
    from isar.session.certification import tier_satisfies

    if entry is None or entry.content_hash != content_hash_value:
        return False
    provenance = entry.provenance or {}
    if provenance.get("spectrum_digest") != spectrum_digest:
        return False
    if provenance.get("master_fit_fingerprint") != fit_fingerprint:
        return False
    if not tier_satisfies(provenance.get("analysis_tier"), requested_tier):
        return False
    # A deterministic scientific non-result is complete.  A caught fit exception is not: retry it.
    if entry.status == "non_result" and any("fit error (" in reason for reason in entry.reasons):
        return False
    if entry.status == "fitted":
        pooled = [outcome for outcome in entry.detectors.values() if outcome.get("pooled")]
        return len(pooled) == 1 and pooled[0].get("n_members") is not None
    return entry.status == "non_result"


def _publication_hashes(workspace: Workspace) -> dict[str, str]:
    result = {}
    for name, path in (("session.json", workspace.session_path),
                       ("card.state.json", workspace.state_path),
                       ("results.csv", workspace.report_dir / "results.csv"),
                       ("card.policy.toml", workspace.policy_path)):
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_canonical_indices_cover_path(worklist, excluded) -> None:
    """Every objective-path point must be accounted for exactly once — certified or excluded.

    The worklist alone is NOT dense: the manifest builder legitimately omits structurally-
    unresolvable points (the inferred reduced-q origin has no pooled master by construction) into
    ``excluded_points``.  So the contiguity invariant is over the UNION of the worklist and excluded
    canonical indices, not the worklist by itself.  Requiring the worklist to equal
    ``range(len(worklist))`` on its own broke every campaign whose path starts at a zone-center
    origin, where the surviving canonical indices begin at 1.
    """
    combined = sorted([int(unit.get("canonical_index", -1)) for unit in worklist]
                      + [int(item.get("canonical_index", -1)) for item in excluded])
    if combined != list(range(len(combined))):
        raise ValueError("certification worklist and excluded points must together cover "
                         "contiguous, unique canonical indices")


def _run_master_certification(workspace, card, session, records, runcal, g, checkpoint_store,
                              *, summary, M, errors_tier, workers, bayes_opts,
                              deadline_gate, timer) -> RunSummary:
    """Execute an ``isar.certification/2`` worklist without fitting or mutating physical rows."""
    manifest = checkpoint_store.manifest
    requested_tier = str(manifest.get("tier") or errors_tier)
    if requested_tier != errors_tier:
        raise ValueError(
            f"certification tier mismatch: manifest={requested_tier!r}, run={errors_tier!r}")
    worklist = list(manifest.get("worklist") or ())
    worklist.sort(key=lambda unit: int(unit.get("canonical_index", -1)))
    _assert_canonical_indices_cover_path(worklist, manifest.get("excluded_points") or ())
    unit_keys = [str(unit.get("unit_key") or "") for unit in worklist]
    master_ids = [str(unit.get("master_id") or "") for unit in worklist]
    if (not all(unit_keys) or len(set(unit_keys)) != len(unit_keys)
            or not all(master_ids) or len(set(master_ids)) != len(master_ids)):
        raise ValueError("certification worklist has missing or duplicate unit/master IDs")

    with timer.stage("collate"):
        collation = collate_mod.collate(records, card)
    summary.collation = collation
    sorted_groups = sorted(collation.groups, key=lambda item: str(item.key))
    groups_by_id = {
        "master:" + "_".join(str(value) for value in group.key): (index, group)
        for index, group in enumerate(sorted_groups)
        if getattr(group, "merged", None) is not None
    }
    rec_by_member = {
        (str(rec.experiment_id), int(rec.scan_number), str(detector)): (rec, detector)
        for rec in records for detector in rec.spectra
    }

    fit_card = copy.deepcopy(card)
    frozen_state = (manifest.get("inputs") or {}).get("frozen_state")
    if not isinstance(frozen_state, dict):
        raise ValueError("certification manifest lacks its frozen scientific state")
    for field_name, value in frozen_state.items():
        if hasattr(fit_card.state, field_name):
            setattr(fit_card.state, field_name, copy.deepcopy(value))

    prepared = []
    boot_adaptive = requested_tier in FULL_TIERS
    for unit in worklist:
        master_id = str(unit["master_id"])
        matched = groups_by_id.get(master_id)
        if matched is None:
            raise ValueError(f"certification master is missing from frozen collation: {master_id}")
        group_index, group = matched
        merged = group.merged
        members = [list(member) for member in merged.members]
        digest = collate_mod.merged_spectrum_digest(merged)
        if members != list(unit.get("ordered_members") or ()):
            raise ValueError(f"certification member drift for {master_id}")
        if digest != unit.get("spectrum_digest"):
            raise ValueError(f"certification spectrum drift for {master_id}")
        if int(unit.get("group_index", -1)) != group_index:
            raise ValueError(f"certification group-index drift for {master_id}")
        spectrum_ref = str(unit.get("spectrum_ref") or "")
        spectrum_path = (workspace.root / spectrum_ref).resolve()
        root = workspace.root.resolve()
        if not spectrum_ref or not spectrum_path.is_relative_to(root) or not spectrum_path.is_file():
            raise ValueError(f"certification spectrum reference is missing or invalid for {master_id}")
        if hashlib.sha256(spectrum_path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"certification spectrum blob digest mismatch for {master_id}")
        first_member = tuple(merged.members[0]) if merged.members else None
        if first_member is None or first_member not in rec_by_member:
            raise ValueError(f"certification master has no unique source member: {master_id}")
        member_rec, member_det = rec_by_member[first_member]
        calres = calibrations.resolve(member_rec, member_det, fit_card, runcal)
        if _json_fingerprint(calres.summary()) != _json_fingerprint(
                dict(unit.get("resolved_calibration") or {})):
            raise ValueError(f"certification calibration drift for {master_id}")
        seed = int(unit["seed"])
        expected_fingerprint = _master_fit_fingerprint(
            merged, fit_card, calres, g, None, errors_tier=requested_tier, M=M, seed=seed,
            boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        if unit.get("master_fit_fingerprint") != expected_fingerprint:
            raise ValueError(f"certification fit fingerprint drift for {master_id}")
        chash = canonical_hash(members)
        prepared.append({
            "unit": unit, "group": group, "merged": merged, "group_index": group_index,
            "member_rec": member_rec, "member_det": member_det, "content_hash": chash,
            "spectrum_digest": digest, "fit_fingerprint": expected_fingerprint,
        })

    reusable_workspace = reusable_checkpoint = 0
    completed: dict[int, ScanEntry] = {}
    missing = []
    for item in prepared:
        unit = item["unit"]
        index = int(unit["canonical_index"])
        existing = session.get(str(unit["master_id"]))
        if _certification_entry_reusable(
                existing, content_hash_value=item["content_hash"],
                spectrum_digest=item["spectrum_digest"],
                fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier):
            completed[index] = copy.deepcopy(existing)
            reusable_workspace += 1
            continue
        cached = checkpoint_store.load(
            str(unit["unit_key"]), content_hash=item["content_hash"], guidelines_hash="",
            fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier)
        if _certification_entry_reusable(
                cached, content_hash_value=item["content_hash"],
                spectrum_digest=item["spectrum_digest"],
                fit_fingerprint=item["fit_fingerprint"], requested_tier=requested_tier):
            completed[index] = cached
            reusable_checkpoint += 1
        else:
            missing.append(item)

    def progress(*, active=0, last_hkl=None, phase="fitting", published=False):
        checkpoint_store.report_progress(
            len(completed), reused_workspace=reusable_workspace,
            reused_checkpoint=reusable_checkpoint,
            newly_fitted=max(len(completed) - reusable_workspace - reusable_checkpoint, 0),
            active=active, last_hkl=last_hkl, phase=phase,
            remaining_analysis_seconds=deadline_gate.remaining_seconds(), published=published)

    progress()
    if missing:
        max_workers = min(max(int(workers), 1), len(missing))
        for offset in range(0, len(missing), max_workers):
            wave = missing[offset:offset + max_workers]
            if not deadline_gate.begin_wave():
                progress(phase="interrupted")
                summary.analysis_incomplete = True
                summary.context_lines.append(
                    f"checkpointed incomplete certification: {len(completed)}/{len(worklist)} masters")
                return summary
            args = [(
                item["merged"], item["group"], item["member_rec"], item["member_det"],
                fit_card, runcal, g, str(item["unit"]["master_id"]),
                _MASTER_SCAN_BASE + item["group_index"], item["content_hash"], M,
                int(item["unit"]["seed"]), requested_tier, boot_adaptive, None, bayes_opts,
                "certification",
            ) for item in wave]
            progress(active=len(wave))
            pool = None
            failures = []

            def accept_completion(entry, item, active_count):
                unit = item["unit"]
                if not _certification_entry_reusable(
                        entry, content_hash_value=item["content_hash"],
                        spectrum_digest=item["spectrum_digest"],
                        fit_fingerprint=item["fit_fingerprint"],
                        requested_tier=requested_tier):
                    # A deterministic non-result is reusable; an exception-aborted fit is not.
                    if not (entry.status == "non_result"
                            and not any("fit error (" in reason for reason in entry.reasons)):
                        raise RuntimeError(
                            f"certification fit did not produce a durable outcome: {entry.scan_id}")
                checkpoint_store.save(str(unit["unit_key"]), entry)
                completed[int(unit["canonical_index"])] = entry
                active_count -= 1
                progress(active=active_count, last_hkl=unit.get("hkl"))
                return active_count

            try:
                active = len(wave)
                if len(wave) == 1 or max_workers == 1:
                    active = accept_completion(_fit_master_group_task(args[0]), wave[0], active)
                else:
                    pool = ProcessPoolExecutor(max_workers=len(wave))
                    future_items = {pool.submit(_fit_master_group_task, arg): item
                                    for arg, item in zip(args, wave)}
                    for future in as_completed(future_items):
                        item = future_items[future]
                        try:
                            active = accept_completion(future.result(), item, active)
                        except Exception as exc:  # drain the wave; preserve every other result
                            failures.append((item, exc))
                            active -= 1
                            progress(active=active, last_hkl=item["unit"].get("hkl"))
            finally:
                if pool is not None:
                    pool.shutdown()
            if failures:
                progress(active=0, phase="failed")
                failed_unit, error = failures[0]
                raise RuntimeError(
                    f"certification worker failed for {failed_unit['unit']['master_id']}: {error}"
                ) from error

    if len(completed) != len(worklist):
        raise RuntimeError("certification worklist completed without every canonical unit")

    progress(phase="publishing")
    physical_rows = {
        key: copy.deepcopy(value) for key, value in session.scans.items()
        if not key.startswith("master:")
    }
    for item in prepared:
        unit = item["unit"]
        entry = completed[int(unit["canonical_index"])]
        spectrum_ref = collate_mod.persist_merged_spectrum(workspace.root / "data", item["merged"])
        for outcome in entry.detectors.values():
            if outcome.get("pooled"):
                outcome["spectrum_ref"] = spectrum_ref
        entry.provenance["spectrum_ref"] = spectrum_ref
        predictive_payload = entry.provenance.pop("predictive_payload", None)
        if predictive_payload is not None:
            predictive_payload["predicted_analysis_seconds"] = (
                _predicted_master_analysis_seconds(predictive_payload))
            predictive_ref = _persist_master_predictive(workspace.root / "data", predictive_payload)
            for outcome in entry.detectors.values():
                if outcome.get("pooled"):
                    outcome["predictive_ref"] = predictive_ref
            entry.provenance["predictive_ref"] = predictive_ref
        session.put(entry)

    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    if _campaign_hypotheses(card):
        _materialize_branch_candidates(session)
    _rebuild_pooled_prior(card, session, session_prior, t_round=t_round)
    track = _apply_tracked_branch(session, card, t_round=t_round)
    _resolve_target_projections(session, workspace.root / "data")
    # Tracking may annotate individual rows for display. Certification is master-only, so restore
    # their exact pre-publication ledger payload after the master-derived state has been rebuilt.
    for key, entry in physical_rows.items():
        session.scans[key] = entry
    _store_branch_models(card.state, t_round)
    _store_branch_beliefs(card.state, t_round, period=_dispersion_period(card),
                          hypotheses=_campaign_hypotheses(card))
    _update_hypothesis_mismatch(card.state, t_round, period=_dispersion_period(card),
                                hypotheses=_campaign_hypotheses(card))
    summary.counts = _tally(session)
    summary.context_lines.append(
        f"certified {len(worklist)} pooled master(s); fitted {len(missing)}, "
        f"reused {reusable_workspace + reusable_checkpoint}")
    if track is not None:
        summary.context_lines.append(f"branch track revision {track['revision']}")
    session.last_run = {
        "counts": summary.counts,
        "scan_gaps": [f"{experiment}: {start}->{end}"
                      for experiment, start, end in summary.gaps],
        "n_fitted_this_run": 0,
        "certification_fingerprint": manifest["fingerprint"],
    }
    publication_root = checkpoint_store.root / "publication"
    staged_workspace = Workspace.open(publication_root)
    _write_results_csv(staged_workspace, session)
    staged_workspace.save(card, session)
    checkpoint_store.begin_publication()
    workspace.root.mkdir(parents=True, exist_ok=True)
    workspace.report_dir.mkdir(parents=True, exist_ok=True)
    # Every payload is complete and fsynced before the first replacement. The current marker is
    # already non-published, so a crash between these atomic replaces is visible and repairable.
    os.replace(staged_workspace.session_path, workspace.session_path)
    os.replace(staged_workspace.state_path, workspace.state_path)
    os.replace(staged_workspace.report_dir / "results.csv",
               workspace.report_dir / "results.csv")
    hashes = _publication_hashes(workspace)
    checkpoint_store.mark_published(unit_keys=unit_keys, published_hashes=hashes)
    progress(phase="completed", published=True)
    return summary


def _fit_masters(collation, records, card, runcal, g, session, *, data_dir=None, M, seed_base,
                 errors_tier="full", force=False, boot_workers=1, boot_executor=None,
                 boot_adaptive=False, scan_workers=1, bayes_opts=None, per_master=None,
                 checkpoint_store=None, completed_master_keys=None, deadline_gate=None) -> int:
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
        carried_contract = (prev.provenance or {}).get("predictive_contract") if prev else None
        if not force and prev is not None and prev.content_hash == chash \
                and carried_contract == PREDICTIVE_CONTRACT:
            # Member set unchanged → carry the prior master. Backfill the member provenance onto an
            # older ledger entry that predates this field: the content-hash match proves the member
            # set is byte-identical, so `_member_labels(ms.members)` is exact — pure bookkeeping, no
            # refit. In-place mutation persists (session.get returns the live ledger ref).
            for oc in prev.detectors.values():
                if oc.get("pooled") and "members" not in oc:
                    oc["members"] = _member_labels(ms.members)
            if prev.provenance and "members" not in prev.provenance:
                prev.provenance["members"] = [list(m) for m in ms.members]
            if data_dir is not None and not any(
                    oc.get("spectrum_ref") for oc in prev.detectors.values() if oc.get("pooled")):
                spectrum_ref = collate_mod.persist_merged_spectrum(data_dir, ms)
                for oc in prev.detectors.values():
                    if oc.get("pooled"):
                        oc["spectrum_ref"] = spectrum_ref
                prev.provenance["spectrum_ref"] = spectrum_ref
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

    cached_entries: dict[int, ScanEntry] = {}
    pending_refits = []
    for refit_index, refit in enumerate(refits):
        _idx, _grp, _ms, master_id, chash, _rec, _det, _hint = refit
        cached = (None if checkpoint_store is None else checkpoint_store.load(
            f"master:{master_id}", content_hash=chash, guidelines_hash=""))
        if cached is None:
            pending_refits.append((refit_index, refit))
        else:
            cached_entries[refit_index] = cached
    master_completed = len(cached_entries)
    if checkpoint_store is not None and master_completed:
        checkpoint_store.report_progress(master_completed)
    pending = [refit for _index, refit in pending_refits]
    fresh_entries: list[ScanEntry] = []

    # --- Fit the refits: parallel across groups when it pays, else serial in-process. ---
    if len(pending) > 1 and scan_workers > 1:
        # Parallel: bootstrap forced serial in each child (boot_workers=1, no shared pool). map
        # preserves order → entries[k] aligns with refits[k], keeping the serial ledger order.
        arg_list = [(ms, grp, member_rec, member_det, card, runcal, g, master_id,
                     _MASTER_SCAN_BASE + idx, chash, M, seed_base + _MASTER_SCAN_BASE + idx,
                     errors_tier, boot_adaptive, hint, bayes_opts)
                    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in pending]
        max_workers = min(scan_workers, len(arg_list))
        with ProcessPoolExecutor(max_workers=max_workers) as ex_pool:
            for offset in range(0, len(arg_list), max_workers):
                if deadline_gate is not None and not deadline_gate.begin_wave():
                    raise AnalysisWorkIncomplete(
                        f"analysis deadline reached after {len(fresh_entries)}/{len(arg_list)} "
                        "master fit units")
                wave = arg_list[offset:offset + max_workers]
                for entry in ex_pool.map(_fit_master_group_task, wave):
                    if checkpoint_store is not None:
                        checkpoint_store.save(f"master:{entry.scan_id}", entry)
                        master_completed += 1
                        checkpoint_store.report_progress(master_completed)
                    fresh_entries.append(entry)
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
            for (idx, grp, ms, master_id, chash, member_rec, member_det, hint) in pending:
                if deadline_gate is not None and not deadline_gate.begin_wave():
                    raise AnalysisWorkIncomplete(
                        f"analysis deadline reached after {len(fresh_entries)}/{len(pending)} "
                        "master fit units")
                entry = _fit_master_group(
                    ms, grp, member_rec, member_det, card, runcal, g, master_id,
                    _MASTER_SCAN_BASE + idx, chash, M=M,
                    seed=seed_base + _MASTER_SCAN_BASE + idx, errors_tier=errors_tier,
                    boot_workers=boot_workers, boot_executor=ex,
                    boot_adaptive=boot_adaptive, bayes_opts=bayes_opts,
                    **({"structure_hint": hint} if hint is not None else {}))
                if checkpoint_store is not None:
                    checkpoint_store.save(f"master:{entry.scan_id}", entry)
                    master_completed += 1
                    checkpoint_store.report_progress(master_completed)
                fresh_entries.append(entry)
        finally:
            if own_pool is not None:
                own_pool.shutdown()

    for (refit_index, _refit), entry in zip(pending_refits, fresh_entries):
        cached_entries[refit_index] = entry
    entries = [cached_entries[index] for index in range(len(refits))]

    # Apply ledger mutations + timing entries serially, in the original sorted-group order.
    for (idx, grp, ms, master_id, chash, member_rec, member_det, hint), entry in zip(refits, entries):
        # Blob-first publication means interruption can leave an orphan, never a dangling ref.
        if data_dir is not None:
            spectrum_ref = collate_mod.persist_merged_spectrum(data_dir, ms)
            for outcome in entry.detectors.values():
                if outcome.get("pooled"):
                    outcome["spectrum_ref"] = spectrum_ref
            entry.provenance["spectrum_ref"] = spectrum_ref
            predictive_payload = entry.provenance.pop("predictive_payload", None)
            if predictive_payload is not None:
                predictive_payload["predicted_analysis_seconds"] = (
                    _predicted_master_analysis_seconds(predictive_payload))
                predictive_ref = _persist_master_predictive(data_dir, predictive_payload)
                for outcome in entry.detectors.values():
                    if outcome.get("pooled"):
                        outcome["predictive_ref"] = predictive_ref
                entry.provenance["predictive_ref"] = predictive_ref
            _record_master_promotion(
                data_dir, entry, source_revision=chash, M=M,
                seed=seed_base + _MASTER_SCAN_BASE + idx)
        session.put(entry)
        if completed_master_keys is not None:
            completed_master_keys.append(f"master:{master_id}")
        if per_master is not None:
            per_master.append(
                {"master_id": master_id,
                 "seconds": entry.provenance.get("fit_seconds"),
                 "two_stage_seconds": entry.provenance.get("two_stage_seconds"),
                 "errors_attach_seconds": entry.provenance.get("errors_attach_seconds"),
                 "bootstrap_seconds": entry.provenance.get("bootstrap_seconds"),
                 "minos_seconds": entry.provenance.get("minos_seconds"),
                 "profile_seconds": entry.provenance.get("profile_seconds"),
                 "bayes_seconds": entry.provenance.get("bayes_seconds"),
                 "bootstrap_M_requested": entry.provenance.get("bootstrap_M_requested"),
                 "bootstrap_M_effective": entry.provenance.get("bootstrap_M_effective")})
    return len(refits)


def _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number,
                      chash, *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None,
                      boot_adaptive=False, structure_hint=None, bayes_opts=None,
                      fit_role="master") -> ScanEntry:
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
    spectrum_digest = collate_mod.merged_spectrum_digest(ms)
    if member_rec is None:
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: no member record for resolution"], **base),
            errors_tier=errors_tier, fit_fingerprint=None,
            spectrum_digest=spectrum_digest)
    try:
        calres = calibrations.resolve(member_rec, member_det, card, runcal)
    except Exception as exc:  # noqa: BLE001
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: resolution failed ({exc})"], **base),
            errors_tier=errors_tier, fit_fingerprint=None,
            spectrum_digest=spectrum_digest)

    sample = str(grp.key_fields.get("sample", ""))
    period = _dispersion_period(card)
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    target_branch, target_label, energy_search = _campaign_target(card)
    prior = (session_prior.prior_for(card.state, sample=sample, T=T, q=q, period=period,
                                     t_round=t_round, branch_id=target_branch)
             if period is not None else None)
    fit_fingerprint = _master_fit_fingerprint(
        ms, card, calres, g, prior, errors_tier=errors_tier, M=M, seed=seed,
        boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)

    x = np.asarray(ms.energy, dtype=float)
    counts = np.asarray(ms.intensity, dtype=float)
    mask = np.asarray(ms.mask, dtype=bool)
    raw_monitor = getattr(ms, "monitor", None)
    monitor = None if raw_monitor is None else np.asarray(raw_monitor, dtype=float)
    # The reference is CARRIED into the fit context (never recomputed at publication time): it is
    # the amplitude basis the predictive artifact publishes as ``fit_reference_exposure``.
    exposure_scale, reference_exposure = ((None, None) if monitor is None
                                          else normalized_exposure(monitor, mask))
    if exposure_scale is not None:
        mask = mask & (exposure_scale > 0)
    if int(mask.sum()) < g.min_fit_points:
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: undersampled pool ({int(mask.sum())} < "
                               f"{g.min_fit_points} kept points)"], **base),
            errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
            spectrum_digest=spectrum_digest)
    # Pooled spectrum → ScanContext directly (bypasses build_context: a merged group has no single
    # ScanRecord/normalization; MergedSpectrum.mask already encodes monitor validity).
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
                      acoustic_omega=ao, exposure_scale=exposure_scale,
                      exposure_reference=reference_exposure)
    near_gamma = (target_branch == "acoustic" and ao is not None
                  and ao < BA.RESOLVABLE_FWHM_FACTOR * calres.gauss_fwhm)
    descriptor = _build_descriptor(ctx)
    try:
        fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, errors_tier=errors_tier,
                      boot_workers=boot_workers, boot_executor=boot_executor,
                      boot_adaptive=boot_adaptive, descriptor=descriptor,
                      structure_hint=structure_hint, bayes_opts=bayes_opts,
                      predictive_n_members=int(ms.n_members), fit_role=fit_role)
    except Exception as exc:  # noqa: BLE001 — one unfittable pool must not kill the batch (N=16 wrinkle)
        return _stamp_master_analysis(
            ScanEntry(status="non_result",
                      reasons=[f"{master_id}: fit error ({type(exc).__name__}): {exc}"], **base),
            errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
            spectrum_digest=spectrum_digest)

    # Phase A: the pooled master runs the SAME branch triage as a per-scan fit — the acoustic hint
    # (ctx.acoustic_omega) makes primary_phonon_index report the acoustic mode (not the brighter optic),
    # and the triage withholds a non-acoustic/unresolvable pool as branch_continuation. Without this a
    # boundary master delivers the optic as the acoustic ω₀.
    (branch_id, assignment_entropy, branch_weight, resolvable, secondary_features,
     branch_posterior, candidate_id, candidate_state, selected_energy) = _apply_branch_triage(
        fit, descriptor, ctx, card=card, sample=sample, T=T, q=q, t_round=t_round,
        near_gamma=near_gamma, master=True)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    exposure_info = _effective_exposure_for(
        None, ctx, fit, absolute_exposure=monitor,
        absolute_exposure_source=("monitor" if monitor is not None else None),
        target_center=selected_energy)
    predictive_payload, predictive_unavailable = _build_predictive_payload(fit, ctx, master_id)
    model_interval = ((predictive_payload or {}).get("omega_interval")
                      if predictive_payload is not None else None)
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=str(grp.key_fields.get("sample", "")), prior_used=None,
        calib=calres.summary(), reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        amplitudes=fit.get("amplitudes"), candidates=fit.get("candidates"),
        model_manifest=fit.get("model_manifest"),
        interval_reliable=fit.get("interval_reliable", True), omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        pooled=True, n_members=int(ms.n_members), members=_member_labels(ms.members),
        master_revision=chash, gamma_at_bound=bool(fit.get("gamma_at_bound", False)),
        model_interval=(tuple(model_interval) if model_interval is not None else None),
        model_robust=bool(model_interval is not None and fit.get("modality") != "fork"),
        model_entropy=((predictive_payload or {}).get("model_entropy_nats")
                       if predictive_payload is not None else None),
        linewidth_state=("resolution_limited" if fit.get("gamma_at_bound") else "identified"),
        pool_consistent=getattr(ms, "pool_consistent", None),
        consistency_statistic=getattr(ms, "consistency_statistic", None),
        consistency_pvalue=getattr(ms, "consistency_pvalue", None),
        consistency_alpha=getattr(ms, "consistency_alpha", None),
        consistency_seed=getattr(ms, "consistency_seed", None),
        fit_selection_semantics=fit.get("fit_selection_semantics"),
        interval_semantics=fit.get("interval_semantics"),
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
        acquisition_id=f"pooled:{chash}")
    outcome.probability_semantics = _refresh_probability_semantics(
        outcome.__dict__,
        predictive_payload=predictive_payload,
    )
    entry = ScanEntry(status="fitted", **base)
    entry.detectors[det] = outcome.to_dict()
    entry.provenance = {"pooled": True, "n_members": int(ms.n_members),
                        "members": [list(m) for m in ms.members],
                        "fit_seconds": round(time.perf_counter() - _t_fit0, 4),
                        "two_stage_seconds": fit.get("two_stage_seconds"),
                        "errors_attach_seconds": fit.get("errors_attach_seconds"),
                        **_timing_provenance(fit)}
    calibration_fingerprint = _json_fingerprint(calres.summary())
    policy_fingerprint = _json_fingerprint(card.policy)
    if predictive_payload is not None:
        predictive_payload.update({
            "master_content_hash": chash,
            "calibration_fingerprint": calibration_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "prior_fingerprint": _json_fingerprint(
                None if prior is None else {"omega0": prior.omega0,
                                            "omega0_sigma": prior.omega0_sigma}),
            "q": q, "T": T, "members": [list(member) for member in ms.members],
        })
        entry.provenance["predictive_payload"] = predictive_payload
        # The authority basis the target projection is later bound to. Carried here because the
        # projection is resolved after the connected tracker, in a scope that has no calibration.
        entry.provenance["target_projection_authority"] = {
            "master_revision": chash,
            "calibration_fingerprint": calibration_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "action_role": str(fit_role),
        }
    if predictive_unavailable is not None:
        entry.provenance["predictive_unavailable"] = predictive_unavailable
    if fit.get("structure_skipped"):
        entry.provenance["structure_skipped"] = True
    return _stamp_master_analysis(
        entry, errors_tier=errors_tier, fit_fingerprint=fit_fingerprint,
        spectrum_digest=spectrum_digest)


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
                "weight": None, "entropy": None, "resolvable": None,
                "selected_energy": fit.get("omega0")}
    selector, target_energy = _campaign_selector(card)
    if selector == "tracked_branch":
        # Preserve the fit as unordered evidence. The authoritative branch label is assigned only
        # after every pooled master has updated, using the whole objective path in both directions.
        return {"deliver": True, "branch_id": None, "weight": None, "entropy": None,
                "resolvable": None, "selected_energy": fit.get("omega0"),
                "candidate_id": f"{target_branch}:tracked_branch",
                "candidate_state": "provisional"}
    u = BA.reduced_q(q, period)
    if target_branch != BA.ACOUSTIC:
        # A fitted mode and the model-free descriptor commonly report the same physical peak.  Do
        # branch assignment from the fitted modes when they exist: counting both representations as
        # rival features splits dominance and can demote an otherwise clean pooled optic fit.  The
        # descriptor remains the honest fallback when no model mode survived.
        target_feats = [feature for feature in feats if feature.source == "mode"] or feats
        target_post = BA.assignment_posterior(target_feats, beliefs, u)
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
        # Stable across q so neighboring measurements update the same scientific hypothesis.  The q
        # remains on every evidence record; it is not part of the candidate's identity.
        energy_key = (f"{float(target_energy):.6g}" if target_energy is not None else "unspecified")
        candidate_id = f"{target_branch}:{selector}:{energy_key}"
        return {"deliver": bool(deliver), "branch_id": assigned,
                "weight": float(qoi.get("p_branch", 0.0)), "entropy": ent,
                "resolvable": bool(qoi.get("resolvable")), "posterior": posterior,
                "selected_energy": qoi.get("omega0"),
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
                "posterior": qoi.get("posterior"), "selected_energy": qoi.get("omega0"),
                "secondary_points": BA.secondary_branch_points(feats, post, qoi,
                                                               gauss_fwhm=ctx.gauss_fwhm)}
    bid = "optic"
    if fit.get("omega0") is not None:
        bid, _ = BA.assign_label(fit["omega0"], fit.get("omega0_err") or 0.3 * ctx.gauss_fwhm,
                                 beliefs, u, gauss_fwhm=ctx.gauss_fwhm)
    return {"deliver": False, "branch_id": bid, "weight": 0.0, "entropy": ent,
            "resolvable": bool(qoi["resolvable"]), "posterior": qoi.get("posterior"),
            "selected_energy": qoi.get("omega0")}


def _build_predictive_payload(fit, ctx, master_id: str) -> tuple[dict | None, dict | None]:
    """Build ``isar.master_predictive/2`` from a finished, triaged fit — isolated from the science.

    Returns ``(payload, unavailable)``: exactly one is not None. ADR-019 producer isolation — the
    predictive side is a *steering* artifact, so its failure degrades steering to the legacy path
    and must never turn a perfectly good scientific fit into a ``non_result``. The reason is
    structured and logged; nothing is swallowed.
    """
    inputs = fit.pop("predictive_inputs", None)
    if inputs is None:
        return None, None
    try:
        payload = predictive_artifact.build_master_predictive(
            inputs["best"], inputs["scores"], inputs["fits"], inputs["by_hyp"], ctx,
            seed=inputs["seed"], n_members=inputs["n_members"],
            fit_selection_semantics=inputs.get("fit_selection_semantics"))
    except Exception as exc:  # noqa: BLE001 — the fit survives any predictive-construction failure
        log.warning("%s: predictive artifact construction failed (%s): %s",
                    master_id, type(exc).__name__, exc)
        return None, {
            "reason_code": predictive_artifact.PREDICTIVE_UNAVAILABLE_CONSTRUCTION_FAILED,
            "error_type": type(exc).__name__, "message": str(exc)}
    if payload is None:
        return None, {
            "reason_code": predictive_artifact.PREDICTIVE_UNAVAILABLE_NO_RETAINED_MODELS,
            "message": "no fitted candidate produced a usable manifest and draw basis"}
    return payload, None


def _resolve_target_projections(session: SessionState, data_dir) -> int:
    """Resolve and publish one ``isar.target_projection/2`` per pooled master (ADR-019).

    Called ONLY after branch triage, ``_materialize_branch_candidates`` and the connected tracker
    have run, because all three can still move the target: triage can withhold the delivery, the
    tracker can reassign the selected candidate or clear it entirely, and neither is visible at fit
    time. Each projection is bound to the authority basis that decided it, so a later track revision
    produces a NEW projection against the unchanged ``/2`` — no refit is involved.
    """
    if data_dir is None:
        return 0
    published = 0
    for key in sorted(session.scans):
        if not key.startswith("master:"):
            continue
        entry = session.scans[key]
        authority = (entry.provenance or {}).get("target_projection_authority") or {}
        for outcome in entry.detectors.values():
            ref = outcome.get("predictive_ref")
            if not outcome.get("pooled") or not ref:
                continue
            try:
                artifact = json.loads(
                    (Path(data_dir).parent / str(ref)).read_text(encoding="utf-8"))
                projection = predictive_artifact.build_target_projection(
                    artifact, outcome,
                    master_revision=authority.get("master_revision")
                    or outcome.get("master_revision"),
                    calibration_fingerprint=authority.get("calibration_fingerprint"),
                    policy_fingerprint=authority.get("policy_fingerprint"),
                    action_role=authority.get("action_role") or "master")
                projection_ref = _persist_content_addressed(
                    data_dir, "target_projection", projection)
            except (OSError, ValueError, KeyError, TypeError) as error:
                # A projection is steering input, never a scientific number: record why it is
                # missing and leave the master's fit and every other artifact untouched.
                log.warning("%s: target projection unavailable (%s): %s",
                            key, type(error).__name__, error)
                entry.provenance["target_projection_unavailable"] = {
                    "reason_code":
                        predictive_artifact.PROJECTION_UNAVAILABLE_CONSTRUCTION_FAILED,
                    "error_type": type(error).__name__, "message": str(error)}
                continue
            outcome["target_projection_ref"] = projection_ref
            entry.provenance["target_projection_ref"] = projection_ref
            entry.provenance.pop("target_projection_unavailable", None)
            published += 1
    return published


def _apply_branch_triage(fit, descriptor, ctx, *, card, sample, T, q, t_round, near_gamma,
                         master: bool):
    """Run branch assignment + honest triage over ``fit`` and apply its verdict (Phase A). Shared by
    the per-scan (:func:`_finish_outcome`) and pooled-master (:func:`_fit_master_group`) paths.

    Returns branch assignment, candidate posterior/state, and secondary features — all
    None when the posterior is off or the fit is a ``signal_unmodeled`` descriptor result. When the
    triage declines delivery it mutates ``fit`` in place: flips the verdict to ``branch_continuation``,
    nulls the ω₀ fields, and appends a reason (worded per ``master``)."""
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
        acquisition_target_energy=acquisition_target, submitted_ncount=submitted_ncount)
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
        effective_exposure=exposure_info.get("value"), exposure_provenance=exposure_info,
        acquisition_id=acquisition_id,
        search_energy_lo=(acquisition or {}).get("search_energy_lo"),
        search_energy_hi=(acquisition or {}).get("search_energy_hi"),
        fit_selection_semantics=fit.get("fit_selection_semantics"),
        interval_semantics=fit.get("interval_semantics"),
        evidence_sink=evidence_sink_for(getattr(rec, "raw_header", None),
                                        getattr(card.state, "acquisition_provenance", None)))
    outcome.probability_semantics = _refresh_probability_semantics(outcome.__dict__)

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


def _coerce_fit_outcome(value: dict | FitOutcome) -> FitOutcome:
    """The one dict→dataclass conversion at the persistence boundary.

    A key persisted by a newer writer but not declared here must not kill the run: the carry path
    rehydrates every previously-fitted scan on every iteration, so a strict ``FitOutcome(**d)``
    turns additive schema drift into a campaign-wide abort far from its cause. Unknown keys are
    reported and dropped *from this view only* — ``ScanEntry.detectors`` keeps the raw dict, so no
    stored value is lost. The writer/schema invariant test is what fails loudly during development.
    """
    if isinstance(value, FitOutcome):
        return value
    declared = {f.name for f in fields(FitOutcome)}
    unknown = sorted(set(value) - declared)
    if unknown:
        log.warning("ledger outcome carries field(s) not declared on FitOutcome: %s — dropped from "
                    "the dataclass view; the raw ledger entry retains them", ", ".join(unknown))
    kept = {k: v for k, v in value.items() if k in declared}
    # Ledgers written before is_reduced_q_origin was declared persist None for it. Normalizing on
    # read as well as at the writer keeps the bool annotation honest for old and new data alike.
    kept["is_reduced_q_origin"] = bool(kept.get("is_reduced_q_origin", False))
    return FitOutcome(**kept)


def _outcome_from_entry(entry: ScanEntry, det: str) -> FitOutcome:
    return _coerce_fit_outcome(entry.detectors[det])


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
    "gamma", "gamma_provisional", "modality", "n_modes", "secondary_omega0", "secondary_omega0_err",
    "best_model", "cost_per_dof", "fit_kind", "branch_id", "assignment_entropy", "resolvable",
    "descriptor_n_peaks", "descriptor_peaks",
    "zero", "zero_source", "gauss_fwhm",
    "lorentz_fwhm", "resolution_source", "resolution_version", "prior_omega", "prior_sigma",
    "prior_n", "prior_source", "reasons",
    # GUI Phase 0 (P0-3/P0-4), appended so older readers/CSVs are unaffected. JSON-encoded strings:
    # the amplitudes dict and the model-selection candidate table.
    "amplitudes", "candidates", "model_manifest",
    # Additive atomic identity: scan numbers are not globally unique across experiments, so new
    # consumers must route detector data using this full ledger key. Appended for old positional CSV
    # consumers, which continue seeing every existing column in its original order.
    "scan_id", "target_branch", "target_label",
    # Dynamic-exposure / branch-candidate provenance (additive v2 columns).
    "effective_exposure", "exposure_provenance", "branch_posterior",
    "candidate_id", "candidate_state", "acquisition_id",
    "branch_candidates", "selected_candidate_index", "track_state", "track_revision",
    "structural_endpoint_status", "is_reduced_q_origin",
    "target_probability", "identity_entropy", "local_identity_clear",
    "spectrum_ref", "predictive_ref", "master_revision", "model_interval_lo",
    "model_interval_med", "model_interval_hi", "model_robust", "model_entropy",
    "gamma_at_bound", "linewidth_state",
    "pool_consistent", "consistency_statistic", "consistency_pvalue", "consistency_alpha",
    "consistency_seed",
    # Appended, not inserted: this list is positional for old CSV consumers, so a new column can only
    # go on the end. Which sink this scan's evidence was acquired for (PR2.6) — every historical row
    # reads "canonical_master".
    "evidence_sink",
    # The authority-bound target projection (ADR-019), resolved after triage + the connected tracker.
    "target_projection_ref",
    # Whether this row may supply the campaign target's delivered omega0 (PR review F2). False only
    # when the tracker's confirmed target identity rests on an auxiliary-probe candidate: the answer
    # is kept, the number is withheld, and the point still needs a canonical target measurement.
    # Every historical row reads True. The other two carry the *answer* across the frozen Stage II/III
    # contract -- withheld delivery is only actionable downstream if the identity it kept, and the
    # reason the number is absent, travel with it.
    "target_delivery_eligible", "identity_branch_id", "delivery_withheld_reason",
    # STAT-1 additive compact provenance. Full records remain in session.json.
    "fit_selection_semantics", "interval_semantics", "probability_semantics",
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
            miv = oc.get("model_interval")
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
                "branch_candidates": (json.dumps(oc.get("branch_candidates"), sort_keys=True)
                                      if oc.get("branch_candidates") is not None else None),
                "selected_candidate_index": oc.get("selected_candidate_index"),
                "track_state": oc.get("track_state"), "track_revision": oc.get("track_revision"),
                "target_probability": oc.get("target_probability"),
                "identity_entropy": oc.get("identity_entropy"),
                "local_identity_clear": oc.get("local_identity_clear"),
                "structural_endpoint_status": oc.get("structural_endpoint_status"),
                "is_reduced_q_origin": oc.get("is_reduced_q_origin"),
                "spectrum_ref": oc.get("spectrum_ref"),
                "predictive_ref": oc.get("predictive_ref"),
                "target_projection_ref": oc.get("target_projection_ref"),
                "master_revision": oc.get("master_revision"),
                "model_interval_lo": (miv[0] if miv else None),
                "model_interval_med": (miv[1] if miv and len(miv) > 2 else None),
                "model_interval_hi": (miv[-1] if miv else None),
                "model_robust": oc.get("model_robust"),
                "model_entropy": oc.get("model_entropy"),
                "gamma_at_bound": oc.get("gamma_at_bound"),
                "linewidth_state": oc.get("linewidth_state"),
                "pool_consistent": oc.get("pool_consistent"),
                "consistency_statistic": oc.get("consistency_statistic"),
                "consistency_pvalue": oc.get("consistency_pvalue"),
                "consistency_alpha": oc.get("consistency_alpha"),
                "consistency_seed": oc.get("consistency_seed"),
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
                "modality": oc.get("modality"),
                "n_modes": oc.get("n_modes") or 1,
                "secondary_omega0": _secondary_mode(oc)[0],
                "secondary_omega0_err": _secondary_mode(oc)[1],
                "best_model": oc.get("best_label"), "cost_per_dof": oc.get("cost_per_dof"),
                "fit_kind": oc.get("fit_kind"),
                "branch_id": oc.get("branch_id"),
                # Defaults True: only a withheld auxiliary-only delivery ever clears it, so every
                # ordinary row and every historical CSV reads as deliverable.
                "target_delivery_eligible": oc.get("target_delivery_eligible", True),
                "identity_branch_id": oc.get("identity_branch_id"),
                "delivery_withheld_reason": oc.get("delivery_withheld_reason"),
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
                "model_manifest": (json.dumps(oc.get("model_manifest"), sort_keys=True)
                                   if oc.get("model_manifest") is not None else None),
                "fit_selection_semantics": (
                    json.dumps(
                        compact_for_csv(oc.get("fit_selection_semantics")),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if oc.get("fit_selection_semantics") is not None
                    else None
                ),
                "interval_semantics": (
                    json.dumps(
                        compact_for_csv(oc.get("interval_semantics")),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if oc.get("interval_semantics") is not None
                    else None
                ),
                "probability_semantics": (
                    json.dumps(
                        compact_for_csv(oc.get("probability_semantics")),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if oc.get("probability_semantics") is not None
                    else None
                ),
            })
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in _CSV_COLUMNS})
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    return path

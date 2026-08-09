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
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import numpy as np

from isar.analyze import errors, loop, select, shadow_selection, verdict
from isar.analyze.evidence import EVIDENCE_REVISION
from isar.analyze import gold as gold_mod
from isar.analyze.context import ScanContext, effective_exposure, select_exposure
from isar.analyze.descriptor import describe_spectrum
from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures
from isar.schema.card import Card
from isar.schema.hypotheses import preset as hypothesis_preset
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    INTERVAL_SCHEMA,
    MODEL_SELECTION_CONTRACT,
    PROBABILITY_SCHEMA,
    SHADOW_MODEL_SELECTION_SCHEMA,
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
from isar.session import predictive_artifact
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
# nested-sampling posterior on the prelim-best label. That single-fit Bayesian interval is
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
    """INTRINSIC amplitude of the primary phonon (the mode nearest the reported ω₀).

    Intrinsic, not measured (H4): the branch model's log A(ω₀) trend and the soft/hard
    constraints it emits target the fitted ``phonon.A``, which is the intrinsic strength — the
    acoustic law is then literally A ∝ 1/ω with no occupation contamination.

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
    return float(a) if a is not None else None


def _intensity_measure(rec, ctx, fit) -> dict | None:
    """Portable :class:`IntensityMeasure` dict for the primary phonon (intrinsic amplitude per
    exposure, H4), or None when not computable."""
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
    is the ``ModelScore`` list the driver selects on — posterior ``P(s) = π_s·Z_s / Σ π·Z`` in the
    evidence lane, Akaike weights on the recorded AIC fallback; this only reshapes it, never
    re-ranks. The winner is the row whose ``label`` equals ``Selection.best`` /
    ``FitOutcome.best_label`` (``delta_aic == 0`` identifies it only on the fallback lane; AIC is a
    persisted diagnostic under /2).
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


def _aggregate_selection_eligibility(candidate_records: list[dict]) -> tuple[str, list[dict]]:
    """Strongest complete normalization defect plus every candidate-specific cause."""
    reason_labels: dict[str, list[str]] = {}
    eligibilities = []
    likelihoods = set()
    for candidate in candidate_records:
        label = str(candidate["label"])
        fit_semantics = candidate["fit_semantics"]
        eligibility = str(fit_semantics["selection_eligibility"])
        eligibilities.append(eligibility)
        likelihoods.add(str(fit_semantics["data_likelihood_fingerprint"]))
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
            candidate_records
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
        reasons.append("runner_up_model_weight_ge_0.10")
    return reasons


def _delivery_constraint_origins(results) -> list[str]:
    origins = set()
    for result in results:
        semantics = getattr(result, "fit_semantics", None)
        if not isinstance(semantics, dict):
            continue
        for constraint in semantics.get("effective_constraints") or ():
            origin = constraint.get("origin") if isinstance(constraint, dict) else None
            if origin:
                origins.add(str(origin))
    return sorted(origins)


def _shadow_can_alias_delivery(results, *, structure_skipped: bool) -> bool:
    if structure_skipped:
        return False
    for result in results:
        semantics = getattr(result, "fit_semantics", None)
        if not isinstance(semantics, dict):
            return False
        if semantics.get("estimator_kind") != "data_likelihood_mle":
            return False
        if semantics.get("selection_eligibility") != "eligible":
            return False
        if semantics.get("effective_constraints"):
            return False
    return True


def _shadow_model_identities(results, selection_ctx) -> list[dict]:
    hypotheses = {hypothesis.label: hypothesis for hypothesis in enumerate_phonon_structures(selection_ctx)}
    hypotheses.update(
        {hypothesis.label: hypothesis for hypothesis in enumerate_phonon_multimode(selection_ctx)}
    )
    identities = []
    for result in results:
        hypothesis = hypotheses.get(result.label)
        if hypothesis is None:
            raise SemanticsConstructionError(
                "shadow_candidate_identity_join_failed", str(result.label)
            )
        parameterization = loop.model_parameterization_fingerprint(
            hypothesis, selection_ctx
        )
        identities.append(
            shadow_selection.shadow_model_identity(result.label, parameterization)
        )
    return identities


def _shadow_unavailable_context_fingerprint(stage: str, reason_code: str) -> str:
    return semantics_fingerprint(
        "isar.shadow_selection_context_unavailable/1",
        {"stage": str(stage), "reason_code": str(reason_code)},
    )


def _minimal_shadow_unavailable(reason_code: str, stage: str, *, runtime_seconds: float) -> dict:
    """Last-resort JSON-clean record when even shadow schema construction is unavailable."""
    return {
        "schema": SHADOW_MODEL_SELECTION_SCHEMA,
        "selection_contract_revision": MODEL_SELECTION_CONTRACT,
        "selection_status": "unavailable",
        "selection_complete": False,
        "selection_changed": None,
        "standard_selected_label": None,
        "standard_model_weights": [],
        "unavailable_stage": str(stage),
        "unavailable_reason_code": str(reason_code),
        "runtime_increment_seconds": max(float(runtime_seconds), 0.0),
        "shadow_execution_kind": "unavailable",
        "reused_model_identities": [],
        "freshly_fitted_model_identities": [],
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }


def _safe_shadow_unavailable(reason_code: str, stage: str, *, point, legacy_fingerprint,
                             legacy_selected_label, legacy_weights) -> dict:
    context_fingerprint = point.get("selection_context_fingerprint")
    if not isinstance(context_fingerprint, str):
        context_fingerprint = _shadow_unavailable_context_fingerprint(stage, reason_code)
    try:
        execution_kind = (
            "skipped"
            if reason_code == "shadow_skipped_soft_deadline"
            else "unavailable"
        )
        return shadow_selection.unavailable_shadow_selection(
            reason_code,
            stage=stage,
            selection_context_fingerprint=context_fingerprint,
            candidate_generation_route=point.get("candidate_generation_route") or {
                "route": "shadow_not_evaluated"
            },
            legacy_model_selection_fingerprint=legacy_fingerprint,
            legacy_selected_label=legacy_selected_label,
            legacy_model_weights=legacy_weights,
            offered_model_identities=point.get("offered_model_identities") or (),
            affected_candidate_identities=point.get("affected_candidate_identities") or (),
            delivery_effective_constraint_origins=point.get(
                "delivery_effective_constraint_origins"
            ) or (),
            runtime_increment_seconds=point.get("runtime_increment_seconds", 0.0),
            shadow_execution_kind=execution_kind,
        )
    except Exception as exc:  # noqa: BLE001 — shadow metadata cannot invalidate a scientific fit
        log.warning(
            "shadow selection unavailable-record construction failed stage=%s reason_code=%s "
            "(%s): %s",
            stage, reason_code, type(exc).__name__, exc,
        )
        return _minimal_shadow_unavailable(
            "shadow_unavailable_record_construction_failed",
            "persistence_projection",
            runtime_seconds=point.get("runtime_increment_seconds", 0.0),
        )


def _build_shadow_point_bundle(ctx, all_results, *, structure_labels, peaks,
                               structure_hint, structure_skipped, boot_executor,
                               shadow_enabled) -> dict:
    """Produce point-fit-only standard evidence without touching legacy selection authority."""
    started = time.perf_counter()
    point = {
        "delivery_effective_constraint_origins": _delivery_constraint_origins(all_results),
        "shadow_execution_kind": "unavailable",
        "reused_model_identities": [],
        "freshly_fitted_model_identities": [],
        "candidate_generation_route": {
            "route": "shadow_not_evaluated",
            "supplied_hint": None if structure_hint is None else str(structure_hint),
            "delivery_hint_accepted": bool(structure_skipped),
            "guideline_structure_filter": (
                [str(label) for label in structure_labels] if structure_labels else []
            ),
            "peak_hint_count": int(len(peaks)) if peaks is not None else 0,
        },
    }
    try:
        selection_ctx = replace(ctx, constraints=ParamConstraints())
        point["selection_context_fingerprint"] = loop.data_likelihood_fingerprint(selection_ctx)
    except Exception as exc:  # noqa: BLE001 — shadow context construction is isolated
        log.warning(
            "shadow selection context construction failed (%s): %s",
            type(exc).__name__, exc,
        )
        point.update(
            status="unavailable",
            unavailable_stage="context_construction",
            unavailable_reason_code="shadow_context_construction_failed",
            runtime_increment_seconds=max(time.perf_counter() - started, 0.0),
        )
        return point

    if not shadow_enabled:
        point.update(
            status="unavailable",
            shadow_execution_kind="skipped",
            unavailable_stage="execution_policy",
            unavailable_reason_code="shadow_skipped_soft_deadline",
            runtime_increment_seconds=0.0,
        )
        return point

    try:
        if _shadow_can_alias_delivery(
                all_results, structure_skipped=structure_skipped):
            candidate_bundle = loop.shadow_bundle_from_complete_results(
                all_results, peaks=peaks
            )
        else:
            reusable = (
                all_results
                if not _delivery_constraint_origins(all_results)
                else ()
            )
            candidate_bundle = loop.fit_shadow_candidates(
                selection_ctx,
                reusable_results=reusable,
                peaks=peaks,
                executor=boot_executor,
            )
    except Exception as exc:  # noqa: BLE001 — shadow fitting is isolated from delivery
        log.warning(
            "shadow selection candidate fitting failed (%s): %s",
            type(exc).__name__, exc,
        )
        point.update(
            status="unavailable",
            unavailable_stage="candidate_fitting",
            unavailable_reason_code="shadow_candidate_fitting_failed",
            runtime_increment_seconds=max(time.perf_counter() - started, 0.0),
        )
        return point

    try:
        standard_results = candidate_bundle.results
        guideline_changed = False
        if structure_labels:
            kept = [
                result for result in standard_results
                if result.label in structure_labels
            ]
            if kept:
                guideline_changed = (
                    [result.label for result in kept]
                    != [result.label for result in standard_results]
                )
                standard_results = kept
        selected_labels = {str(result.label) for result in standard_results}
        route = {
            "route": "full_two_stage",
            "supplied_hint": None if structure_hint is None else str(structure_hint),
            "delivery_hint_accepted": bool(structure_skipped),
            "guideline_structure_filter": (
                [str(label) for label in structure_labels] if structure_labels else []
            ),
            "guideline_changed_model_set": bool(guideline_changed),
            "labels_before_guideline": list(candidate_bundle.offered_labels),
            "peak_hint_count": int(len(peaks)) if peaks is not None else 0,
            "multimode_route": candidate_bundle.multimode_route,
            "offered_labels": [str(result.label) for result in standard_results],
            "attempted_labels": [
                label for label in candidate_bundle.attempted_labels
                if label in selected_labels
            ],
            "converged_labels": [
                label for label in candidate_bundle.converged_labels
                if label in selected_labels
            ],
            "failed_labels": [
                label for label in candidate_bundle.failed_labels
                if label in selected_labels
            ],
        }
        identities = _shadow_model_identities(standard_results, selection_ctx)
        identity_by_label = {identity["label"]: identity for identity in identities}
        reused_labels = [
            label for label in candidate_bundle.reused_labels
            if label in selected_labels
        ]
        freshly_fitted_labels = [
            label for label in candidate_bundle.freshly_fitted_labels
            if label in selected_labels
        ]
        if reused_labels and freshly_fitted_labels:
            execution_kind = "partial"
        elif reused_labels:
            execution_kind = "exact"
        else:
            execution_kind = "fresh"
        point.update(
            status="ready",
            shadow_execution_kind=execution_kind,
            candidates=standard_results,
            candidate_bundle=candidate_bundle,
            offered_model_identities=identities,
            attempted_model_identities=[
                identity_by_label[label] for label in route["attempted_labels"]
            ],
            converged_model_identities=[
                identity_by_label[label] for label in route["converged_labels"]
            ],
            failed_model_identities=[
                identity_by_label[label] for label in route["failed_labels"]
            ],
            reused_model_identities=[
                identity_by_label[label] for label in reused_labels
            ],
            freshly_fitted_model_identities=[
                identity_by_label[label] for label in freshly_fitted_labels
            ],
            candidate_generation_route=route,
            runtime_increment_seconds=max(time.perf_counter() - started, 0.0),
        )
        return point
    except Exception as exc:  # noqa: BLE001 — projection cannot invalidate delivery
        log.warning(
            "shadow selection candidate projection failed (%s): %s",
            type(exc).__name__, exc,
        )
        point.update(
            status="unavailable",
            unavailable_stage="candidate_projection",
            unavailable_reason_code="shadow_candidate_projection_failed",
            runtime_increment_seconds=max(time.perf_counter() - started, 0.0),
        )
        return point


def _finalize_shadow_selection(point, selection, fit_selection_semantics) -> dict:
    legacy_fingerprint = (
        fit_selection_semantics.get("model_selection_fingerprint")
        if isinstance(fit_selection_semantics, dict)
        else None
    )
    legacy_weights = list(selection.scores)
    if point.get("status") != "ready":
        return _safe_shadow_unavailable(
            point.get("unavailable_reason_code") or "shadow_point_bundle_unavailable",
            point.get("unavailable_stage") or "point_bundle",
            point=point,
            legacy_fingerprint=legacy_fingerprint,
            legacy_selected_label=selection.best,
            legacy_weights=legacy_weights,
        )
    try:
        return shadow_selection.compare_shadow_selection(
            point["candidates"],
            offered_model_identities=point["offered_model_identities"],
            candidate_generation_route=point["candidate_generation_route"],
            selection_context_fingerprint=point["selection_context_fingerprint"],
            legacy_model_selection_fingerprint=legacy_fingerprint,
            legacy_selected_label=selection.best,
            legacy_model_weights=legacy_weights,
            shadow_execution_kind=point["shadow_execution_kind"],
            reused_model_identities=point["reused_model_identities"],
            freshly_fitted_model_identities=point[
                "freshly_fitted_model_identities"
            ],
            delivery_effective_constraint_origins=point.get(
                "delivery_effective_constraint_origins"
            ) or (),
            runtime_increment_seconds=point["runtime_increment_seconds"],
        )
    except Exception as exc:  # noqa: BLE001 — comparator/validator failure is diagnostic only
        log.warning(
            "shadow selection comparison failed (%s): %s",
            type(exc).__name__, exc,
        )
        return _safe_shadow_unavailable(
            "shadow_comparison_failed",
            "comparison",
            point=point,
            legacy_fingerprint=legacy_fingerprint,
            legacy_selected_label=selection.best,
            legacy_weights=legacy_weights,
        )


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None,
            errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
            descriptor=None, structure_hint=None, bayes_opts=None,
            predictive_n_members: int | None = None, fit_role="provisional_scan",
            shadow_enabled=True):
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
    offered_hypotheses = {}
    results, structure_skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, peaks=peaks, structure_hint=structure_hint,
        executor=boot_executor, hypotheses_out=offered_hypotheses)
    two_stage_seconds = round(time.perf_counter() - _t_two_stage, 4)
    all_results = results
    labels_before_guideline = [str(r.label) for r in results]
    # by_hyp spans both stages so errors.attach can find any surviving label's hypothesis.
    by_hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}
    by_hyp.update({h.label: h for h in enumerate_phonon_multimode(ctx)})
    # …and the catalogue constrained variants (SCENE-1 PR-c), which no re-enumeration can produce:
    # they exist only because a fit came out near-degenerate, and each carries the derived context
    # its evidence and bootstrap must run under.
    by_hyp.update(offered_hypotheses)

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

    shadow_point = _build_shadow_point_bundle(
        ctx,
        all_results,
        structure_labels=structure_labels,
        peaks=peaks,
        structure_hint=structure_hint,
        structure_skipped=structure_skipped,
        boot_executor=boot_executor,
        shadow_enabled=shadow_enabled,
    )

    fits = {r.label: r for r in results}

    prelim = select.select_models(results, by_hyp, ctx, stamp=False)
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
                # A variant survivor bootstraps under its own derived context, so the resampled
                # refits keep its pin instead of quietly re-fitting a different model.
                survivor_items.append(
                    (r, by_hyp[label], loop.candidate_context(by_hyp[label], ctx)))
        error_attachment_labels = [str(item[0].label) for item in survivor_items]
        errors.attach_batch(
            survivor_items, M=M, seed=seed, with_profile=True,
            bayes_label=(prelim.best if run_bayes else None),
            prior=_bayes_prior_for(prior, card), boot_workers=boot_workers,
            executor=boot_executor, boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        errors_attach_seconds = round(time.perf_counter() - _t_errors, 4)
    # Same fits, same arena: reuse the prelim evidence rather than paying the ladder twice.
    # Sound because errors.attach_batch only adds within_model interval material — it never
    # moves theta-hat, cost, covariance, or at_limit, which are all the ladder reads.
    sel = select.select_models(results, by_hyp, ctx, evidence=prelim.evidence)
    candidate_generation_route["scored_labels"] = [str(score.label) for score in sel.scores]
    fit_selection_semantics = _build_selection_semantics(
        results,
        sel,
        candidate_generation_route,
        error_attachment_labels=error_attachment_labels,
    )
    sel.fit_selection_semantics = fit_selection_semantics
    shadow_model_selection = _finalize_shadow_selection(
        shadow_point, sel, fit_selection_semantics
    )
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

    return {
        "verdict": sv.verdict, "confidence": sv.confidence, "reasons": reasons,
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
        "gamma": gamma,
        "cost_per_dof": (best.cost_per_dof if best else None),
        "n_modes": n_modes, "modes": modes,
        "amplitudes": amplitudes, "candidates": candidates,
        "model_manifest": model_manifest,
        "fit_selection_semantics": fit_selection_semantics,
        "shadow_model_selection": shadow_model_selection,
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


def _process_one_task(args):
    """Top-level (picklable) entry point for a scan-pool worker: unpack plain data and fit.

    On Windows spawn the worker re-imports this module; every argument is picklable (verified:
    ScanRecord, Card, Guidelines, RunCalibration). No shared session/card mutation happens here —
    the returned :class:`ScanEntry` carries everything; the parent applies ledger + prior mutations
    serially in submission order."""
    (rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
     M, errors_tier, seed, boot_workers, boot_adaptive, bayes_opts, shadow_enabled) = args
    shadow_kw = {} if shadow_enabled else {"shadow_enabled": False}
    return _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                        chash, ghash, M=M, seed=seed, errors_tier=errors_tier,
                        boot_workers=boot_workers, boot_adaptive=boot_adaptive,
                        bayes_opts=bayes_opts, **shadow_kw)


def _run_tasks(tasks, card, g, runcal, exclusions, *, M, errors_tier, boot_workers,
               use_pool, scan_workers, boot_executor=None, boot_adaptive=False,
               bayes_opts=None, on_complete=None, deadline_gate=None,
               shadow_enabled=True) -> list:
    """Fit the fresh scan-detectors, serially or over a process pool, preserving submission order.

    ``boot_executor`` (a shared bootstrap pool) is threaded only through the SERIAL path — an
    executor is not picklable and must never enter a scan-pool worker (nested pools). The pool path
    only runs when ``boot_workers == 1`` (Layer A owns the workers), so it never wants a shared pool.
    """
    effective_shadow_enabled = bool(
        shadow_enabled
        and (deadline_gate is None or deadline_gate.deadline is None)
    )
    arg_list = [(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
                 M, errors_tier, seed, boot_workers, boot_adaptive, bayes_opts,
                 effective_shadow_enabled)
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
             M_, tier_, seed, bw, adaptive_, bopts_, shadow_enabled_) = a
            shadow_kw = {} if shadow_enabled_ else {"shadow_enabled": False}
            entry = _process_one(rec, det, q, T, sample, scan_id, card_, g_, runcal_,
                                 exclusions_, chash, ghash, M=M_, seed=seed, errors_tier=tier_,
                                 boot_workers=bw, boot_executor=boot_executor,
                                 boot_adaptive=adaptive_, bayes_opts=bopts_,
                                 **shadow_kw)
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
            original_shadow = fit.get("shadow_model_selection")
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
                               descriptor=descriptor, bayes_opts=bayes_opts,
                               shadow_enabled=False)
            except Exception:  # noqa: BLE001 — escalation is best-effort; keep the original fit
                hfit = None
            if hfit is not None and hfit.get("omega0") is not None \
                    and not hfit.get("gamma_at_bound"):
                hfit["shadow_model_selection"] = original_shadow
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
        shadow_model_selection=fit.get("shadow_model_selection"),
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
                 boot_executor=None, boot_adaptive=False, bayes_opts=None,
                 shadow_enabled=True) -> ScanEntry:
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
                      bayes_opts=bayes_opts, shadow_enabled=shadow_enabled)
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

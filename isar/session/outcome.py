"""The per-scan persistence DTO and its two projections.

Three things live here, all of them persistence-layer:

* :class:`FitOutcome` — the dataclass every fitted detector result is stored as
  (``ScanEntry.detectors[det] == FitOutcome.to_dict()`` in ``session.json``);
* :func:`_coerce_fit_outcome` / :func:`_outcome_from_entry` — the tolerant historical reader that
  rehydrates a stored dict, dropping (and logging) keys a newer writer added;
* ``_CSV_COLUMNS`` + :func:`_write_results_csv` and its cell helpers — the ``results.csv``
  projection of the ledger (one summary row per spectrum), and ``_MODES_COLUMNS`` — its linked
  ``modes.csv`` (one row per fitted mode of the selected count, ordered by energy).

The *scientific producers* stay in :mod:`isar.session.driver`: nothing here fits, selects, or
interprets a number — it only declares the storage shape and flattens stored dicts. Accordingly this
module imports stdlib + schema/session-state code only (no scipy, iminuit, fitting or Qt), so the
persistence contract can be read without dragging in the fitter.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from isar.schema.statistics import compact_for_csv
from isar.schema.steering import CANONICAL_MASTER
from isar.session.atomic import replace_retrying
from isar.session.state import ScanEntry, SessionState
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
    # ({linear_param_name: value} for the winning structure — phonon height(s) ``…h``, elastic
    # ``elastic.h``, quasi-elastic ``qe.QE``, background ``bg.BG``), the same linear params the
    # fitter solved, kept so a params-only curve evaluator can rebuild the model without re-solving
    # NNLS against counts (report.audit.reconstruct_curve does the latter today). ``candidates``: one
    # compact row per SCORED structure ({label, k, ndof, cost, cost_per_dof, n_modes, delta_aic,
    # akaike_weight, posterior_probability, log_evidence, evidence_method, usable}) — the fit
    # inspector's standing answer to "what did ISAR choose and why". Selection is evidence-driven
    # (isar.model_selection/2): the winner is the row whose label equals ``best_label``
    # (delta_aic == 0 identifies it only on the recorded AIC fallback lane; AIC stays a persisted
    # diagnostic); non-converged fits are absent. Both None on older ledgers, when there is no
    # delivered fit, or when nothing converged.
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
    # STAT-2a diagnostics only. It cannot drive any scientific or steering authority in this phase.
    shadow_model_selection: dict | None = None
    # SCENE-1 PR-c persistence (commit D), all additive and all records-only.
    # ``at_limit``: the DELIVERED (winning) fit's per-parameter boundary-pin flags, keyed by the real
    # dotted parameter names — the same map the evidence validity ladder reads, kept so a reader can
    # see which parameter railed without re-fitting. ``None`` on older ledgers and when nothing
    # converged.
    at_limit: dict | None = None
    # ``evidence_identity``: the catalogue phenomenon the winning candidate CLAIMED (open vocabulary,
    # never an enum); ``None`` for the identity-agnostic candidate, which is every candidate the
    # production catalogue emits today. ``identity_ba_disagreement``: the records-only diagnostic
    # noting that this identity and the branch-assignment label disagree. Nothing in the verdict,
    # confidence, stopping or steering path reads either — the (Q,E) object that OWNS an identity
    # lives in the scene store (SCENE-3); this scan only contributes evidence to it.
    evidence_identity: str | None = None
    identity_ba_disagreement: dict | None = None
    # ``energy_window``: (lo, hi) meV of the calibrated, QC-masked energy axis this fit ran on — the
    # window the scan actually measured, which the scene layer reads as an observation footprint (a
    # searched-and-empty window is evidence). Distinct from ``search_energy_lo/hi``, which is an
    # auxiliary probe's REQUESTED window. ``None`` on older ledgers and when no point survived the mask.
    energy_window: tuple | None = None
    # ``energy_bands``: the measured CELLS of that same axis's kept bins, merged into runs,
    # ``[[lo, hi], …]``. The window above is the kept POINT extent; these are the measured ground,
    # so a QC mask or an excluded region in the middle of the axis stays a hole instead of being
    # claimed as searched coverage, and the end cells reach half a bin past the window.
    # ``None`` on older ledgers and wherever the window is.
    energy_bands: list | None = None
    # The fast pass's result as persisted (C-fastpass-record, ``isar.fast-pass/1``, written once by
    # scan_delivery.fit_one): the count state and probabilities, every model tried, the selected
    # model's peaks and the flags. ``None`` for an outcome with no fast pass. Read via fast_pass_of.
    fast_pass: dict | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["interval"] = list(self.interval) if self.interval is not None else None
        d["omega0_profile"] = list(self.omega0_profile) if self.omega0_profile is not None else None
        d["hesse_interval"] = list(self.hesse_interval) if self.hesse_interval is not None else None
        d["bayes_interval"] = list(self.bayes_interval) if self.bayes_interval is not None else None
        d["model_interval"] = list(self.model_interval) if self.model_interval is not None else None
        d["energy_window"] = list(self.energy_window) if self.energy_window is not None else None
        return d


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


_CSV_COLUMNS = [
    "scan", "detector", "sample", "T", "h", "k", "l", "status", "verdict", "disposition",
    "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
    "omega0_profile_lo", "omega0_profile_hi", "hesse_lo", "hesse_hi", "hesse_trusted",
    "bayes_lo", "bayes_med", "bayes_hi", "bayes_ok", "bayes_ess", "bayes_fail_reason",
    "bayes_freq_ratio", "hesse_freq_ratio", "bayes_prior_driven", "bayes_fork_suspected",
    "bayes_unconstrained", "log_evidence", "log_evidence_err",
    "pooled", "n_members", "members",
    "gamma", "gamma_provisional", "modality", "n_modes",
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
    # STAT-2a diagnostics-only comparison.
    "shadow_model_selection",
    # Evidence-driven selection (isar.fit_selection_semantics/2), appended for positional legacy
    # CSV readers. ``best_model_posterior`` is the winner's P(model) from the candidate table —
    # deliberately NOT named log_evidence*: the log_evidence/log_evidence_err columns above are
    # the bayes-tier within-model diagnostic, a different lane; per-candidate selection evidence
    # lives inside the candidates/fit_selection_semantics JSON cells.
    "selection_method", "best_model_posterior",
    # SCENE-1 PR-c, appended (this list stays positional for legacy CSV readers): the winning
    # candidate's claimed catalogue identity — a plain string, empty on every historical row and on
    # every identity-agnostic fit. The dict-valued companions (``at_limit``,
    # ``identity_ba_disagreement``) deliberately stay out of the compact projection; they are
    # per-parameter / diagnostic detail and session.json already carries them in full.
    "evidence_identity",
    # SCENE-3, appended (this list stays positional for legacy CSV readers): the calibrated, masked
    # energy window this fit ran on. Empty on every historical row and wherever no point survived
    # the mask. Both are floats — see _NUMERIC_COLUMNS in isar.drive.isar_io.readers.
    "energy_lo", "energy_hi",
    # SCENE-3, appended: the kept energy runs as one JSON cell (variable length, so not float
    # columns) — see _JSON_COLUMNS in isar.drive.isar_io.readers. Empty wherever energy_lo/hi are.
    "energy_bands",
    # SCENE-4 PR 4.0 review response (W3, #26 join hardening): the delivery-generation identity
    # (``ScanEntry.content_hash``) this row was fitted against — the same value the measurement-
    # arena record stamps, so the scene adapter can refuse to join a fit row from delivery N to a
    # measurement record from delivery N+1. Scan-level, not per-detector, so every detector row of
    # the same scan carries the same value. Empty on every historical row.
    "content_hash",
    # The fast pass's count outcome (C-fastpass-record), projected by _fast_pass_columns: the state and
    # its reasons, the preferred count's probability and every count's, whether they are conditional
    # or numerically marginal, the search limit, the confident and tentative peak counts, the flags and
    # the deep-pass candidacy. Every mode of the selected count is a row of the linked modes.csv.
    "count_state", "count_state_reason", "count_probability", "count_probabilities",
    "probabilities_conditional", "numerically_marginal", "search_limit_reason",
    "n_confident", "n_tentative", "flags", "deep_pass_candidate",
]

# report/modes.csv: one row per fitted mode of the selected count, keyed to its results.csv row by
# scan_id + detector, mode_index 0 the lowest energy. omega0 is the undamped DHO energy E0 (the
# fitted parameter), peak_position the damped frequency sqrt(E0^2 - Gamma^2), empty when the mode is
# overdamped (Gamma >= E0); a missing value stays empty. The columns after hesse_trusted come from the
# mode's fast-pass peak (joined by index); Q_nm is the analyzer's |Q| when the data provides it.
_MODES_COLUMNS = [
    "scan_id", "scan", "detector", "sample", "T", "h", "k", "l", "count_state", "best_model",
    "n_modes", "mode_index", "omega0", "omega0_err", "peak_position", "peak_position_err",
    "gamma", "gamma_err", "amplitude", "amplitude_intrinsic", "uncertainty_kind",
    "hesse_trusted", "content_hash",
    "Q_nm", "confident", "delta", "regime", "omega0_status", "gamma_status", "height", "height_err",
    "area_stokes", "area_stokes_err", "area_status", "corr_omega0_gamma", "deletion_impact",
    "manual_fit",
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


def _best_model_posterior(oc: dict):
    """The winner's posterior probability from the candidate table.

    None on the recorded AIC fallback lane (posterior_probability is None there), on older
    ledgers, and when nothing converged — absence is the honest degraded-lane face.
    """
    best = oc.get("best_label")
    if best is None:
        return None
    for row in oc.get("candidates") or ():
        if row.get("label") == best:
            return row.get("posterior_probability")
    return None


def fast_pass_of(oc: dict) -> dict:
    """The stored fast-pass record (``oc["fast_pass"]``), ``{}`` when absent; never raises."""
    return (oc or {}).get("fast_pass") or {}


# The non-fit count states, spelled here so this read path does not import the fitting stack.
NOT_FITTED = "not fitted"       # excluded, undersampled, or otherwise no fit
NOT_RECORDED = "not recorded"   # a detector outcome with no fast-pass record


def _unfitted_count(e: ScanEntry) -> dict:
    """The count columns of a scan with no detector outcome: a held scan is ``held`` with its hold
    reason, an ingested scan is still ``fitting`` (no result yet), any other is ``not fitted``."""
    if e.status.startswith("held"):
        state = "held"
    elif e.status == "ingested":
        state = "fitting"
    else:
        state = NOT_FITTED
    return {"count_state": state, "count_state_reason": "; ".join(e.reasons) or e.status}


def _finite(value):
    """A number as written: a non-finite one is missing, never a ``nan`` cell."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _count_reason(fp: dict) -> str | None:
    """The count state's reasons, read from the record alone: the probabilities, the close-call
    alternatives, the counts without evidence and why, and the search limit."""
    probs = fp.get("count_probabilities") or {}
    parts = []
    if probs:
        parts.append("counts " + ", ".join(f"{k} ({p:.2f})" for k, p in probs.items()))
    alternatives = fp.get("close_call_alternatives") or []
    if alternatives:
        parts.append("close call with alternatives " + ", ".join(
            f"{k} ({probs.get(str(k), 0.0):.2f})" for k in alternatives))
    if fp.get("probabilities_conditional"):
        parts.append("probabilities conditional on the counts with evidence")
    if fp.get("numerically_marginal"):
        parts.append("numerically marginal")
    # only the contending counts with no priced basin, each once, with its best basin's reason
    for k in sorted(set((fp.get("flag_statistics") or {}).get("unpriced_contenders") or ())):
        basins = [m for m in fp.get("models") or ()
                  if m.get("n_peaks") == k and m.get("evidence_status") == "no_evidence"]
        best = max(basins, default={},
                   key=lambda m: -math.inf if m.get("ln_like") is None else m["ln_like"])
        parts.append(f"count {k} has no evidence ({best.get('reason') or 'unknown'})")
    if fp.get("search_limit_reason"):
        parts.append(f"search limited ({fp['search_limit_reason']})")
    return "; ".join(parts) or None


def _fast_pass_columns(oc: dict) -> dict:
    """The results.csv count columns of one stored outcome, projected from its fast-pass record."""
    fp = fast_pass_of(oc)
    if not fp:
        return {"count_state": NOT_RECORDED, "count_state_reason": "no fast-pass record"}
    probs = fp.get("count_probabilities") or {}
    preferred = fp.get("preferred_count")
    peaks = fp.get("peaks") or []
    n_confident = sum(1 for p in peaks if p.get("confident"))
    flags = [f"{f['kind'].replace('_', ' ')} {f['e_lo']:.2f}-{f['e_hi']:.2f} meV"
             for f in fp.get("flags") or []]
    return {
        "count_state": fp.get("state"), "count_state_reason": _count_reason(fp),
        "count_probability": probs.get(str(preferred)) if preferred is not None else None,
        "count_probabilities": json.dumps(probs, sort_keys=True),
        "probabilities_conditional": fp.get("probabilities_conditional"),
        "numerically_marginal": fp.get("numerically_marginal"),
        "search_limit_reason": fp.get("search_limit_reason") or None,
        "n_confident": n_confident, "n_tentative": len(peaks) - n_confident,
        "flags": "; ".join(flags) or None,
        "deep_pass_candidate": fp.get("deep_pass_candidate"),
    }


def _mode_rows(e: ScanEntry, det: str, oc: dict) -> list:
    """The modes.csv rows of one stored outcome, ordered by energy. Each mode joins the selected
    model's fast-pass peak of the same index (ascending omega0), which carries its confidence, regime,
    statuses and height and area."""
    modes = sorted((m for m in (oc.get("modes") or []) if isinstance(m, dict)
                    and _finite(m.get("omega0")) is not None),
                   key=lambda m: float(m["omega0"]))
    fp = fast_pass_of(oc)
    peaks = {p.get("index"): p for p in fp.get("peaks") or () if isinstance(p, dict)}
    q = oc.get("q") or [None, None, None]
    q_nm = (((e.provenance or {}).get("analyzer_facts") or {}).get("Q_nm") or {}).get("value")
    rows = []
    for index, mode in enumerate(modes):
        err = _finite(mode.get("omega0_err"))
        peak = peaks.get(index) or {}
        status = peak.get("status") or {}
        rows.append({
            "scan_id": e.scan_id, "scan": e.scan_number, "detector": det,
            "sample": oc.get("sample"), "T": oc.get("T"), "h": q[0], "k": q[1], "l": q[2],
            "count_state": fp.get("state") or NOT_RECORDED,
            "best_model": oc.get("best_label"),
            "n_modes": len(modes), "mode_index": index,
            "omega0": mode.get("omega0"), "omega0_err": err,
            "peak_position": _finite(mode.get("peak_position")),
            "peak_position_err": _finite(mode.get("peak_position_err")),
            "gamma": _finite(mode.get("gamma")), "gamma_err": _finite(mode.get("gamma_err")),
            "amplitude": _finite(mode.get("amplitude")),
            "amplitude_intrinsic": _finite(mode.get("amplitude_intrinsic")),
            "uncertainty_kind": "hesse" if err is not None else None,
            "hesse_trusted": mode.get("hesse_trusted"), "content_hash": e.content_hash,
            "Q_nm": q_nm, "confident": peak.get("confident"), "delta": peak.get("delta"),
            "regime": peak.get("regime"), "omega0_status": status.get("omega0"),
            "gamma_status": status.get("Gamma"), "height": peak.get("height"),
            "height_err": peak.get("sigma_height"), "area_stokes": peak.get("area_stokes"),
            "area_stokes_err": peak.get("sigma_area"), "area_status": peak.get("area_status"),
            "corr_omega0_gamma": peak.get("corr_omega0_Gamma"),
            "deletion_impact": peak.get("deletion_impact"), "manual_fit": peak.get("manual_fit"),
        })
    return rows


def _replace_csv(path: Path, columns: list, rows: list) -> None:
    """Write ``rows`` to a temporary file in full and swap it in, so no reader sees a torn file."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in columns})
        f.flush()
        os.fsync(f.fileno())
    replace_retrying(temporary, path)


def _write_results_csv(workspace: Workspace, session: SessionState) -> Path:
    """Write ``report/results.csv`` and its linked ``report/modes.csv``, each replaced whole."""
    workspace.report_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.report_dir / "results.csv"
    rows = []
    mode_rows = []
    for e in sorted(session.scans.values(), key=lambda e: e.scan_number):
        if not e.detectors:
            rows.append({"scan_id": e.scan_id, "scan": e.scan_number, "status": e.status,
                         "reasons": "; ".join(e.reasons), "content_hash": e.content_hash,
                         **_unfitted_count(e)})
            continue
        for det, oc in e.detectors.items():
            q = oc.get("q") or [None, None, None]
            iv = oc.get("interval")
            piv = oc.get("omega0_profile")
            hiv = oc.get("hesse_interval")
            biv = oc.get("bayes_interval")
            miv = oc.get("model_interval")
            ew = oc.get("energy_window")
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
            mode_rows.extend(_mode_rows(e, det, oc))
            rows.append({
                "scan_id": e.scan_id, "scan": e.scan_number, "detector": det, "sample": oc.get("sample"),
                "content_hash": e.content_hash,
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
                # Absent/unrecognised means canonical_master (see FitOutcome.evidence_sink), so
                # historical CSVs stay readable — but a written row must carry the recorded sink,
                # or auxiliary-probe rows read as canonical evidence.
                "evidence_sink": oc.get("evidence_sink", CANONICAL_MASTER),
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
                "n_modes": oc.get("n_modes") if oc.get("n_modes") is not None else 1,
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
                "shadow_model_selection": (
                    json.dumps(
                        compact_for_csv(oc.get("shadow_model_selection")),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if oc.get("shadow_model_selection") is not None
                    else None
                ),
                "selection_method": (
                    (oc.get("fit_selection_semantics") or {}).get("selection_method")
                ),
                "best_model_posterior": _best_model_posterior(oc),
                "evidence_identity": oc.get("evidence_identity"),
                "energy_lo": (ew[0] if ew else None), "energy_hi": (ew[-1] if ew else None),
                "energy_bands": (json.dumps(oc.get("energy_bands"), sort_keys=True)
                                 if oc.get("energy_bands") is not None else None),
                **_fast_pass_columns(oc),
            })
    _replace_csv(path, _CSV_COLUMNS, rows)
    _replace_csv(workspace.report_dir / "modes.csv", _MODES_COLUMNS, mode_rows)
    return path

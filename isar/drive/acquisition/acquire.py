"""The acquisition function — propose the next SCAN (LLM_HARNESS_DESIGN §2.2, §8 step 4.1).

**The acquisition unit is a SCAN, not a point.** A proposal is a complete constant-Q energy scan:

- a **Q position** — one objective-path grid point (the driver scans at fixed H,K,L and sweeps deltaE);
- an **energy window** — centered on the predicted ω(q), spanning **both sides of elastic** so the
  Stokes/anti-Stokes pair lands inside when ``kT ≳ ħω`` (low side ``−(E_pred + 2)`` meV), with a tight
  high side (``E_pred + 1.5 × resolution FWHM`` from ``GET /resolution``) to exclude higher-branch
  leakage — ISAR's N-mode fitter is the safety net if something leaks anyway;
- a **step** ≈ resolution FWHM / 4 (steps ≪ FWHM waste points, ≫ it undersample), clamped so the scan
  has ≤ 100 points (the budget cap);
- a **neutron count** — escalating from a cheap quick-look (:data:`QUICKLOOK_NCOUNT`, to seed the count
  rate / ETA) to a production count (:data:`PRODUCTION_NCOUNT`) only where the tolerance demands it.

**Target selection.** Unvisited grid q are quick-looked first (largest model-predicted σ). Once every q
has at least a quick-look, the q with the largest calibrated-σ-above-tolerance **deficit** is escalated:
production, then — after a resolved first crossing — a dedicated ``confirm`` scan (the peeking-bias
guard lives in :mod:`isar.drive.supervisor.stopping`).

"Peak outside the chosen window" is a first-class outcome, not a failure (recover: widen / re-center —
a LOOK, §1, handled in :mod:`isar.drive.supervisor.exceptions`). Cost is movement-aware once TAVI's
virtual instrument clock exists; until then the function counts neutrons only.
"""
from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from isar.drive.acquisition.eig import (
    counts_eig,
    eig_per_cost,
    fisher_eig,
    predicted_sigma_after,
)
from isar.drive.acquisition.classify_eig import widen_factor
from isar.predictive import predictive_score
from isar.drive.supervisor.stopping import (
    PRODUCTION_NCOUNT,
    QUICKLOOK_NCOUNT,
    GridQ,
    summarize_grid,
)
from isar.drive.acquisition.inventory import (
    ActionEligibility,
    ActionEvaluation,
    ActionInventory,
    GENERATOR_REGISTRY,
    GeneratorReport,
    GeneratorStatus,
    LEGACY_SELECTOR_GENERATOR,
    MeasurementIntent,
    NARROW_RIVAL_PROBE_GENERATOR,
    RejectionClass,
)
from isar.drive.acquisition.scan_design import (
    AUXILIARY_DETECTION_KINDS,
    DEFAULT_FWHM_MEV,
    HIGH_RES_FACTOR,
    LOW_MARGIN_MEV,
    MAX_POINTS,
    STEP_RES_FRACTION,
    SURVEY_E_HI_MEV,
    SURVEY_SIGMA_K,
    BudgetInsufficientForMinimumBatch,
    InstrumentLimitBelowMinimumBatch,
    ScanDesignContext,
    ScanDesignInventory,
    _count_request as _design_count_request,
    _declared_window,
    _energy_window,
    design_scan_windows,
    points_in_window,
)
from isar.drive.acquisition.scan_grid import format_scan_command
from isar.schema.steering import AUXILIARY_TRACK_NODE, NON_CANONICAL_SINKS, ProposalScore
from isar.schema.hypotheses import MAX_NCOUNT_PER_SCAN

# Where an identity probe sits in the selection order. Above zero so every ordinary action outranks it:
# the probe is what to try when the cheap path has run out, never a competitor for beam while ordinary
# work remains. One shared constant so the identity generators cannot drift apart.
IDENTITY_PROBE_RANK = 100.0

ASSOCIATION_UNCERTAINTY_PER_UNMEASURED_STEP = 0.15


@dataclass(frozen=True)
class ShadowScoringSource:
    """Exact completed-master evidence carried to the live shadow-scoring adapter.

    This value is deliberately in-memory only.  It freezes the selected action's source records at
    realization time so the adapter never has to search the workspace for a plausible same-Q or
    newer master after action selection has already happened.
    """

    master_session_key: str | None
    detector_id: str | None
    master_revision: str | None
    source_acquisition_id: str | None
    source_submission_request_fingerprint: str | None
    scientific_action_fingerprint: str
    predictive_ref: str | None
    target_projection_ref: str | None
    target_outcome: dict[str, Any] | None
    selection_record: dict[str, Any] | None
    shadow_record: dict[str, Any] | None
    target_projection_authority: dict[str, Any] | None
    action_intent: MeasurementIntent
    action_provenance: dict[str, Any]


@dataclass
class ScanProposal:
    """A complete constant-Q energy-scan request the instrument client can submit."""

    h: float
    k: float
    l: float
    e_lo: float
    e_hi: float
    e_step: float
    ncount: int
    kind: str                # "quicklook" | "production" | "confirm"
    reason: str
    e_pred: float = 0.0
    fwhm_dE: float = 0.0
    n_points: int = 0
    acquisition_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # In-memory only: refresh exposure-sensitive score fields after runtime clamps. Deliberately
    # omitted from to_dict(), so the event/file contract remains plain JSON.
    score_recompute: Callable[[int], dict[str, float | None]] | None = field(
        default=None, repr=False, compare=False)
    # In-memory only: the scientific question this request realizes. The JSON contract carries only
    # ``meta["scientific_action_fingerprint"]``; the object itself is what lets the loop build the
    # finalized stage against the intent it belongs to instead of against a bare hash string.
    intent: MeasurementIntent | None = field(default=None, repr=False, compare=False)
    # In-memory only: the complete scan-design inventory that produced this planned request. The drive
    # loop persists it after finalization and adds only its content-addressed reference to ``meta``.
    scan_design_inventory: ScanDesignInventory | None = field(
        default=None, repr=False, compare=False)
    # In-memory only: exact source evidence for diagnostic scan-window scoring.  It is intentionally
    # absent from ``to_dict`` and therefore cannot affect the request fingerprint or wire payload.
    shadow_scoring_source: ShadowScoringSource | None = field(
        default=None, repr=False, compare=False)

    def scan_command(self) -> str:
        """The TAVI ``scan_command1`` for this window: ``deltaE <lo> <hi> <step>``.

        Rendered by the shared grid helper, which is also what candidate construction formats — one
        rendering, so a designed window and the submitted request cannot drift apart.
        """
        return format_scan_command(self.e_lo, self.e_hi, self.e_step)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hkl": [self.h, self.k, self.l], "e_lo": self.e_lo, "e_hi": self.e_hi,
            "e_step": self.e_step, "ncount": self.ncount, "kind": self.kind, "reason": self.reason,
            "e_pred": self.e_pred, "fwhm_dE": self.fwhm_dE, "n_points": self.n_points,
            "acquisition_id": self.acquisition_id,
            "scan_command": self.scan_command(), "meta": dict(self.meta),
        }


def _fisher_supports_exposure(provider) -> bool:
    """Whether a Fisher seam accepts the additive fourth exposure argument (legacy 3-arg stays valid)."""
    if bool(getattr(provider, "supports_exposure", False)):
        return True
    try:
        inspect.signature(provider).bind(None, None, None, None)
    except (TypeError, ValueError):
        # Some extension callables do not expose a signature; without an explicit capability marker,
        # preserve the legacy three-argument protocol instead of risking a runtime TypeError.
        return False
    return True


def normalize_proposal_score(proposal: ScanProposal, tolerance: float) -> None:
    """Make score diagnostics describe the proposal's current emitted point/count batch."""
    score = (proposal.meta or {}).get("score")
    if not isinstance(score, dict):
        return
    dc = float(proposal.n_points) * float(proposal.ncount)
    method = score.get("method")
    if method == "fisher" and proposal.score_recompute is not None:
        score.update(proposal.score_recompute(int(proposal.ncount)))
    sigma_prior = score.get("sigma_prior")
    c_spent = float(score.get("c_spent") or 0.0)
    if method in ("counts", "deficit") and sigma_prior is not None and c_spent > 0.0:
        eig = counts_eig(float(sigma_prior), float(tolerance), c_spent, dc)
        sigma_scan = predicted_sigma_after(float(sigma_prior), c_spent, dc)
        classification = float(score.get("classification_eig") or 0.0)
        combined = eig + float(score.get("classification_weight") or 0.0) * classification
        score.update({"eig": _finite_or_none(eig),
                      "sigma_scan": _finite_or_none(sigma_scan),
                      "combined_information": _finite_or_none(combined)})
    information = score.get("combined_information")
    if information is None:
        information = score.get("eig")
    score["cost"] = dc
    score["eig_per_cost"] = (None if information is None
                             else _finite_or_none(eig_per_cost(float(information), dc)))


def _count_request(gridq: GridQ, *, kind: str, n_points: int, tolerance: float,
                   policy: dict, budget_state: dict | None,
                   runtime_ncount_limit: int | None = None) -> tuple[int, dict[str, Any]]:
    """Compatibility adapter for direct callers; runtime design uses :func:`design_legacy_scan`."""
    policy = dict(policy or {})
    budget = dict(budget_state or {})
    master = gridq.master or {}
    context = ScanDesignContext(
        scientific_action_fingerprint="compatibility-count-request",
        hkl=tuple(float(value) for value in gridq.hkl),
        q=float(gridq.q),
        kind=str(kind),
        reason="compatibility count request",
        window_family="target",
        declared_energy_lo=None,
        declared_energy_hi=None,
        target_prediction_mean_meV=0.0,
        target_prediction_sigma_meV=0.0,
        design_prediction_mean_meV=0.0,
        design_prediction_sigma_meV=0.0,
        resolution_fwhm_meV=DEFAULT_FWHM_MEV,
        survey=False,
        energy_search_meV=None,
        target_band=None,
        rival_band=None,
        widen_factor=1.0,
        positive_only=False,
        policy_max_points=max(int(n_points), 2),
        runtime_max_points=max(int(n_points), 2),
        tolerance_meV=float(tolerance),
        quicklook_ncount=int(policy.get("quicklook_ncount", QUICKLOOK_NCOUNT)),
        production_ncount=int(policy.get("production_batch_ncount", PRODUCTION_NCOUNT)),
        identity_detection_ncount=int(policy.get(
            "identity_detection_ncount",
            policy.get("quicklook_ncount", QUICKLOOK_NCOUNT))),
        dynamic_ncount=bool(policy.get("dynamic_ncount", False)),
        tolerance_margin=float(policy.get("tolerance_margin", 0.8)),
        ncount_growth_cap=float(policy.get("ncount_growth_cap", 4.0)),
        max_ncount_per_scan=int(policy.get("max_ncount_per_scan", MAX_NCOUNT_PER_SCAN)),
        diagnostic_halfwidth_meV=master.get(
            "diagnostic_halfwidth", master.get("halfwidth")),
        certification_halfwidth_meV=gridq.cert_halfwidth,
        effective_exposure=master.get("effective_exposure"),
        submitted_exposure=gridq.submitted_exposure,
        production_scans=int(gridq.n_production),
        last_submitted_ncount=gridq.last_submitted_ncount,
        runtime_max_neutrons_per_point=runtime_ncount_limit,
        runtime_queue_neutron_budget=None,
        budget_kind=budget.get("kind"),
        budget_amount=budget.get("amount"),
        budget_spent=float(budget.get("spent", 0.0)),
    )
    requested, diagnostics = _design_count_request(context, int(n_points))
    return requested, dict(diagnostics)


def _resolve_fwhm(resolution_info, h: float, k: float, l: float, deltaE: float) -> float:
    """Extract the energy-resolution FWHM (meV) from ``resolution_info``.

    Accepts a plain float, a dict (``{"fwhm_dE": x}`` or a raw ``GET /resolution`` body with
    ``fwhm.dE``, honoring ``ok == false`` refusals), or a callable ``(h,k,l,deltaE) -> (any of these)``.
    Falls back to :data:`DEFAULT_FWHM_MEV` when nothing usable is present.
    """
    info = resolution_info(h, k, l, deltaE) if callable(resolution_info) else resolution_info
    return _fwhm_from(info)


def _fwhm_from(info) -> float:
    if info is None:
        return DEFAULT_FWHM_MEV
    if isinstance(info, (int, float)):
        val = float(info)
        return val if val > 0 else DEFAULT_FWHM_MEV
    if isinstance(info, dict):
        if info.get("ok") is False:               # /resolution refusal (infeasible geometry)
            return DEFAULT_FWHM_MEV
        if "fwhm_dE" in info and info["fwhm_dE"]:
            return float(info["fwhm_dE"])
        fwhm = info.get("fwhm")
        if isinstance(fwhm, dict) and fwhm.get("dE"):
            return float(fwhm["dE"])
        if isinstance(fwhm, (int, float)) and fwhm > 0:
            return float(fwhm)
    return DEFAULT_FWHM_MEV


def identity_domain(objective, *, rival_energy_meV: float | None,
                    fwhm_meV: float = DEFAULT_FWHM_MEV) -> tuple[tuple[float, float], str | None]:
    """The energy band an identity-class action may scan, and why it may not be widened.

    Returns ``(domain, forbidden_reason)``. A non-``None`` reason means the rival-supporting action is
    *policy-forbidden*: the inventory reports it as an ``OPERATOR`` rejection rather than silently
    scanning outside what the campaign declared.

    The rules, in order:

    1. A **declared** identity domain is the answer. Nothing to migrate.
    2. No rival to reach means nothing to widen for.
    3. A rival **already inside** the target band needs no widening, so no provenance question arises.
       Every rule below decides whether the band may be *widened*; asking them first reported
       ``policy_forbidden`` for a rival the operator had already authorised, disabling identity work
       inside the very range they declared. Containment is tested on the rival *centre* alone —
       admitting a rival whose shoulders fall outside would be widening by another name.
    4. A band the **compiler** chose is a default, not an instruction, so it may be widened to admit
       the rival that settles the identity — bounded by that rival plus a few resolution widths, not
       opened to an arbitrary band.
    5. A band the **operator** declared is a hard constraint. Spending their beam time outside the
       range they asked for is not ours to decide.
    6. A band with **no provenance** (an old frozen objective) is treated like the operator's. That a
       range equals the shipped default is not evidence the compiler chose it, and guessing wrong
       here silently rewrites a scientist's declared intent.
    """
    target = tuple(objective.energy_search_meV)
    declared = getattr(objective, "identity_search_meV", None)
    if declared is not None:
        return (float(declared[0]), float(declared[1])), None
    if rival_energy_meV is None or not math.isfinite(float(rival_energy_meV)):
        return target, None
    if target[0] <= float(rival_energy_meV) <= target[1]:
        return target, None                        # already authorised; nothing to widen
    source = getattr(objective, "target_search_source", None)
    if source == "operator":
        return target, (
            "policy_forbidden: the operator declared the search band "
            f"{target[0]:g}..{target[1]:g} meV and the rival support at {rival_energy_meV:g} meV "
            "lies outside it; declare identity_search_meV to authorise identity work there")
    if source != "compiler":
        return target, (
            "policy_forbidden: this objective carries no search-band provenance, so the band "
            f"{target[0]:g}..{target[1]:g} meV cannot be assumed to be a compiler default; declare "
            "identity_search_meV (or re-compile the objective) to authorise identity work at "
            f"{rival_energy_meV:g} meV")
    # Finite and instrument-aware: enough to observe the rival and its shoulders, not a free hand.
    margin = 3.0 * max(float(fwhm_meV), 1e-3)
    rival = float(rival_energy_meV)
    return (min(target[0], rival - margin), max(target[1], rival + margin)), None


def _q_sigma(g: GridQ) -> float | None:
    """The current pooled-master ω₀ half-width belief, never an individual-fit fallback."""
    master = g.master or {}
    value = master.get("diagnostic_halfwidth", master.get("halfwidth"))
    return float(value) if value is not None else None


def _q_next_cost(g: GridQ) -> float:
    """The ΔC of this q's next production scan — its most recent production cost, else its accumulated
    cost as a proxy (a scale for the EIG-per-cost key, not a submitted number)."""
    return float(g.last_prod_cost) if g.last_prod_cost else float(g.counts_spent)


def _finite_or_none(x) -> float | None:
    """``x`` if finite, else ``None`` — so ``+inf`` scores round-trip through strict JSON."""
    return float(x) if x is not None and math.isfinite(x) else None


def _driver_inserted_candidate(objective, grid: list[GridQ], observations: list[dict],
                               classify) -> GridQ | None:
    """Choose one deterministic classification probe outside the certification grid.

    The operator path remains the complete certification set.  After every requested point has at
    least a quicklook, O9 permits a higher-classification-value midpoint or segment extension to win
    one scan.  Already observed inserted points are skipped, so resume cannot repeat the same probe.
    """
    if classify is None or not getattr(objective, "hypotheses", None) or len(grid) < 2:
        return None
    if not all(g.visited for g in grid):
        return None

    path = [tuple(map(float, point)) for point in objective.path]
    scalars = [0.0]
    for left, right in zip(path, path[1:]):
        scalars.append(scalars[-1] + math.sqrt(sum((b - a) ** 2 for a, b in zip(left, right))))
    candidates: list[tuple[tuple[float, float, float], float]] = []
    for i, (left, right) in enumerate(zip(path, path[1:])):
        candidates.append((tuple((a + b) / 2.0 for a, b in zip(left, right)),
                           (scalars[i] + scalars[i + 1]) / 2.0))
    first_delta = tuple(b - a for a, b in zip(path[0], path[1]))
    last_delta = tuple(b - a for a, b in zip(path[-2], path[-1]))
    # Multiple deterministic extension shells make the authority genuinely able to leave the
    # requested segment; acquisition value, not a special-case branch rule, chooses among them.
    for shell in (1.0, 2.0, 3.0):
        candidates.append((tuple(a - shell * d for a, d in zip(path[0], first_delta)),
                           scalars[0] - shell * (scalars[1] - scalars[0])))
        candidates.append((tuple(a + shell * d for a, d in zip(path[-1], last_delta)),
                           scalars[-1] + shell * (scalars[-1] - scalars[-2])))

    observed = [tuple(map(float, row["hkl"])) for row in observations
                if isinstance(row.get("hkl"), (list, tuple)) and len(row["hkl"]) == 3]
    transient = []
    seen_candidates: set[tuple[float, float, float]] = set()
    differentiation = (getattr(objective, "steering", None) or {}).get(
        "differentiation_tolerance_meV")
    differentiation = (float(differentiation) if differentiation is not None
                       else 3.0 * float(objective.tolerance_meV))
    for offset, (hkl, q) in enumerate(candidates):
        identity = tuple(round(value, 12) for value in hkl)
        if identity in seen_candidates:
            continue
        seen_candidates.add(identity)
        # Keep a hard geometric separation from every certification point.  Explicit provenance is
        # still the authoritative gate, but this also prevents a pooled off-path result from being
        # nearest-matched to an extremely fine requested grid after resume.
        if any(math.dist(hkl, requested) <= 1e-4 for requested in path):
            continue
        if any(math.dist(hkl, old) <= 1e-4 for old in observed):
            continue
        transient.append(GridQ(index=len(grid) + offset, hkl=hkl, q=q,
                               tolerance=differentiation))
    if not transient:
        return None

    def classification_value(candidate: GridQ) -> float:
        value = float(classify(candidate))
        # The provider estimates mutual information, whose support is non-negative.  Tiny negative
        # numerical artifacts carry no information and must not make another candidate look valuable.
        return max(value, 0.0) if math.isfinite(value) else float("-inf")

    inserted_scores = [(classification_value(candidate), candidate) for candidate in transient]
    inserted_value, best = max(inserted_scores, key=lambda item: item[0])
    requested_value = max(classification_value(candidate) for candidate in grid)
    # An inserted probe may win only by a real comparison.  If the classification provider cannot
    # score any requested point, fall back to ordinary target acquisition rather than interpreting
    # missing/non-finite grid evidence as permission to leave the path.
    if not math.isfinite(requested_value) or inserted_value <= requested_value + 1e-12:
        return None
    return best


def _candidate_precision(master: dict) -> float | None:
    """Conservative half-width of the fitted branch candidates, when one is available."""
    candidates = list(master.get("branch_candidates") or ())
    widths = []
    for candidate in candidates:
        width = candidate.get("sigma_meV")
        if width is None:
            width = candidate.get("omega0_err")
        if width is None:
            for key in ("interval", "profile_interval", "bayes_interval", "hesse_interval"):
                interval = candidate.get(key)
                if isinstance(interval, (list, tuple)) and len(interval) >= 2:
                    finite = [float(value) for value in interval if value is not None]
                    if len(finite) >= 2 and all(math.isfinite(value) for value in finite):
                        width = 0.5 * (max(finite) - min(finite))
                        break
        if width is not None and math.isfinite(float(width)):
            widths.append(abs(float(width)))
    if candidates:
        # Identity routing is conservative: every displayed candidate must carry usable precision.
        # A target-level diagnostic cannot stand in for an unquantified rival candidate.
        return max(widths) if len(widths) == len(candidates) else None
    diagnostic = master.get("diagnostic_halfwidth")
    if diagnostic is not None and math.isfinite(float(diagnostic)):
        return abs(float(diagnostic))
    return None


def _tracked_evidence(master: dict, point: GridQ) -> dict[str, Any]:
    candidates = list(master.get("branch_candidates") or ())
    precision = _candidate_precision(master)
    precision_adequate = bool(candidates) and (
        point.interval_floored
        or (precision is not None and precision <= float(point.tolerance)))
    target_branch = master.get("target_branch")
    posterior = master.get("branch_posterior") or {}
    probability = posterior.get(target_branch) if target_branch else None
    entropy = master.get("assignment_entropy")
    entropy = float(entropy) if entropy is not None else None
    return {
        "candidate_count": len(candidates),
        "candidate_precision_meV": _finite_or_none(precision),
        "tolerance_meV": float(point.tolerance),
        "branch_probability": _finite_or_none(probability),
        "assignment_entropy": _finite_or_none(entropy),
        # Precision adequacy answers "is this centre measured finely enough", which is a *different*
        # question from "do I know which branch this is".  Conflating them was the OP10 defect: a
        # 0.129 meV interval against a 0.5 meV tolerance made an undecided assignment look settled
        # and permanently suppressed same-q counting.  ``adequate`` is retained under its original
        # meaning for existing callers and diagnostics.
        "adequate": precision_adequate,
        "precision_adequate": precision_adequate,
        # Sourced from the tracker's own confirmation predicate, never re-derived here.  Absent
        # tracker fields (legacy ledgers) leave identity unknown, and unknown identity is treated as
        # unclear so the controller counts rather than stops.
        "target_probability": _finite_or_none(point.target_probability),
        "identity_entropy": _finite_or_none(point.identity_entropy),
        "local_identity_clear": bool(point.local_identity_clear),
    }


def _adjacent_classification_pick(grid: list[GridQ], source_index: int, classify):
    """Best unvisited immediate neighbor; path order is the deterministic tie-breaker."""
    options = [index for index in (source_index - 1, source_index + 1)
               if 0 <= index < len(grid) and not grid[index].visited]
    if not options:
        return None, None
    if classify is None:
        pick_index = min(options)
        return grid[pick_index], None
    scored = [(max(float(classify(grid[index])), 0.0), index) for index in options]
    information, pick_index = max(scored, key=lambda item: (item[0], -item[1]))
    return grid[pick_index], _finite_or_none(information)


def _fitted_evidence_sequences(point: GridQ) -> list[tuple[int, dict]]:
    """Chronological authoritative revisions that actually carry fitted mode evidence."""
    return [
        (int(sequence), update)
        for sequence, update in zip(point.evidence_update_sequences, point.evidence_updates)
        if update.get("branch_candidates") and update.get("pool_consistent") is not False
    ]


def _identity_revisit_basis(grid: list[GridQ], source_index: int) -> dict | None:
    """Newest adjacent pooled evidence acquired after the source's latest production scan."""
    source = grid[source_index]
    last_production = max(source.production_sequences, default=-1)
    revisions = []
    for index in (source_index - 1, source_index + 1):
        if not 0 <= index < len(grid):
            continue
        for sequence, update in _fitted_evidence_sequences(grid[index]):
            if sequence > last_production:
                revision = (update.get("content_hash")
                            or update.get("production_batch_fingerprint")
                            or update.get("track_revision") or sequence)
                revisions.append((sequence, index, str(revision)))
    if not revisions:
        return None
    sequence, index, revision = max(revisions)
    return {
        "source_last_production_sequence": last_production,
        "neighbor_path_index": index,
        "neighbor_evidence_sequence": sequence,
        "authoritative_evidence_revision": revision,
    }


MAX_RECONNECT_PRODUCTIONS = 2   # reconnect attempts per gap q before the frontier gets the beam


def _fitted_mode_count(point: GridQ) -> int:
    """How many distinct modes the pooled evidence actually resolved at this q."""
    master = point.evidence_master or point.master or {}
    return len(list(master.get("branch_candidates") or ()))


def _rival_gap_pick(grid: list[GridQ], frontier_index: int, direction: int) -> GridQ | None:
    """Nearest q on the connected run behind the frontier that resolved fewer modes than it.

    A tracked target is confirmed by *continuity*, so its identity can be blocked by a hole in the
    **rival** trajectory rather than by anything about the frontier itself.  OP10 stopped exactly
    that way: the frontier at H=2.75 resolved two well-separated modes (5.53 and 7.72 meV, ~22σ
    apart) but the acoustic branch had gone unfitted at 2.60/2.65/2.70 — q that had only ever had a
    10 M quicklook — so nothing connected 5.53 to the acoustic trajectory and P(optic) stalled at
    0.752 under a 0.85 gate.  Counting the frontier could not repair that, and did not: a replay
    spent ~17 billion counts there across twelve productions without moving the assignment.

    Walking back along the *confirmed* run keeps this connected: a q past a break in the trajectory
    cannot restore continuity to the frontier, so only the run that actually feeds it is considered.
    """
    want = _fitted_mode_count(grid[frontier_index])
    if want < 2:
        return None                      # nothing to reconnect: the frontier sees no rival either
    options = []
    index = frontier_index - direction
    while 0 <= index < len(grid) and grid[index].track_state == "confirmed":
        point = grid[index]
        resolved = _fitted_mode_count(point)
        # ``1 <= resolved < want`` is the signature of a *partially* resolved q — the target mode
        # was fitted there and the rival was not.  Zero fitted modes is not a gap in the rival
        # trajectory but a q with no pooled evidence at all, which this rung has no business
        # re-counting on the frontier's behalf.
        #
        # ``n_production < MAX_RECONNECT_PRODUCTIONS`` is the anti-livelock guard.  A gap is
        # under-resolved precisely because it was only ever quicklooked, so its production count is
        # an honest record of how many times this rung has already tried and failed to raise its
        # mode count.  Without the cap a reconnect that never resolves the rival would be re-picked
        # until the point's own 12-production ceiling — with geometric escalation, billions of
        # counts spent proving the same negative.  The point's own ceiling still applies on top.
        if (point.visited and 1 <= resolved < want
                and point.n_production < MAX_RECONNECT_PRODUCTIONS
                and not (point.same_q_ceiling_reached or point.exposure_saturated)):
            options.append((abs(index - frontier_index), index, point))
        index -= direction
    if not options:
        return None
    return min(options)[2]


def _same_q_counting_suppressed(point: GridQ) -> bool:
    """Whether *same-q counting* at this q has stopped paying — the only action class the general
    ranking path below generates.

    Equivalent today to the point-wide ``stuck`` test it replaced, but scoped to the **action** rather
    than the q: ``stuck`` is a reporting aggregate, and when PR2 adds non-same-q actions (focused
    rival, boundary probe) reachable from this path they must be evaluated on their own merits instead
    of being dropped along with the q.
    """
    return point.stuck


def _same_q_evidence_advanced(point: GridQ) -> bool:
    """Whether another statistics scan is justified by a newer local authoritative revision."""
    if point.exposure_saturated:
        return False
    if not point.production_sequences:
        return point.n_production == 0 or not point.evidence_update_sequences
    return max(point.evidence_update_sequences, default=-1) > max(point.production_sequences)


def _association_probability(point: GridQ) -> float:
    """Known-branch association carried by the newest authoritative source evidence."""
    master = point.evidence_master or point.master or {}
    direct = master.get("association_probability")
    if direct is None:
        posterior = master.get("branch_posterior") or {}
        if posterior:
            direct = sum(float(posterior.get(branch, 0.0))
                         for branch in ("acoustic", "optic"))
    try:
        value = float(direct)
    except (TypeError, ValueError):
        value = 1.0
    return min(max(value, 0.0), 1.0) if math.isfinite(value) else 1.0


def _classification_link_diagnostics(grid: list[GridQ], source_index: int,
                                     candidate_index: int, raw_information: float) -> dict:
    """Discount identity information by uncertainty accumulated across unmeasured path links.

    The decay is deliberately soft: it makes a disconnected endpoint less useful for *identity*
    without making it infeasible.  Strong enough O6/O9 information can still justify a distant q.
    """
    lo, hi = sorted((int(source_index), int(candidate_index)))
    # Count unvisited interior points plus the proposed (necessarily unvisited) endpoint.  Naming
    # the endpoint explicitly keeps the uncertainty symmetric when broadening left or right.
    unmeasured = (sum(not grid[index].visited for index in range(lo + 1, hi))
                  + int(not grid[candidate_index].visited))
    source_association = _association_probability(grid[source_index])
    retention = 1.0 / (
        1.0 + ASSOCIATION_UNCERTAINTY_PER_UNMEASURED_STEP * float(unmeasured))
    propagated = source_association * retention
    usable = max(float(raw_information), 0.0) * propagated
    return {
        "classification_information": _finite_or_none(raw_information),
        "expected_identity_information": _finite_or_none(usable),
        "unmeasured_gap_steps": int(unmeasured),
        "source_association_probability": _finite_or_none(source_association),
        "association_retention": _finite_or_none(retention),
        "propagated_association_uncertainty": _finite_or_none(1.0 - propagated),
    }


def _broadening_classification_pick(grid: list[GridQ], source_index: int, classify):
    """Best usable identity information once the local route stops making progress."""
    options = [point for point in grid if not point.visited]
    if not options:
        return None, None, None
    if classify is None:
        pick = min(options, key=lambda point: (abs(point.index - source_index), point.index))
        diagnostics = _classification_link_diagnostics(grid, source_index, pick.index, 0.0)
        return pick, None, diagnostics
    scored = []
    for point in options:
        raw = max(float(classify(point)), 0.0)
        diagnostics = _classification_link_diagnostics(grid, source_index, point.index, raw)
        scored.append((float(diagnostics["expected_identity_information"] or 0.0),
                       int(diagnostics["unmeasured_gap_steps"]), point, diagnostics))
    best = scored[0]
    for candidate in scored[1:]:
        if candidate[0] > best[0] and not math.isclose(
                candidate[0], best[0], rel_tol=1e-9, abs_tol=1e-12):
            best = candidate
        elif math.isclose(candidate[0], best[0], rel_tol=1e-9, abs_tol=1e-12):
            if (candidate[1], candidate[2].index) < (best[1], best[2].index):
                best = candidate
    information, _gap, pick, diagnostics = best
    return pick, _finite_or_none(information), diagnostics


def _tracked_frontier_target(grid: list[GridQ], *, maximum_bridge_steps: int = 1,
                             anchor_index: int | None = None, classify=None):
    """Connected tracked-branch walk with separate evidence and identity states."""
    if not grid:
        return None
    anchor_index = 0 if anchor_index is None else max(0, min(int(anchor_index), len(grid) - 1))
    anchor = grid[anchor_index]
    if not anchor.visited:
        return anchor, "quicklook", "tracked branch: establish identity at the resolvable anchor", None
    if anchor.track_state not in {"confirmed", "inferred_unresolved"}:
        # ``stuck`` (== ``ambiguity_exhausted``) is a per-q *reporting* aggregate, never a global
        # eligibility gate.  It folds identity saturation, which says counting cannot settle the
        # *label* here — it says nothing about a neighbouring classification probe or a precision
        # scan.  Dropping the whole anchor on it suppressed a legal broadening action, and because a
        # stuck anchor also has ``same_q_identity_work_remains == False`` the controller-invariant
        # guard never caught it: the campaign reported ``action_inventory_exhausted`` while that
        # action was still available.  The per-action brakes below own the suppression instead —
        # identity revisit by ``not anchor.stuck``, same-q identity counting by
        # ``same_q_identity_work_remains``, and the precision scan by the ceiling / exposure test.
        master = anchor.evidence_master or anchor.master or {}
        evidence = _tracked_evidence(master, anchor)
        if evidence["adequate"]:
            pick, information = _adjacent_classification_pick(
                grid, anchor_index, classify)
            if pick is not None:
                reason = ("tracked branch: statistically adequate ambiguous anchor requires "
                          "neighboring identity evidence")
                transition = {
                    "source_hkl": list(anchor.hkl), "selected_probe_hkl": list(pick.hkl),
                    "source_q": float(anchor.q), "selected_probe_q": float(pick.q),
                    **evidence, "classification_information": information,
                    "transition_reason": "adequate_candidates_ambiguous_identity",
                }
                return pick, "quicklook", reason, transition
            basis = _identity_revisit_basis(grid, anchor_index)
            if (basis is not None and not anchor.stuck
                    and anchor.n_production < anchor.max_production_batches):
                return (anchor, "production",
                        "tracked branch: revisit the ambiguous anchor using neighboring identity evidence",
                        {**evidence, **basis,
                         "transition_reason": "new_neighbor_revision_unlocked_identity_revisit"})
            pick, information, link = _broadening_classification_pick(
                grid, anchor_index, classify)
            if pick is not None:
                return (pick, "quicklook",
                        "tracked branch: broaden ambiguous identity to the most informative unvisited q",
                        {**evidence, **(link or {}), "source_hkl": list(anchor.hkl),
                         "selected_probe_hkl": list(pick.hkl),
                         "transition_reason": "local_identity_route_exhausted_broaden"})
            # Same last rung as the frontier: an undecided anchor identity with authorized budget
            # left is a counting problem once every classification route is spent.
            if anchor.same_q_identity_work_remains:
                return (anchor, "production",
                        "tracked branch: classification routes exhausted; resolve the ambiguous "
                        "anchor identity with more same-q exposure",
                        {**evidence, "transition_reason": "identity_route_exhausted_count_more"})
            return None
        if (anchor.n_production >= anchor.max_production_batches
                or not _same_q_evidence_advanced(anchor)):
            pick, information, link = _broadening_classification_pick(
                grid, anchor_index, classify)
            if pick is not None:
                reason = ("tracked branch: strategy ceiling reached; broaden to an untried q"
                          if anchor.n_production >= anchor.max_production_batches else
                          "tracked branch: same-q evidence did not advance; broaden to an untried q")
                return (pick, "quicklook",
                        reason,
                        {**evidence, **(link or {}), "source_hkl": list(anchor.hkl),
                         "selected_probe_hkl": list(pick.hkl),
                         "transition_reason": "unchanged_authoritative_evidence_broaden"})
            return None
        # A *precision* scan, not an identity one: it asks "is this centre measured finely enough",
        # which the O27/OP10 repair established is a different question from "which branch is this".
        # So identity saturation must NOT gate it — conflating the two was the OP10 defect.  Its own
        # brakes are the ceiling and exposure test above (``_same_q_evidence_advanced`` is False once
        # ``exposure_saturated``), which is why removing the blanket ``stuck`` drop cannot leak
        # unbounded same-q counting here.
        return (anchor, "production",
                "tracked branch: anchor candidate evidence is not yet statistically adequate", None)

    actions = []
    for direction in (-1, 1):
        frontier_index = anchor_index + direction
        while (0 <= frontier_index < len(grid)
               and grid[frontier_index].track_state in {
                   "confirmed", "inferred_unresolved"}):
            frontier_index += direction
        if not 0 <= frontier_index < len(grid):
            continue
        frontier = grid[frontier_index]
        if not frontier.visited:
            actions.append((0, frontier.index, frontier, "quicklook",
                            "tracked branch: extend the connected target frontier by one adjacent q"))
            continue
        master = frontier.evidence_master or frontier.master or {}
        evidence = _tracked_evidence(master, frontier)
        # A *precision* scan, not an identity one — "is this centre measured finely enough", a
        # different question from "which branch is this".  So ``frontier.stuck`` (which folds identity
        # saturation) must NOT gate it, exactly as O27 established at the anchor; conflating the two
        # denied a legitimate precision scan at an identity-saturated frontier with budget left.  Its
        # own brakes are the production ceiling and ``_same_q_evidence_advanced`` (False once
        # ``exposure_saturated``), which keep same-q counting bounded.  ``frontier.stuck`` stays on the
        # identity-revisit rung below, where it belongs.
        if (not evidence["adequate"]
                and frontier.n_production < frontier.max_production_batches
                and _same_q_evidence_advanced(frontier)):
            actions.append((1, frontier.index, frontier, "production",
                            "tracked branch: frontier candidate evidence is not yet statistically adequate"))
            continue
        bridge_index = frontier_index + direction
        if maximum_bridge_steps > 0 and 0 <= bridge_index < len(grid):
            bridge = grid[bridge_index]
            if not bridge.visited:
                actions.append((2, bridge.index, bridge, "quicklook",
                                "tracked branch: one provisional adjacent bridge to resolve the ambiguous frontier"))
                continue
        basis = _identity_revisit_basis(grid, frontier_index)
        if (basis is not None and not frontier.stuck
                and frontier.n_production < frontier.max_production_batches):
            actions.append((3, frontier.index, frontier, "production",
                            "tracked branch: revisit the ambiguous frontier using newer adjacent identity evidence",
                            basis))
            continue
        pick, information, link = _broadening_classification_pick(
            grid, frontier_index, classify)
        if pick is not None:
            actions.append((4, pick.index, pick, "quicklook",
                            "tracked branch: broaden ambiguous frontier to the most informative unvisited q",
                            {**evidence, **(link or {}), "source_hkl": list(frontier.hkl),
                             "selected_probe_hkl": list(pick.hkl),
                             "transition_reason": "local_frontier_exhausted_broaden"}))
            continue
        # Before spending more on the frontier, repair the rival trajectory it is being judged
        # against.  A hole there blocks the frontier's identity no matter how long the frontier
        # itself is counted, and the gap q is usually the cheaper, more informative scan.
        gap = _rival_gap_pick(grid, frontier_index, direction)
        if gap is not None and not frontier.local_identity_clear:
            actions.append((5, gap.index, gap, "production",
                            "tracked branch: reconnect the rival branch at an under-resolved q so "
                            "the ambiguous frontier identity has continuous evidence",
                            {**evidence, "source_hkl": list(frontier.hkl),
                             "selected_probe_hkl": list(gap.hkl),
                             "frontier_mode_count": _fitted_mode_count(frontier),
                             "probe_mode_count": _fitted_mode_count(gap),
                             "transition_reason": "rival_trajectory_gap_reconnect"}))
            continue
        # Last rung.  Cheap adjacent discrimination is always preferred above, but once every
        # classification route is spent an undecided identity is a *counting* problem again.  OP10
        # stopped here with one of twelve authorized productions used and the per-scan ceiling three
        # orders of magnitude away; refusing to count was never an evidence-based decision.
        if frontier.same_q_identity_work_remains:
            actions.append((6, frontier.index, frontier, "production",
                            "tracked branch: classification routes exhausted; resolve the ambiguous "
                            "frontier identity with more same-q exposure",
                            {**evidence, "transition_reason": "identity_route_exhausted_count_more"}))
    if not actions:
        return None
    _priority, _path_order, pick, kind, reason, *transition = min(
        actions, key=lambda item: (item[0], item[1]))
    return pick, kind, reason, transition[0] if transition else None


def tracked_anchor_index(path, period: float | None = None, *, target_branch: str = "optic",
                         model=None, resolution_info=None, hypothesis: dict | None = None) -> int:
    """Nearest reduced-zone centre, with path order as the deterministic tie-breaker.

    A cold workspace may not have a card yet.  In that one case reciprocal-lattice integers are the
    only available reduced-coordinate convention; initialized campaigns pass their declared period.
    """
    points = [tuple(float(value) for value in q) for q in path]
    if not points:
        return 0
    spans = [max(point[axis] for point in points) - min(point[axis] for point in points)
             for axis in range(len(points[0]))]
    axis = max(range(len(spans)), key=lambda item: spans[item])
    valid_period = period is not None and math.isfinite(float(period)) and float(period) > 0.0
    scale = float(period) if valid_period else 1.0
    reduced = [abs(point[axis] - round(point[axis] / scale) * scale) for point in points]
    zone_anchor = min(range(len(points)), key=lambda index: (reduced[index], index))
    if target_branch != "acoustic" or model is None:
        return zone_anchor
    scalars = [0.0]
    for left, right in zip(points, points[1:]):
        scalars.append(scalars[-1] + math.dist(left, right))
    resolvable = []
    model_points = getattr(model, "qs", None)
    cold_model = model_points is not None and len(model_points) == 0
    zc = (hypothesis or {}).get("zc_energy") or {}
    slope = (hypothesis or {}).get("slope_zc") or {}
    hypothesis_zc = float(zc.get("mean", zc.get("mode", 0.0)))
    hypothesis_slope = float(slope.get("mean", 0.0))
    for index, point in enumerate(points):
        try:
            predicted = (abs(hypothesis_zc + hypothesis_slope * reduced[index])
                         if cold_model and hypothesis else
                         abs(float(model.predict(scalars[index])[0])))
            fwhm = _resolve_fwhm(resolution_info, *point, predicted)
        except (TypeError, ValueError, IndexError):
            continue
        if predicted >= fwhm:
            resolvable.append(index)
    # If no point clears the predicted resolution threshold, the farthest reduced-q point is the
    # highest-energy acoustic option and therefore the best available attempt at establishing an
    # anchor. Falling back to the zone centre would deliberately choose the least resolvable point.
    if resolvable:
        return min(resolvable, key=lambda index: (reduced[index], index))
    farthest = max(reduced)
    return min(index for index, distance in enumerate(reduced)
               if math.isclose(distance, farthest, rel_tol=0.0, abs_tol=1e-12))


def _select_target(grid: list[GridQ], model, *,
                   fisher=None, opening=None, classify=None,
                   classification_weight: float = 0.0, tracked_branch: bool = False,
                   maximum_bridge_steps: int = 1,
                   track_anchor_index: int | None = None,
                   ranking_cost_kind: str = "measurement",
                   scoring_allowance_seconds: float = 60.0) -> tuple[GridQ | None, str, str, ProposalScore | None]:
    """Pick the next grid q and its scan kind. Returns ``(gridq, kind, reason, score)``.

    Tiers, most-specific first:

    1. **Unvisited** q → a quick-look (largest model-predicted σ) to seed the ω(q) model + count rate.
    2. **No-baseline** workable q → a PRODUCTION scan to establish a first usable ω₀ (largest σ-deficit).
    3. **Baselined** workable q → ranked first by the pooled master's model-weighted posterior-
       predictive information about joint ω/model/branch identity, normalized by analysis time in
       deterministic mode or measurement cost in measurement-dominated mode. Conservative utility,
       expected ω-width reduction, and canonical q order break unresolved Monte Carlo ties.
    4. A compatibility fallback uses the older Fisher/counts tiers only when no master-predictive
       artifact is available:
       - **Fisher tier (5b)** — when a ``fisher`` provider grounds any candidate (an in-anchor branch +
         an unconstrained last fit), rank *only* the grounded candidates by
         ``fisher_eig(σ_prior, σ_scan)/ΔC`` (the identifiability-aware screen). No silent method-mixing:
         a candidate the Fisher can't ground drops to the counts tier only if *none* is grounded.
       - **Counts tier (5a)** — else the goal-weighted counts-EIG per cost, when every candidate carries
         a measured cost history; otherwise the σ-deficit ranking (regression-safe for hand-built /
         pre-Phase-5 callers).

    Low utility is a visible ``diminishing_returns`` diagnostic, not an abandon rule. ``score`` carries
    ``cost=0``/``eig_per_cost=None`` where the window cost is not yet known;
    :func:`propose_scan` fills the actual ΔC once the window is known.
    """
    if tracked_branch:
        opening_pick = _tracked_frontier_target(
            grid, maximum_bridge_steps=maximum_bridge_steps, anchor_index=track_anchor_index,
            classify=classify)
        if opening_pick is not None:
            pick, kind, reason, evidence_transition = opening_pick
            differentiation = any(token in reason for token in (
                "ambiguous", "identity evidence", "bridge", "differentiation", "strategy ceiling"))
            return pick, kind, reason, ProposalScore(
                method="classification" if differentiation else "explore",
                c_spent=float(pick.counts_spent), reason=reason,
                classification_eig=(
                    _finite_or_none((evidence_transition or {}).get(
                        "expected_identity_information"))
                    if evidence_transition else None),
                combined_information=(
                    _finite_or_none((evidence_transition or {}).get(
                        "expected_identity_information"))
                    if evidence_transition else None),
                purpose="differentiate" if differentiation else "hypothesis_check",
                evidence_transition=evidence_transition)
        # Once a connected identity frontier has no admissible action, global deficit ranking must
        # not jump to a locally attractive mode and silently relabel it.  The caller turns this
        # exhausted identity route into an honest incomplete result.  After identity coverage is
        # complete, ordinary ranking remains available for target-precision production.
        if any(point.visited and point.track_state not in (
                None, "confirmed", "inferred_unresolved") for point in grid):
            return None, "", "tracked branch identity frontier exhausted", None

    for source in grid:
        master = source.evidence_master or source.master or {}
        diagnostic_hw = master.get("diagnostic_halfwidth")
        statistics_limited = (diagnostic_hw is None
                              or float(diagnostic_hw) > source.tolerance)
        if (source.visited and not source.converged and not source.stuck
                and source.n_production < source.max_production_batches
                and master.get("candidate_state") == "provisional" and statistics_limited):
            candidate_id = master.get("candidate_id") or "unknown"
            reason = (f"statistics-limited provisional candidate {candidate_id} at q={source.q:g} "
                      f"(hkl={list(source.hkl)}); pool more exposure before branch discrimination")
            return source, "production", reason, ProposalScore(
                method="deficit", sigma_prior=_finite_or_none(_q_sigma(source)),
                c_spent=float(source.counts_spent), reason=reason,
                purpose="target_or_classification")
    unvisited = [g for g in grid if not g.visited]
    if unvisited and classify is not None:
        for source in grid:
            master = source.evidence_master or source.master or {}
            diagnostic_hw = master.get("diagnostic_halfwidth")
            evidence_ready = (source.interval_floored or
                              (diagnostic_hw is not None
                               and float(diagnostic_hw) <= source.tolerance))
            if master.get("candidate_state") != "provisional" or not evidence_ready:
                continue
            options = [candidate for candidate in unvisited if candidate.hkl != source.hkl]
            if not options:
                continue
            pick = max(options, key=lambda candidate: max(float(classify(candidate)), 0.0))
            information = max(float(classify(pick)), 0.0)
            candidate_id = master.get("candidate_id") or "unknown"
            reason = (f"neighboring-q quicklook for provisional candidate {candidate_id}; "
                      f"q={pick.q:g} (hkl={list(pick.hkl)}) maximizes branch information")
            return pick, "quicklook", reason, ProposalScore(
                method="classification", c_spent=float(pick.counts_spent), reason=reason,
                classification_eig=_finite_or_none(information),
                combined_information=_finite_or_none(information), purpose="differentiate")
    if unvisited:
        pick = min(unvisited, key=opening) if opening is not None else max(
            unvisited, key=lambda g: model.predict(g.q)[1])
        purpose = "hypothesis_check" if opening is not None else "survey"
        reason = (f"unvisited q={pick.q:g} (hkl={list(pick.hkl)}); quick-look to seed the ω(q) model "
                  f"and count rate (predicted σ={model.predict(pick.q)[1]:g} meV)")
        return pick, "quicklook", reason, ProposalScore(
            method="explore", c_spent=float(pick.counts_spent), reason=reason, purpose=purpose)
    # ``same_q_ceiling_reached`` rather than ``max_production_batches + maximum_bridge_steps``: the
    # geometry knob never caps same-q counting (O27).  This is the general ranking path, so leaving
    # the old sum here would have kept a second, larger effective ceiling for non-tracked selectors.
    candidates = [g for g in grid if g.precision_coverage_eligible
                  and not (g.converged or _same_q_counting_suppressed(g))
                  and not (g.same_q_ceiling_reached
                           and (g.evidence_master is not None and g.master is None
                                or g.track_state in {
                                    "ambiguous", "missing", "provisional", "blocked"}))]
    if not candidates:
        return None, "", "all grid q converged", None

    # A statistically adequate (or empirically floored) provisional candidate needs branch evidence,
    # not more counts at the same q. Prefer an untested neighboring q by expected classification
    # information; this is evidence collection, never a hard acoustic/optic exclusion.
    for source in grid:
        master = source.evidence_master or source.master or {}
        candidate_id = master.get("candidate_id")
        diagnostic_hw = master.get("diagnostic_halfwidth")
        evidence_ready = (source.interval_floored or
                          (diagnostic_hw is not None and float(diagnostic_hw) <= source.tolerance))
        if master.get("candidate_state") != "provisional" or not candidate_id or not evidence_ready:
            continue
        measured = {tuple(update.get("hkl") or ()) for point in grid
                    for update in (point.evidence_updates or point.master_updates)
                    if update.get("candidate_id") == candidate_id}
        neighbors = [candidate for candidate in candidates
                     if tuple(candidate.hkl) not in measured and candidate.hkl != source.hkl]
        if not neighbors:
            continue
        if classify is not None:
            pick = max(neighbors, key=lambda candidate: max(float(classify(candidate)), 0.0))
            information = max(float(classify(pick)), 0.0)
        else:
            pick = min(neighbors, key=lambda candidate: abs(candidate.q - source.q))
            information = None
        reason = (f"neighboring-q evidence for provisional candidate {candidate_id}; "
                  f"q={pick.q:g} (hkl={list(pick.hkl)}) discriminates branch hypotheses")
        return pick, "production", reason, ProposalScore(
            method="classification", c_spent=float(pick.counts_spent), reason=reason,
            classification_eig=_finite_or_none(information), combined_information=_finite_or_none(information),
            purpose="differentiate")

    # Baseline-first: a workable q with no usable ω₀ yet gets a production scan to establish one
    # (unchanged behaviour — these carry a +inf σ-deficit and win outright).
    no_baseline = [g for g in candidates if g.evidence_master is None and g.master is None]
    if no_baseline:
        pick = max(no_baseline, key=lambda g: g.deficit)
        reason = (f"baseline q={pick.q:g} (hkl={list(pick.hkl)}); no usable ω₀ interval yet; production "
                  f"scan to establish the master")
        return pick, "production", reason, ProposalScore(
            method="explore", c_spent=float(pick.counts_spent), reason=reason)

    predictive = []
    for candidate in candidates:
        master = candidate.evidence_master or candidate.master or {}
        diagnostics = master.get("expected_next_information") or {}
        if not diagnostics.get("usable"):
            continue
        artifact = master.get("predictive") or {}
        levels = list(artifact.get("score_levels") or [diagnostics])
        cost = (float(artifact.get("predicted_analysis_seconds") or 1.0)
                if ranking_cost_kind == "analysis" else max(_q_next_cost(candidate), 1.0))
        predictive.append((candidate, artifact, levels, cost))
    if predictive:
        # Consume the precomputed common-random-number levels in lockstep. Stop as soon as the best
        # candidate's conservative lower bound clears every competitor's upper bound; otherwise use
        # the largest bounded level and the documented deterministic tie-breaks.
        ranked = []
        scoring_started = time.monotonic()
        allowance = min(max(float(scoring_allowance_seconds), 0.0), 60.0)
        simulation_counts = (128, 512, 2048)
        max_levels = (len(simulation_counts) if any(item[1].get("models") for item in predictive)
                      else max(len(item[2]) for item in predictive))
        for level_index in range(max_levels):
            ranked = []
            for candidate, artifact, levels, cost in predictive:
                if artifact.get("models"):
                    common_seed = int((artifact.get("diagnostics") or {}).get(
                        "common_random_seed", 940_301))
                    diagnostics = predictive_score(
                        artifact["models"], seed=common_seed,
                        n_simulations=simulation_counts[level_index])
                else:
                    diagnostics = levels[min(level_index, len(levels) - 1)]
                information = float(diagnostics.get("expected_information_nats") or 0.0)
                stderr = float(diagnostics.get("score_standard_error_nats") or 0.0)
                lower = max(information - 2.0 * stderr, 0.0)
                upper = max(information + 2.0 * stderr, 0.0)
                ranked.append((candidate, diagnostics, lower, upper, cost))
            ranked.sort(key=lambda item: (
                item[2] / item[4],
                float(item[1].get("expected_omega_halfwidth_reduction_meV") or 0.0),
                tuple(-value for value in item[0].hkl)), reverse=True)
            if len(ranked) == 1 or ranked[0][2] / ranked[0][4] > ranked[1][3] / ranked[1][4]:
                break
            if time.monotonic() - scoring_started >= allowance:
                break
        pick, diagnostics, lower, _upper, cost = ranked[0]
        joint = float(diagnostics.get("expected_information_nats") or 0.0)
        omega_info = float(diagnostics.get("omega_information_nats") or 0.0)
        identity_info = float(diagnostics.get("model_branch_information_nats") or 0.0)
        reduction = float(diagnostics.get("expected_omega_halfwidth_reduction_meV") or 0.0)
        reason = (f"master-predictive EIG q={pick.q:g} (hkl={list(pick.hkl)}); "
                  f"joint={joint:.4g} nats, expected ω half-width reduction={reduction:.4g} meV, "
                  f"model/branch entropy reduction={identity_info:.4g} nats")
        return pick, "production", reason, ProposalScore(
            method="master_predictive", eig=_finite_or_none(omega_info),
            classification_eig=_finite_or_none(identity_info),
            combined_information=_finite_or_none(joint), reason=reason,
            purpose="target_or_classification", ranking_cost=float(cost),
            eig_per_cost=_finite_or_none(joint / cost),
            omega_halfwidth_reduction=_finite_or_none(reduction),
            model_entropy_reduction=_finite_or_none(identity_info),
            score_standard_error=_finite_or_none(
                float(diagnostics.get("score_standard_error_nats") or 0.0)),
            simulation_count=int(diagnostics.get("simulation_count") or 0),
            conservative_utility=_finite_or_none(lower / cost))

    # Fisher tier (5b): rank the identifiability-aware EIG for the candidates a grounded rate model can
    # score. Grounding (in-anchor branch + unconstrained last fit) lives entirely in the provider.
    if fisher is not None and model is not None:
        graded = []
        for g in candidates:
            w0 = float(model.predict(g.q)[0])
            sp = _q_sigma(g)
            ss = fisher(g, w0, sp)
            if ss is not None and math.isfinite(ss):
                graded.append((g, sp, ss))
        if graded:
            def _fkey(t):
                g, sp, ss = t
                classification = max(float(classify(g)), 0.0) if classify is not None else 0.0
                return eig_per_cost(
                    fisher_eig(sp, ss) + classification_weight * classification,
                    _q_next_cost(g))
            pick, sp, ss = max(graded, key=_fkey)
            eig = fisher_eig(sp, ss)
            classification = max(float(classify(pick)), 0.0) if classify is not None else 0.0
            combined = eig + classification_weight * classification
            reason = (f"max-Fisher-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ_prior={sp:g}→"
                      f"σ_scan={ss:g} meV, C={pick.counts_spent:g}; production scan (identifiability-aware)")
            return pick, "production", reason, ProposalScore(
                method="fisher", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(sp),
                sigma_scan=_finite_or_none(ss), c_spent=float(pick.counts_spent), reason=reason,
                classification_eig=_finite_or_none(classification),
                classification_weight=float(classification_weight),
                combined_information=_finite_or_none(combined), purpose="target_or_classification",
                ranking_cost=float(_q_next_cost(pick)),
                ranking_ncount=getattr(fisher, "default_exposure", None))

    # Counts tier (5a): goal-weighted EIG-per-cost when every candidate has a cost history.
    if all(g.counts_spent > 0.0 for g in candidates):
        def _ckey(g):
            dc = _q_next_cost(g)
            target = counts_eig(_q_sigma(g), g.tolerance, g.counts_spent, dc)
            classification = max(float(classify(g)), 0.0) if classify is not None else 0.0
            return eig_per_cost(target + classification_weight * classification, dc)
        pick = max(candidates, key=_ckey)
        dc = _q_next_cost(pick)
        eig = counts_eig(_q_sigma(pick), pick.tolerance, pick.counts_spent, dc)
        classification = max(float(classify(pick)), 0.0) if classify is not None else 0.0
        combined = eig + classification_weight * classification
        reason = (f"max-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ={_q_sigma(pick):g} meV over "
                  f"tolerance {pick.tolerance:g}, C={pick.counts_spent:g}; production scan (EIG-per-cost ranked)")
        return pick, "production", reason, ProposalScore(
            method="counts", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(_q_sigma(pick)),
            sigma_scan=_finite_or_none(predicted_sigma_after(_q_sigma(pick), pick.counts_spent, dc)),
            c_spent=float(pick.counts_spent), reason=reason,
            classification_eig=_finite_or_none(classification),
            classification_weight=float(classification_weight),
            combined_information=_finite_or_none(combined), purpose="target_or_classification",
            ranking_cost=float(dc))

    pick = max(candidates, key=lambda g: g.deficit)
    deficit = pick.deficit
    deficit_str = "unfitted" if deficit == float("inf") else f"{deficit:g} meV over tolerance"
    n_pool = (pick.master or {}).get("n_members") or pick.n_production
    reason = (f"largest-deficit q={pick.q:g} (hkl={list(pick.hkl)}); {deficit_str}; production scan to "
              f"accumulate the master (pool now {n_pool})")
    return pick, "production", reason, ProposalScore(
        method="deficit", sigma_prior=_finite_or_none(_q_sigma(pick)),
        c_spent=float(pick.counts_spent), reason=reason)


@dataclass(frozen=True)
class AcquisitionContext:
    """Everything a generator may read, built exactly once per decision boundary.

    Before this, the world a generator reasons about (working grid, steering policy, runtime limits,
    the frozen target hypothesis, the tracked anchor) was assembled inline inside the single legacy
    selector. A second generator added beside it would have had to rebuild that world from the raw
    arguments — and any divergence (a differently-summarized grid, a second read of
    ``objective.steering``, an un-normalized inserted candidate index) would be invisible: two
    generators would silently be answering questions about two different states, and the inventory
    would present their actions as comparable. One frozen context removes that whole class of bug.
    """

    objective: Any
    model: Any
    # Tuples, not lists: ``frozen=True`` stops the *field* being reassigned but would happily let one
    # generator append to or reorder a shared list, which is precisely the divergence this type exists
    # to prevent. The sequence is read-only; the GridQ objects it holds are still the live per-q state.
    grid: tuple[GridQ, ...]
    results_so_far: tuple[dict, ...]
    resolution_info: Any
    policy: dict
    runtime_limits: dict
    classify: Any
    fisher: Any
    target_hypothesis: dict | None
    classification_weight: float
    opening: Any
    tracked_branch: bool
    maximum_bridge_steps: int
    track_anchor_index: int | None
    track_zone_period: float | None
    branch_track: dict
    ranking_cost_kind: str
    scoring_allowance_seconds: float
    # Scientific action fingerprints the policy has already durably refused, mapped to
    # ``{"class": ..., "reason": ...}``. Applied to the *evaluations*, never by dropping the action
    # before enumeration: a suppressed action must still appear in the inventory, or the exhaustion
    # proof would claim an empty action space with no record of what emptied it.
    suppressed: dict = field(default_factory=dict)
    # Q positions the instrument cannot reach, mapped to ``{"class": ..., "reason": ...}``.
    #
    # Keyed by hkl rather than by scientific action fingerprint on purpose. Unreachability is a
    # property of the geometry, not of the question being asked there: the scientific fingerprint
    # includes the evidence basis, so charging a geometry refusal to it would let the suppression
    # evaporate the moment any evidence advanced, and the controller would re-propose the same
    # unreachable q for the rest of the campaign.
    unreachable_hkl: dict = field(default_factory=dict)
    # Which (sample, temperature-bin) belief slice these actions are formed against. Part of every
    # intent's identity: the same question asked of a different slice is answered by different
    # evidence and updates a different posterior.
    campaign_slice: object | None = None
    # Predicts the most probable NON-target branch at a grid point as ``(mean, sigma)``, from the same
    # belief models the classifier uses; None when nothing is predicted. The identity generators need
    # it at *generation* time — where a rival is predicted to be is what decides whether there is an
    # identity action to propose at all, not merely how to widen a window that was already chosen.
    rival_band: object | None = None


@dataclass(frozen=True)
class GeneratedAction:
    """One generator's output: the scientific question, plus the raw state a proposal is built from.

    ``intent`` is what the inventory, dedup and the terminal proof reason about; ``pick``/``kind``/
    ``reason``/``score`` are the concrete selection state :func:`_build_proposal` consumes. Keeping
    both on one frozen value is what lets generation and evaluation be separate phases without the
    evaluator having to re-derive the pick it is scoring.
    """

    intent: MeasurementIntent
    generator: str
    pick: GridQ
    kind: str
    reason: str
    score: ProposalScore
    # Decided before the intent so the proposal builder reuses it rather than re-deriving a value the
    # scientific action identity has already been fixed against.
    driver_inserted: bool = False


@dataclass(frozen=True)
class GeneratorResult:
    """What one generator produced, plus its own account of why it produced that.

    A generator that returns nothing is not self-explanatory, and the difference between the reasons
    matters to an operator. "Nothing here needed an identity probe" is routine. "An identity probe
    would have settled this, and your declared search band forbade it" is a decision someone may want
    to revisit, and it is invisible unless the generator can say so — the accounting layer can only
    observe *that* nothing came back, never why.
    """

    actions: list[GeneratedAction] = field(default_factory=list)
    # Preferred over the count-derived reason when present. Absent means "no reason beyond what the
    # counts already show", and the derived reason stands.
    reason_code: str | None = None


@dataclass(frozen=True)
class _CandidateAction:
    """A generated action paired with its evaluation and the context both were formed in.

    The proposal builder reads its policy/limits/hypothesis from ``context`` — the exact values
    selection used — so no second read of ``objective.steering`` can diverge from what enumeration
    chose. The ``pick``/``kind``/… accessors keep the pre-split call shape for existing callers.
    """

    generated: GeneratedAction
    evaluation: ActionEvaluation
    context: AcquisitionContext

    @property
    def pick(self) -> GridQ:
        return self.generated.pick

    @property
    def kind(self) -> str:
        return self.generated.kind

    @property
    def reason(self) -> str:
        return self.generated.reason

    @property
    def score(self) -> ProposalScore:
        return self.generated.score

    @property
    def driver_inserted(self) -> bool:
        return self.generated.driver_inserted

    @property
    def target_hypothesis(self) -> dict | None:
        return self.context.target_hypothesis

    @property
    def policy(self) -> dict:
        return self.context.policy

    @property
    def runtime_limits(self) -> dict:
        return self.context.runtime_limits

    @property
    def classification_weight(self) -> float:
        return self.context.classification_weight


def _driver_inserted_pick(objective, pick) -> bool:
    """Whether the selected q sits off the requested certification path (a driver-inserted probe).

    Decided once, *before* the intent is built, so the scientific action identity cannot disagree with
    the emitted proposal: the proposal builder previously re-derived this and flipped the score's
    purpose to ``differentiate`` afterwards, leaving the inventory intent labelled ``quicklook`` /
    ``production`` while the emitted action was a differentiation probe.
    """
    return not any(math.dist(pick.hkl, tuple(point)) <= 1e-4 for point in objective.path)


def _intent_from_pick(objective, pick, kind, score, *, driver_inserted: bool = False,
                      campaign_slice=None) -> MeasurementIntent:
    """Describe the *scientific question* a selected pick answers, for dedup and terminal accounting."""
    # A driver-inserted pick *is* a differentiation probe by construction — the candidate generator
    # exists to place a classification probe off the certification grid, and the proposal builder
    # stamps ``purpose``/``insertion_purpose`` = "differentiate" for it unconditionally. So it drives
    # both the action class and the window family: describing the same action as a "target" window in
    # the inventory while emitting a differentiation probe is the incoherence this fixes. This affects
    # only the intent's description and fingerprint — the emitted window still comes from
    # ``_energy_window``, which is unchanged.
    differentiate = (getattr(score, "purpose", None) == "differentiate") or driver_inserted
    action_class = "differentiation_probe" if differentiate else kind
    target_branch = getattr(objective, "target_branch", None)
    rival_branch = ("acoustic" if target_branch == "optic"
                    else "optic" if target_branch == "acoustic" else None)
    search = getattr(objective, "energy_search_meV", None) or (None, None)
    # The evidence the action is a response to — same fallback chain the proposal records as its
    # ``authoritative_evidence_revision``, so a changed evidence basis changes the scientific
    # fingerprint and two iterations against different evidence cannot collapse to one identity.
    selected_evidence = pick.evidence_master or pick.master or {}
    evidence = ((getattr(score, "evidence_transition", None) or {}).get(
        "authoritative_evidence_revision")
        or selected_evidence.get("content_hash")
        or selected_evidence.get("production_batch_fingerprint")
        or selected_evidence.get("track_revision"))
    return MeasurementIntent(
        action_class=str(action_class), hkl=tuple(pick.hkl),
        energy_lo=search[0], energy_hi=search[1],
        window_family=("differentiate" if differentiate else "target"),
        target_branch_role=target_branch, rival_branch_role=rival_branch,
        evidence_basis_fingerprint=(str(evidence) if evidence is not None else None),
        hypotheses_fingerprint=getattr(objective, "hypothesis_fingerprint", None),
        campaign_slice_fingerprint=(campaign_slice.fingerprint
                                    if campaign_slice is not None else None))


def build_acquisition_context(objective, model, results_so_far: list[dict], resolution_info=None,
                              master_rows: list[dict] | None = None, fisher=None, classify=None,
                              runtime_limits: dict | None = None,
                              track_anchor_index: int | None = None,
                              track_zone_period: float | None = None,
                              branch_track: dict | None = None,
                              scoring_allowance_seconds: float = 60.0,
                              suppressed: dict | None = None,
                              unreachable_hkl: dict | None = None,
                              campaign_slice=None,
                              rival_band=None) -> AcquisitionContext:
    """Assemble the one world every generator reasons about at this decision boundary.

    This is verbatim the preamble the fused legacy selector used to run inline — the summarized grid,
    the frozen target hypothesis, the tracked anchor, the steering policy, and the driver-inserted
    candidate normalized to its real grid position. Hoisting it here is what makes a second generator
    safe to add: it cannot see a differently-built grid or a differently-read policy.
    """
    grid = summarize_grid(
        objective, results_so_far or [], master_rows or [], branch_track=branch_track)
    target_hypothesis = next((h for h in getattr(objective, "hypotheses", [])
                              if h.get("name") == getattr(objective, "target_hypothesis", None)), None)
    if (getattr(objective, "target_selector", None) == "tracked_branch"
            and track_anchor_index is None):
        declared_period = (getattr(objective, "meta", None) or {}).get("zone_period")
        track_anchor_index = tracked_anchor_index(
            objective.path, track_zone_period if track_zone_period is not None else declared_period,
            target_branch=getattr(objective, "target_branch", "optic"), model=model,
            resolution_info=resolution_info, hypothesis=target_hypothesis)
    opening = None
    if target_hypothesis is not None:
        name = target_hypothesis.get("name")
        if name == "optic":
            opening = lambda g: (g.q, g.index)
        elif name == "acoustic":
            def opening(g):
                mu, _ = model.predict(g.q)
                return (0 if mu >= DEFAULT_FWHM_MEV else 1, g.q, g.index)
    policy = dict(getattr(objective, "steering", {}) or {})
    inserted = _driver_inserted_candidate(objective, grid, results_so_far or [], classify)
    if inserted is not None:
        # ``_driver_inserted_candidate`` carries a synthetic ``index = len(grid) + offset`` purely to
        # keep transient candidates distinct while it scores them.  That synthetic value must never
        # enter the working grid: position-based readers (``_classification_link_diagnostics`` does
        # raw ``grid[candidate_index]`` / ``range(lo+1, hi)``) treat ``.index`` as a list position, and
        # a candidate whose ``.index`` overshoots ``len(grid)`` indexes out of range.  Normalise it to
        # its real position on entry.
        inserted.index = len(grid)
        grid = [*grid, inserted]
    return AcquisitionContext(
        objective=objective, model=model, grid=tuple(grid),
        results_so_far=tuple(results_so_far or []),
        resolution_info=resolution_info, policy=policy,
        runtime_limits=dict(runtime_limits or {}), classify=classify, fisher=fisher,
        target_hypothesis=target_hypothesis,
        classification_weight=float(policy.get("classification_weight", 0.0)),
        opening=opening,
        tracked_branch=(getattr(objective, "target_selector", None) == "tracked_branch"),
        maximum_bridge_steps=int(policy.get("maximum_bridge_steps", 1)),
        track_anchor_index=track_anchor_index, track_zone_period=track_zone_period,
        branch_track=dict(branch_track or {}),
        ranking_cost_kind=("analysis" if getattr(objective, "tier", None) == "deterministic"
                           else "measurement"),
        scoring_allowance_seconds=scoring_allowance_seconds,
        suppressed=_validated_suppressions(suppressed),
        unreachable_hkl=_validated_suppressions(unreachable_hkl),
        campaign_slice=campaign_slice, rival_band=rival_band)


def hkl_key(hkl) -> str:
    """The stable string key an unreachable-q suppression is recorded under."""
    return ",".join(f"{round(float(value), 6):g}" for value in hkl)


def _validated_suppressions(suppressed: dict | None) -> dict:
    """Reject a transient suppression at the boundary rather than making it durable.

    A network blip, a busy queue or a momentary instrument state says nothing about whether the
    scientific question is worth asking. Nothing in the type system stops a caller passing one in, so
    the refusal lives here — the one place every suppression must pass through.
    """
    entries = dict(suppressed or {})
    for fingerprint, entry in entries.items():
        if str((entry or {}).get("class", "")).lower() == RejectionClass.TRANSIENT.value:
            raise ValueError(
                f"refusing a transient suppression for action {fingerprint!r}: a transient failure "
                "must never durably suppress a scientific action")
    return entries


def _legacy_selector_generate(context: AcquisitionContext) -> list[GeneratedAction]:
    """The single registered generator today: the legacy priority/ranking selector, adapted.

    It yields at most ONE action, chosen by the pre-existing tier logic, so the inventory it backs is
    a proof about the *legacy selector's* action space rather than the complete physical one — which
    is exactly why it is registered as ``legacy_selector_adapter``. PR2.7 registers explicit per-intent
    generators beside it, at which point the registry (and its derived fingerprint) grows.
    """
    pick, kind, reason, score = _select_target(
        context.grid, context.model, fisher=context.fisher, opening=context.opening,
        classify=context.classify, classification_weight=context.classification_weight,
        tracked_branch=context.tracked_branch,
        maximum_bridge_steps=context.maximum_bridge_steps,
        track_anchor_index=context.track_anchor_index,
        ranking_cost_kind=context.ranking_cost_kind,
        scoring_allowance_seconds=context.scoring_allowance_seconds)
    if pick is None:
        return GeneratorResult()
    driver_inserted = _driver_inserted_pick(context.objective, pick)
    intent = _intent_from_pick(context.objective, pick, kind, score,
                               driver_inserted=driver_inserted,
                               campaign_slice=context.campaign_slice)
    return GeneratorResult([GeneratedAction(
        intent=intent, generator=LEGACY_SELECTOR_GENERATOR, pick=pick,
        kind=kind, reason=reason, score=score, driver_inserted=driver_inserted)])


def _legacy_selector_evaluate(action: GeneratedAction,
                              context: AcquisitionContext) -> ActionEvaluation:
    """Score the legacy adapter's action from the vector its own tier logic already produced.

    The legacy selector generates and ranks in one pass, so its evaluator reads the resulting
    :class:`ProposalScore` rather than recomputing anything — carrying the *real* vector, because
    ``to_full_dict`` presents this as the ranked score record and defaults would be a lie.
    ``policy_rank`` stays at its default: the staged ranking comparator is PR3c's, and the legacy
    adapter contributes a single already-chosen action with nothing to order against.
    """
    score = action.score
    return ActionEvaluation(
        intent=action.intent, generator=action.generator,
        eligibility=ActionEligibility.ELIGIBLE,
        selection_reason=action.reason,
        expected_identity_gain=_finite_or_none(score.classification_eig),
        expected_precision_gain=_finite_or_none(score.eig),
        expected_measurement_cost=_finite_or_none(
            score.ranking_cost if score.ranking_cost is not None else score.cost),
        score_method=score.method)


def _narrow_rival_generate(context: AcquisitionContext) -> list[GeneratedAction]:
    """Propose a narrow window on the predicted rival at each point where counting has stopped paying.

    This is the action AP6 lacked. A point whose identity is ambiguous is normally answered by counting
    longer, and while that is still working this generator stays silent — ``identity_saturated`` is the
    point's own record that exposure grew and neither the target probability nor the identity entropy
    moved, i.e. that more of the same window is known-wasted beam. What has *not* been tried at that
    stage is looking where the rival is predicted to be, so that is what this proposes: one narrow,
    uncertainty-relative window centred on the rival, fit independently and routed to
    ``auxiliary_track_node`` so it can revise the identity without ever touching the target's ω₀.

    It refuses rather than reaches. A rival outside the identity domain is left un-probed, and the
    campaign terminates naming an action it was not authorised to take — a reviewable statement an
    operator can act on, instead of a silent scan outside the intent they declared.
    """
    objective = context.objective
    rival_band = context.rival_band
    if not callable(rival_band):
        return GeneratorResult(reason_code="no_rival_predictor")
    actions: list[GeneratedAction] = []
    forbidden_reasons: list[str] = []
    malformed = 0
    for pick in context.grid:
        if pick.local_identity_clear or not pick.identity_saturated:
            continue
        band = rival_band(pick)
        if band is None:
            continue
        try:
            mu, sigma = (float(band[0]), float(band[1]))
        except (TypeError, ValueError, IndexError):
            # A predictor that answered but answered nonsense is not the same as one that had nothing
            # to say. Counted so it surfaces as a diagnostic rather than reading as routine silence,
            # which would leave a broken predictor indistinguishable from a settled frontier.
            malformed += 1
            continue
        if not (math.isfinite(mu) and math.isfinite(sigma)):
            malformed += 1
            continue
        # The *local* instrument FWHM at the rival energy, resolved once and used for everything
        # downstream. It must precede ``identity_domain``: that function's compiler-band widening
        # margin is ``3 × fwhm_meV``, so calling it first spent the fixed DEFAULT_FWHM_MEV and sized
        # the authorised domain against a resolution the instrument does not have here.
        local_fwhm = _resolve_fwhm(context.resolution_info, *pick.hkl, mu)
        domain, forbidden = identity_domain(objective, rival_energy_meV=mu, fwhm_meV=local_fwhm)
        if forbidden is not None or not (domain[0] <= mu <= domain[1]):
            # Not an error and not silence. This point had a settleable identity question and a
            # predicted rival to settle it with, and the declared search band is the only thing
            # stopping the scan. Recorded so the terminal can name the action it was not authorised
            # to take, which an operator can act on by widening the band.
            forbidden_reasons.append(
                f"hkl={tuple(round(float(v), 4) for v in pick.hkl)} rival={mu:.3f} meV "
                f"outside identity domain [{domain[0]:.3f}, {domain[1]:.3f}]")
            continue
        # Uncertainty- and resolution-relative half-width. A rival predicted loosely needs more room or
        # the probe misses the mode it was fired to find and reports a false null; a tightly predicted
        # one gets a cheap window. A broad local resolution would otherwise smear the mode below a
        # default-width window and turn a real rival into a false null. Clamped into the identity domain
        # so the emitted request never exceeds what was authorised.
        half = max(2.0 * sigma, 1.5 * local_fwhm)
        lo, hi = max(domain[0], mu - half), min(domain[1], mu + half)
        if not lo < hi:
            continue
        intent = replace(
            _intent_from_pick(objective, pick, "identity_rival_probe",
                              ProposalScore(method="identity"),
                              campaign_slice=context.campaign_slice),
            action_class="identity_rival_probe", window_family="rival",
            energy_lo=lo, energy_hi=hi, window_center_meV=mu,
            evidence_sink=AUXILIARY_TRACK_NODE)
        actions.append(GeneratedAction(
            intent=intent, generator=NARROW_RIVAL_PROBE_GENERATOR, pick=pick,
            kind="identity_rival_probe",
            reason=(f"identity saturated at hkl={tuple(pick.hkl)}; probing predicted rival "
                    f"{mu:.3f}±{sigma:.3f} meV over [{lo:.3f}, {hi:.3f}]"),
            score=ProposalScore(method="identity", purpose="differentiate",
                                reason="predicted-rival identity probe")))
    # Only when nothing was emitted: a run that produced an action is adequately described by that
    # action, and reporting a refusal beside a success would present a partly-served frontier as a
    # blocked one.
    if not actions and forbidden_reasons:
        return GeneratorResult(reason_code="policy_forbidden:identity_domain: "
                                           + "; ".join(forbidden_reasons[:3]))
    if not actions and malformed:
        return GeneratorResult(reason_code=f"invalid_rival_prediction: {malformed} point(s)")
    return GeneratorResult(actions)


def _narrow_rival_evaluate(action: GeneratedAction,
                           context: AcquisitionContext) -> ActionEvaluation:
    """Rank the probe below ordinary work.

    Deliberately a fixed low priority rather than a computed EIG: this generator only ever fires where
    the cheap path has already been shown not to pay, so the ordering that matters is "after everything
    else", and a fabricated information estimate would give the ranking a precision it does not have.
    """
    return ActionEvaluation(
        intent=action.intent, generator=action.generator,
        eligibility=ActionEligibility.ELIGIBLE,
        selection_reason=action.reason,
        score_method="identity",
        policy_rank=IDENTITY_PROBE_RANK)


# The callables behind the declarative registry. The registry says *what* must run for an exhaustion
# proof to be valid; these say *how*. They are validated against each other at import (below) because
# either half drifting is a silent correctness failure: a registered generator with no callable would
# report nothing and let an exhaustion proof cover an action class that never ran, and a callable with
# no registry entry would simply never be consulted.
ActionGenerator = Callable[[AcquisitionContext], list[GeneratedAction]]
ActionEvaluator = Callable[[GeneratedAction, AcquisitionContext], ActionEvaluation]

ACTION_GENERATORS: dict[str, ActionGenerator] = {
    LEGACY_SELECTOR_GENERATOR: _legacy_selector_generate,
    NARROW_RIVAL_PROBE_GENERATOR: _narrow_rival_generate,
}
ACTION_EVALUATORS: dict[str, ActionEvaluator] = {
    LEGACY_SELECTOR_GENERATOR: _legacy_selector_evaluate,
    NARROW_RIVAL_PROBE_GENERATOR: _narrow_rival_evaluate,
}

_declared = {spec.generator_id for spec in GENERATOR_REGISTRY}
if set(ACTION_GENERATORS) != _declared or set(ACTION_EVALUATORS) != _declared:
    raise RuntimeError(
        "action generator/evaluator dispatch does not match the declared GENERATOR_REGISTRY: "
        f"registry={sorted(_declared)} generators={sorted(ACTION_GENERATORS)} "
        f"evaluators={sorted(ACTION_EVALUATORS)}")
del _declared


def _dedupe_generated(actions: list[GeneratedAction]) -> list[GeneratedAction]:
    """Collapse actions asking the same scientific question, first-seen wins.

    A duplicate is not a second option — it is one question that would be paid for twice. The surviving
    order comes from :meth:`ActionInventory.dedupe_intents` so the intent-level rule has exactly one
    implementation.
    """
    surviving = {intent.scientific_action_fingerprint
                 for intent in ActionInventory.dedupe_intents([a.intent for a in actions])}
    seen: set[str] = set()
    kept: list[GeneratedAction] = []
    for action in actions:
        fingerprint = action.intent.scientific_action_fingerprint
        if fingerprint in seen or fingerprint not in surviving:
            continue
        seen.add(fingerprint)
        kept.append(action)
    return kept


def generate_action_intents(
        context: AcquisitionContext) -> tuple[list[GeneratedAction], list[GeneratorReport]]:
    """Run every registered generator, dedupe the result, and account for what each one did.

    Failure isolation is per generator: one that raises leaves *its* actions unknown while its peers
    keep contributing. The inventory is then incomplete — exhaustion is unprovable over an action
    class that never ran — which the terminal reads as a resumable ``controller_fault`` rather than a
    scientific result, and rather than the escaped exception that used to kill an unattended campaign.
    """
    raw: list[GeneratedAction] = []
    failures: dict[str, str] = {}
    declared_reasons: dict[str, str] = {}
    for spec in GENERATOR_REGISTRY:
        generator = ACTION_GENERATORS.get(spec.generator_id)
        if generator is None:
            failures[spec.generator_id] = "NotRegistered: no callable for a declared generator"
            continue
        try:
            result = generator(context) or GeneratorResult()
            # The registry, not the generator, owns attribution: a generator that stamped a peer's id
            # (by construction, by copy-paste, or by delegating to it) would make the accounting name
            # the wrong producer, and every completeness/dedup count downstream would be about a
            # generator that never ran.
            raw.extend(replace(action, generator=spec.generator_id)
                       for action in (result.actions or []))
            if result.reason_code:
                declared_reasons[spec.generator_id] = str(result.reason_code)[:300]
        except Exception as error:  # noqa: BLE001 - a fault must be recorded, never escape
            failures[spec.generator_id] = f"{type(error).__name__}: {error}"[:300]
    kept = _dedupe_generated(raw)
    return kept, _generator_reports(raw, kept, failures, declared_reasons)


def _generator_reports(raw: list[GeneratedAction], kept: list[GeneratedAction],
                       failures: dict[str, str],
                       declared_reasons: dict[str, str] | None = None) -> list[GeneratorReport]:
    """One report per declared generator — the accounting an exhaustion proof rests on."""
    reports: list[GeneratorReport] = []
    for spec in GENERATOR_REGISTRY:
        generator_id = spec.generator_id
        if generator_id in failures:
            reports.append(GeneratorReport(
                generator=generator_id, status=GeneratorStatus.FAILED, generated=0,
                error_code=failures[generator_id]))
            continue
        produced = sum(1 for action in raw if action.generator == generator_id)
        survived = sum(1 for action in kept if action.generator == generator_id)
        # A generator's own account wins over the derived one: "nothing came back" is all the counts
        # can show, and it cannot distinguish routine silence from a withheld action.
        derived = ("no_selectable_action" if not produced else
                   "all_actions_deduplicated" if not survived else None)
        reports.append(GeneratorReport(
            generator=generator_id, status=GeneratorStatus.COMPLETED, generated=survived,
            deduped=produced - survived,
            reason_code=(declared_reasons or {}).get(generator_id) or derived))
    return reports


def evaluate_action_intents(actions: list[GeneratedAction],
                            context: AcquisitionContext) -> list[ActionEvaluation]:
    """Score each generated action. Raises whatever an evaluator raises — see :func:`_evaluate_safely`."""
    return [ACTION_EVALUATORS[action.generator](action, context) for action in actions]


def _apply_suppressions(evaluations: list[ActionEvaluation],
                        context: AcquisitionContext) -> list[ActionEvaluation]:
    """Demote already-refused actions to REJECTED, keeping them in the inventory.

    Suppression is applied to the *evaluation*, not by dropping the action during generation, for two
    reasons: the accounting stays consistent (a generator's ``generated`` count still matches its
    recorded evaluations, so the proof stays structurally valid), and the terminal names what emptied
    the action space instead of presenting a blank one.
    """
    if not context.suppressed and not context.unreachable_hkl:
        return evaluations
    applied: list[ActionEvaluation] = []
    for evaluation in evaluations:
        entry = (context.suppressed.get(evaluation.intent.scientific_action_fingerprint)
                 or context.unreachable_hkl.get(hkl_key(evaluation.intent.hkl)))
        if entry is None:
            applied.append(evaluation)
            continue
        try:
            rejection_class = RejectionClass(str(entry.get("class", "structural")).lower())
        except ValueError:
            rejection_class = RejectionClass.STRUCTURAL
        applied.append(replace(
            evaluation, eligibility=ActionEligibility.REJECTED,
            rejection_class=rejection_class,
            rejection_reason=str(entry.get("reason") or "previously refused")))
    return applied


def _evaluate_safely(actions: list[GeneratedAction], context: AcquisitionContext
                     ) -> tuple[list[GeneratedAction], list[ActionEvaluation], dict[str, str]]:
    """Evaluate with the same per-generator isolation generation gets.

    An action whose utility could not be computed cannot be ranked against its rivals: leaving it
    eligible would let selection pick an unscored action, and dropping it quietly would let the
    remaining set masquerade as the complete space. So an evaluator fault demotes its whole generator
    to ``FAILED``, exactly as a generation fault does.
    """
    evaluations: dict[int, ActionEvaluation] = {}
    failures: dict[str, str] = {}
    for position, action in enumerate(actions):
        evaluator = ACTION_EVALUATORS.get(action.generator)
        if evaluator is None:
            failures[action.generator] = "NotRegistered: no evaluator for a declared generator"
            continue
        try:
            evaluations[position] = evaluator(action, context)
        except Exception as error:  # noqa: BLE001 - an unscorable action is a fault, not an option
            failures[action.generator] = f"{type(error).__name__}: {error}"[:300]
    kept, scored = [], []
    for position, action in enumerate(actions):
        if action.generator in failures or position not in evaluations:
            continue
        kept.append(action)
        scored.append(evaluations[position])
    return kept, scored, failures


def enumerate_actions(objective, model, results_so_far: list[dict], resolution_info=None,
                      budget_state: dict | None = None, master_rows: list[dict] | None = None,
                      fisher=None, classify=None, rival_band=None,
                      runtime_limits: dict | None = None,
                      track_anchor_index: int | None = None,
                      track_zone_period: float | None = None,
                      branch_track: dict | None = None,
                      scoring_allowance_seconds: float = 60.0,
                      suppressed: dict | None = None,
                      unreachable_hkl: dict | None = None,
                      campaign_slice=None, campaign_slice_error: str | None = None):
    """Enumerate the acquisition action space and report per-generator completeness.

    The pipeline is now the declared one — build one context, generate intents, dedupe them, evaluate
    them, and hand the survivors to :func:`select_action` — rather than a single fused selector. With
    only ``legacy_selector_adapter`` registered the selected action is identical to before; the split
    exists so PR2.7's endpoint generators plug in beside it without either side rebuilding the world.
    """
    if campaign_slice_error:
        # Fail closed without crashing an unattended run. The generators cannot be given a
        # trustworthy evidence basis, so they did not run — the action space is unknown, exhaustion is
        # unprovable, and the terminal becomes a resumable ``controller_fault`` rather than a guess
        # made against whichever belief slice happened to sort highest.
        return ActionInventory(
            evaluations=[],
            reports=[GeneratorReport(generator=spec.generator_id, status=GeneratorStatus.FAILED,
                                     generated=0, error_code=str(campaign_slice_error)[:300])
                     for spec in GENERATOR_REGISTRY]), []
    context = build_acquisition_context(
        objective, model, results_so_far, resolution_info=resolution_info,
        master_rows=master_rows, fisher=fisher, classify=classify, runtime_limits=runtime_limits,
        track_anchor_index=track_anchor_index, track_zone_period=track_zone_period,
        branch_track=branch_track, scoring_allowance_seconds=scoring_allowance_seconds,
        suppressed=suppressed, unreachable_hkl=unreachable_hkl, campaign_slice=campaign_slice,
        rival_band=rival_band)
    generated, reports = generate_action_intents(context)
    generated, evaluations, evaluator_failures = _evaluate_safely(generated, context)
    evaluations = _apply_suppressions(evaluations, context)
    if evaluator_failures:
        # Demote just the faulting generators, leaving every healthy generator's generation accounting
        # (including its dedupe count) intact — the failure is in scoring, not in what was enumerated.
        reports = [GeneratorReport(generator=report.generator, status=GeneratorStatus.FAILED,
                                   generated=0, error_code=evaluator_failures[report.generator])
                   if report.generator in evaluator_failures else report
                   for report in reports]
    # Only eligible evaluations yield a buildable candidate; the rejected ones stay in the inventory
    # as the record of what was refused and why.
    candidates = [_CandidateAction(generated=action, evaluation=evaluation, context=context)
                  for action, evaluation in zip(generated, evaluations) if evaluation.is_eligible]
    return ActionInventory(evaluations=evaluations, reports=reports), candidates


def select_action(inventory: ActionInventory,
                  candidates: list[_CandidateAction]) -> _CandidateAction | None:
    """Choose the best eligible candidate. The staged ranking comparator arrives in PR3c."""
    eligible = [c for c in candidates if c.evaluation.is_eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda c: c.evaluation.policy_rank)


@dataclass
class ActionPlan:
    """The enumerated action space plus the ability to realize its actions into requests, in order.

    The controller used to get exactly one proposal per iteration, so a request the instrument refused
    cost the whole iteration even when the inventory still held other eligible actions. Holding the
    candidates lets the loop try the next one immediately, while ``inventory`` stays the single
    canonical action space the terminal decision reads.
    """

    inventory: ActionInventory
    candidates: list[_CandidateAction]
    _build_context: tuple
    _tried: set = field(default_factory=set)

    def next_proposal(self) -> "ScanProposal | None":
        """Realize the best eligible action not yet attempted this iteration, or ``None``."""
        objective, model, resolution_info, budget_state, fisher, rival_band, classify = \
            self._build_context
        for candidate in sorted(self.candidates, key=lambda c: c.evaluation.policy_rank):
            fingerprint = candidate.evaluation.intent.scientific_action_fingerprint
            if fingerprint in self._tried:
                continue
            self._tried.add(fingerprint)
            return _build_proposal(objective, candidate, model, resolution_info, budget_state,
                                   fisher, rival_band, classify)
        return None


def plan_actions(objective, model, results_so_far: list[dict], resolution_info=None,
                 budget_state: dict | None = None, master_rows: list[dict] | None = None,
                 fisher=None, classify=None, rival_band=None,
                 runtime_limits: dict | None = None,
                 track_anchor_index: int | None = None,
                 track_zone_period: float | None = None,
                 branch_track: dict | None = None,
                 scoring_allowance_seconds: float = 60.0,
                 suppressed: dict | None = None,
                 unreachable_hkl: dict | None = None,
                 campaign_slice=None, campaign_slice_error: str | None = None) -> ActionPlan:
    """Enumerate the action space once and return a plan that can realize its actions in order."""
    inventory, candidates = enumerate_actions(
        objective, model, results_so_far, resolution_info=resolution_info,
        budget_state=budget_state, master_rows=master_rows, fisher=fisher, classify=classify,
        rival_band=rival_band, runtime_limits=runtime_limits, track_anchor_index=track_anchor_index,
        track_zone_period=track_zone_period, branch_track=branch_track,
        scoring_allowance_seconds=scoring_allowance_seconds, suppressed=suppressed,
        unreachable_hkl=unreachable_hkl, campaign_slice=campaign_slice,
        campaign_slice_error=campaign_slice_error)
    return ActionPlan(
        inventory=inventory, candidates=candidates,
        _build_context=(objective, model, resolution_info, budget_state, fisher, rival_band,
                        classify))


def propose_action(objective, model, results_so_far: list[dict], resolution_info=None,
                   budget_state: dict | None = None, master_rows: list[dict] | None = None,
                   fisher=None, classify=None, rival_band=None,
                   runtime_limits: dict | None = None,
                   track_anchor_index: int | None = None,
                   track_zone_period: float | None = None,
                   branch_track: dict | None = None,
                   scoring_allowance_seconds: float = 60.0,
                   suppressed: dict | None = None,
                   unreachable_hkl: dict | None = None,
                   campaign_slice=None, campaign_slice_error: str | None = None
                   ) -> tuple[ScanProposal | None, ActionInventory]:
    """Enumerate, select, and build the single best action — plus the canonical inventory.

    A thin view over :func:`plan_actions` that takes only its first proposal. The drive loop uses the
    plan directly, so a refused request can fall through to the next eligible action instead of
    costing the whole iteration; callers that only ever want one action keep this shape. When the
    proposal is ``None`` the terminal decision reads :meth:`ActionInventory.terminal_cause` off the
    very inventory acquisition enumerated (PR1).
    """
    plan = plan_actions(
        objective, model, results_so_far, resolution_info=resolution_info,
        budget_state=budget_state, master_rows=master_rows, fisher=fisher, classify=classify,
        rival_band=rival_band, runtime_limits=runtime_limits, track_anchor_index=track_anchor_index,
        track_zone_period=track_zone_period, branch_track=branch_track,
        scoring_allowance_seconds=scoring_allowance_seconds, suppressed=suppressed,
        unreachable_hkl=unreachable_hkl, campaign_slice=campaign_slice,
        campaign_slice_error=campaign_slice_error)
    return plan.next_proposal(), plan.inventory


def propose_scan(objective, model, results_so_far: list[dict], resolution_info=None,
                 budget_state: dict | None = None, master_rows: list[dict] | None = None,
                 fisher=None, classify=None, rival_band=None,
                 runtime_limits: dict | None = None,
                 track_anchor_index: int | None = None,
                 track_zone_period: float | None = None,
                 branch_track: dict | None = None,
                 scoring_allowance_seconds: float = 60.0) -> ScanProposal | None:
    """Propose the next scan (Q, energy window, neutron count), or ``None`` if nothing remains.

    Back-compat wrapper over :func:`propose_action` that returns only the proposal. Callers that need
    the completeness/terminal proof (the drive loop) call :func:`propose_action` directly.

    Args:
        objective: the :class:`~isar.drive.objective.ObjectiveSpec` (path + tolerance).
        model: a fitted ω(q) model exposing ``predict(q) -> (mean, sigma)``
            (:mod:`isar.drive.acquisition.model`).
        results_so_far: the chronological observation list (see
            :func:`isar.drive.supervisor.stopping.summarize_grid`).
        resolution_info: the energy-resolution source — a float, a dict, or a callable
            ``(h,k,l,deltaE) -> …`` (the loop wraps ``GET /resolution``). See :func:`_resolve_fwhm`.
        budget_state: informational; carried onto the proposal meta. The stopping criterion owns the
            budget verdict, not the acquisition function.
        fisher: optional Poisson-Fisher screen provider ``(gridq, omega0_pred, sigma_prior) -> σ_scan``
            (5b). Returns the predicted post-scan σ(ω₀) when the q is Fisher-grounded, else ``None``.
    """
    proposal, _inventory = propose_action(
        objective, model, results_so_far, resolution_info=resolution_info,
        budget_state=budget_state, master_rows=master_rows, fisher=fisher, classify=classify,
        rival_band=rival_band, runtime_limits=runtime_limits, track_anchor_index=track_anchor_index,
        track_zone_period=track_zone_period, branch_track=branch_track,
        scoring_allowance_seconds=scoring_allowance_seconds)
    return proposal


def _build_proposal(objective, chosen: "_CandidateAction", model, resolution_info,
                    budget_state, fisher, rival_band, classify) -> ScanProposal:
    """Build the executable :class:`ScanProposal` from a selected candidate.

    Reads its energy-window/count context from ``chosen`` (the exact values selection used), never a
    second read of ``objective.steering`` that could diverge from what ``enumerate_actions`` chose.
    """
    pick, kind, reason, score = chosen.pick, chosen.kind, chosen.reason, chosen.score
    # The grid, opening, inserted candidate and target selection all live in ``enumerate_actions``;
    # here we only realise the winning candidate into an executable window/count.
    target_hypothesis = chosen.target_hypothesis
    policy = chosen.policy
    runtime_limits = chosen.runtime_limits
    classification_weight = chosen.classification_weight
    # Reuse the decision the intent was fixed against — never re-derive it here, or the emitted
    # action's scientific class can disagree with the inventory's.
    driver_inserted = chosen.driver_inserted
    if driver_inserted:
        reason = (f"driver-inserted classification probe q={pick.q:g} (hkl={list(pick.hkl)}); "
                  "higher expected branch-information value than every certification point")
        classification = max(float(classify(pick)), 0.0)
        combined = ((score.eig or 0.0) + classification_weight * classification)
        score = replace(score, reason=reason, purpose="differentiate",
                        classification_eig=_finite_or_none(classification),
                        classification_weight=classification_weight,
                        combined_information=_finite_or_none(combined))

    e_pred, e_sigma = model.predict(pick.q)
    target_prediction_mean = float(e_pred)
    target_prediction_sigma = float(e_sigma)
    design_prediction_mean = target_prediction_mean
    design_prediction_sigma = target_prediction_sigma
    h, k, l = pick.hkl
    fwhm = _resolve_fwhm(resolution_info, h, k, l, max(float(e_pred), 0.0))
    # Survey the band until this q has a confirmed resolved fit — a concave dispersion sits above the
    # local-linear e_pred, so a tight window on an unconfirmed q misses the peak and never seeds.
    survey = not bool(getattr(pick, "best_resolved", False))
    target_band = None
    positive_only = False
    if target_hypothesis is not None and target_hypothesis.get("name") == "optic" and survey:
        zc = target_hypothesis.get("zc_energy") or {}
        target_band = (zc.get("mean", zc.get("mode", e_pred)), zc.get("sigma", e_sigma))
        positive_only = True
    elif target_hypothesis is not None:
        target_band = (max(float(e_pred), 0.0), max(float(e_sigma), 0.5 * fwhm))
    widen = widen_factor(
        getattr(pick, "n_decline", 0), step=float(policy.get("absent_signal_widen_step", 0.5)),
        max_steps=int(policy.get("absent_signal_widen_steps", 3)))
    rb = rival_band(pick) if callable(rival_band) else rival_band
    intent = chosen.evaluation.intent
    if intent.window_family == "rival" and intent.energy_lo is not None and intent.energy_hi is not None:
        if (rb is None or len(rb) != 2
                or not all(math.isfinite(float(value)) for value in rb)
                or float(rb[1]) < 0.0):
            raise ValueError(
                "rival-window scan design requires a finite rival mean and non-negative sigma")
        design_prediction_mean, design_prediction_sigma = map(float, rb)
        # The energy this scan is *about* is the rival, not the target. ``e_pred`` becomes the
        # persisted ``acquisition_target_energy`` and the centre the effective-exposure weighting is
        # computed around, so leaving it at the target model's prediction would score the probe's
        # counts over a region it deliberately did not scan. Prefer the declared centre: the bounds
        # were clamped into the identity domain, so their midpoint is not the predicted rival whenever
        # the domain clipped one side.
        e_pred = (float(intent.window_center_meV) if intent.window_center_meV is not None
                  else 0.5 * (float(intent.energy_lo) + float(intent.energy_hi)))
        # Re-resolve the instrument resolution *at the rival*. ``fwhm`` above was resolved at the
        # target's prediction, and using it here set the step spacing and the persisted ``fwhm_dE`` from
        # a resolution belonging to an energy this scan does not visit — undoing the generator's
        # deliberate use of the rival-local FWHM to keep a broad-resolution mode from reading as a
        # false null.
        fwhm = _resolve_fwhm(resolution_info, h, k, l, e_pred)

    master = pick.master or {}
    budget = dict(budget_state or {})
    policy_max_points = int(policy.get("max_scan_points", MAX_POINTS))
    design_context = ScanDesignContext(
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        hkl=(float(h), float(k), float(l)),
        q=float(pick.q),
        kind=str(kind),
        reason=str(reason),
        window_family=str(intent.window_family),
        declared_energy_lo=intent.energy_lo,
        declared_energy_hi=intent.energy_hi,
        target_prediction_mean_meV=target_prediction_mean,
        target_prediction_sigma_meV=target_prediction_sigma,
        design_prediction_mean_meV=design_prediction_mean,
        design_prediction_sigma_meV=design_prediction_sigma,
        resolution_fwhm_meV=float(fwhm),
        survey=bool(survey),
        energy_search_meV=(
            tuple(float(value) for value in objective.energy_search_meV)
            if getattr(objective, "energy_search_meV", None) is not None else None),
        target_band=(
            tuple(float(value) for value in target_band)
            if target_band is not None else None),
        rival_band=tuple(float(value) for value in rb) if rb is not None else None,
        widen_factor=float(widen),
        positive_only=bool(positive_only),
        policy_max_points=policy_max_points,
        runtime_max_points=int(runtime_limits.get("max_points", policy_max_points)),
        tolerance_meV=float(objective.tolerance_meV),
        # Frozen into the design context so the whole candidate space is reconstructible from the
        # persisted record; the loop never re-reads the live policy to decide what was designed.
        scan_window_eig_mode=str(policy.get("scan_window_eig_mode", "off")),
        minimum_target_support_points=int(policy.get("minimum_target_support_points", 6)),
        quicklook_ncount=int(policy.get("quicklook_ncount", QUICKLOOK_NCOUNT)),
        production_ncount=int(policy.get("production_batch_ncount", PRODUCTION_NCOUNT)),
        identity_detection_ncount=int(policy.get(
            "identity_detection_ncount",
            policy.get("quicklook_ncount", QUICKLOOK_NCOUNT))),
        dynamic_ncount=bool(policy.get("dynamic_ncount", False)),
        tolerance_margin=float(policy.get("tolerance_margin", 0.8)),
        ncount_growth_cap=float(policy.get("ncount_growth_cap", 4.0)),
        max_ncount_per_scan=int(policy.get("max_ncount_per_scan", MAX_NCOUNT_PER_SCAN)),
        diagnostic_halfwidth_meV=master.get(
            "diagnostic_halfwidth", master.get("halfwidth")),
        certification_halfwidth_meV=pick.cert_halfwidth,
        effective_exposure=master.get("effective_exposure"),
        submitted_exposure=pick.submitted_exposure,
        production_scans=int(pick.n_production),
        last_submitted_ncount=pick.last_submitted_ncount,
        runtime_max_neutrons_per_point=runtime_limits.get("max_neutrons_per_point"),
        runtime_queue_neutron_budget=runtime_limits.get("queue_neutron_budget"),
        budget_kind=budget.get("kind"),
        budget_amount=budget.get("amount"),
        budget_spent=float(budget.get("spent", 0.0)),
    )
    design_inventory = design_scan_windows(design_context)
    design = design_inventory.selected_candidate
    e_lo, e_hi, e_step, n_points = (
        design.e_lo, design.e_hi, design.e_step, design.n_points)
    ncount = design.ncount
    count_diagnostics = design.diagnostics

    selected_evidence = pick.evidence_master or pick.master or {}
    evidence_revision = (
        selected_evidence.get("master_revision")
        or (score.evidence_transition or {}).get("authoritative_evidence_revision")
        or selected_evidence.get("content_hash")
        or selected_evidence.get("production_batch_fingerprint")
        or selected_evidence.get("track_revision")
    )
    action_role = ("classification" if score.purpose == "differentiate" else
                   "discovery" if kind == "quicklook" else "production")
    target_branch = getattr(objective, "target_branch", None)
    rival_branch = ("acoustic" if target_branch == "optic" else
                    "optic" if target_branch == "acoustic" else None)

    score_recompute = None
    if score.method == "fisher" and _fisher_supports_exposure(fisher):
        sigma_prior = score.sigma_prior
        classification = float(score.classification_eig or 0.0)
        score_classification_weight = float(score.classification_weight or 0.0)

        def score_recompute(exposure: int) -> dict[str, float | None]:
            sigma_scan = fisher(pick, float(e_pred), sigma_prior, int(exposure))
            eig = fisher_eig(sigma_prior, sigma_scan)
            combined = eig + score_classification_weight * classification
            return {"eig": _finite_or_none(eig),
                    "sigma_scan": _finite_or_none(sigma_scan),
                    "combined_information": _finite_or_none(combined)}

    proposal = ScanProposal(
        h=h, k=k, l=l, e_lo=e_lo, e_hi=e_hi, e_step=e_step, ncount=ncount, kind=kind,
        reason=reason, e_pred=float(e_pred), fwhm_dE=float(fwhm), n_points=n_points,
        meta={"e_sigma": _finite_or_none(e_sigma), "q": pick.q, "score": score.to_dict(),
              "purpose": score.purpose, "widen_factor": widen,
              "acquisition_action_role": action_role,
              "target_branch_role": target_branch,
              "rival_branch_role": rival_branch,
              "authoritative_evidence_revision": (
                  str(evidence_revision) if evidence_revision is not None else None),
              "evidence_transition": score.evidence_transition,
              "driver_inserted": driver_inserted,
              # Joins the emitted scan back to the action evaluation that justified it: without it a
              # scan carries only its own request fingerprint and cannot be attributed to an intent.
              "scientific_action_fingerprint": chosen.evaluation.intent.scientific_action_fingerprint,
              "action_class": chosen.evaluation.intent.action_class,
              "evidence_sink": chosen.evaluation.intent.evidence_sink,
              # The belief context this action was formed against. Carried so the pooled-master update
              # it produces can record which identity question it answered — saturation is only
              # comparable across revisions that answered the same one.
              "hypotheses_fingerprint": chosen.evaluation.intent.hypotheses_fingerprint,
              "campaign_slice_fingerprint": chosen.evaluation.intent.campaign_slice_fingerprint,
              # A non-canonical sink is never certification-eligible, whoever inserted it: the scan was
              # fired at a deliberately narrow question and its exposure was never spent on delivering
              # the target, so it cannot stand in for a requested certification point.
              "certification_eligible": (
                  not driver_inserted
                  and chosen.evaluation.intent.evidence_sink not in NON_CANONICAL_SINKS),
              "insertion_purpose": "differentiate" if driver_inserted else None,
              "max_scan_points": int(policy.get("max_scan_points", MAX_POINTS)),
              "target_band": list(target_band) if target_band is not None else None,
              "rival_band": list(rb) if rb is not None else None,
              "count_diagnostics": count_diagnostics,
              "budget_state": dict(budget_state) if budget_state else {}},
        score_recompute=score_recompute,
        intent=chosen.evaluation.intent,
        scan_design_inventory=design_inventory)
    proposal.shadow_scoring_source = ShadowScoringSource(
        master_session_key=(
            str(selected_evidence["master_session_key"])
            if selected_evidence.get("master_session_key") is not None else None),
        detector_id=(
            str(selected_evidence["detector_id"])
            if selected_evidence.get("detector_id") is not None else None),
        master_revision=(
            str(selected_evidence["master_revision"])
            if selected_evidence.get("master_revision") is not None else None),
        source_acquisition_id=(
            str(selected_evidence["source_acquisition_id"])
            if selected_evidence.get("source_acquisition_id") is not None else None),
        source_submission_request_fingerprint=(
            str(selected_evidence["source_submission_request_fingerprint"])
            if selected_evidence.get("source_submission_request_fingerprint") is not None else None),
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        predictive_ref=(
            str(selected_evidence["predictive_ref"])
            if selected_evidence.get("predictive_ref") is not None else None),
        target_projection_ref=(
            str(selected_evidence["target_projection_ref"])
            if selected_evidence.get("target_projection_ref") is not None else None),
        target_outcome=dict(selected_evidence) if selected_evidence else None,
        selection_record=(
            dict(selected_evidence["fit_selection_semantics"])
            if isinstance(selected_evidence.get("fit_selection_semantics"), dict) else None),
        shadow_record=(
            dict(selected_evidence["shadow_model_selection"])
            if isinstance(selected_evidence.get("shadow_model_selection"), dict) else None),
        target_projection_authority=(
            dict(selected_evidence["target_projection_authority"])
            if isinstance(selected_evidence.get("target_projection_authority"), dict) else None),
        action_intent=intent,
        action_provenance={
            "action_class": proposal.meta.get("action_class"),
            "scientific_action_fingerprint": intent.scientific_action_fingerprint,
            "proposal_scientific_action_fingerprint": intent.scientific_action_fingerprint,
            "acquisition_action_role": proposal.meta.get("acquisition_action_role"),
            "evidence_sink": proposal.meta.get("evidence_sink"),
            "driver_inserted": proposal.meta.get("driver_inserted"),
            "insertion_purpose": proposal.meta.get("insertion_purpose"),
            "authoritative_evidence_revision": proposal.meta.get(
                "authoritative_evidence_revision"),
            "hkl": [float(h), float(k), float(l)],
        },
    )
    normalize_proposal_score(proposal, float(objective.tolerance_meV))
    normalized_score = proposal.meta["score"]
    epc = normalized_score.get("eig_per_cost")
    threshold = float(policy.get("minimum_information_per_cost", 0.0))
    if epc is not None and threshold > 0.0 and epc < threshold:
        proposal.meta.setdefault("diagnostics", []).append("diminishing_returns")
    return proposal

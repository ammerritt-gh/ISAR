"""Canonical action-inventory value types for the drive controller (PR1, increment 1).

The controller's terminal decision must depend on the *same* action inventory that acquisition
enumerates — never on a separate approximation. These pure value types are that inventory. Later
increments make ``propose_scan`` produce an :class:`ActionInventory` and route the branch-ambiguity
terminal through it; this module only defines the vocabulary and the completeness rule.

Three identities travel with a measurement action, each stable across the stage it names, so retries,
suppression, resume, and audit can reason about them independently (see the plan, "three lifecycle
types"):

* ``MeasurementIntent`` — *what uncertainty are we trying to resolve?* Its
  ``scientific_action_fingerprint`` is the durable id used for dedup and anti-repeat.
* ``FinalizedProposal`` — *what exact request did we ask TAVI to validate/submit?*
  (``request_fingerprint``); it exists before validation.
* ``ValidatedAttempt`` — *under what instrument geometry did TAVI accept or reject it?*
  (``feasibility_manifest_fingerprint``).

The load-bearing invariant lives in :meth:`ActionInventory.terminal_cause`: a branch-ambiguity
terminal (``action_inventory_exhausted``) is legal only when the inventory is **complete** — every
applicable generator ran to completion — and holds no eligible action. A generator that raised, timed
out, or lacked required context makes the inventory incomplete, and an incomplete inventory is a
``controller_fault``, never a scientific result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum


class ActionEligibility(str, Enum):
    ELIGIBLE = "eligible"
    REJECTED = "rejected"


class RejectionClass(str, Enum):
    """Why an enumerated action is not selectable — and, crucially, whether the rejection is durable.

    A ``TRANSIENT`` rejection (a network or queue failure, a momentary instrument state) must never
    permanently suppress the scientific action; the other classes are durable for the current
    evidence/instrument basis.
    """

    STRUCTURAL = "structural"        # physically/geometrically impossible for this evidence basis
    SATURATION = "saturation"        # this action class has stopped paying at this q
    BUDGET = "budget"                # unaffordable under the remaining budget
    TRANSIENT = "transient"          # a momentary failure; retryable, non-suppressing
    OPERATOR = "operator"            # forbidden by declared operator/scientist policy


class GeneratorStatus(str, Enum):
    COMPLETED = "completed"          # ran to completion (it may still have generated nothing)
    INAPPLICABLE = "inapplicable"    # ran, decided it does not apply, gave a structured reason
    FAILED = "failed"                # raised, timed out, or lacked required context


# The controller's terminal state stays one of the existing StopDecision states; ``terminal_cause``
# is an additive refinement. These are the two the inventory itself can produce.
TERMINAL_ACTION_INVENTORY_EXHAUSTED = "action_inventory_exhausted"
TERMINAL_CONTROLLER_FAULT = "controller_fault"


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def scientific_fingerprint(intent: "MeasurementIntent") -> str:
    """Stable hash of the *scientific question* an intent resolves — its dedup / anti-repeat id.

    Deliberately excludes anything about the exact emitted request (window step, ncount, feasibility):
    two intents that ask the same scientific question with different executable realizations share one
    fingerprint and must not both occupy the inventory.
    """
    payload = _canonical([
        intent.action_class,
        [round(float(x), 9) for x in intent.hkl],
        None if intent.energy_lo is None else round(float(intent.energy_lo), 6),
        None if intent.energy_hi is None else round(float(intent.energy_hi), 6),
        intent.window_family,
        intent.target_branch_role,
        intent.rival_branch_role,
        intent.evidence_basis_fingerprint,
        intent.hypotheses_fingerprint,
        intent.campaign_slice_fingerprint,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MeasurementIntent:
    """The scientific question an action resolves, independent of any exact emitted request."""

    action_class: str
    hkl: tuple[float, float, float]
    energy_lo: float | None
    energy_hi: float | None
    window_family: str
    target_branch_role: str | None = None
    rival_branch_role: str | None = None
    evidence_basis_fingerprint: str | None = None
    hypotheses_fingerprint: str | None = None
    campaign_slice_fingerprint: str | None = None

    @property
    def scientific_action_fingerprint(self) -> str:
        return scientific_fingerprint(self)


@dataclass(frozen=True)
class FinalizedProposal:
    """The exact request after policy/envelope/budget finalization — pre-validation."""

    intent: MeasurementIntent
    request_fingerprint: str
    e_lo: float
    e_hi: float
    e_step: float
    n_points: int
    ncount: int


@dataclass(frozen=True)
class ValidatedAttempt:
    """TAVI's verdict on one finalized request, under a specific instrument geometry."""

    proposal: FinalizedProposal
    feasibility_manifest_fingerprint: str
    feasible_points: int
    status: str


@dataclass(frozen=True)
class ActionEvaluation:
    """One enumerated intent plus its eligibility and (deferred) scored utility."""

    intent: MeasurementIntent
    generator: str
    eligibility: ActionEligibility
    rejection_reason: str | None = None
    rejection_class: RejectionClass | None = None
    # Score vector, populated by evaluators in a later increment; None until then.
    expected_identity_gain: float | None = None
    expected_precision_gain: float | None = None
    expected_coverage_gain: float | None = None
    expected_measurement_cost: float | None = None
    expected_analysis_cost: float | None = None
    score_method: str | None = None
    score_confidence: float | None = None
    # A deterministic ordering key; lower is preferred. The real staged ranking replaces this later.
    policy_rank: float = 0.0

    @property
    def is_eligible(self) -> bool:
        return self.eligibility is ActionEligibility.ELIGIBLE


@dataclass(frozen=True)
class GeneratorReport:
    """Per-generator accounting — the proof that the inventory is complete (or why it is not).

    ``status`` is the single source of truth; ``applicable`` is derived from it rather than stored, so
    the two can never contradict (an ``applicable=False, status=FAILED`` limbo cannot be constructed).
    """

    generator: str
    status: GeneratorStatus
    generated: int = 0
    reason_code: str | None = None   # why INAPPLICABLE / why it generated nothing
    error_code: str | None = None    # why FAILED

    @property
    def applicable(self) -> bool:
        return self.status is not GeneratorStatus.INAPPLICABLE

    @property
    def completed_cleanly(self) -> bool:
        """Whether this generator contributes a trustworthy '0 or more actions' to an exhaustion proof.

        A generator that ran (COMPLETED) or cleanly declined (INAPPLICABLE) is trustworthy input; one
        that FAILED is not — its actions are unknown, so exhaustion cannot be proven over it.
        """
        return self.status in (GeneratorStatus.COMPLETED, GeneratorStatus.INAPPLICABLE)


@dataclass
class ActionInventory:
    """The enumerated action space at one decision boundary, with its completeness proof."""

    evaluations: list[ActionEvaluation] = field(default_factory=list)
    reports: list[GeneratorReport] = field(default_factory=list)
    inventory_version: str = "isar.action-inventory/1"

    @property
    def is_complete(self) -> bool:
        """True only when every applicable generator ran to completion (none FAILED)."""
        return all(report.completed_cleanly for report in self.reports)

    def eligible_actions(self) -> list[ActionEvaluation]:
        return [e for e in self.evaluations if e.is_eligible]

    @property
    def has_eligible_action(self) -> bool:
        return any(e.is_eligible for e in self.evaluations)

    def terminal_cause(self) -> str | None:
        """The action-space terminal this inventory implies, or None if it is not terminal.

        Only speaks to the action-space: hard terminals (met, budget, analysis guardrail, operator
        stop, physical infeasibility) are decided before the inventory is consulted.
        """
        if not self.is_complete:
            return TERMINAL_CONTROLLER_FAULT
        if self.has_eligible_action:
            return None
        return TERMINAL_ACTION_INVENTORY_EXHAUSTED

    def failed_reports(self) -> list[GeneratorReport]:
        return [r for r in self.reports if r.status is GeneratorStatus.FAILED]

    @staticmethod
    def dedupe_intents(intents: list[MeasurementIntent]) -> list[MeasurementIntent]:
        """Collapse intents that ask the same scientific question, preserving first-seen order."""
        seen: set[str] = set()
        deduped: list[MeasurementIntent] = []
        for intent in intents:
            key = intent.scientific_action_fingerprint
            if key in seen:
                continue
            seen.add(key)
            deduped.append(intent)
        return deduped

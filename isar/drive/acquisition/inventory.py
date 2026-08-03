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
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from isar.schema.steering import CANONICAL_MASTER, EVIDENCE_SINKS


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


@dataclass(frozen=True)
class GeneratorSpec:
    """One registered action generator: the declarative unit an inventory is validated against."""

    generator_id: str
    version: int = 1


# The registered generator set that produced an inventory. An ``action_inventory_exhausted`` terminal
# is exhaustion *relative to this registry* — a later version that adds generators (PR2) does not make
# an old honest terminal a lie, so the proof must always name the version it was computed under.
#
# Today this is deliberately a single **compatibility adapter** around the legacy selector, not the
# complete physical action space: it selects one action by the legacy priority/ranking logic. Naming it
# so keeps the exhaustion proof honest about its scope. PR2 splits it into explicit per-intent
# generators and grows this tuple; the fingerprint below changes with it automatically.
GENERATOR_REGISTRY: tuple[GeneratorSpec, ...] = (
    GeneratorSpec("legacy_selector_adapter", version=1),
    # The identity-class action the legacy selector cannot express. It fires only where counting has
    # demonstrably stopped paying, so it does not compete with the ordinary path for beam.
    GeneratorSpec("narrow_rival_probe", version=1),
)
LEGACY_SELECTOR_GENERATOR = GENERATOR_REGISTRY[0].generator_id
NARROW_RIVAL_PROBE_GENERATOR = GENERATOR_REGISTRY[1].generator_id


# The canonicalization the content addresses are computed under. Versioned in its own right so a
# future change to the encoding is distinguishable from a change to the content it encodes.
CANONICALIZATION_VERSION = "isar.canonical/1"


def _finite(value: float | None) -> float | None:
    """Normalize a non-finite score to ``None`` so it can never reach :func:`canonical_json`.

    The canonicalization is deliberately strict (``allow_nan=False``), which makes a NaN or infinity
    from a future evaluator an exception in the *persistence* path — i.e. a crashed unattended
    campaign. A score that is not a number is not a score, so record it as absent instead.
    """
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def canonical_json(value) -> str:
    """Deterministic JSON for content addressing — strict on purpose.

    ``allow_nan=False`` because NaN/Infinity are not JSON and would make the artifact unreadable by a
    conforming parser, and no ``default=`` coercion so an unserializable value raises instead of
    silently becoming a ``str`` whose text (and therefore the content address) is not stable.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_number(value):
    """Return a strict-JSON representation of one number without losing non-finite provenance.

    :func:`canonical_json` refuses NaN/Infinity outright, which is correct for the artifact but
    would turn a merely *degenerate* input (an instrument that reported an infinite resolution, a
    search band with no bound) into an unstructured ``ValueError`` from deep inside a fingerprint.
    Every numeric field that reaches a canonical payload goes through here instead, so the
    non-finiteness is recorded as a stable token rather than crashing or being silently zeroed.
    Finite values pass through unchanged, so no existing content address moves.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    number = float(value)
    if math.isnan(number):
        return "nan"
    if math.isinf(number):
        return "+inf" if number > 0.0 else "-inf"
    return number


def registry_fingerprint(registry: tuple[GeneratorSpec, ...] = GENERATOR_REGISTRY) -> str:
    """Derive the registry version from the ordered generator ids + versions.

    Deriving rather than hand-maintaining a string means a generator added, removed, renamed, or
    revised cannot silently keep an old terminal's proof looking current.
    """
    payload = canonical_json([[spec.generator_id, spec.version] for spec in registry])
    return "isar.generator-registry/1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


GENERATOR_REGISTRY_VERSION = registry_fingerprint()


def canonical_hkl(hkl) -> tuple[float, float, float]:
    """Canonical scientific Q identity shared by action and EIG policy.

    Fingerprints have always used nine decimal places.  Giving that rule one
    named implementation prevents a nearby-but-not-identical tolerance from
    becoming a second scientific identity rule in a later consumer.
    """
    if not isinstance(hkl, (list, tuple)) or len(hkl) != 3:
        raise ValueError("HKL must contain exactly three values")
    values = tuple(float(value) for value in hkl)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("HKL values must be finite")
    return tuple(round(value, 9) for value in values)


def scientific_fingerprint(intent: "MeasurementIntent") -> str:
    """Stable hash of the *scientific question* an intent resolves — its dedup / anti-repeat id.

    Deliberately excludes anything about the exact emitted request (window step, ncount, feasibility):
    two intents that ask the same scientific question with different executable realizations share one
    fingerprint and must not both occupy the inventory.
    """
    payload = canonical_json([
        intent.action_class,
        list(canonical_hkl(intent.hkl)),
        None if intent.energy_lo is None else round(float(intent.energy_lo), 6),
        None if intent.energy_hi is None else round(float(intent.energy_hi), 6),
        intent.window_family,
        intent.target_branch_role,
        intent.rival_branch_role,
        intent.evidence_basis_fingerprint,
        intent.hypotheses_fingerprint,
        intent.campaign_slice_fingerprint,
        intent.evidence_sink,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CampaignSliceKey:
    """Which ``(sample, temperature bin)`` slice of belief evidence an action belongs to.

    Belief models are stored per slice, so an action formed against one slice is not the same
    scientific action as the identical question asked of another — it is answered by different
    evidence and updates a different posterior. Carrying the key on the intent makes that difference
    part of the action's identity rather than an invisible ambient choice.

    ``binning_policy_fingerprint`` is part of the identity because the rule that maps a measured
    temperature into a bin decides which slice evidence lands in; a rounding change could silently
    reroute it while every other field stayed put.

    ``temperature_bin`` is ``None`` for the cold-start slice, where no measured belief evidence exists
    yet and the common prior is used — an ordinary opening state, not an unresolved one.
    """

    sample: str
    temperature_bin: float | None = None
    binning_policy_fingerprint: str | None = None

    @property
    def fingerprint(self) -> str:
        payload = canonical_json([
            str(self.sample),
            None if self.temperature_bin is None else round(float(self.temperature_bin), 6),
            self.binning_policy_fingerprint,
        ])
        return "isar.campaign-slice/1:" + hashlib.sha256(
            payload.encode("utf-8")).hexdigest()[:16]


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
    # Where this action's evidence may go (isar.schema.steering.EVIDENCE_SINKS). Part of the scientific
    # identity, not a delivery detail: the same window at the same q routed to a different sink is
    # answered against different evidence and updates a different posterior, so the two are genuinely
    # different actions and must not dedupe onto one another.
    evidence_sink: str = CANONICAL_MASTER
    # The energy this action is *about*, when that is not the midpoint of its window. A rival probe's
    # bounds are clamped into the identity domain, so a domain that clips one side moves the midpoint
    # off the predicted rival; realization must resolve the instrument resolution and persist the
    # acquisition target at the prediction, not at the midpoint of what survived the clamp.
    #
    # Deliberately EXCLUDED from ``scientific_fingerprint``: ``energy_lo``/``energy_hi`` already carry
    # the scientific window, and folding the centre in would re-mint every fingerprint ever computed,
    # invalidating the frozen replay artifacts for no gain in what the identity distinguishes.
    window_center_meV: float | None = None

    def __post_init__(self) -> None:
        if self.evidence_sink not in EVIDENCE_SINKS:
            raise ValueError(f"unknown evidence_sink {self.evidence_sink!r}; "
                             f"expected one of {EVIDENCE_SINKS}")

    @property
    def scientific_action_fingerprint(self) -> str:
        return scientific_fingerprint(self)


def scientific_fingerprint_from_mapping(payload: Mapping[str, object]) -> str:
    """Reconstruct and authenticate a persisted :class:`MeasurementIntent`.

    Inventory consumers must never hash a convenient subset of an intent map.
    Rebuilding the value type keeps the persisted action identity subject to the
    same Q, finite-number, and evidence-sink rules as a live proposal.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("measurement intent must be a mapping")

    def required_text(name: str) -> str:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"measurement intent {name!r} must be a non-empty string")
        return value

    def optional_text(name: str) -> str | None:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"measurement intent {name!r} must be a string or null")
        return value

    def optional_number(name: str) -> float | None:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"measurement intent {name!r} must be a finite number or null")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"measurement intent {name!r} must be a finite number or null") from error
        if not math.isfinite(number):
            raise ValueError(f"measurement intent {name!r} must be a finite number or null")
        return number

    intent = MeasurementIntent(
        action_class=required_text("action_class"),
        hkl=canonical_hkl(payload.get("hkl")),
        energy_lo=optional_number("energy_lo"),
        energy_hi=optional_number("energy_hi"),
        window_family=required_text("window_family"),
        target_branch_role=optional_text("target_branch_role"),
        rival_branch_role=optional_text("rival_branch_role"),
        evidence_basis_fingerprint=optional_text("evidence_basis_fingerprint"),
        hypotheses_fingerprint=optional_text("hypotheses_fingerprint"),
        campaign_slice_fingerprint=optional_text("campaign_slice_fingerprint"),
        evidence_sink=required_text("evidence_sink"),
        window_center_meV=(optional_number("window_center_meV")
                           if "window_center_meV" in payload else None),
    )
    return scientific_fingerprint(intent)


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
    # Why an *eligible* action was produced — the counterpart of ``rejection_reason``, so the full
    # artifact records the controller's stated justification, not only its refusals.
    selection_reason: str | None = None
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
    # Actions this generator produced that collapsed onto an earlier action's identical scientific
    # question — whether that earlier action came from a peer generator or from this one asking the
    # same thing twice. ``generated`` counts only the survivors, so it keeps matching the evaluations
    # (the consistency rule in :meth:`ActionInventory.structural_problems`); without this field the
    # collapse would be invisible and a generator that *did* contribute would look inapplicable.
    deduped: int = 0

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

    def structural_problems(self) -> list[str]:
        """Registry-independent ways an inventory can fail to back its own completeness claim.

        These are the invariants that make ``generated = 0`` trustworthy input to an exhaustion proof.
        Without them an inventory with **no reports at all** counted as complete, and a report could
        claim actions that no evaluation records (or an evaluation could name a generator that never
        reported) while still "proving" the action space empty.
        """
        problems: list[str] = []
        if not self.reports:
            problems.append("no generator reports: an inventory with no accounting proves nothing")
        seen: set[str] = set()
        for report in self.reports:
            if report.generator in seen:
                problems.append(f"duplicate report for generator {report.generator!r}")
            seen.add(report.generator)
        counts: dict[str, int] = {}
        for evaluation in self.evaluations:
            counts[evaluation.generator] = counts.get(evaluation.generator, 0) + 1
            if evaluation.generator not in seen:
                problems.append(
                    f"evaluation from generator {evaluation.generator!r} has no report")
        for report in self.reports:
            evaluated = counts.get(report.generator, 0)
            if int(report.generated) != evaluated:
                problems.append(
                    f"generator {report.generator!r} reported {report.generated} generated but "
                    f"{evaluated} evaluation(s) are recorded")
        return problems

    def validate(self, registry: tuple[GeneratorSpec, ...] = GENERATOR_REGISTRY) -> list[str]:
        """Every way this inventory fails to be a valid proof for ``registry`` — empty list when valid.

        Adds the registry-dependent checks to :meth:`structural_problems`: a registered generator that
        never reported (so its actions are simply unknown) and an unknown generator that is not part of
        the declared set. Both make an exhaustion claim unfounded.
        """
        problems = self.structural_problems()
        reported = [report.generator for report in self.reports]
        required = [spec.generator_id for spec in registry]
        for generator_id in required:
            if reported.count(generator_id) != 1:
                problems.append(
                    f"registered generator {generator_id!r} reported "
                    f"{reported.count(generator_id)} times, expected exactly 1")
        for generator in reported:
            if generator not in required:
                problems.append(f"unknown generator {generator!r} is not in the declared registry")
        return problems

    @property
    def is_complete(self) -> bool:
        """True only when every applicable generator ran to completion **and** the accounting adds up.

        Registry-independent by design so synthetic/adapter inventories can be reasoned about without a
        matching global registry; :meth:`validate` adds the registry-membership checks.
        """
        return (all(report.completed_cleanly for report in self.reports)
                and not self.structural_problems())

    def eligible_actions(self) -> list[ActionEvaluation]:
        return [e for e in self.evaluations if e.is_eligible]

    @property
    def has_eligible_action(self) -> bool:
        return any(e.is_eligible for e in self.evaluations)

    def terminal_cause(self, registry: tuple[GeneratorSpec, ...] | None = None) -> str | None:
        """The action-space terminal this inventory implies, or None if it is not terminal.

        Only speaks to the action-space: hard terminals (met, budget, analysis guardrail, operator
        stop, physical infeasibility) are decided before the inventory is consulted.

        ``is_complete`` is registry-independent so synthetic/adapter inventories can be reasoned about
        in isolation. Pass ``registry`` to also require registry consistency before proving exhaustion:
        an inventory whose own accounting is clean but which is missing a registry-required generator
        (one that never ran) has not proven the action space empty, and claiming exhaustion there would
        hide a controller fault behind a valid-looking terminal.
        """
        if not self.is_complete:
            return TERMINAL_CONTROLLER_FAULT
        if registry is not None and self.validate(registry):
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

    def summary(self, *, selected_action_class: str | None = None,
                registry: tuple[GeneratorSpec, ...] | None = None) -> dict:
        """A compact, per-iteration record of the action space — small enough to log every iteration.

        Carries the completeness proof (``is_complete`` + the per-generator accounting) and the
        registry version, so a ``terminal_cause`` read off this summary is interpretable long after the
        run. The full ranked evaluations live in the referenced artifact (:meth:`to_full_dict`), not here.

        ``registry`` must be the same one the live terminal was decided against. Omitting it there would
        let the persisted audit record ``action_inventory_exhausted`` for the very iteration the
        controller terminated as ``controller_fault``, and a proof that contradicts the decision it
        documents is worse than no proof. It stays optional, and registry-blind by default, for the
        synthetic/adapter callers :meth:`terminal_cause` is registry-independent for.
        """
        return {
            "inventory_version": self.inventory_version,
            "generator_registry_version": GENERATOR_REGISTRY_VERSION,
            "n_evaluations": len(self.evaluations),
            "n_eligible": len(self.eligible_actions()),
            "is_complete": self.is_complete,
            "has_eligible_action": self.has_eligible_action,
            "terminal_cause": self.terminal_cause(registry),
            "selected_action_class": selected_action_class,
            "generators": [
                {"generator": r.generator, "status": r.status.value, "generated": r.generated,
                 "deduped": r.deduped, "reason_code": r.reason_code, "error_code": r.error_code}
                for r in self.reports],
        }

    def to_full_dict(self) -> dict:
        """The full ranked inventory for the referenced hashed artifact — every evaluation and score.

        Deterministic given the same enumerated action space, so its content hash is stable and a
        resume that recomputes the inventory from the same authoritative evidence addresses the same
        artifact rather than duplicating it.
        """
        return {
            "inventory_version": self.inventory_version,
            "generator_registry_version": GENERATOR_REGISTRY_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "reports": [
                {"generator": r.generator, "status": r.status.value, "generated": r.generated,
                 "deduped": r.deduped, "reason_code": r.reason_code, "error_code": r.error_code}
                for r in self.reports],
            "evaluations": [
                {
                    "generator": e.generator,
                    "eligibility": e.eligibility.value,
                    "rejection_class": e.rejection_class.value if e.rejection_class else None,
                    "rejection_reason": e.rejection_reason,
                    "selection_reason": e.selection_reason,
                    "policy_rank": e.policy_rank,
                    "scientific_action_fingerprint": e.intent.scientific_action_fingerprint,
                    "intent": {
                        "action_class": e.intent.action_class,
                        "hkl": [float(x) for x in e.intent.hkl],
                        "energy_lo": e.intent.energy_lo,
                        "energy_hi": e.intent.energy_hi,
                        "window_family": e.intent.window_family,
                        "target_branch_role": e.intent.target_branch_role,
                        "rival_branch_role": e.intent.rival_branch_role,
                        "evidence_basis_fingerprint": e.intent.evidence_basis_fingerprint,
                        "hypotheses_fingerprint": e.intent.hypotheses_fingerprint,
                        "campaign_slice_fingerprint": e.intent.campaign_slice_fingerprint,
                        # Part of the scientific action identity: the same window at the same q routed
                        # to a different sink answers against different evidence and updates a different
                        # posterior, so a reader must be able to tell a canonical action from an
                        # auxiliary-node one in the persisted artifact.
                        "evidence_sink": e.intent.evidence_sink,
                    },
                    "score": {
                        "expected_identity_gain": _finite(e.expected_identity_gain),
                        "expected_precision_gain": _finite(e.expected_precision_gain),
                        "expected_coverage_gain": _finite(e.expected_coverage_gain),
                        "expected_measurement_cost": _finite(e.expected_measurement_cost),
                        "expected_analysis_cost": _finite(e.expected_analysis_cost),
                        "score_method": e.score_method,
                        "score_confidence": _finite(e.score_confidence),
                    },
                }
                for e in self.evaluations],
        }

    def content_hash(self) -> str:
        """SHA-256 of the canonical full inventory — the content address of its persisted artifact."""
        return hashlib.sha256(canonical_json(self.to_full_dict()).encode("utf-8")).hexdigest()

"""The exhaustion producer (PR 4.2b C6 / B5): builds ``QuestionExhaustion`` proofs from C5's
``RunResult`` — never claims exhaustion an evaluator would refute, never raises on a run fault.

One rule per matrix row (plan file, C6 section), evaluated in priority order per question:

1. A required generator FAILED or is missing from the run record ⇒ ``controller_fault`` — the
   run itself is untrustworthy, checked BEFORE anything else (an eligible action from a sibling
   generator does not un-break a run whose accounting is incomplete).
2. Any ELIGIBLE surviving evaluation associated to the question (directly, or deduped ONTO an
   eligible survivor that serves it — ``union_associations`` already folded that association onto
   the survivor, so one scan over post-dedup associations covers both) ⇒ ``not_exhausted``.
3. ``dependency_blocked`` disposition ⇒ ``deferred``.
4. Rejected-only survivors, TRANSIENT class present ⇒ ``retryable``.
5. Rejected-only survivors, BUDGET class present (no TRANSIENT) ⇒ ``budget_only``.
6. Rejected-only survivors, every class STRUCTURAL/SATURATION ⇒ ``proof`` only when a matching
   :class:`~isar.schema.generators.QuestionBlockerEvidence` exists (same question ref, fresh
   comparands, a supporting generator among the question's required set); otherwise
   ``blocked_without_evidence`` — an unsupported proof is never emitted (the generator-defect
   lane is ``apply_exhaustion``'s job, downstream of a genuine proof, not this producer's).
7. No survivors at all: every required generator's outcome is ``clean_zero`` with
   ``exhaustion_clean`` set ⇒ ``proof``, generator status ``"exhausted"`` for every required id.
8. Anything else (an INAPPLICABLE outcome — never ``exhaustion_clean`` by
   :class:`~isar.drive.steering.generators.GeneratorOutcome`'s own construction, or a clean zero
   without the flag) ⇒ ``not_exhausted``.
9. ``no_generators`` disposition ⇒ ``no_generators`` — an empty requirement set can never exhaust
   (mirrors ``QuestionExhaustion.exhausted()``'s ``False`` on an empty required set).

Every "no proof" outcome above is exactly that — this module never raises on a run fault or an
unsupported blocker; it hands the classification back so C7 can route it (a ``controller_fault``
outcome stops nothing here).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from isar.drive.acquisition.inventory import ActionEligibility, ActionInventory, RejectionClass
from isar.drive.steering.generators import DiscoveryContext
from isar.drive.steering.readiness import DEPENDENCY_BLOCKED, NO_GENERATORS, RAN, RunResult
from isar.schema.generators import QuestionBlockerEvidence
from isar.scene.questions import QuestionExhaustion

__all__ = [
    "PROOF", "NOT_EXHAUSTED", "DEFERRED", "RETRYABLE", "BUDGET_ONLY",
    "BLOCKED_WITHOUT_EVIDENCE", "CONTROLLER_FAULT", "NO_GENERATORS_OUTCOME",
    "QuestionExhaustionOutcome", "ExhaustionProducerResult", "produce_exhaustion",
]

# Producer outcome tags.
PROOF = "proof"
NOT_EXHAUSTED = "not_exhausted"
DEFERRED = "deferred"
RETRYABLE = "retryable"
BUDGET_ONLY = "budget_only"
BLOCKED_WITHOUT_EVIDENCE = "blocked_without_evidence"
CONTROLLER_FAULT = "controller_fault"
NO_GENERATORS_OUTCOME = "no_generators"

# Mirrors isar.drive.steering.readiness.GeneratorRunOutcome.kind's serialized vocabulary.
_KIND_FAILED = "failed"
_KIND_CLEAN_ZERO = "clean_zero"
_KIND_PROPOSALS = "proposals"

_STRUCTURAL_SATURATION = {RejectionClass.STRUCTURAL, RejectionClass.SATURATION}


@dataclass(frozen=True)
class QuestionExhaustionOutcome:
    """One question's exhaustion classification -- ``proof`` is the only tag that carries one."""

    question_id: str
    outcome: str
    proof: QuestionExhaustion | None = None
    detail: str = ""


@dataclass(frozen=True)
class ExhaustionProducerResult:
    outcomes: tuple[QuestionExhaustionOutcome, ...]

    @property
    def proofs(self) -> tuple[QuestionExhaustion, ...]:
        return tuple(o.proof for o in self.outcomes if o.proof is not None)


def _matching_evidence(evidence_for_question, context: DiscoveryContext, required: tuple,
                       registry_fingerprint: str) -> QuestionBlockerEvidence | None:
    required_set = set(required)
    for evidence in evidence_for_question:
        if evidence.basis_agenda_digest != context.basis_agenda_digest:
            continue
        if evidence.scene_digest != context.scene_digest:
            continue
        if evidence.generator_registry_fingerprint != registry_fingerprint:
            continue
        if not set(evidence.supporting_generator_ids) & required_set:
            continue
        return evidence
    return None


def _proof_for(context: DiscoveryContext, question_id: str, required: tuple[str, ...],
              registry_fingerprint: str) -> QuestionExhaustion:
    return QuestionExhaustion(
        question_id=question_id, basis_agenda_digest=context.basis_agenda_digest,
        scene_digest=context.scene_digest, generator_registry_fingerprint=registry_fingerprint,
        required_generator_ids=required,
        generator_status=tuple((g, "exhausted") for g in required))


def _decide_one(context: DiscoveryContext, record, survivors: list,
                evidence_for_question: list, registry_fingerprint: str
                ) -> QuestionExhaustionOutcome:
    question_id = record.question_id

    if record.disposition == DEPENDENCY_BLOCKED:
        return QuestionExhaustionOutcome(question_id, DEFERRED)
    if record.disposition == NO_GENERATORS:
        return QuestionExhaustionOutcome(question_id, NO_GENERATORS_OUTCOME)
    if record.disposition != RAN:
        raise ValueError(f"unknown QuestionRunRecord disposition {record.disposition!r} for "
                         f"question {question_id}")

    required = record.generator_ids
    outcomes_by_generator = {o.generator: o for o in record.generator_outcomes}
    broken = [g for g in required
             if g not in outcomes_by_generator or outcomes_by_generator[g].kind == _KIND_FAILED]
    if broken:
        return QuestionExhaustionOutcome(
            question_id, CONTROLLER_FAULT,
            detail=f"required generator(s) failed or missing from the run record: {broken}")

    eligible = [e for e in survivors if e.eligibility == ActionEligibility.ELIGIBLE]
    if eligible:
        return QuestionExhaustionOutcome(question_id, NOT_EXHAUSTED)

    rejected = [e for e in survivors if e.eligibility == ActionEligibility.REJECTED]
    if rejected:
        classes = {e.rejection_class for e in rejected}
        if RejectionClass.TRANSIENT in classes:
            return QuestionExhaustionOutcome(question_id, RETRYABLE)
        if RejectionClass.BUDGET in classes:
            return QuestionExhaustionOutcome(question_id, BUDGET_ONLY)
        if classes and classes <= _STRUCTURAL_SATURATION:
            # Every action being refused only exhausts the QUESTION when the actions were the
            # question's admissible space rather than a sample of it (review finding 2026-08-18).
            # Most generators offer one representative pick, so without this the rule read "the one
            # heuristic action was refused" as "nothing can answer this" and closed a region that a
            # point a few hundredths of an rlu away would have surveyed. Only generators that
            # actually PROPOSED gate here; one that returned a clean zero is governed by
            # ``exhaustion_clean`` on the branch below, which is the same declaration for the other
            # outcome shape.
            sampled = sorted(g for g in required
                             if outcomes_by_generator[g].kind == _KIND_PROPOSALS
                             and not outcomes_by_generator[g].proposals_exhaustive)
            if sampled:
                return QuestionExhaustionOutcome(
                    question_id, NOT_EXHAUSTED,
                    detail=f"generator(s) {sampled} sampled the admissible action space rather "
                           "than enumerating it, so every action they offered being refused does "
                           "not prove this question unanswerable; it stays open and blocked")
            match = _matching_evidence(evidence_for_question, context, required,
                                       registry_fingerprint)
            if match is None:
                return QuestionExhaustionOutcome(question_id, BLOCKED_WITHOUT_EVIDENCE)
            return QuestionExhaustionOutcome(
                question_id, PROOF,
                proof=_proof_for(context, question_id, required, registry_fingerprint))
        # OPERATOR (or a mix no rule enumerates): no rule covers it -- never claim proof.
        return QuestionExhaustionOutcome(
            question_id, NOT_EXHAUSTED,
            detail=f"unhandled rejection class(es): {sorted(c.value for c in classes)}")

    all_clean = bool(required) and all(
        outcomes_by_generator[g].kind == _KIND_CLEAN_ZERO
        and outcomes_by_generator[g].exhaustion_clean for g in required)
    if all_clean:
        return QuestionExhaustionOutcome(
            question_id, PROOF,
            proof=_proof_for(context, question_id, required, registry_fingerprint))
    return QuestionExhaustionOutcome(question_id, NOT_EXHAUSTED)


def produce_exhaustion(context: DiscoveryContext, run_result: RunResult, *,
                       generator_registry_fingerprint: str,
                       blocker_evidence: tuple[QuestionBlockerEvidence, ...] = ()
                       ) -> ExhaustionProducerResult:
    """Classify every question in ``run_result`` and build a proof wherever the matrix allows
    one. ``generator_registry_fingerprint`` is the DISCOVERY registry's fingerprint (the caller's
    to name explicitly -- no implicit default, matching ``registry_fingerprint``'s own posture in
    ``isar.schema.generators``: an implicit fingerprint subject is how two registries end up
    sharing one version by accident).

    Deterministic: questions are processed in ``question_id`` order.
    """
    survivors, _deduped = ActionInventory.dedupe_discovery_evaluations(list(run_result.evaluations))

    by_question: dict[str, list] = {}
    for evaluation in survivors:
        for association in evaluation.associations:
            by_question.setdefault(association.question_id, []).append(evaluation)

    evidence_by_question: dict[str, list] = {}
    for evidence in blocker_evidence:
        evidence_by_question.setdefault(evidence.question_id, []).append(evidence)

    outcomes = tuple(
        _decide_one(context, record, by_question.get(record.question_id, []),
                   evidence_by_question.get(record.question_id, []),
                   generator_registry_fingerprint)
        for record in sorted(run_result.records, key=lambda r: r.question_id))
    return ExhaustionProducerResult(outcomes)

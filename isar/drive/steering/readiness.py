"""Readiness gating and per-question run accounting (PR 4.2b C5 / B4).

Two steps, kept separate because they answer different questions: :func:`classify_readiness` is
a pure read of the agenda payload (no generator ever runs) that decides WHICH open questions may
even be attempted; :func:`run_ready_questions` is the only place a generator function is called,
with per-generator fault isolation (mirrors ``acquire.py:1741-1769``: a raising generator becomes
a ``failed`` entry, its peers and every sibling question keep running — one bad generator must
never take an unattended campaign down).

``QuestionRunRecord`` is the accounting C6's exhaustion producer consumes and C7 persists: no
proof of exhaustion is possible without knowing, for the required generator set, which ran
cleanly, which declined, and which failed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from isar.drive.acquisition.inventory import ActionEligibility, ActionEvaluation
from isar.drive.steering.generators import GENERATOR_FUNCTIONS, DiscoveryContext, dependency_depth

__all__ = [
    "READY", "DEPENDENCY_BLOCKED", "RAN", "NO_GENERATORS",
    "GeneratorRunOutcome", "QuestionRunRecord", "RunResult",
    "classify_readiness", "run_ready_questions",
]

_LOG = logging.getLogger(__name__)

# Readiness partition (classify_readiness).
READY = "ready"
DEPENDENCY_BLOCKED = "dependency_blocked"

# Final per-question disposition (run_ready_questions) -- DEPENDENCY_BLOCKED is shared with the
# partition above (a blocked question's disposition never changes); RAN / NO_GENERATORS only
# arise once a ready question is actually processed.
RAN = "ran"
NO_GENERATORS = "no_generators"

# Per-generator outcome kinds.
_OUTCOME_PROPOSALS = "proposals"
_OUTCOME_CLEAN_ZERO = "clean_zero"
_OUTCOME_INAPPLICABLE = "inapplicable"
_OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class GeneratorRunOutcome:
    """What one generator did for one question, on one run."""

    generator: str
    kind: str                          # proposals | clean_zero | inapplicable | failed
    generated: int = 0
    clean_zero_reason: str | None = None
    exhaustion_clean: bool = False
    inapplicable_reason: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {"generator": self.generator, "kind": self.kind, "generated": self.generated,
                "clean_zero_reason": self.clean_zero_reason,
                "exhaustion_clean": self.exhaustion_clean,
                "inapplicable_reason": self.inapplicable_reason, "error_code": self.error_code}


@dataclass(frozen=True)
class QuestionRunRecord:
    """One question's run accounting -- the proof C6 checks before claiming exhaustion."""

    question_id: str
    family: str
    generator_ids: tuple[str, ...]
    disposition: str                   # ran | dependency_blocked | no_generators
    generator_outcomes: tuple[GeneratorRunOutcome, ...] = ()

    def to_dict(self) -> dict:
        return {"question_id": self.question_id, "family": self.family,
                "generator_ids": list(self.generator_ids), "disposition": self.disposition,
                "generator_outcomes": [o.to_dict() for o in self.generator_outcomes]}


@dataclass(frozen=True)
class RunResult:
    """Every question's run record, plus the flat evaluation list C7 dedups directly through
    ``ActionInventory.dedupe_discovery_evaluations`` — one evaluation per surviving proposal,
    already carrying its association."""

    records: tuple[QuestionRunRecord, ...]
    evaluations: tuple[ActionEvaluation, ...]


def classify_readiness(agenda_payload: Mapping) -> dict[str, str]:
    """``question_id -> READY | DEPENDENCY_BLOCKED`` for every OPEN spec in the agenda payload.

    A dependency counts as satisfied only when its OWN assessment in this same payload is
    present and not ``open`` — a dependency the payload never assessed (absent from
    ``assessments``) blocks exactly like an open one: no proof is possible over an unknown
    state. Closed questions (answered / unresolvable) are out of scope entirely, not included in
    the result — nothing is ever run for them.
    """
    assessments = {a["question_id"]: a for a in agenda_payload.get("assessments") or ()}
    readiness: dict[str, str] = {}
    for spec in agenda_payload.get("specs") or ():
        question_id = spec["question_id"]
        assessment = assessments.get(question_id)
        if assessment is None or assessment["status"] != "open":
            continue
        blocked = any(assessments.get(dep) is None or assessments[dep]["status"] == "open"
                     for dep in spec.get("dependencies") or ())
        readiness[question_id] = DEPENDENCY_BLOCKED if blocked else READY
    return readiness


def _validate_association(association, spec: Mapping, generator_id: str,
                          context: DiscoveryContext, question_id: str) -> None:
    """Every returned association must describe the SAME question the controller invoked the
    generator for (PR #33 A-P1-a) -- these fields drive scheduling and exhaustion, so a generator
    that fabricates one (or copies a sibling's) must never pass silently. Raised OUTSIDE the
    per-generator fault-isolation try/except below: a bad generator is that generator's problem,
    but a forged association is a controller-integrity problem for the whole decision."""
    checks = (
        ("question_id", association.question_id, question_id),
        ("generator", association.generator, generator_id),
        ("basis_agenda_digest", association.basis_agenda_digest, context.basis_agenda_digest),
        ("family_priority", association.family_priority, int(spec["priority"])),
        ("dependency_depth", association.dependency_depth,
         dependency_depth(spec, context.agenda_payload)),
    )
    for field, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f"generator {generator_id!r} on question {question_id!r} returned an "
                f"association with {field}={actual!r}, expected {expected!r} -- association "
                "fields must match the controller-owned spec, never the generator's own guess")


def run_ready_questions(context: DiscoveryContext, readiness: Mapping[str, str]) -> RunResult:
    """Run every READY question's declared generators, in spec order, with per-generator fault
    isolation. ``dependency_blocked`` questions are recorded but never touch a generator
    function; a ready question with no declared generators (unreachable for the shipped set —
    plan resolution (f)) is recorded ``no_generators`` and likewise never touches one.

    Deterministic: questions are processed in ``question_id`` order, generators within a
    question in ``spec.generator_ids`` order (itself sorted at spec construction).
    """
    specs_by_id = {s["question_id"]: s for s in context.agenda_payload.get("specs") or ()}
    records: list[QuestionRunRecord] = []
    evaluations: list[ActionEvaluation] = []

    for question_id in sorted(readiness):
        state = readiness[question_id]
        if state not in (READY, DEPENDENCY_BLOCKED):
            # A readiness value this module did not mint means the caller mixed payloads;
            # running generators on it would attribute work to a question nobody classified.
            raise ValueError(
                f"unknown readiness state {state!r} for question {question_id}; "
                "run_ready_questions only accepts classify_readiness output")
        spec = specs_by_id.get(question_id)
        if spec is None:
            # Same caller fault, other direction: readiness derived from one agenda payload,
            # specs from another. Refuse loudly -- silently skipping would let an exhaustion
            # proof claim a question that was never even looked at.
            raise ValueError(
                f"readiness names question {question_id} but the agenda payload carries no "
                "such spec; readiness and specs must come from the same derivation")
        generator_ids = tuple(spec.get("generator_ids") or ())
        family = spec["family"]

        if state == DEPENDENCY_BLOCKED:
            records.append(QuestionRunRecord(question_id, family, generator_ids,
                                             DEPENDENCY_BLOCKED))
            continue
        if not generator_ids:
            records.append(QuestionRunRecord(question_id, family, generator_ids, NO_GENERATORS))
            continue

        outcomes: list[GeneratorRunOutcome] = []
        for generator_id in generator_ids:
            generator_fn = GENERATOR_FUNCTIONS.get(generator_id)
            if generator_fn is None:
                # Unreachable for the shipped set (GENERATOR_FUNCTIONS is asserted at import to
                # cover every id FAMILY_GENERATORS declares, and specs are only ever stamped
                # with FAMILY_GENERATORS ids) -- kept for a persisted spec from an older,
                # differently-registered agenda, mirroring acquire.py's NotRegistered branch.
                outcomes.append(GeneratorRunOutcome(
                    generator_id, _OUTCOME_FAILED,
                    error_code="NotRegistered: no callable for a declared generator"))
                _LOG.error("declared generator %s on question %s has no registered callable",
                           generator_id, question_id)
                continue
            try:
                outcome = generator_fn(context, spec)
            except Exception as error:  # noqa: BLE001 - fault isolation, never escape
                outcomes.append(GeneratorRunOutcome(
                    generator_id, _OUTCOME_FAILED,
                    error_code=f"{type(error).__name__}: {error}"[:300]))
                _LOG.exception("discovery generator %s failed on question %s",
                              generator_id, question_id)
                continue
            if outcome.proposals:
                for intent, association in outcome.proposals:
                    _validate_association(association, spec, generator_id, context, question_id)
                    evaluations.append(ActionEvaluation(
                        intent=intent, generator=generator_id,
                        eligibility=ActionEligibility.ELIGIBLE, associations=(association,)))
                outcomes.append(GeneratorRunOutcome(generator_id, _OUTCOME_PROPOSALS,
                                                    generated=len(outcome.proposals)))
            elif outcome.clean_zero_reason is not None:
                outcomes.append(GeneratorRunOutcome(
                    generator_id, _OUTCOME_CLEAN_ZERO,
                    clean_zero_reason=outcome.clean_zero_reason,
                    exhaustion_clean=outcome.exhaustion_clean))
            else:
                outcomes.append(GeneratorRunOutcome(
                    generator_id, _OUTCOME_INAPPLICABLE,
                    inapplicable_reason=outcome.inapplicable_reason))

        records.append(QuestionRunRecord(question_id, family, generator_ids, RAN,
                                         tuple(outcomes)))

    return RunResult(records=tuple(records), evaluations=tuple(evaluations))

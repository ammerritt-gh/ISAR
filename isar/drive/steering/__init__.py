"""The discovery-generator layer (PR 4.2b C4 / B3): turns one READY agenda question spec into
physical measurement proposals. Readiness gating, exhaustion, and scheduling are later slices in
this same package (C5-C7); this module is construction only.
"""
from __future__ import annotations

from isar.drive.steering.generators import (
    DiscoveryContext,
    GENERATOR_FUNCTIONS,
    GeneratorOutcome,
    dependency_depth,
)
from isar.drive.steering.readiness import (
    DEPENDENCY_BLOCKED,
    NO_GENERATORS,
    RAN,
    READY,
    GeneratorRunOutcome,
    QuestionRunRecord,
    RunResult,
    classify_readiness,
    run_ready_questions,
)
from isar.drive.steering.exhaustion import (
    BLOCKED_WITHOUT_EVIDENCE,
    BUDGET_ONLY,
    CONTROLLER_FAULT,
    DEFERRED,
    NO_GENERATORS_OUTCOME,
    NOT_EXHAUSTED,
    PROOF,
    RETRYABLE,
    ExhaustionProducerResult,
    QuestionExhaustionOutcome,
    produce_exhaustion,
)

__all__ = [
    "DiscoveryContext", "GENERATOR_FUNCTIONS", "GeneratorOutcome", "dependency_depth",
    "READY", "DEPENDENCY_BLOCKED", "RAN", "NO_GENERATORS",
    "GeneratorRunOutcome", "QuestionRunRecord", "RunResult",
    "classify_readiness", "run_ready_questions",
    "PROOF", "NOT_EXHAUSTED", "DEFERRED", "RETRYABLE", "BUDGET_ONLY",
    "BLOCKED_WITHOUT_EVIDENCE", "CONTROLLER_FAULT", "NO_GENERATORS_OUTCOME",
    "QuestionExhaustionOutcome", "ExhaustionProducerResult", "produce_exhaustion",
]

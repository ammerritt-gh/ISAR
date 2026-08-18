"""Lexicographic scheduling over eligible discovery evaluations (PR 4.2b C7 / B6).

The rank vector per evaluation is ``(family_priority, dependency_depth, placement_rank,
locality, scientific_action_fingerprint)`` — the first three components come straight from
:func:`~isar.schema.generators.aggregate_rank`'s supplier (the MINIMUM lexicographic association
over the evaluation's, already order-invariant by construction); ``locality`` is a named v1
stand-in (:data:`LOCALITY_VERSION`) for a future motor-cost model — until then it is either 0.0
for every action (no ``locality_coordinate`` supplied, structurally order-preserving) or the
absolute distance along the step's varying reciprocal axis; the fingerprint is the final,
always-distinguishing tiebreak.

:data:`SCHEDULER_POLICY` is the declaration embedded in every persisted decision — it names every
rule above so a reader can audit what ranked without re-deriving it from source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from isar.drive.acquisition.inventory import ActionEvaluation
from isar.schema.generators import QuestionActionAssociation, aggregate_rank

__all__ = ["SCHEDULER_POLICY", "RankedEvaluation", "rank_all", "select"]

LOCALITY_VERSION = "isar.scheduler.locality.axis-distance/1"

# ``placement_rank`` (review finding, 2026-08-18) is what keeps the generator's own physics
# ordering ahead of the fingerprint tiebreak; see QuestionActionAssociation for the failure that
# added it. It does NOT price anything — the gap below is unchanged.
# WHAT THIS POLICY DOES NOT PRICE (DECISIONS H21(a), deferred to SCENE-5.5). ``rank_fields`` is
# the WHOLE ranking, and it carries no cost term: the exposure a chosen action will spend is
# ``discovery_monitor_ncount`` PER POINT times the point count its window and snapped step imply
# (``isar.drive.steering.realization``, charged in ``isar.drive.loop``), so two candidates at the
# same family priority and dependency depth can differ in cost by more than an order of magnitude
# and be separated here by the fingerprint tiebreak alone. That is the known gap against O43's
# symmetric per-neutron comparison; worthwhileness pricing is SCENE-5's, and no field is added
# here in the meantime because this dict is embedded verbatim in every persisted decision and a
# new key would move every decision's bytes for a value nothing yet computes. Until SCENE-5.5 the
# cost bound is the declared budget (which trips after the spend) plus
# ``MAX_DISCOVERY_DELIVERIES_PER_STEP``, never this ranking.
SCHEDULER_POLICY = {
    "id": "isar.scheduler.lexicographic/1",
    "rank_fields": ["family_priority", "dependency_depth", "placement_rank", "locality",
                    "scientific_action_fingerprint"],
    "priority_source": "question-policy family_priorities via associations, min-aggregated",
    "depth_algorithm": "aggregate_rank's minimum-lexicographic association supplier "
                       "(isar.schema.generators.aggregate_rank)",
    "locality_fn": LOCALITY_VERSION,
    "tiebreak": "scientific_action_fingerprint",
    "aggregation": "min lexicographic over associations",
}


def _dominant_axis(step_payload: Mapping) -> int:
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    return deltas.index(max(deltas))


def _locality(step_payload: Mapping, hkl, locality_coordinate: float | None) -> float:
    """0.0 for every action when no coordinate is supplied — structurally order-preserving, so
    an absent locality input can never perturb the rank a caller without one would see."""
    if locality_coordinate is None:
        return 0.0
    axis = _dominant_axis(step_payload)
    return abs(float(hkl[axis]) - float(locality_coordinate))


@dataclass(frozen=True)
class RankedEvaluation:
    evaluation: ActionEvaluation
    rank_vector: tuple
    supplying_association: QuestionActionAssociation


def rank_all(evaluations, step_payload: Mapping,
            locality_coordinate: float | None = None) -> tuple[RankedEvaluation, ...]:
    """Every evaluation, ranked and sorted by its full rank vector. Deterministic and
    order-invariant: the sort key is the complete vector, ending in the always-unique
    scientific action fingerprint, so no two evaluations can tie."""
    ranked = []
    for evaluation in evaluations:
        rank_key, supplier = aggregate_rank(evaluation.associations)
        family_priority, dependency_depth, placement_rank = rank_key[0], rank_key[1], rank_key[2]
        locality = _locality(step_payload, evaluation.intent.hkl, locality_coordinate)
        vector = (family_priority, dependency_depth, placement_rank, locality,
                  evaluation.intent.scientific_action_fingerprint)
        ranked.append(RankedEvaluation(evaluation=evaluation, rank_vector=vector,
                                       supplying_association=supplier))
    return tuple(sorted(ranked, key=lambda r: r.rank_vector))


def select(evaluations, step_payload: Mapping, locality_coordinate: float | None = None
          ) -> tuple[ActionEvaluation, tuple, QuestionActionAssociation,
                     tuple[RankedEvaluation, ...]]:
    """``(winner, rank_vector, winning_association, ordered_rank_table)`` — the winner is the
    lexicographic minimum; the table is the full ranking, for persistence/audit."""
    if not evaluations:
        raise ValueError("select needs at least one eligible evaluation")
    table = rank_all(evaluations, step_payload, locality_coordinate)
    winner = table[0]
    return winner.evaluation, winner.rank_vector, winner.supplying_association, table

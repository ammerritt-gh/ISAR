"""Question–action associations (PR 4.2b C3 / B2): many-to-many provenance beside the
intent, union-on-dedup, minimum-lexicographic rank aggregation, refusal on conflicting
realization metadata. Order-invariance is the load-bearing property — exhaustion honesty
rests on attribution that cannot depend on which generator happened to run first."""
from __future__ import annotations

import pytest

from isar.drive.acquisition.inventory import (
    ActionEligibility,
    ActionEvaluation,
    ActionInventory,
    GeneratorReport,
    GeneratorStatus,
    MeasurementIntent,
    QuestionActionAssociation,
    aggregate_rank,
    union_associations,
)

BASIS = "d" * 64


def assoc(qid, generator, priority, depth=0, reason="serves the predicate"):
    return QuestionActionAssociation(
        basis_agenda_digest=BASIS, question_id=qid, generator=generator,
        family_priority=priority, dependency_depth=depth, reason=reason)


def intent(center=None, **overrides):
    fields = dict(action_class="presence_probe", hkl=(2.0, 0.0, 0.0), energy_lo=1.0,
                  energy_hi=8.0, window_family="discovery", evidence_basis_fingerprint="s" * 12,
                  window_center_meV=center)
    fields.update(overrides)
    return MeasurementIntent(**fields)


def evaluation(generator, associations=(), center=None, **intent_overrides):
    return ActionEvaluation(intent=intent(center, **intent_overrides), generator=generator,
                            eligibility=ActionEligibility.ELIGIBLE,
                            associations=tuple(associations))


# ----------------------------------------------------------------------------
# DTO
# ----------------------------------------------------------------------------
def test_association_validates_and_round_trips():
    a = assoc("q:1", "presence_probe", 1, depth=2)
    assert QuestionActionAssociation.from_dict(a.to_dict()) == a
    for bad in [dict(question_id=""), dict(generator="  "), dict(family_priority=-1),
                dict(dependency_depth=True), dict(reason="")]:
        fields = a.to_dict() | bad
        with pytest.raises(ValueError):
            QuestionActionAssociation(**fields)


def test_union_dedupes_and_orders_canonically():
    a1 = assoc("q:b", "g1", 1)
    a2 = assoc("q:a", "g2", 1)
    a3 = assoc("q:c", "g1", 0)
    assert union_associations([a1], [a2, a3], [a1]) == (a3, a2, a1)   # rank, then identity
    assert union_associations([a3, a2, a1]) == union_associations([a1], [a2], [a3])


def test_aggregate_rank_is_minimum_and_names_its_supplier():
    low = assoc("q:low", "g2", 0, depth=3)
    high = assoc("q:high", "g1", 4, depth=0)
    rank, supplier = aggregate_rank((high, low))
    assert supplier == low and rank == low.rank_key
    assert aggregate_rank((low, high)) == (rank, supplier)            # order-invariant
    with pytest.raises(ValueError):
        aggregate_rank(())


# ----------------------------------------------------------------------------
# Dedup: union, order-invariance, refusal
# ----------------------------------------------------------------------------
def test_dedup_unions_associations_order_invariantly():
    """The B2 acceptance: one physical action serving priorities 0 and 4 ranks by 0, and
    reversing generator iteration order changes neither the association set nor the rank."""
    from_survey = evaluation("opening_book_survey", [assoc("q:cov", "opening_book_survey", 0)])
    from_refine = evaluation("parameter_refine", [assoc("q:est", "parameter_refine", 4)])

    forward, dropped_f = ActionInventory.dedupe_discovery_evaluations(
        [from_survey, from_refine])
    reverse, dropped_r = ActionInventory.dedupe_discovery_evaluations(
        [from_refine, from_survey])

    assert len(forward) == len(reverse) == 1
    assert forward[0].associations == reverse[0].associations
    assert {a.question_id for a in forward[0].associations} == {"q:cov", "q:est"}
    rank_f, supplier_f = aggregate_rank(forward[0].associations)
    rank_r, supplier_r = aggregate_rank(reverse[0].associations)
    assert rank_f == rank_r and supplier_f == supplier_r
    assert supplier_f.family_priority == 0
    assert dropped_f == {"parameter_refine": 1} and dropped_r == {"opening_book_survey": 1}


def test_dedup_refuses_conflicting_realization_metadata():
    """window_center_meV is outside the scientific fingerprint by design, so two generators
    can mint one action with contradictory acquisition aims — refuse, never first-seen."""
    a = evaluation("identity_discriminator", [assoc("q:id", "identity_discriminator", 2)],
                   center=5.0)
    b = evaluation("parameter_refine", [assoc("q:est", "parameter_refine", 4)], center=5.5)
    with pytest.raises(ValueError, match="window_center_meV.*controller fault|controller fault"):
        ActionInventory.dedupe_discovery_evaluations([a, b])
    # Agreement (including both-None) unions fine.
    survivors, _ = ActionInventory.dedupe_discovery_evaluations(
        [a, evaluation("parameter_refine", [assoc("q:est", "parameter_refine", 4)], center=5.0)])
    assert len(survivors) == 1 and len(survivors[0].associations) == 2


def test_distinct_scientific_actions_never_collapse():
    a = evaluation("presence_probe", [assoc("q:p1", "presence_probe", 1)])
    b = evaluation("presence_probe", [assoc("q:p2", "presence_probe", 1)],
                   hkl=(3.0, 0.0, 0.0))
    survivors, dropped = ActionInventory.dedupe_discovery_evaluations([a, b])
    assert len(survivors) == 2 and dropped == {}


# ----------------------------------------------------------------------------
# Serialization: legacy bytes untouched, discovery provenance persisted
# ----------------------------------------------------------------------------
def test_association_free_evaluations_serialize_without_the_key():
    """Legacy inventories must keep their exact canonical bytes: the associations key exists
    only where provenance was recorded."""
    bare = ActionInventory(
        evaluations=[evaluation("legacy_selector_adapter")],
        reports=[GeneratorReport("legacy_selector_adapter", GeneratorStatus.COMPLETED,
                                 generated=1)])
    persisted = bare.to_full_dict()["evaluations"][0]
    assert "associations" not in persisted

    carrying = ActionInventory(
        evaluations=[evaluation("presence_probe", [assoc("q:p", "presence_probe", 1)])],
        reports=[GeneratorReport("presence_probe", GeneratorStatus.COMPLETED, generated=1)])
    row = carrying.to_full_dict()["evaluations"][0]
    assert row["associations"] == [assoc("q:p", "presence_probe", 1).to_dict()]
    assert bare.content_hash() != carrying.content_hash()

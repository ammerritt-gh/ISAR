"""Unit contract for the action-inventory value types (PR1, increment 1).

These are pure data structures with no wiring yet: later increments make ``propose_scan`` build an
``ActionInventory`` and the terminal decision consume it. What matters here is the invariant that the
whole repair turns on — a branch-ambiguity terminal is legal *only* when every registered generator
ran and produced no eligible action. A generator that raised or timed out makes the inventory
**incomplete**, and an incomplete inventory can never prove exhaustion; it is a controller fault.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition.inventory import (
    ActionEligibility,
    ActionEvaluation,
    ActionInventory,
    GENERATOR_REGISTRY,
    GENERATOR_REGISTRY_VERSION,
    GeneratorReport,
    GeneratorSpec,
    GeneratorStatus,
    LEGACY_SELECTOR_GENERATOR,
    MeasurementIntent,
    RejectionClass,
    registry_fingerprint,
    scientific_fingerprint,
)


def _intent(action_class="same_q_identity", *, hkl=(2.9, 0.0, 0.0), e_lo=-2.0, e_hi=7.0,
            target_role="acoustic", rival_role="optic", evidence_basis="rev51",
            hypotheses="hyp-a") -> MeasurementIntent:
    return MeasurementIntent(
        action_class=action_class, hkl=hkl, energy_lo=e_lo, energy_hi=e_hi,
        target_branch_role=target_role, rival_branch_role=rival_role,
        window_family="two_branch", evidence_basis_fingerprint=evidence_basis,
        hypotheses_fingerprint=hypotheses, campaign_slice_fingerprint="300K")


def _eligible(intent, generator="g", *, rank=0.0):
    return ActionEvaluation(intent=intent, generator=generator,
                            eligibility=ActionEligibility.ELIGIBLE, policy_rank=rank)


def _rejected(intent, generator="g", cls=RejectionClass.SATURATION):
    return ActionEvaluation(intent=intent, generator=generator,
                            eligibility=ActionEligibility.REJECTED, rejection_class=cls,
                            rejection_reason="saturated")


def _report(name, status=GeneratorStatus.COMPLETED, generated=0, **kw):
    return GeneratorReport(generator=name, status=status, generated=generated, **kw)


# -- fingerprints ----------------------------------------------------------------------------------

def test_scientific_fingerprint_is_deterministic_and_identity_scoped():
    a = _intent()
    b = _intent()
    assert scientific_fingerprint(a) == scientific_fingerprint(b)
    # A different scientific question (energy support) changes the fingerprint...
    assert scientific_fingerprint(a) != scientific_fingerprint(_intent(e_hi=12.0))
    # ...but the fingerprint is the intent's own stable id.
    assert a.scientific_action_fingerprint == scientific_fingerprint(a)


def test_fingerprint_ignores_nothing_material_but_is_stable_across_equal_content():
    assert scientific_fingerprint(_intent(action_class="narrow_rival")) != \
        scientific_fingerprint(_intent(action_class="same_q_identity"))
    assert scientific_fingerprint(_intent(evidence_basis="rev51")) == \
        scientific_fingerprint(_intent(evidence_basis="rev51"))


# -- completeness / terminal cause -----------------------------------------------------------------

def test_complete_with_an_eligible_action_is_not_terminal():
    intent = _intent()
    inv = ActionInventory(evaluations=[_eligible(intent)], reports=[_report("g", generated=1)])
    assert inv.is_complete is True
    assert inv.has_eligible_action is True
    assert inv.terminal_cause() is None


def test_complete_with_no_eligible_action_is_action_inventory_exhausted():
    intent = _intent()
    inv = ActionInventory(evaluations=[_rejected(intent)], reports=[_report("g", generated=1)])
    assert inv.is_complete is True
    assert inv.has_eligible_action is False
    assert inv.terminal_cause() == "action_inventory_exhausted"


def test_a_failed_generator_makes_the_inventory_incomplete_and_a_controller_fault():
    intent = _intent()
    # Structurally consistent apart from the failure, so the FAILED status is the only thing under test.
    inv = ActionInventory(
        evaluations=[_rejected(intent, generator="ok")],
        reports=[_report("ok", generated=1),
                 _report("boundary", status=GeneratorStatus.FAILED, error_code="zone_geometry")])
    assert inv.structural_problems() == []
    assert inv.is_complete is False
    # Exhaustion is illegal while any applicable generator failed to run — that is a controller fault,
    # never a scientific result.
    assert inv.terminal_cause() == "controller_fault"


def test_inapplicable_generator_with_a_reason_still_permits_exhaustion():
    intent = _intent()
    inv = ActionInventory(
        evaluations=[_rejected(intent, generator="ok")],
        reports=[_report("ok", generated=1),
                 GeneratorReport(generator="rival_gap", status=GeneratorStatus.INAPPLICABLE,
                                 reason_code="no_gap_q")])
    assert inv.is_complete is True
    assert inv.terminal_cause() == "action_inventory_exhausted"
    # ``applicable`` is derived from status, so it can never contradict it.
    inapplicable = next(r for r in inv.reports if r.generator == "rival_gap")
    assert inapplicable.applicable is False
    assert _report("ok").applicable is True


# -- dedup / selection surface ---------------------------------------------------------------------

def test_duplicate_scientific_intents_collapse_before_evaluation():
    intent = _intent()
    twin = _intent()  # same logical content -> same scientific fingerprint
    assert intent.scientific_action_fingerprint == twin.scientific_action_fingerprint
    deduped = ActionInventory.dedupe_intents([intent, twin, _intent(action_class="narrow_rival")])
    assert len(deduped) == 2


def test_intents_are_frozen_and_hashable_so_they_can_key_sets_and_dicts():
    a, b = _intent(), _intent()
    assert hash(a) == hash(b)                 # equal content -> equal hash (frozen dataclass)
    assert len({a, b, _intent(action_class="narrow_rival")}) == 2
    with pytest.raises(Exception):
        a.action_class = "mutated"            # frozen: no accidental mutation of a fingerprint source


def test_eligible_actions_exposes_only_eligible_evaluations():
    a, b = _intent(), _intent(action_class="narrow_rival")
    inv = ActionInventory(evaluations=[_eligible(a), _rejected(b)],
                          reports=[_report("g", generated=2)])
    eligible = inv.eligible_actions()
    assert [e.intent.action_class for e in eligible] == ["same_q_identity"]


# -- validated completeness + declarative registry (PR1.7b) ----------------------------------------

def test_an_inventory_with_no_reports_cannot_prove_exhaustion():
    """The vacuous-completeness hole: ``all([])`` is True, so an inventory carrying no generator
    accounting at all used to count as complete and "prove" the action space empty."""
    empty = ActionInventory()
    assert empty.is_complete is False
    assert empty.terminal_cause() == "controller_fault"
    assert "no generator reports" in empty.structural_problems()[0]


def test_duplicate_orphan_and_miscounted_reports_are_controller_faults():
    intent = _intent()
    duplicate = ActionInventory(evaluations=[], reports=[_report("g"), _report("g")])
    assert duplicate.is_complete is False and duplicate.terminal_cause() == "controller_fault"
    assert any("duplicate report" in problem for problem in duplicate.structural_problems())

    # An evaluation whose generator never reported: its provenance is unaccounted for.
    orphan = ActionInventory(evaluations=[_rejected(intent, generator="ghost")],
                             reports=[_report("g")])
    assert orphan.is_complete is False and orphan.terminal_cause() == "controller_fault"
    assert any("has no report" in problem for problem in orphan.structural_problems())

    # A report claiming actions that no evaluation records.
    miscounted = ActionInventory(evaluations=[], reports=[_report("g", generated=3)])
    assert miscounted.is_complete is False and miscounted.terminal_cause() == "controller_fault"
    assert any("generated" in problem for problem in miscounted.structural_problems())


def test_validate_checks_membership_of_the_declared_registry():
    spec_a, spec_b = GeneratorSpec("a"), GeneratorSpec("b")
    registry = (spec_a, spec_b)
    complete = ActionInventory(evaluations=[], reports=[_report("a"), _report("b")])
    assert complete.validate(registry) == []

    missing = ActionInventory(evaluations=[], reports=[_report("a")])
    assert any("'b'" in problem and "expected exactly 1" in problem
               for problem in missing.validate(registry))

    unknown = ActionInventory(evaluations=[], reports=[_report("a"), _report("b"), _report("rogue")])
    assert any("unknown generator" in problem for problem in unknown.validate(registry))


def test_the_live_registry_is_a_single_honest_legacy_adapter_and_its_version_is_derived():
    assert [spec.generator_id for spec in GENERATOR_REGISTRY] == [LEGACY_SELECTOR_GENERATOR]
    assert GENERATOR_REGISTRY_VERSION == registry_fingerprint(GENERATOR_REGISTRY)
    # The fingerprint is derived from the ordered ids+versions, so growing the registry (PR2) or
    # revising a generator cannot leave an old proof looking current.
    assert registry_fingerprint((GeneratorSpec("a"),)) != registry_fingerprint((GeneratorSpec("b"),))
    assert registry_fingerprint((GeneratorSpec("a", 1),)) != registry_fingerprint((GeneratorSpec("a", 2),))


# -- persistence surface (PR1.6): compact summary, full serialization, content address -------------

def test_summary_carries_the_completeness_proof_and_registry_version():
    intent = _intent()
    inv = ActionInventory(evaluations=[_rejected(intent)],
                          reports=[_report("g", generated=1)])
    summary = inv.summary(selected_action_class="same_q_identity")
    assert summary["inventory_version"] == inv.inventory_version
    assert summary["generator_registry_version"] == GENERATOR_REGISTRY_VERSION
    assert summary["n_evaluations"] == 1 and summary["n_eligible"] == 0
    assert summary["is_complete"] is True
    assert summary["has_eligible_action"] is False
    assert summary["terminal_cause"] == "action_inventory_exhausted"
    assert summary["selected_action_class"] == "same_q_identity"
    assert summary["generators"] == [
        {"generator": "g", "status": "completed", "generated": 1,
         "reason_code": None, "error_code": None}]


def test_full_dict_exposes_every_evaluation_and_its_intent():
    a, b = _intent(), _intent(action_class="narrow_rival")
    inv = ActionInventory(evaluations=[_eligible(a), _rejected(b)],
                          reports=[_report("g", generated=2)])
    full = inv.to_full_dict()
    assert full["generator_registry_version"] == GENERATOR_REGISTRY_VERSION
    assert [e["intent"]["action_class"] for e in full["evaluations"]] == [
        "same_q_identity", "narrow_rival"]
    rejected = full["evaluations"][1]
    assert rejected["eligibility"] == "rejected"
    assert rejected["rejection_class"] == "saturation"


def test_content_hash_is_deterministic_and_content_sensitive():
    intent = _intent()
    a = ActionInventory(evaluations=[_rejected(intent)], reports=[_report("g", generated=1)])
    b = ActionInventory(evaluations=[_rejected(_intent())], reports=[_report("g", generated=1)])
    assert a.content_hash() == b.content_hash()          # equal content -> same address (resume re-refs)
    different = ActionInventory(evaluations=[_eligible(_intent())],
                                reports=[_report("g", generated=1)])
    assert different.content_hash() != a.content_hash()   # a different action space -> different address

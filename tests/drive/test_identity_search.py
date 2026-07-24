"""The identity search domain, separate from the target search domain (PR2.5).

One band did two jobs. ``energy_search_meV`` bounds where the *target* mode is expected — for an
acoustic campaign, ``(-2, 7)`` meV — and it was also the hard ceiling on every emitted window. AP6
shows what that costs: at H=2.9 the target sits at 5.754 meV with P(target)=0.706, and the rival that
would settle the identity is at **7.115 meV**, above the ceiling. The window builder widens to cover a
supplied rival band and then clamps it straight back
([acquire.py:332](isar/drive/acquisition/acquire.py#L332)), so the evidence that could break the tie was
never in any scan ISAR was willing to emit, and the campaign stopped ``branch_ambiguity`` with beam
time left.

Splitting the domains is not a licence to scan wherever we like:

* the **target** domain still bounds target delivery and precision work;
* the **identity** domain bounds identity-class actions only, and is finite and instrument-aware;
* an operator who *declared* a narrow range keeps it as a hard constraint — the rival action is then
  reported as policy-forbidden rather than silently scanned outside declared intent;
* a range that merely *equals* the shipped default proves nothing about who chose it, so a legacy
  objective carrying no provenance is not quietly widened.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor.goal_to_spec import spec_from_dict


def _spec(**kw):
    base = dict(h_start=2.0, h_end=3.0, step=0.1, tolerance_meV=0.5,
                budget={"kind": "neutrons", "amount": 1e12}, target_branch="acoustic")
    base.update(kw)
    return ObjectiveSpec.along_h(**base)


# -- the two domains --------------------------------------------------------------------------------

def test_the_target_domain_keeps_its_legacy_name_and_default():
    objective = _spec()
    assert objective.energy_search_meV == (-2.0, 7.0)
    assert objective.target_search_meV == (-2.0, 7.0)      # the new vocabulary, same value


def test_an_undeclared_identity_domain_defaults_to_the_target_domain():
    """Never widen by default. A campaign that said nothing about identity search gets exactly the
    band it declared, and any widening is an explicit, recorded decision."""
    objective = _spec()
    assert objective.identity_search_meV is None            # nothing was declared...
    assert objective.effective_identity_search_meV == objective.target_search_meV   # ...so: target


def test_the_undeclared_state_survives_replace_and_copy():
    """The field must stay ``None`` rather than being normalized to the target band in __post_init__.

    Filling it in looked harmless and was not: ``dataclasses.replace()`` feeds the stored field back
    through the constructor, so a normalized fallback returns as a *declaration*. A legacy objective
    would silently acquire a declared identity domain, and with it the loss of the policy-forbidden
    report that tells an operator identity work was blocked."""
    import copy
    import dataclasses

    objective = _spec()
    assert dataclasses.replace(objective, name="renamed").identity_search_meV is None
    assert copy.deepcopy(objective).identity_search_meV is None
    _domain, forbidden = acq.identity_domain(
        dataclasses.replace(objective, name="renamed"), rival_energy_meV=7.115)
    assert forbidden is not None, "replace() must not silence the policy-forbidden report"


def test_a_declared_domain_equal_to_the_target_band_stays_declared():
    """The other half of the distinction: declaring exactly the target band is a real declaration and
    must not be mistaken for silence."""
    objective = _spec(identity_search_meV=(-2.0, 7.0), identity_search_source="operator")
    assert objective.identity_search_meV == (-2.0, 7.0)
    _domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert forbidden is None, "an explicit declaration answers the question, even when it is narrow"


def test_a_declared_identity_domain_is_kept_and_may_exceed_the_target_domain():
    objective = _spec(identity_search_meV=(-2.0, 9.0))
    assert objective.target_search_meV == (-2.0, 7.0)
    assert objective.identity_search_meV == (-2.0, 9.0)


def test_an_inverted_identity_domain_is_rejected():
    with pytest.raises(ValueError, match="identity_search_meV"):
        _spec(identity_search_meV=(9.0, -2.0))


# -- provenance -------------------------------------------------------------------------------------

def test_the_compiler_records_who_chose_each_band():
    """Equality with the shipped default cannot distinguish "the compiler supplied it" from "the
    scientist asked for exactly that", so the compiler records the answer instead of leaving it to be
    inferred later."""
    supplied = spec_from_dict({
        "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0], "step_rlu": 0.1,
        "tolerance_meV": 0.5, "budget_neutrons": 1e12,
        "target_branch": "acoustic", "energy_search_meV": [-2.0, 7.0]})
    assert supplied.target_search_source == "operator"

    defaulted = spec_from_dict({
        "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0], "step_rlu": 0.1,
        "tolerance_meV": 0.5, "budget_neutrons": 1e12,
        "target_branch": "acoustic"})
    assert defaulted.target_search_source == "compiler"
    # Both carry the identical band: only the provenance separates them.
    assert supplied.energy_search_meV == defaulted.energy_search_meV


def test_provenance_round_trips():
    objective = _spec(identity_search_meV=(-2.0, 9.0), target_search_source="operator",
                      identity_search_source="operator")
    restored = ObjectiveSpec.from_dict(objective.to_dict())
    assert restored.identity_search_meV == (-2.0, 9.0)
    assert restored.target_search_source == "operator"
    assert restored.identity_search_source == "operator"


def test_the_llm_goal_schema_cannot_grant_identity_scanning_authority():
    """``identity_search_meV`` authorises scans *outside* the declared target band, which is
    beam-time-affecting acquisition policy, not scientific intent. The LLM schema deliberately
    excludes that class of field — the supervisor says what to look for, not how much of someone
    else's beam time to spend, nor where it may look beyond what was declared."""
    from isar.drive.supervisor.goal_to_spec import FILE_SPEC_SCHEMA, LLM_GOAL_SCHEMA

    assert "identity_search_meV" not in LLM_GOAL_SCHEMA["properties"]
    assert "identity_search_meV" in FILE_SPEC_SCHEMA["properties"]
    # The schema is closed, so exclusion is enforced rather than merely undocumented.
    assert LLM_GOAL_SCHEMA["additionalProperties"] is False


# -- the migration rule for legacy objectives -------------------------------------------------------

def test_a_compiler_supplied_band_may_be_widened_for_identity_work():
    objective = _spec(target_search_source="compiler")
    domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert forbidden is None
    assert domain[1] >= 7.115, "the compiler's default must not bound the identity question"


def test_an_operator_declared_band_is_a_hard_constraint():
    """The scientist said 7 meV. Quietly scanning to 7.6 to settle an identity question spends their
    beam time outside the intent they declared, which is not ours to reinterpret."""
    objective = _spec(target_search_source="operator")
    domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert domain[1] == pytest.approx(7.0)
    assert forbidden is not None and "operator" in forbidden


def test_a_legacy_objective_without_provenance_is_not_quietly_widened():
    """The trap this rule exists for: an old frozen objective whose band happens to equal the shipped
    default. Equality is not evidence of authorship, so it stays a hard constraint until a human
    says otherwise."""
    objective = _spec()
    assert objective.target_search_source is None
    domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert domain[1] == pytest.approx(7.0)
    assert forbidden is not None and "provenance" in forbidden


def test_a_rival_already_inside_the_declared_band_needs_no_authorisation():
    """Containment is asked before provenance. The migration rules exist to decide whether a band may
    be *widened*; a rival already inside it needs no widening, so no provenance question arises. Asking
    provenance first reported ``policy_forbidden`` for a probe the operator had already authorised —
    disabling identity work inside the very range they declared."""
    for source in ("operator", None):
        objective = _spec(energy_search_meV=(-2.0, 7.0), target_search_source=source)
        domain, forbidden = acq.identity_domain(objective, rival_energy_meV=6.0)
        assert forbidden is None, f"a rival at 6 meV is inside the declared band ({source=})"
        assert domain == pytest.approx((-2.0, 7.0))


def test_containment_is_tested_on_the_rival_centre_not_its_shoulders():
    """A rival *outside* the band still needs widening, and an operator band still forbids it. The
    short-circuit tests the centre alone: admitting a rival whose shoulders fall outside would be
    widening by another name, which is exactly what the operator rule refuses."""
    objective = _spec(energy_search_meV=(-2.0, 7.0), target_search_source="operator")
    _domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert forbidden is not None and "operator" in forbidden


def test_an_explicit_identity_domain_overrides_the_migration_question_entirely():
    objective = _spec(identity_search_meV=(-2.0, 9.0), identity_search_source="operator")
    domain, forbidden = acq.identity_domain(objective, rival_energy_meV=7.115)
    assert forbidden is None and domain == (-2.0, 9.0)


def test_the_identity_domain_stays_finite_and_instrument_aware():
    """A widened domain is bounded by the declared rival support, not opened to an arbitrary band:
    the point is to admit the rival that settles the question, not to scan the whole spectrum."""
    objective = _spec(target_search_source="compiler")
    near, _ = acq.identity_domain(objective, rival_energy_meV=7.115)
    far, _ = acq.identity_domain(objective, rival_energy_meV=20.0)
    assert far[1] > near[1]
    assert near[1] < 9.0, "a 7.115 meV rival must not open a 20 meV window"


def test_no_rival_energy_leaves_the_domain_unwidened():
    objective = _spec(target_search_source="compiler")
    domain, forbidden = acq.identity_domain(objective, rival_energy_meV=None)
    assert domain == objective.target_search_meV and forbidden is None


# -- the window builder honours the identity domain -------------------------------------------------

def test_an_identity_window_can_reach_a_rival_above_the_target_ceiling():
    """The AP6 mechanism, isolated. The rival band widens the window and the search clamp puts it
    straight back; only a separate identity domain lets the emitted scan reach 7.115 meV."""
    target_only = acq._energy_window(
        5.754, 0.8, 0.1, energy_search_meV=(-2.0, 7.0),
        target_band=(5.754, 0.3), rival_band=(7.115, 0.2))
    assert target_only[1] == pytest.approx(7.0), "the target ceiling clips the rival"

    identity = acq._energy_window(
        5.754, 0.8, 0.1, energy_search_meV=(-2.0, 7.6),
        target_band=(5.754, 0.3), rival_band=(7.115, 0.2))
    assert identity[1] > 7.115, "the identity domain must admit the rival it exists to observe"

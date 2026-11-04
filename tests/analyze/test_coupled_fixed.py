"""The fixed mode set (S1U3): ``refit.fixed_assignment`` builds a spectrum's frozen modes from the link, no pricing.

Spectra are generated at test time and the link is made up (``_search_harness``); the real ``_Spec`` and the real
engine build the start vector, so the slot order, the delta mask and the linear values are the core's own.
"""
import copy

import pytest
from _search_harness import add, drop, links_for, pred, snapshot_for, split_of
from test_coupled_identity import held as held_id
from test_coupled_identity import link as link_id
from test_coupled_identity import run as run_ident
from test_coupled_refit import record

from isar.analyze.coupled import refit as R
from isar.analyze.coupled import search as SE
from isar.analyze.coupled.linking import Change
from isar.analyze.coupled.types import Assignment

OWN = [(5.0, 0.5, 20.0), (9.0, 0.5, 20.0)]
B12 = ["b1", "b2"]
NEIGH = ([(5.0, 0.5, 20.0), (8.6, 0.5, 20.0), (9.4, 0.5, 20.0), (13.0, 0.5, 20.0)], ["b1", "b2", "b3", "b4"])


def snap(links, *, rec=None):
    """y holds b1, b2; its neighbour carries b1..b4, so b3 and b4 can be added or split off."""
    return snapshot_for(OWN, B12, {"x": NEIGH, "z": NEIGH}, links=links, rec=rec)


def heights(a):
    """The mode heights of the start vector (slot order): the record's layout is (omega0, Gamma, h) per mode."""
    return [a.selected_model[4][3 * i + 2] for i in range(a.count)]


def energies(a):
    return [a.selected_model[4][3 * i] for i in range(a.count)]


def split_b2_b3():
    return split_of(1, "b2", 8.6, "b3", 9.4, A1=1.0, A2=3.0)


def test_a_split_replaces_the_mode_by_two_branches_and_drops_nothing_else():
    a = R.fixed_assignment(snap(links_for(2, changes=[split_b2_b3()])), apply_changes=True)
    assert (a.count, a.slots, a.discrete, a.changed) == (3, ("b1", "b2", "b3"), "fixed", True)
    assert a.origins == ("matched",) * 3 and a.triggers == () and a.candidates == () and a.reasons == ()
    assert energies(a) == pytest.approx([5.0, 8.6, 9.4])
    assert heights(a) == pytest.approx([20.0, 20.0 * 0.25, 20.0 * 0.75])
    assert a.search["applied"] == [0] and a.search["skipped"] == [] and a.search["domain"] == "fixed"
    assert a.unlifted == ("b2", "b3")
    assert isinstance(a, Assignment)


def test_without_apply_changes_the_set_is_the_incoming_one_with_the_link_labels():
    a = R.fixed_assignment(snap(links_for(2, changes=[split_b2_b3()])), apply_changes=False)
    assert (a.count, a.slots, a.changed) == (2, ("b1", "b2"), False)
    assert a.search["applied"] == [] and a.unlifted == ()
    assert energies(a) == pytest.approx([5.0, 9.0])


def test_a_drop_is_never_applied_and_is_not_an_error():
    a = R.fixed_assignment(snap(links_for(2, changes=[drop(0)])), apply_changes=True)
    assert (a.count, a.slots, a.changed) == (2, ("b1", "b2"), False)
    assert a.search["applied"] == [] and a.search["skipped"] == []


def test_a_mode_carrying_two_splits_becomes_three_modes():
    """Operator 2026-11-04: several splits of one mode apply as their union, no cap on how many modes result."""
    ch = [split_of(0, "b2", 8.6, "b3", 9.4, A1=1.0, A2=1.0), split_of(0, "b2", 8.6, "b4", 9.0, A1=1.0, A2=2.0),
          split_of(0, "b3", 9.4, "b4", 9.0, A1=1.0, A2=2.0)]
    s = snapshot_for([(9.0, 0.5, 20.0)], ["b1"], {"x": NEIGH, "z": NEIGH}, links=links_for(1, changes=ch))
    a = R.fixed_assignment(s, apply_changes=True)
    assert (a.count, a.slots) == (3, ("b2", "b4", "b3"))              # ascending in predicted energy
    assert energies(a) == pytest.approx([8.6, 9.0, 9.4])
    assert heights(a) == pytest.approx([20.0 * 0.25, 20.0 * 0.5, 20.0 * 0.25])    # intensities 1, 2, 1
    assert a.search["applied"] == [0, 1, 2] and a.search["skipped"] == []
    assert a.changed


def test_two_splits_on_different_modes_apply_together_and_a_clash_is_skipped_with_the_search_s_reason():
    ch = [split_of(0, "b3", 4.6, "b4", 5.4), split_of(1, "b3", 8.6, "b4", 9.4), add("b1", 13.0)]
    a = R.fixed_assignment(snap(links_for(2, changes=ch)), apply_changes=True)
    # canonical order: the add (b1 already named here) first, then the two splits that both name b3 and b4
    assert a.count == 3 and a.slots == ("b3", "b4", "b2")
    assert [(x["id"], x["why"]) for x in a.search["skipped"]] == [
        ("add:b1", "a branch would be named twice"), ("split:1:b3+b4", "a branch would be named twice")]
    assert a.search["applied"] == [0]


def test_a_branch_the_pool_cannot_carry_is_skipped_as_the_search_records_it():
    s = snap(links_for(2, changes=[add("b9", 13.0)]))
    a = R.fixed_assignment(s, apply_changes=True)
    assert a.count == 2 and a.search["skipped"] == [{"change": 0, "id": "add:b9",
                                                     "why": "branch not carried by a conditioning neighbour"}]


def test_an_added_mode_has_a_free_width_at_the_predicted_start_and_its_height_is_solved():
    s = snap(links_for(2, changes=[Change("add", None, ("b4",), (pred("b4", 13.0, w=4.0),))]))
    a = R.fixed_assignment(s, apply_changes=True)
    assert (a.count, a.slots, a.selected_model[3]) == (3, ("b1", "b2", "b4"), ())
    g = a.selected_model[4][3 * 2 + 1]
    assert g == pytest.approx(SE.gamma_start(4.0, R._Spec(s).fwhm))
    assert energies(a)[2] == pytest.approx(13.0) and heights(a)[2] >= 0.0    # Poisson NNLS: finite, not negative


def delta_record(delta):
    rec = copy.deepcopy(record(OWN))
    rec["models"][0]["delta_modes"] = list(delta)
    return rec


def test_an_incoming_delta_mode_stays_delta_at_its_new_position_and_a_split_parent_is_not_delta():
    below = Change("add", None, ("b4",), (pred("b4", 2.0),))
    s = snap(links_for(2, changes=[split_b2_b3()]), rec=delta_record([1]))
    a = R.fixed_assignment(s, apply_changes=True)
    assert a.slots == ("b1", "b2", "b3") and a.selected_model[3] == ()            # the delta parent was split
    s = snap(links_for(2, changes=[below]), rec=delta_record([1]))
    a = R.fixed_assignment(s, apply_changes=True)
    assert a.slots == ("b4", "b1", "b2") and a.selected_model[3] == (2,)          # b2 moved from position 1 to 2


def test_the_selected_model_keeps_the_incoming_variant_and_linear_values():
    a = R.fixed_assignment(snap(links_for(2, changes=[split_b2_b3()])), apply_changes=True)
    el, bg, qe, delta, theta = a.selected_model
    assert (el, bg, qe) == R._Spec(snap(links_for(2))).inc_variant == (True, True, False)
    assert delta == () and list(theta[-2:]) == pytest.approx([100.0, 2.0])         # record(): elastic.h, bg.BG


def test_an_incoming_model_of_another_count_raises():
    with pytest.raises(ValueError, match="no incoming model"):
        R.fixed_assignment(snap(links_for(2), rec=record(OWN[:1])), apply_changes=True)


def test_the_search_still_refuses_two_splits_of_one_mode_in_one_candidate():
    ch = [split_of(0, "b2", 8.6, "b3", 9.4), split_of(0, "b2", 8.6, "b4", 9.0)]
    s = snapshot_for([(9.0, 0.5, 20.0)], ["b1"], {"x": NEIGH}, links=links_for(1, changes=ch))
    spec = R._Spec(s)
    usable, _ = SE.usable_changes(spec, s.links)
    assert SE.compose_changes(spec, s.links, usable) == "two changes meet on one mode"
    assert SE.compose_rows(spec, s.links, usable, union=True)[0][0] == "b2"


def test_identity_reads_a_fixed_assignment_as_confident_and_keeps_the_split_s_ids():
    a = R.fixed_assignment(snap(links_for(2, changes=[split_b2_b3()])), apply_changes=True)
    a = Assignment(**{**a.__dict__, "snapshot_id": "b:ring1", "unit": "k"})
    prev = {"A": held_id("A", "fA", [("b1", 5.0), ("b2", 8.6), ("b3", 9.4)])}
    r = run_ident(["k"], [link_id("A", "k")], {"k": a}, prev)
    refs = [("k", "f_k", m) for m in range(3)]
    assert [r.ids[x] for x in refs] == ["b1", "b2", "b3"] and "identity_ambiguous" not in r.flags

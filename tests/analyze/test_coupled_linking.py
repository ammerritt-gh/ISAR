"""Mode linking core (C-link-core): the worked examples of the plan as tests.

Prevents: a broad overdamped mode matched by its omega0 instead of where its intensity peaks (the a06 failure),
an intensity compared across temperatures without the Bose factor, a wrong link built into every later fit, a
near-tie forced into one answer, names that change when nothing changed, an input order that changes the result,
and a JSON form that does not round-trip. Spectra are built here from omega0, Gamma and area; nothing is read
from a workspace.

Defaults throughout: T = 100 K, resolution fwhm 0.4, every mode Gamma 0.2, area 10, window (1, 12), q links
of step 0.1 with metric "lattice" (tolerance 8.0 x 0.1 = 0.8 meV), the default linking settings.
"""
from __future__ import annotations

import itertools
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from isar.analyze.coupled import linking as lk
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held, HeldMode
from isar.session.inter_spectra.graph import Link

REPO_ROOT = Path(__file__).resolve().parents[2]
SET = InterSettings()


def test_the_import_root_is_this_checkout():
    assert Path(lk.__file__).resolve().is_relative_to(REPO_ROOT)


def inp(unit, modes, T=100.0, fwhm=0.4, window=(1.0, 12.0), anchored=False, ids=None, scale=1.0,
        at_cap=(), fit="fast", sigma_g=None):
    """A spectrum from ``modes``: omega0, or (omega0, Gamma, area)."""
    rows = [(m, 0.2, 10.0) if not isinstance(m, tuple) else m for m in modes]
    hm = tuple(HeldMode(branch=(ids[i] if ids else f"fast:{unit}#{i}"), omega0=o, gamma=g, height=1.0, guard=None,
                        guard_source="unavailable", status="determined_by_own_data", regime="underdamped",
                        pinned=False) for i, (o, g, a) in enumerate(rows))
    held = Held(unit=unit, fit=fit, inputs="h-" + unit, window=window, modes=hm, dormant=(), T=T)
    n = len(rows)
    return lk.LinkInput(held=held, fwhm=fwhm, areas=tuple(a for _, _, a in rows), sigma_areas=(None,) * n,
                        sigma_gammas=(sigma_g,) * n if sigma_g is not None else (None,) * n,
                        at_cap=tuple(i in at_cap for i in range(n)),
                        stamp=lk.fit_stamp(fit, "h-" + unit, rows), scale=scale, anchored=anchored)


def qlink(a, b, dq=(0.1, 0.0, 0.0)):
    return Link("q", a, b, 0.1, dq, "lattice")


ROW, COL = (0.1, 0.0, 0.0), (0.0, 0.1, 0.0)


def grid_2x3():
    names = ["a01", "a02", "a03", "a04", "a05", "a06"]
    links = [qlink("a01", "a02"), qlink("a02", "a03"), qlink("a04", "a05"), qlink("a05", "a06"),
             qlink("a01", "a04", COL), qlink("a02", "a05", COL), qlink("a03", "a06", COL)]
    return names, links


def amp(omega0, gamma=0.2, T=100.0, area=10.0):
    return lk.matching_intensity(area, omega0=omega0, gamma=gamma, T=T)


def members(r):
    return {frozenset(b.members) for b in r.branches}


# ---- the summaries -------------------------------------------------------------------------------
@pytest.mark.parametrize("omega0,gamma,T,want", [
    (9.7, 9.6, 97, 1.453), (9.7, 9.6, 150, 0.945), (9.7, 9.6, 300, 0.474), (9.7, 9.6, None, 5.658),
    (8.7, 0.3, 97, 8.692), (8.7, 0.001, 97, 8.700), (6, 0.2, 97, 5.994), (4, 2, 97, 2.985),
    (5.0, 0.2, 100, 4.993), (5.6, 0.2, 100, 5.594), (2.0, 1.5, 100, 0.367)])
def test_peak_energy_table(omega0, gamma, T, want):
    assert lk.peak_energy(omega0, gamma, T) == pytest.approx(want, abs=0.002)


def test_peak_energy_refuses_a_mode_it_cannot_place():
    for bad in ((0.0, 0.2), (5.0, 0.0), (float("nan"), 0.2), (5.0, float("inf"))):
        with pytest.raises(ValueError):
            lk.peak_energy(*bad, 100.0)


def test_the_core_copies_agree_with_the_lineshapes_module():
    from isar.analyze.lineshapes import KB
    assert lk.KB_MEV_PER_K == KB


def test_width_is_the_line_and_the_resolution_in_quadrature():
    assert lk.matching_width(0.2, 0.4) == pytest.approx(math.hypot(0.4, 0.4))
    assert lk.matching_width(0.0, 0.4) == 0.4


def test_intensity_scale_and_unknown_areas():
    base = lk.matching_intensity(10, omega0=5, gamma=0.2, T=100)
    assert lk.matching_intensity(10, omega0=5, gamma=0.2, T=100, scale=0.5) == pytest.approx(0.5 * base)
    for bad in (None, 0, -3.0, float("nan"), float("inf")):
        assert lk.matching_intensity(bad, omega0=5, gamma=0.2, T=100) is None


def test_a_narrow_mode_intensity_is_area_over_n_plus_one():
    kT = lk.KB_MEV_PER_K * 100
    n1 = 1.0 / (1.0 - math.exp(-5.0 / kT))
    assert lk.matching_intensity(10, omega0=5, gamma=0.001, T=100) == pytest.approx(10 / n1, rel=1e-3)


@pytest.mark.parametrize("omega0,gamma,T", [(9.7, 9.6, 150), (5.0, 0.2, 100), (2.0, 1.5, 100), (9.7, 9.6, None)])
def test_unit_stokes_area_agrees_with_quad(omega0, gamma, T):
    from scipy.integrate import quad
    o2, g2 = omega0 ** 2, gamma ** 2
    f = lambda E: (omega0 / math.pi) * 4 * gamma * lk._y(E, T) / ((E * E - o2) ** 2 + 4 * g2 * E * E)   # noqa: E731
    ref = quad(f, 0, math.inf, limit=500)[0]
    assert lk.unit_stokes_area(omega0, gamma, T) == pytest.approx(ref, rel=1e-6)


def test_T_none_gives_the_T_to_zero_area_and_the_bose_factor_is_integrated_across_a_broad_line():
    cold = lk.matching_intensity(10, omega0=9.7, gamma=9.6, T=None)
    assert cold == pytest.approx(10 / lk.unit_stokes_area(9.7, 9.6, 1e-9), rel=1e-6)
    # a broad mode at 150 K is corrected over its whole width, not by the (n+1) of its omega0
    kT = lk.KB_MEV_PER_K * 150
    at_omega0 = 1.0 / (1.0 - math.exp(-9.7 / kT))
    assert lk.matching_intensity(10, omega0=9.7, gamma=9.6, T=150) != pytest.approx(10 / at_omega0, rel=0.05)


def test_same_intrinsic_amplitude_at_97_and_300_K_costs_nothing_on_intensity():
    rows = {}
    for u, T in (("u97", 97.0), ("u300", 300.0)):
        rows[u] = (5.0, 0.001, 10.0 * lk.unit_stokes_area(5.0, 0.001, T))
    ratio = rows["u300"][2] / rows["u97"][2]
    assert ratio == pytest.approx(2.56, abs=0.05)                  # the stored areas differ by the Bose factor
    ln = [math.log(lk.matching_intensity(a, omega0=o, gamma=g, T=T))
          for (o, g, a), T in ((rows["u97"], 97.0), (rows["u300"], 300.0))]
    assert (ln[0] - ln[1]) ** 2 / SET.intensity_tolerance_ln ** 2 < 1e-6
    r = lk.link({"u97": inp("u97", [rows["u97"]], T=97.0), "u300": inp("u300", [rows["u300"]], T=300.0)},
                [Link("T", "u97", "u300", 203.0, None, None)], SET)
    assert r.labels["u97"][0] == r.labels["u300"][0] is not None
    assert r.markers == ()


# ---- (a) missing middle mode, interpolated add --------------------------------------------------
def test_a_missing_middle_mode_is_added_at_the_interpolated_energy():
    names, links = grid_2x3()
    modes = {"a01": [4.0, 8.0], "a02": [8.1], "a03": [4.4, 8.2], "a04": [4.1, 8.0], "a05": [4.3, 8.1], "a06": [4.4, 8.2]}
    r = lk.link({u: inp(u, modes[u]) for u in names}, links, SET)
    assert r.contradictions == () and r.ties == ()
    assert [b.name for b in r.branches] == ["B1", "B2"]
    b1, b2 = r.branches
    assert set(b1.members) == {("a01", 0), ("a03", 0), ("a04", 0), ("a05", 0), ("a06", 0)}
    assert set(b2.members) == {("a01", 1), ("a02", 0), ("a03", 1), ("a04", 1), ("a05", 1), ("a06", 1)}
    assert r.labels["a02"] == (b2.id,)
    p = next(p for p in r.predictions["a02"] if p.branch == b1.id)
    assert p.mode is None and p.E == pytest.approx(4.2418, abs=0.002) and p.tol_E == pytest.approx(0.566, abs=0.002)
    assert p.w == pytest.approx(0.5657, abs=0.001) and p.from_units == ("a01", "a03", "a05")
    carriers = [amp(4.0), amp(4.4), amp(4.3)]
    assert p.A == pytest.approx(math.exp(sum(map(math.log, carriers)) / 3), rel=1e-9)
    assert r.changes["a02"] == (lk.Change("add", None, (b1.id,), (p,)),)
    assert all(not [c for c in r.changes[u] if c.kind != "drop"] for u in names if u != "a02")
    assert not [c for u in names for c in r.changes[u] if c.kind == "drop"]
    assert r.markers == ()
    assert all(all(s) for s in r.settled.values())


def test_a_branch_both_ends_of_a_bare_row_carry_is_added_in_the_middle():
    """The design's own example: 4 meV in the first spectrum, 8 meV in the third, expected near 6 meV in the second."""
    ins = {"L": inp("L", [4.0]), "X": inp("X", []), "R": inp("R", [8.0])}
    r = lk.link(ins, [qlink("L", "X"), qlink("X", "R")], SET)
    assert len(r.branches) == 1 and set(r.branches[0].members) == {("L", 0), ("R", 0)}
    (c,) = r.changes["X"]
    assert c.kind == "add" and c.predictions[0].E == pytest.approx(6.0, abs=0.01)
    assert r.changes["L"] == () and r.changes["R"] == ()
    assert all(m.kind != "unlinked" for m in r.markers)


def test_skip_pair_overlap_is_a_split_and_a_far_pair_is_an_add():
    links = [qlink("L", "X"), qlink("X", "R")]
    ins = {"L": inp("L", [5.0, 5.2]), "X": inp("X", [5.0]), "R": inp("R", [5.0, 5.2])}
    r = lk.link(ins, links, SET)
    a = next(b for b in r.branches if ("X", 0) in b.members)
    b = next(b for b in r.branches if b is not a)
    assert set(a.members) == {("L", 0), ("X", 0), ("R", 0)} and set(b.members) == {("L", 1), ("R", 1)}
    (c,) = r.changes["X"]
    assert c.kind == "split" and c.mode == 0 and c.branches == tuple(sorted((a.id, b.id)))
    far = lk.link({"L": inp("L", [5.0, 6.0]), "X": inp("X", [5.0]), "R": inp("R", [5.0, 6.0])}, links, SET)
    (c,) = far.changes["X"]
    assert c.kind == "add" and c.predictions[0].E == pytest.approx(5.994, abs=0.01)


def test_a_lone_mode_between_empty_neighbours_is_a_drop_with_an_unlinked_marker():
    r = lk.link({"a": inp("a", [4.0]), "b": inp("b", [])}, [qlink("a", "b")], SET)
    assert r.branches == () and r.labels["a"] == (None,)
    assert r.changes["a"] == (lk.Change("drop", 0, (), ()),) and r.changes["b"] == ()
    assert [(m.unit, m.kind) for m in r.markers] == [("a", "unlinked")]
    alone = lk.link({"a": inp("a", [4.0])}, [], SET)
    assert alone.changes["a"] == () and alone.markers == ()


# ---- (b) swap, competition ---------------------------------------------------------------------
def test_a_crossing_within_the_tie_margin_is_one_swap_group():
    r = lk.link({"a": inp("a", [5.0, 5.6]), "b": inp("b", [5.25, 5.35])}, [qlink("a", "b")], SET)
    b1, b2 = r.branches
    (t,) = r.ties
    assert (t.id, t.kind, t.units) == ("t1", "swap", ("a", "b"))
    assert t.modes == {"a": (0, 1), "b": (0, 1)} and t.branches == tuple(sorted((b1.id, b2.id)))
    assert t.alternatives == {"a": (b2.id, b1.id), "b": (b2.id, b1.id)}
    assert t.d_best == pytest.approx(0.196, abs=0.02) and t.d_alt == pytest.approx(0.384, abs=0.03)
    assert not any(any(s) for s in r.settled.values()) and r.contradictions == ()


def test_an_anchored_spectrum_lists_no_alternative_and_its_modes_stay_settled_in_a_tie():
    r = lk.link({"a": inp("a", [5.0, 5.6], anchored=True), "b": inp("b", [5.25, 5.35])}, [qlink("a", "b")], SET)
    b1, b2 = r.branches
    (t,) = r.ties
    assert t.units == ("a", "b") and t.modes == {"b": (0, 1)} and t.alternatives == {"b": (b2.id, b1.id)}
    assert r.settled["a"] == (True, True) and not any(r.settled["b"])


def test_a_wide_crossing_is_no_group():
    r = lk.link({"a": inp("a", [5.0, 6.0]), "b": inp("b", [5.0, 6.0])}, [qlink("a", "b")], SET)
    assert r.ties == () and all(all(s) for s in r.settled.values())


def test_competition_is_one_group_per_contested_mode_and_mirrors():
    ins = {"a": inp("a", [5.0]), "b": inp("b", [5.0, 5.3]), "c": inp("c", [5.3])}
    r = lk.link(ins, [qlink("a", "b"), qlink("b", "c")], SET)
    b1, b2 = r.branches
    assert set(b1.members) == {("a", 0), ("b", 0)} and set(b2.members) == {("b", 1), ("c", 0)}
    t1, t2 = r.ties
    assert (t1.id, t1.kind, t1.units) == ("t1", "competition", ("a", "b"))
    assert t1.modes == {"a": (0,), "b": (0, 1)} and t1.alternatives == {"a": (b2.id,), "b": (None, b2.id)}
    assert t1.d_best == pytest.approx(0.0, abs=0.01) and t1.d_alt == pytest.approx(0.14, abs=0.03)
    assert (t2.id, t2.units) == ("t2", ("b", "c"))
    assert t2.modes == {"c": (0,), "b": (1, 0)} and t2.alternatives == {"c": (b1.id,), "b": (b1.id, None)}
    assert not any(any(s) for s in r.settled.values())
    far = lk.link({**ins, "b": inp("b", [5.0, 6.5])}, [qlink("a", "b"), qlink("b", "c")], SET)
    assert far.ties == ()


# ---- (c) loop, (f) anchored --------------------------------------------------------------------
def _block(a_modes):
    ins = {"a": inp("a", a_modes), "b": inp("b", [5.5]), "c": inp("c", [6.6]), "d": inp("d", [6.0])}
    links = [qlink("a", "b"), qlink("c", "d"), qlink("a", "c", COL), qlink("b", "d", COL)]
    return lk.link(ins, links, SET)


def test_a_loop_that_would_put_two_modes_of_one_spectrum_on_one_branch_is_a_contradiction():
    r = _block([5.0, 7.5])
    (b1,) = r.branches
    assert set(b1.members) == {("a", 0), ("b", 0), ("c", 0), ("d", 0)}
    assert r.labels["a"] == (b1.id, None)
    (c,) = r.contradictions
    assert (c.units, c.modes, c.reason) == (("a", "c"), (1, 0), "loop") and c.d == pytest.approx(1.268, abs=0.1)
    assert r.changes["a"] == (lk.Change("drop", 1, (), ()),)
    assert {(m.unit, m.mode, m.kind) for m in r.markers if m.kind != "change"} == {
        ("a", 1, "contradiction"), ("c", 0, "contradiction"), ("a", 1, "unlinked")}
    assert r.settled["a"] == (True, False) and r.settled["c"] == (False,)


def test_the_refused_pair_is_the_last_one_processed_on_the_loop():
    r = _block([5.0, 6.5])
    (c,) = r.contradictions
    assert (c.units, c.modes, c.reason) == (("c", "d"), (0, 0), "loop") and c.d == pytest.approx(0.564, abs=0.1)


def test_anchored_modes_are_fixed_points_and_two_anchors_never_join():
    ins = {"u1": inp("u1", [5.0, 7.0], anchored=True, ids=["P", "Q"]),
           "u2": inp("u2", [5.6], anchored=True, ids=["Q2"]),
           "f": inp("f", [5.28])}
    r = lk.link(ins, [qlink("f", "u1"), qlink("f", "u2")], SET)
    assert {b.id for b in r.branches} == {"P", "Q", "Q2"}
    assert r.labels["f"] == ("P",) and r.labels["u1"] == ("P", "Q") and r.labels["u2"] == ("Q2",)
    (c,) = r.contradictions
    assert c.reason == "anchor" and c.units == ("f", "u2")
    assert r.changes["u1"] == () and r.changes["u2"] == ()


def test_two_anchored_modes_with_one_incoming_id_are_one_branch():
    ins = {"u1": inp("u1", [5.0], anchored=True, ids=["P"]), "u2": inp("u2", [9.0], anchored=True, ids=["P"])}
    r = lk.link(ins, [qlink("u1", "u2")], SET)
    assert [b.id for b in r.branches] == ["P"] and set(r.branches[0].members) == {("u1", 0), ("u2", 0)}
    with pytest.raises(ValueError, match="u1"):
        lk.link({"u1": inp("u1", [5.0, 6.0], anchored=True, ids=["P", "P"])}, [], SET)


# ---- (d) an overdamped mode is matched where its intensity peaks ---------------------------------
def _a06_a07(**kw):
    ins = {"a06": inp("a06", [(8.7, 0.3, 40.0), (9.7, 9.6, 400.0)], T=150.0, at_cap=kw.get("cap", ())),
           "a07": inp("a07", [(8.8, 0.3, 38.0), (9.5, 4.0, 300.0)], T=150.0)}
    return lk.link(ins, [qlink("a06", "a07")], SET)


def test_an_overdamped_mode_is_not_linked_by_its_omega0():
    r = _a06_a07()
    assert r.peak_E["a06"] == (pytest.approx(8.691, abs=0.002), pytest.approx(0.945, abs=0.002))
    assert r.peak_E["a07"][1] == pytest.approx(7.966, abs=0.01)
    assert len(r.branches) == 1 and set(r.branches[0].members) == {("a06", 0), ("a07", 0)}
    assert r.labels["a06"][1] is None and r.labels["a07"][1] is None
    for u in ("a06", "a07"):
        assert r.changes[u] == (lk.Change("drop", 1, (), ()),)
    assert {(m.unit, m.mode, m.kind) for m in r.markers} == {("a06", 1, "unlinked"), ("a07", 1, "unlinked")}
    # by omega0 alone the broad pair would have matched: the energy part is 0.062, the width part 1.56, the intensity 0.123
    e_by_omega0 = (9.7 - 9.5) ** 2 / 0.64
    e_by_peak = (r.peak_E["a06"][1] - r.peak_E["a07"][1]) ** 2 / 0.64
    assert e_by_omega0 < 0.1 and e_by_peak > 70


def test_the_intensity_part_of_the_overdamped_pair_is_the_intrinsic_one():
    """Recomputed with the built function: the verdicts of the original example stand."""
    d_int = (math.log(amp(8.7, 0.3, 150.0, 40.0)) - math.log(amp(8.8, 0.3, 150.0, 38.0))) ** 2
    assert d_int == pytest.approx(INT_PART_M0, abs=1e-6)
    broad = (math.log(amp(9.7, 9.6, 150.0, 400.0)) - math.log(amp(9.5, 4.0, 150.0, 300.0))) ** 2
    assert broad == pytest.approx(INT_PART_BROAD, abs=1e-6)
    assert d_int < SET.link_threshold


def test_a_mode_at_the_width_cap_carries_an_at_cap_marker_with_no_quantity():
    r = _a06_a07(cap=(1,))
    m = [m for m in r.markers if m.kind == "at_cap"]
    assert [(x.unit, x.mode, x.quantity) for x in m] == [("a06", 1, None)]
    assert "width cap of the fit in use" in m[0].words and "fast-pass" not in m[0].words


INT_PART_M0 = 0.001876            # ln(intrinsic ratio)^2 over tau^2 = 1, recomputed with the built function
INT_PART_BROAD = 0.12298          # was 0.083 on the stored areas


# ---- (e) names ----------------------------------------------------------------------------------
def _four_units(changed=(), unit_modes=None):
    modes = unit_modes or [5.0, 7.0]
    ins = {u: inp(u, modes) for u in ("u1", "u2", "u3", "u4")}
    links = [qlink("u1", "u2"), qlink("u2", "u3"), qlink("u3", "u4")]
    return ins, links


def test_names_survive_a_relink_where_one_fit_changed():
    ins, links = _four_units()
    p = lk.link(ins, links, SET)
    assert [b.name for b in p.branches] == ["B1", "B2"] and p.next_name == 3
    same = lk.link(ins, links, SET, p, next_name=p.next_name)
    assert same.branches == p.branches and same.next_name == 3
    # u1's fit changes: its modes no longer vote; the others keep B1 and B2
    ins2 = dict(ins)
    ins2["u1"] = inp("u1", [5.0, 7.0], fit="f-new")
    r = lk.link(ins2, links, SET, p, next_name=p.next_name)
    assert {b.name: b.members for b in r.branches} == {b.name: b.members for b in p.branches}
    assert {b.id for b in r.branches} == {b.id for b in p.branches}


def test_a_split_component_keeps_the_biggest_name_and_the_new_part_gets_the_next_one():
    ins, links = _four_units(unit_modes=[5.0])
    p = lk.link(ins, links, SET)
    (b1,) = p.branches
    assert b1.name == "B1" and b1.id == "link:u1#0"
    # the middle link breaks (u2 and u3 are too far apart in energy): {u1,u2} and {u3,u4}
    ins2 = dict(ins)
    ins2["u3"] = inp("u3", [9.0])
    ins2["u4"] = inp("u4", [9.0])
    r = lk.link(ins2, links, SET, p, next_name=p.next_name)
    names = {b.name: set(b.members) for b in r.branches}
    assert names == {"B1": {("u1", 0), ("u2", 0)}, "B2": {("u3", 0), ("u4", 0)}}
    assert r.name_start == 2 and r.next_name == 3
    # a tie in score goes to the lower mean E*: both halves' fits changed, so each votes 1 of 4 members... lower keeps B1
    ins3 = dict(ins2)
    ins3["u2"] = inp("u2", [5.0], fit="f-new")
    ins3["u4"] = inp("u4", [9.0], fit="f-new")
    r3 = lk.link(ins3, links, SET, p, next_name=p.next_name)
    low = next(b for b in r3.branches if ("u1", 0) in b.members)
    assert low.name == "B1"


def test_a_refit_that_carries_the_link_ids_keeps_the_names_whatever_its_fit_id():
    ins, links = _four_units(unit_modes=[5.0, 7.0])
    p = lk.link(ins, links, SET)
    ids = [b.id for b in p.branches]
    refit = {u: inp(u, [5.0, 7.0], fit=f"f-{u}", ids=ids) for u in ins}
    r = lk.link(refit, links, SET, p, next_name=p.next_name)
    assert [(b.id, b.name) for b in r.branches] == [(b.id, b.name) for b in p.branches]


def test_a_vanished_branch_name_is_never_reused():
    ins, links = _four_units(unit_modes=[5.0])
    p = lk.link(ins, links, SET)
    gone = lk.link({u: inp(u, []) for u in ins}, links, SET, p, next_name=p.next_name)
    assert gone.branches == () and gone.next_name == p.next_name
    back = lk.link(ins, links, SET, gone, next_name=gone.next_name)
    assert [b.name for b in back.branches] == ["B2"]


# ---- predictions, markers, determinism, JSON ----------------------------------------------------
def test_a_mode_far_from_what_its_neighbours_predict_is_a_change_marker():
    ins = {"a": inp("a", [5.0]), "b": inp("b", [5.5]), "c": inp("c", [6.0])}
    ok = lk.link(ins, [qlink("a", "b"), qlink("b", "c")], SET)
    assert ok.markers == ()
    ins["b"] = inp("b", [(5.5, 0.2, 1000.0)])
    r = lk.link({**ins, "b": inp("b", [(5.5, 0.2, 40.0)])}, [qlink("a", "b"), qlink("b", "c")], SET)
    kinds = {(m.unit, m.quantity) for m in r.markers if m.kind == "change"}
    assert ("b", "intensity") in kinds
    assert any("intrinsic intensity" in m.words for m in r.markers)


def test_the_intensity_marker_words_show_the_efficiency_corrected_numbers():
    # efficiency 0.5 is scale 2: the shown intensities must be the ones compared, not divided back by the scale
    ins = {"a": inp("a", [5.0]), "b": inp("b", [(5.5, 0.2, 40.0)], scale=2.0), "c": inp("c", [6.0])}
    r = lk.link(ins, [qlink("a", "b"), qlink("b", "c")], SET)
    (m,) = [m for m in r.markers if m.unit == "b" and m.quantity == "intensity"]
    seen = lk.matching_intensity(40.0, omega0=5.5, gamma=0.2, T=100.0, scale=2.0)
    predicted = math.sqrt(amp(5.0) * amp(6.0))
    assert f"{seen:.3g}" in m.words and f"{predicted:.3g}" in m.words


def _examples():
    names, links = grid_2x3()
    modes = {"a01": [4.0, 8.0], "a02": [8.1], "a03": [4.4, 8.2], "a04": [4.1, 8.0], "a05": [4.3, 8.1], "a06": [4.4, 8.2]}
    out = [(
        {u: inp(u, modes[u]) for u in names}, links)]
    out.append(({"a": inp("a", [5.0, 5.6]), "b": inp("b", [5.25, 5.35])}, [qlink("a", "b")]))
    out.append(({"a": inp("a", [5.0]), "b": inp("b", [5.0, 5.3]), "c": inp("c", [5.3])},
                [qlink("a", "b"), qlink("b", "c")]))
    out.append(({"a": inp("a", [5.0, 7.5]), "b": inp("b", [5.5]), "c": inp("c", [6.6]), "d": inp("d", [6.0])},
                [qlink("a", "b"), qlink("c", "d"), qlink("a", "c", COL), qlink("b", "d", COL)]))
    out.append(({"a06": inp("a06", [(8.7, 0.3, 40.0), (9.7, 9.6, 400.0)], T=150.0, at_cap=(1,)),
                 "a07": inp("a07", [(8.8, 0.3, 38.0), (9.5, 4.0, 300.0)], T=150.0)}, [qlink("a06", "a07")]))
    out.append(({"L": inp("L", [5.0, 5.2]), "X": inp("X", [5.0]), "R": inp("R", [5.0, 5.2])},
                [qlink("L", "X"), qlink("X", "R")]))
    return out


@pytest.mark.parametrize("k", range(6))
def test_the_result_does_not_depend_on_input_order_and_round_trips_exactly(k):
    ins, links = _examples()[k]
    base = lk.link(ins, links, SET, excluded={"zz": ("no fit", None)})
    text = json.dumps(lk.to_json(base), allow_nan=False)
    rng = random.Random(k)
    for _ in range(3):
        keys = list(ins)
        rng.shuffle(keys)
        ls = list(links)
        rng.shuffle(ls)
        again = lk.link({u: ins[u] for u in keys}, ls, SET, excluded={"zz": ("no fit", None)})
        assert json.dumps(lk.to_json(again), allow_nan=False) == text
    assert lk.from_json(json.loads(text)) == base
    assert lk.from_json(lk.to_json(base)) == base


def test_from_json_names_the_missing_key_and_refuses_another_schema():
    ins, links = _examples()[1]
    d = lk.to_json(lk.link(ins, links, SET))
    with pytest.raises(ValueError, match="schema"):
        lk.from_json({**d, "schema": "other/1"})
    bad = {k: v for k, v in d.items() if k != "ties"}
    with pytest.raises(ValueError, match="ties"):
        lk.from_json(bad)
    with pytest.raises(ValueError):
        lk.to_json(lk.LinkResult(**{**lk.from_json(d).__dict__, "peak_E": {"a": (float("nan"),)}}))


def test_validation_names_the_unit():
    good = inp("u", [5.0])
    with pytest.raises(ValueError, match="u"):
        lk.link({"u": lk.LinkInput(**{**good.__dict__, "areas": ()})}, [], SET)
    with pytest.raises(ValueError, match="u"):
        lk.link({"u": lk.LinkInput(**{**good.__dict__, "fwhm": 0.0})}, [], SET)
    with pytest.raises(ValueError, match="next_name"):
        lk.link({"u": good}, [], SET, next_name=0)
    with pytest.raises(ValueError, match="twice"):
        lk.link({"u": good}, [qlink("u", "u")], SET)
    assert lk.link({"u": good}, [qlink("u", "w")], SET).labels == {"u": (None,)}      # a link to a spectrum with no fit is ignored


def test_inputs_are_not_mutated_and_the_result_shares_nothing_mutable():
    ins, links = _examples()[0]
    before = {u: i.__dict__.copy() for u, i in ins.items()}
    r = lk.link(ins, links, SET)
    assert {u: i.__dict__ for u, i in ins.items()} == before
    assert r.settings == SET.to_values() and r.settings is not SET.to_values()


def test_fit_stamp_follows_the_fit_and_its_modes_not_their_order():
    s = lk.fit_stamp("fast", "h", [(5.0, 0.2, 10.0), (6.0, 0.2, None)])
    assert len(s) == 12 and s == lk.fit_stamp("fast", "h", [(6.0, 0.2, None), (5.0, 0.2, 10.0)])
    assert s != lk.fit_stamp("fast", "h", [(5.0, 0.2, 10.5), (6.0, 0.2, None)])
    assert s != lk.fit_stamp("f-1", "h", [(5.0, 0.2, 10.0), (6.0, 0.2, None)])


def test_the_module_imports_no_numpy_qt_or_session_code():
    code = ("import sys; import isar.analyze.coupled.linking; "
            "bad = [m for m in ('numpy', 'scipy', 'PySide6') if m in sys.modules]; "
            "bad += [m for m in sys.modules if m.startswith('isar.session')]; assert not bad, bad")
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); {code}"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr


def test_a_grid_of_28_by_4_temperatures_links():
    ins, links = {}, []
    for t in range(4):
        for r_ in range(4):
            for c in range(7):
                u = f"t{t}r{r_}c{c}"
                ins[u] = inp(u, [3.0 + 0.2 * c + 0.1 * r_ + 0.05 * t, 8.0 + 0.1 * c], T=100.0 + 50 * t)
                if c:
                    links.append(qlink(f"t{t}r{r_}c{c - 1}", u))
                if r_:
                    links.append(qlink(f"t{t}r{r_ - 1}c{c}", u, COL))
                if t:
                    links.append(Link("T", f"t{t - 1}r{r_}c{c}", u, 50.0, None, None))
    r = lk.link(ins, links, SET)
    assert len(r.branches) == 2 and r.contradictions == ()

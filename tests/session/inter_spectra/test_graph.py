"""The link graph on hand-made coordinates: links, curvature terms, the batch scope (C-graph)."""
from __future__ import annotations

import pytest

from inter_synthetic import SCAN, build, grid_coords, node, point, scan_nodes
from isar.session.inter_spectra.graph import anchored_scope
from isar.session.inter_spectra.nodes import NodeSet
from isar.session.inter_spectra.q_provider import QPoint


def _pairs(g, kind):
    return {(l.a, l.b) for l in g.links if l.kind == kind}


def test_full_grid_links_and_curvature_terms():
    g = build(scan_nodes(), coords=grid_coords())
    q = _pairs(g, "q")
    assert len(q) == 45                                # 24 along the rows, 21 down the columns
    assert len(g.triplets) == 34                       # 4 rows x 5 windows + 7 columns x 2 windows
    assert all(l.step == pytest.approx(0.1) for l in g.links)
    assert g.metric == {SCAN: "hkl"}
    assert (f"{SCAN}:a03", f"{SCAN}:a14") in q        # down a column
    assert (f"{SCAN}:a03", f"{SCAN}:a15") not in q    # no diagonal


def test_t_links_join_adjacent_temperatures_of_one_series_only():
    nodes = {}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, dets=("a03", "a04"), series="A", T=T))
    nodes.update(scan_nodes("E:4", dets=("a03", "a04"), series="B", T=150.0))   # another q setting
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3"), "B": ("E:4",)},
                 slots={"E:1": 100.0, "E:2": 150.0, "E:3": 200.0, "E:4": 150.0}, repeats=())
    g = build(nodes, ns, kinds=("T",))
    t = _pairs(g, "T")
    assert t == {(f"{s}:{d}", f"{u}:{d}") for s, u in (("E:1", "E:2"), ("E:2", "E:3")) for d in ("a03", "a04")}
    assert {l.step for l in g.links} == {50.0}
    assert g.unlinked["E:4:a03"] == ("T: only scan of its series",)


def test_repeated_temperature_takes_no_t_link_and_is_noted():
    nodes = scan_nodes("E:1", dets=("a03",), series="A", T=100.0)
    nodes.update(scan_nodes("E:2", dets=("a03",), series="A", T=100.04))
    nodes.update(scan_nodes("E:3", dets=("a03",), series="A", T=200.0))
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3")},
                 slots={"E:1": 100.0, "E:2": 100.0, "E:3": 200.0},
                 repeats=("A: 100.0 K holds 2 scans (E:1, E:2)",))
    g = build(nodes, ns, kinds=("T",))
    assert g.links == ()
    assert any("share one temperature" in n for n in g.notes)


def test_neighbour_outside_the_scope_is_inactive():
    nodes = {}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, dets=("a03",), series="A", T=T))
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3")},
                 slots={"E:1": 100.0, "E:2": 150.0, "E:3": 200.0}, repeats=())
    g = build(nodes, ns, scope={"E:1:a03", "E:3:a03"}, kinds=("T",))
    assert g.links == ()                               # 100 K and 200 K are not joined through 150 K


def test_no_coordinates_gives_no_q_links_and_says_why():
    g = build(scan_nodes(), coords={})
    assert g.links == () and g.triplets == ()
    assert g.unlinked[f"{SCAN}:a04"] == ("q: no accepted hkl",)


def test_curvature_term_spacings_and_position():
    nodes = scan_nodes(dets=("a14", "a15", "a16"))
    coords = {f"{SCAN}:a14": point((0.0, 0.1, 0.0)), f"{SCAN}:a15": point((0.1, 0.1, 0.0)),
              f"{SCAN}:a16": point((0.4, 0.1, 0.0))}
    g = build(nodes, coords=coords)
    (t,) = g.triplets
    assert t.members == (f"{SCAN}:a14", f"{SCAN}:a15", f"{SCAN}:a16")
    assert t.spacings == pytest.approx((0.1, 0.3))
    assert t.t_frac == pytest.approx(0.25) and t.line == "row 2"


@pytest.mark.parametrize("tol, passes", [(0.05, False), (0.15, True)])
def test_bent_triplet_needs_the_tolerance(tol, passes):
    nodes = scan_nodes(dets=("a14", "a15", "a16"))
    coords = {f"{SCAN}:a14": point((0.0, 0.0, 0.0)), f"{SCAN}:a15": point((0.1, 0.02, 0.0)),
              f"{SCAN}:a16": point((0.2, 0.0, 0.0))}
    g = build(nodes, coords=coords, tol=tol)
    assert len(g.triplets) == (1 if passes else 0)
    if passes:
        assert g.triplets[0].t_frac == pytest.approx(0.5)
    else:
        assert any("off the line by 10% of its length" in n for n in g.notes)


def test_two_spectra_joined_by_one_q_link_are_noted():
    nodes = scan_nodes(dets=("a03", "a04"))
    g = build(nodes, coords=grid_coords(dets=("a03", "a04")))
    assert g.triplets == ()
    assert any("two spectra joined by one q-link, no curvature term" in n for n in g.notes)


def test_metric_is_chosen_per_scan():
    nodes = {**scan_nodes("E:1", dets=("a14", "a15")), **scan_nodes("E:2", dets=("a14", "a15"))}
    coords = {
        "E:1:a14": QPoint(hkl=(0.0, 0.1, 0.0), source="gpi", cart=(0.0, 0.2, 0.0), epoch="v1"),
        "E:1:a15": QPoint(hkl=(0.1, 0.1, 0.0), source="gpi", cart=(0.2, 0.2, 0.0), epoch="v1"),
        "E:2:a14": point((0.0, 0.1, 0.0)), "E:2:a15": point((0.1, 0.1, 0.0)),
    }
    g = build(nodes, coords=coords)
    assert g.metric == {"E:1": "lattice", "E:2": "hkl"}
    steps = {l.a: l.step for l in g.links}
    assert steps["E:1:a14"] == pytest.approx(0.2) and steps["E:2:a14"] == pytest.approx(0.1)


def test_anchored_scope_by_direction():
    nodes, series, slots = {}, {"A": ("E:1", "E:2", "E:3")}, {"E:1": 100.0, "E:2": 150.0, "E:3": 200.0}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, series="A", T=T))
    nodes.update(scan_nodes("E:4", series=None))
    ns = NodeSet(nodes=nodes, series=series, slots={**slots}, repeats=())
    scope, kinds = anchored_scope(["E:2:a09"], "q", ns)
    assert len(scope) == 28 and kinds == {"q"}
    scope, kinds = anchored_scope(["E:2:a09"], "T", ns)
    assert scope == {"E:1:a09", "E:2:a09", "E:3:a09"} and kinds == {"T"}
    scope, kinds = anchored_scope(["E:2:a09"], "qT", ns)
    assert len(scope) == 84 and kinds == {"q", "T"}
    assert anchored_scope(["E:4:a09"], "T", ns)[0] == {"E:4:a09"}
    assert len(anchored_scope(["E:4:a09"], "qT", ns)[0]) == 28

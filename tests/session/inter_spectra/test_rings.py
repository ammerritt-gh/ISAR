"""Anchored rings, seams, locks and the not-reached list (C-graph, rings.plan_anchored)."""
from __future__ import annotations

from inter_synthetic import DETS, SCAN, build, grid_coords, node, point, scan_nodes
from isar.session.inter_spectra.layout import BL43_GRID
from isar.session.inter_spectra.rings import plan_anchored


def _key(det, scan=SCAN):
    return f"{scan}:{det}"


def _grid_plan(anchors, locks=(), stale=()):
    g = build(scan_nodes(), coords=grid_coords())
    return g, plan_anchored(g, [_key(a) for a in anchors], [_key(k) for k in locks], [_key(k) for k in stale])


def test_single_anchor_rings_are_the_grid_distance():
    g, plan = _grid_plan(["a09"])                       # row 0, column 6
    assert len(plan.rings) == 9 and plan.not_reached == {} and plan.seams == ()
    for det in DETS:
        r, c = BL43_GRID.position(det)
        assert plan.ring_of[_key(det)] == r + abs(c - 6)
    assert plan.ring_of[_key("a41")] == 3 and plan.ring_of[_key("a35")] == 9


def test_two_opposing_anchors_meet_at_seams():
    g, plan = _grid_plan(["a03", "a41"])
    assert plan.ring_of[_key("a09")] == 3 and plan.fronts[_key("a09")] == (_key("a41"),)
    assert plan.fronts[_key("a06")] == (_key("a03"),)     # 3 steps from a03, 6 from a41
    expect = {(plan.ring_of[l.a], l) for l in g.links
              if plan.ring_of.get(l.a) == plan.ring_of.get(l.b) and plan.ring_of.get(l.a, 0) >= 1}
    assert expect and {(s.ring, s.link) for s in plan.seams} == expect


def test_equidistant_node_keeps_both_anchors_in_its_fronts():
    _, plan = _grid_plan(["a03", "a09"])                # a06 is three steps from each
    assert plan.fronts[_key("a06")] == (_key("a03"), _key("a09"))


def test_lock_is_reached_not_refitted_and_conditions_its_neighbours():
    _, plan = _grid_plan(["a03"], locks=["a14"])
    ring1, ring2 = plan.rings[0], plan.rings[1]
    assert _key("a14") in ring1.locked and _key("a14") not in ring1.members
    assert _key("a15") in ring2.members
    assert any(_key("a14") in (l.a, l.b) for l in ring2.conditioners[_key("a15")])


def test_members_are_natural_order_and_only_earlier_or_locked_neighbours_condition():
    _, plan = _grid_plan(["a03", "a41"], locks=["a16"])
    for ring in plan.rings:
        assert ring.members == tuple(sorted(ring.members, key=lambda k: DETS.index(k.rsplit(":", 1)[1])))
        for v, conds in ring.conditioners.items():
            for l in conds:
                u = l.b if l.a == v else l.a
                assert plan.ring_of[u] < ring.index or u == _key("a16")


def test_stale_lock_is_reached_but_never_conditions():
    nodes = scan_nodes(dets=("a03", "a14", "a15"))
    coords = {_key("a03"): point((0, 0, 0)), _key("a14"): point((0, 0.1, 0)), _key("a15"): point((0.1, 0.1, 0))}
    g = build(nodes, coords=coords)
    plan = plan_anchored(g, [_key("a03")], [_key("a14")], [_key("a14")])
    assert plan.ring_of[_key("a14")] == 1
    assert _key("a15") not in plan.ring_of
    assert plan.not_reached[_key("a15")] == "only fitted neighbour is a stale locked fit"
    assert plan.rings[1].members == ()


def test_unreached_nodes_say_why():
    nodes = scan_nodes(dets=("a03", "a04", "a14", "a25"))
    nodes[_key("a25")] = node(SCAN, "a25", eligible=False, reason="held for calibration")
    coords = {_key("a03"): point((0, 0, 0)), _key("a14"): point((0, 0.1, 0))}     # a04 has no accepted hkl
    g = build(nodes, coords=coords)
    plan = plan_anchored(g, [_key("a03")], [], [])
    assert plan.not_reached == {_key("a04"): "no accepted hkl", _key("a25"): "held for calibration"}
    assert set(plan.ring_of) == {_key("a03"), _key("a14")}


def test_ineligible_and_unknown_anchors_are_ignored_with_a_reason():
    nodes = scan_nodes(dets=("a03", "a04"))
    nodes[_key("a04")] = node(SCAN, "a04", eligible=False, reason="declared dead")
    g = build(nodes, coords=grid_coords(dets=("a03", "a04")))
    plan = plan_anchored(g, [_key("a04"), "E:99:a03"], [])
    assert plan.anchors == ()
    assert plan.ignored_anchors == {_key("a04"): "declared dead", "E:99:a03": "not in the batch's scope"}
    assert plan.rings == ()

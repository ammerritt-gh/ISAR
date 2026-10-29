"""The batch preview: the line shown before Start and the reason Start is disabled (C-graph, preview.py)."""
from __future__ import annotations

from inter_synthetic import SCAN, build, grid_coords, node, scan_nodes
from isar.session.inter_spectra.nodes import NodeSet
from isar.session.inter_spectra.preview import preview_anchored, preview_simultaneous
from isar.session.inter_spectra.rings import plan_anchored
from isar.session.inter_spectra.schedule import plan_simultaneous


def _k(det, scan=SCAN):
    return f"{scan}:{det}"


def test_one_anchor_in_one_scan_previews_its_size():
    g = build(scan_nodes(), coords=grid_coords())
    p = preview_anchored(g, plan_anchored(g, [_k("a09")], ()), "q")
    assert p.text == "1 anchor · spread in q · 28 spectra · 9 rings"
    assert p.can_start and p.blocked is None
    assert (p.n_spectra, p.n_rings, p.n_refit, p.n_not_reached) == (28, 9, 27, 0)


def test_two_anchors_in_two_scans_spread_over_56_spectra():
    nodes = {**scan_nodes("E:1"), **scan_nodes("E:2")}
    coords = {**grid_coords("E:1"), **grid_coords("E:2")}
    g = build(nodes, coords=coords)
    p = preview_anchored(g, plan_anchored(g, ["E:1:a03", "E:2:a03"], ()), "q")
    assert p.text == "2 anchors · spread in q · 56 spectra · 9 rings"


def test_spread_in_t_from_one_analyzer_of_three_temperatures():
    nodes, slots = {}, {"E:1": 100.0, "E:2": 150.0, "E:3": 200.0}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, dets=("a03",), series="A", T=T))
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3")}, slots=slots, repeats=())
    g = build(nodes, ns, kinds=("T",))
    p = preview_anchored(g, plan_anchored(g, ["E:2:a03"], ()), "T")
    assert p.text == "1 anchor · spread in T · 3 spectra · 1 ring" and p.can_start


def test_no_coordinates_blocks_a_q_spread_and_says_why():
    g = build(scan_nodes(), coords={})
    p = preview_anchored(g, plan_anchored(g, [_k("a09")], ()), "q")
    assert not p.can_start and "accepted hkl" in p.blocked


def test_no_anchor_in_the_list_blocks():
    g = build(scan_nodes(), coords=grid_coords())
    p = preview_anchored(g, plan_anchored(g, [], ()), "q")
    assert not p.can_start and p.blocked == "no anchor in the list"


def test_only_ineligible_anchors_block_with_their_reason():
    nodes = scan_nodes()
    nodes[_k("a09")] = node(SCAN, "a09", eligible=False, reason="held for calibration")
    g = build(nodes, coords=grid_coords())
    p = preview_anchored(g, plan_anchored(g, [_k("a09")], ()), "q")
    assert not p.can_start and "held for calibration" in p.blocked


def test_q_and_t_without_q_links_is_allowed_with_a_note():
    nodes = {}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, dets=("a03",), series="A", T=T))
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3")},
                 slots={"E:1": 100.0, "E:2": 150.0, "E:3": 200.0}, repeats=())
    g = build(nodes, ns, kinds=("q", "T"), coords={})
    p = preview_anchored(g, plan_anchored(g, ["E:2:a03"], ()), "qT")
    assert p.can_start and any("q-links unavailable" in n for n in p.notes)


def test_simultaneous_preview_counts_terms_and_groups():
    g = build(scan_nodes(), coords=grid_coords())
    plan = plan_simultaneous(g, ())
    p = preview_simultaneous(g, plan)
    assert p.text == f"28 spectra · 0 T-links · 34 curvature terms · {len(plan.groups)} groups"
    assert p.can_start and (p.n_terms, p.n_groups) == (34, len(plan.groups))


def test_simultaneous_selection_with_no_links_blocks():
    g = build(scan_nodes(), coords={})
    p = preview_simultaneous(g, plan_simultaneous(g, ()))
    assert not p.can_start and p.blocked.startswith("the selection has no linked spectra")

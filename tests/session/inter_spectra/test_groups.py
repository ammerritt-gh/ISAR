"""The simultaneous plan: terms, held and free spectra, and conflict-free parallel groups (C-graph, schedule.py)."""
from __future__ import annotations

import random
from dataclasses import replace

import pytest

from inter_synthetic import DETS, SCAN, build, grid_coords, node, scan_nodes
from isar.session.inter_spectra.nodes import NodeSet
from isar.session.inter_spectra.schedule import check_groups, plan_simultaneous


def _k(det, scan=SCAN):
    return f"{scan}:{det}"


def test_full_grid_groups_are_conflict_free_and_few():
    plan = plan_simultaneous(build(scan_nodes(), coords=grid_coords()), locks=())
    assert set(plan.free) == {_k(d) for d in DETS} and len(plan.terms) == 34
    check_groups(plan)
    assert 1 < len(plan.groups) <= 6                    # the toy colouring gives 4; a sanity bound only


def test_q_and_t_terms_group_without_conflict():
    nodes, coords = {}, {}
    for scan, T in (("E:1", 100.0), ("E:2", 150.0), ("E:3", 200.0)):
        nodes.update(scan_nodes(scan, series="A", T=T))
        coords.update(grid_coords(scan))
    ns = NodeSet(nodes=nodes, series={"A": ("E:1", "E:2", "E:3")},
                 slots={"E:1": 100.0, "E:2": 150.0, "E:3": 200.0}, repeats=())
    plan = plan_simultaneous(build(nodes, ns, kinds=("q", "T"), coords=coords), locks=())
    assert sum(t.kind == "T-change" for t in plan.terms) == 2 * 28
    check_groups(plan)


def test_a_grouping_that_puts_term_members_together_is_refused():
    plan = plan_simultaneous(build(scan_nodes(), coords=grid_coords()), locks=())
    a03, a05 = _k("a03"), _k("a05")                     # both ends of the curvature term a03-a04-a05
    bad = replace(plan, groups=((a03, a05),) + tuple((k,) for k in plan.free if k not in (a03, a05)))
    with pytest.raises(AssertionError):
        check_groups(bad)


def _refit(values, reads, s):
    return 0.5 * values[s] + 0.5 * sum(values[t] for t in reads[s]) / len(reads[s])


def _parallel(values, reads, group):
    return {**values, **{s: _refit(values, reads, s) for s in group}}     # every refit reads the group-start state


def _sequential(values, reads, order):
    cur = dict(values)
    for s in order:
        cur[s] = _refit(cur, reads, s)
    return cur


def test_parallel_groups_equal_sequential_refits_and_a_violation_does_not():
    plan = plan_simultaneous(build(scan_nodes(), coords=grid_coords()), locks=())
    rng = random.Random(0)
    members = {m for t in plan.terms for m in t.members}
    values = {k: rng.random() for k in members}
    reads = {s: sorted({m for i in plan.terms_of[s] for m in plan.terms[i].members} - {s}) for s in plan.free}
    for group in plan.groups:
        par = _parallel(values, reads, group)
        for order in (group, tuple(reversed(group))):
            seq = _sequential(values, reads, order)
            assert all(par[s] == pytest.approx(seq[s]) for s in group)
    pair = (_k("a03"), _k("a05"))                        # one term holds both: each refit reads the other
    par, seq = _parallel(values, reads, pair), _sequential(values, reads, pair)
    assert any(par[s] != pytest.approx(seq[s]) for s in pair)


def test_locked_neighbour_is_held_and_ineligible_spectra_are_named():
    nodes = scan_nodes()
    nodes[_k("a09")] = node(SCAN, "a09", eligible=False, reason="declared dead")
    plan = plan_simultaneous(build(nodes, coords=grid_coords()), locks=[_k("a04")])
    assert plan.held == (_k("a04"),)
    assert _k("a04") not in plan.free and _k("a04") not in plan.not_coupled
    assert plan.not_coupled[_k("a09")] == "ineligible: declared dead"

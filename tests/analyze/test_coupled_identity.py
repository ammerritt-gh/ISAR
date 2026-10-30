"""Branch identity and track diagnostics (S1U3): the draft's six synthetic checks, built at test time.

The drift numbers are calculated expectations of the draft, not measurements.
"""
from __future__ import annotations

import itertools
import math
import random
from pathlib import Path

import pytest

import isar
from isar.analyze.coupled import identity as ident
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Assignment, Dormant, Held, HeldMode
from isar.session.inter_spectra.graph import Link

REPO = Path(__file__).resolve().parents[2]
S = InterSettings()


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


# ---- builders -----------------------------------------------------------------------------------
def held(unit, fit, modes, dormant=()):
    """modes: [(branch, omega0)] ascending."""
    hm = tuple(HeldMode(branch=b, omega0=e, gamma=1.0, height=1.0, guard=0.1, guard_source="solo",
                        status="determined_by_own_data", regime="underdamped", pinned=False) for b, e in modes)
    return Held(unit=unit, fit=fit, inputs=None, window=(0.0, 30.0), modes=hm, dormant=tuple(dormant), T=None)


def asg(unit, sid, slots, origins=None, *, cands=None, changed=False, discrete="priced"):
    slots = tuple(slots)
    origins = tuple(origins or ("matched" if b else "new_branch" for b in slots))
    if cands is None:
        cands = [(slots, 0.0)]
    candidates = tuple({"count": len(s), "slots": tuple(s), "ln_z": z, "status": "ok", "weight": None}
                       for s, z in cands)
    return Assignment(unit=unit, snapshot_id=sid, count=len(slots), slots=slots, origins=origins, factors=(),
                      discrete=discrete, triggers=(), candidates=candidates, coverage={},
                      search_limited=False, changed=changed, reasons=())


def refs(unit, n):
    return tuple((unit, f"f_{unit}", m) for m in range(n))


def link(a, b, kind="T"):
    return Link(kind=kind, a=a, b=b, step=1.0, dq=None)


def run(stage, links, assignments, previous, *, mode="anchored", rings=None, sid="b:ring1", settings=S,
        energies=None):
    if mode == "anchored" and rings is None:
        rings = {k: 1 for k in stage}
    energies = energies or {k: [10.0 + m for m in range(assignments[k].count)] for k in assignments}
    return ident.propagate_ids(stage, links, {k: refs(k, assignments[k].count) for k in assignments}, settings,
                               batch_mode=mode, rings=rings, snapshot_id=sid, assignments=assignments,
                               previous=previous, energies=energies)


# ---- 1. crossings, permutations, completion order ------------------------------------------------
@pytest.mark.parametrize("cands", [
    [(("p", "q"), 0.0), (("q", "p"), 0.0)],          # exchange-symmetric, energy-only
    [(("q", "p"), 0.0), (("p", "q"), -0.5)],         # steep crossing: the nearest-energy exchange is close
])
def test_unresolved_crossing_stays_ambiguous(cands):
    A = held("A", "fA", [("p", 10.0), ("q", 10.4)])
    a = asg("k", "b:ring1", cands[0][0], cands=cands)
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A})
    for ref in refs("k", 2):
        assert r.status[ref] == "ambiguous" and r.ids[ref] is None
    assert r.flags["identity_ambiguous"]["severity"] == "serious"
    assert r.flags["identity_ambiguous"]["units"] == ["k"]
    kept = r.alternatives["by_unit"]["k"]["close"]
    assert {tuple(c["slots"]) for c in kept[0]} == {cands[1][0]}


def test_distinguishable_crossing_keeps_ids_when_energy_order_reverses():
    # Neighbour: p below q. Locally the order has reversed and the core's evidence says so clearly.
    A = held("A", "fA", [("p", 10.0), ("q", 12.0)])
    a = asg("k", "b:ring1", ("q", "p"), cands=[(("q", "p"), 0.0), (("p", "q"), -10.0)])
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A})
    assert [r.ids[ref] for ref in refs("k", 2)] == ["q", "p"]
    assert all(r.status[ref] == "matched" for ref in refs("k", 2))
    assert r.flags == {}


def _square(z_slots=("fast:W#0", "fast:W#1"), z_cands=None):
    """Simultaneous 2x2 loop W-X-Y-Z-W, every member a fast solo with its own fast ids."""
    units = ["W", "X", "Y", "Z"]
    previous = {u: held(u, "fast", [(f"fast:{u}#0", 10.0), (f"fast:{u}#1", 14.0)]) for u in units}
    nxt = {"W": "X", "X": "Y", "Y": "Z"}
    A = {u: asg(u, "b:round1", (f"fast:{v}#0", f"fast:{v}#1")) for u, v in nxt.items()}
    A["Z"] = asg("Z", "b:round1", z_slots, cands=z_cands)
    links = [link("W", "X", "q"), link("X", "Y", "q"), link("Y", "Z", "q"), link("Z", "W", "q")]
    return units, links, A, previous


def test_completion_order_changes_neither_ids_nor_confidence():
    units, links, A, previous = _square()
    base = run(units, links, A, previous, mode="simultaneous", rings=None, sid="b:round1")
    rng = random.Random(7)
    for _ in range(12):
        order = units[:]
        rng.shuffle(order)
        lk = links[:]
        rng.shuffle(lk)
        shuffled_A = {u: A[u] for u in order}
        shuffled_prev = {u: previous[u] for u in reversed(order)}
        again = run(order, lk, shuffled_A, shuffled_prev, mode="simultaneous", rings=None, sid="b:round1")
        assert again == base


# ---- 2. loop consistency ------------------------------------------------------------------------
def test_consistent_loop_shares_ids_and_keeps_old_ids_interpretable():
    units, links, A, previous = _square()
    r = run(units, links, A, previous, mode="simultaneous", rings=None, sid="b:round1")
    assert r.loops == [] and r.flags == {}
    assert {r.ids[(u, f"f_{u}", 0)] for u in units} == {"fast:W#0"}
    assert {r.ids[(u, f"f_{u}", 1)] for u in units} == {"fast:W#1"}
    # older records' ids stay readable: each maps to the display id through the alias record
    assert r.alternatives["aliases"]["fast:W#0"] == ["fast:X#0", "fast:Y#0", "fast:Z#0"]


def test_fast_solos_that_name_each_other_resolve_to_one_id():
    """Simultaneous from fast solos: each spectrum's own id is unknown to its neighbour, so stage 1 changes each
    slot from unmatched to the neighbour's id (x takes fast:y#0, y takes fast:x#0). Each slot continues its own
    incoming mode, so the two ids meet in one component instead of swapping; a slot that relabels away from an
    id its neighbour carries is a real change and continues nothing."""
    previous = {u: held(u, "fast", [(f"fast:{u}#0", 10.0)]) for u in "xy"}
    A = {"x": asg("x", "b:round1", ("fast:y#0",), changed=True), "y": asg("y", "b:round1", ("fast:x#0",), changed=True)}
    r = run(["x", "y"], [link("x", "y")], A, previous, mode="simultaneous", sid="b:round1")
    assert r.ids[("x", "f_x", 0)] == r.ids[("y", "f_y", 0)] == "fast:x#0"
    assert {r.status[("x", "f_x", 0)], r.status[("y", "f_y", 0)]} == {"matched"}
    assert r.alternatives["aliases"] == {"fast:x#0": ["fast:y#0"]} and r.loops == [] and r.flags == {}
    # the other direction: y carries x's own id, so x naming p instead is a relabel; x's old id is not aliased
    previous = {"x": held("x", "fast", [("q", 10.0)]), "y": held("y", "fast", [("p", 9.0), ("q", 10.0)])}
    A = {"x": asg("x", "b:round1", ("p",), changed=True), "y": asg("y", "b:round1", ("p", "q"))}
    r = run(["x", "y"], [link("x", "y")], A, previous, mode="simultaneous", sid="b:round1")
    assert r.ids[("x", "f_x", 0)] == "p" and r.alternatives["aliases"] == {}


def test_loop_conflict_reports_its_cycle_and_publishes_no_union():
    # Z's incoming association is crossed relative to the rest of the loop.
    units, links, A, previous = _square(z_slots=("fast:W#1", "fast:W#0"))
    r = run(units, links, A, previous, mode="simultaneous", rings=None, sid="b:round1")
    assert len(r.loops) == 1
    loop = r.loops[0]
    a, b = loop["collision"]
    assert a[:2] == b[:2] and a[2] != b[2]                   # two modes of one fit fell together
    walked = {frozenset(l) for step in loop["witness"] for l in step["links"]}
    assert walked == {frozenset(p) for p in [("W", "X"), ("X", "Y"), ("Y", "Z"), ("Z", "W")]}
    assert all(r.status[ref] == "ambiguous" for u in units for ref in refs(u, 2))
    assert r.flags["loop_inconsistent"]["severity"] == "serious"
    assert r.flags["loop_inconsistent"]["units"] == units


def test_low_weight_conflicting_proposal_creates_no_union():
    units, links, A, previous = _square(
        z_slots=("fast:W#1", "fast:W#0"),
        z_cands=[(("fast:W#1", "fast:W#0"), 0.0), (("fast:W#0", "fast:W#1"), -0.5)])
    r = run(units, links, A, previous, mode="simultaneous", rings=None, sid="b:round1")
    assert r.loops == []
    assert [r.status[ref] for ref in refs("Z", 2)] == ["ambiguous", "ambiguous"]
    assert r.flags["identity_ambiguous"]["units"] == ["Z"]
    assert {r.ids[(u, f"f_{u}", 0)] for u in "WXY"} == {"fast:W#0"}  # the rest is unaffected
    assert r.alternatives["by_unit"]["Z"]["close"][0][0]["slots"] == ("fast:W#0", "fast:W#1")


# ---- 3. fade and return -------------------------------------------------------------------------
def test_faded_branch_returns_with_its_id_over_an_eligible_path():
    A = held("A", "fA", [("p", 10.0), ("r", 20.0)])                  # anchor, coldest
    ring1 = run(["B"], [link("A", "B")], {"B": asg("B", "b:ring1", ("r",))}, {"A": A})
    assert ring1.absent["B"] == [{"branch": "p", "last_seen_fit": "fA", "last_seen_unit": "A",
                                  "status": "not_observed_here"}]
    assert ring1.flags == {}                                         # not a death
    rec = ring1.absent["B"][0]
    B = held("B", "f_B", [(ring1.ids[("B", "f_B", 0)], 20.0)],
             dormant=[Dormant(branch=rec["branch"], last_seen_fit=rec["last_seen_fit"],
                              last_seen_unit=rec["last_seen_unit"])])
    C = asg("C", "b:ring2", ("p", "r"), ("returning", "matched"))
    ring2 = run(["C"], [link("A", "B"), link("B", "C")], {"C": C}, {"A": A, "B": B},
                rings={"C": 2}, sid="b:ring2")
    assert ring2.ids[("C", "f_C", 0)] == "p" and ring2.status[("C", "f_C", 0)] == "matched"
    assert ring2.flags == {}


def test_return_without_eligible_support_is_unresolved_not_a_birth():
    A = held("A", "fA", [("p", 10.0), ("r", 20.0)])
    B = held("B", "f_B", [("r", 20.0)])                              # the dormant record is gone
    C = asg("C", "b:ring2", ("p", "r"), ("returning", "matched"))
    r = run(["C"], [link("A", "B"), link("B", "C")], {"C": C}, {"A": A, "B": B}, rings={"C": 2}, sid="b:ring2")
    ref = ("C", "f_C", 0)
    assert r.status[ref] == "unresolved_return" and r.ids[ref] is None
    assert r.flags["unresolved_return"]["severity"] == "serious"
    assert "new_branch" not in r.status.values()
    assert r.ids[("C", "f_C", 1)] == "r"


# ---- 4. cumulative T drift ----------------------------------------------------------------------
def _segment(guard_ends=0.0):
    obs = [{"unit": f"s{i}", "T": 100.0 + 10.0 * i, "omega0": 10.0 + 0.4 * i, "guard": 0.0} for i in range(11)]
    obs[0]["guard"] = obs[-1]["guard"] = guard_ends
    return {"analyzer": "a03", "branch": "p", "observations": obs}


def test_drift_flags_the_endpoint_while_every_step_is_quiet():
    out = ident.drift_report([_segment()], S)
    seg = out["segments"][0]
    assert seg["max_step_U"] == pytest.approx(0.5 * 12 * (0.4 / 0.1 ** 0.25) ** 2)  # ~3.04
    assert seg["max_step_U"] < S.flag_threshold
    assert seg["U"] == pytest.approx(96.0) and seg["dE"] == pytest.approx(4.0) and seg["dT"] == 100.0
    assert seg["flagged"] and out["flags"]["t_drift_cumulative"]["severity"] == "warn"
    # the threshold is the setting, not a literal
    assert not ident.drift_report([_segment()], InterSettings.from_values({"flag_threshold": 100.0}))[
        "segments"][0]["flagged"]
    # broader endpoint guards lower the score
    assert ident.drift_report([_segment(0.5)], S)["segments"][0]["U"] < seg["U"]
    quartic = ident.drift_report([_segment()], InterSettings.from_values({"shape_family": "quartic"}))
    assert quartic["segments"][0]["U"] == pytest.approx(12 * 4.0 ** 4)


def test_drift_is_unavailable_or_qualified_when_it_cannot_be_judged():
    repeats = {"analyzer": "a03", "branch": "p", "observations": [
        {"unit": "s0", "T": 100.0, "omega0": 10.0, "guard": 0.1},
        {"unit": "s1", "T": 100.0, "omega0": 14.0, "guard": 0.1}]}
    unsure = _segment()
    unsure["observations"][5]["status"] = "ambiguous"
    no_guard = _segment()
    no_guard["observations"][0]["guard"] = None
    out = ident.drift_report([repeats, unsure, no_guard], S)["segments"]
    assert [s["status"] for s in out] == ["unavailable", "unavailable", "qualified"]
    assert not out[0]["flagged"] and not out[1]["flagged"]


# ---- 5. same-ring seams -------------------------------------------------------------------------
def _ring(previous_extra=None):
    A = held("A", "fA", [("p", 10.0)])
    a = asg("a", "b:ring1", ("p", "s"), ("matched", "matched"))       # "s" is carried only by ring-mate b
    b = asg("b", "b:ring1", ("p", None))
    previous = {"A": A, **(previous_extra or {})}
    links = [link("A", "a", "q"), link("A", "b", "q"), link("a", "b", "q")]
    return {"a": a, "b": b}, previous, links


def test_a_ring_mate_never_supports_an_identity():
    A, previous, links = _ring({"b": held("b", "fast", [("p", 10.0), ("s", 15.0)])})
    r = run(["a", "b"], links, A, previous)
    assert r.status[("a", "f_a", 1)] == "ambiguous"
    assert {"link": ("a", "b"), "issue": "identity_unresolved", "modes": [("a", "f_a", 1)]} in r.seams


def test_independent_births_across_a_seam_are_flagged_not_merged():
    A, previous, links = _ring()
    A["a"] = asg("a", "b:ring1", ("p", None))
    r = run(["b", "a"], links, A, previous)
    born = [("a", "f_a", 1), ("b", "f_b", 1)]
    assert [r.ids[x] for x in born] == ["f_a#1", "f_b#1"]
    assert r.flags["seam_disagreement"]["severity"] == "warn"
    assert r.seams == [{"link": ("a", "b"), "issue": "independent_births", "modes": born,
                        "ids": ["f_a#1", "f_b#1"]}]
    assert run(["a", "b"], links[::-1], dict(reversed(A.items())), previous) == r


def test_births_far_apart_across_a_seam_are_not_a_disagreement():
    # both ring-mates mint a branch, but 5 and 25 meV apart: nothing shows the two are one mode
    A, previous, links = _ring()
    A["a"] = asg("a", "b:ring1", ("p", None))
    r = run(["a", "b"], links, A, previous, energies={"a": [10.0, 5.0], "b": [10.0, 25.0]})
    assert [r.ids[x] for x in [("a", "f_a", 1), ("b", "f_b", 1)]] == ["f_a#1", "f_b#1"]
    assert r.flags == {} and r.seams == []


# ---- 6. identity and count bookkeeping ----------------------------------------------------------
def _local(n):
    return [ident.ModeObs(ref=("k", "fk", m), omega0=10.0 + m, gamma=1.0, height=1.0, sigma_guard=None,
                          sigma_provenance={}, branch=None) for m in range(n)]


def _brute(n, pool, cost, fixed):
    best = {}
    for slots in itertools.product([None, *pool], repeat=n):
        named = [b for b in slots if b]
        if len(set(named)) != len(named):
            continue
        c = sum(cost(m, b) for m, b in enumerate(slots))
        comp = (tuple(sorted(named)), slots.count(None))
        if comp not in best or (c, slots) < best[comp]:
            best[comp] = (c, slots)
    return sorted((c, comp) for comp, (c, s) in best.items() if comp not in fixed)


@pytest.mark.parametrize("seed", range(6))
def test_match_link_ranks_like_full_enumeration(seed):
    rng = random.Random(seed)
    pool = ["p", "q", "r", "s"]
    table = {(m, b): rng.uniform(0, 12) for m in range(3) for b in [None, *pool]}
    cost = lambda m, b: table[(m, b)]
    out = ident.match_link(_local(3), pool, cost, incoming=("p", None, "q"))
    fixed = {(("p", "q"), 1), ((), 3)}
    want = _brute(3, pool, cost, fixed)
    got = [(p["cost"], p["composition"]) for p in out["proposals"] if p["source"] == "ranked"]
    assert [comp for _, comp in got] == [comp for _, comp in want[:4]]
    assert [c for c, _ in got] == pytest.approx([c for c, _ in want[:4]])
    assert [p["source"] for p in out["proposals"][:2]] == ["incoming", "all_unmatched"]
    full = ident.match_link(_local(3), pool, cost, keep=None)
    assert not full["coverage"]["search_limited"]
    assert len({p["composition"] for p in full["proposals"]}) == full["coverage"]["compositions_total"]
    assert out["weight_unavailable"] and out["coverage"]["search_limited"]


@pytest.mark.parametrize("seed", range(4))
def test_match_link_orders_match_full_enumeration(seed):
    rng = random.Random(seed)
    pool = ["p", "q", "r"]
    table = {(m, b): rng.uniform(0, 4) for m in range(3) for b in [None, *pool]}
    out = ident.match_link(_local(3), pool, lambda m, b: table[(m, b)], incoming=("p", "q", None), keep=None,
                           close=S.close_call_nats)
    every = {s: sum(table[(m, b)] for m, b in enumerate(s)) for s in itertools.product([None, *pool], repeat=3)
             if len({b for b in s if b}) == sum(b is not None for b in s)}
    for p in out["proposals"]:
        found = {s: c for s, c in every.items() if ident._composition(s) == p["composition"]}
        low = min(found.values())
        assert {o["slots"] for o in p["orders"]} == {s for s, c in found.items() if c <= low + 2 * S.close_call_nats}


def test_duplicate_and_absent_branches_cost_nothing_extra():
    table = {(0, None): 5.0, (0, "p"): 1.0, (0, "q"): 3.0, (1, None): 7.0, (1, "p"): 2.0, (1, "q"): 9.0}
    out = ident.match_link(_local(2), ["p", "p", "q"], lambda m, b: table[(m, b)], keep=None)
    assert out["coverage"]["compositions_total"] == 4               # {}, {p}, {q}, {p,q}: p counted once
    by = {p["composition"]: p for p in out["proposals"]}
    assert by[(("p",), 1)]["cost"] == 5.0 + 2.0                      # cheapest association; q absent adds nothing
    assert by[(("p",), 1)]["slots"] == (None, "p")
    assert by[((), 2)]["cost"] == 12.0                               # unmatched modes pay the core's entry cost
    assert ident.match_link(_local(2), ["p"], lambda m, b: 0.0, incoming=("q", None))["incoming_invalid"]


def test_close_slot_orders_of_a_composition_are_kept_for_pricing():
    # a crossing: (p, q) and its exchange (q, p) are one composition, 1.5 apart; the far order is not kept
    table = {(0, "p"): 0.0, (0, "q"): 1.0, (1, "p"): 0.5, (1, "q"): 0.0, (0, None): 10.0, (1, None): 10.0}
    cost = lambda m, b: table[(m, b)]
    out = ident.match_link(_local(2), ["p", "q"], cost, keep=None, close=S.close_call_nats)
    pq = next(p for p in out["proposals"] if p["composition"] == (("p", "q"), 0))
    assert pq["slots"] == ("p", "q")
    assert [(o["slots"], o["cost"]) for o in pq["orders"]] == [(("p", "q"), 0.0), (("q", "p"), 1.5)]
    # close is nats: 0.5 nats is a 1.0 deviance window, too narrow for the 1.5 exchange
    tight = ident.match_link(_local(2), ["p", "q"], cost, keep=None, close=0.5)
    pq = next(p for p in tight["proposals"] if p["composition"] == (("p", "q"), 0))
    assert [o["slots"] for o in pq["orders"]] == [("p", "q")]


def test_exchanged_order_within_the_close_window_reaches_the_core():
    # the exchange of (p, q) is 3 deviance units (1.5 nats) behind the cheapest order: inside close_call_nats = 2
    table = {(0, "p"): 0.0, (0, "q"): 1.5, (1, "p"): 1.5, (1, "q"): 0.0, (0, None): 10.0, (1, None): 10.0}
    out = ident.match_link(_local(2), ["p", "q"], lambda m, b: table[(m, b)], keep=None, close=S.close_call_nats)
    pq = next(p for p in out["proposals"] if p["composition"] == (("p", "q"), 0))
    assert {(o["slots"], o["cost"]) for o in pq["orders"]} == {(("p", "q"), 0.0), (("q", "p"), 3.0)}


def test_recovered_branch_is_matched_and_only_a_true_newcomer_is_born():
    A1 = held("A1", "fA1", [("p", 10.0)])
    A2 = held("A2", "fA2", [("p", 10.1)])                           # a duplicate observation of p
    k = asg("k", "b:ring1", ("p", None), ("matched", "new_branch"))  # split: p recovered, plus a newcomer
    r = run(["k"], [link("A1", "k"), link("A2", "k")], {"k": k}, {"A1": A1, "A2": A2})
    assert r.ids[("k", "f_k", 0)] == "p" and r.status[("k", "f_k", 0)] == "matched"
    assert r.ids[("k", "f_k", 1)] == "f_k#1" and r.status[("k", "f_k", 1)] == "new_branch"
    assert [m.branch for m in r.alternatives["by_unit"]["k"]["matches"]] == ["p"]   # one assignable branch


def test_invalid_input_gives_a_diagnostic_and_no_ids():
    A = held("A", "fA", [("p", 10.0)])
    stale = asg("k", "b:ring0", ("p",))
    r = run(["k"], [link("A", "k")], {"k": stale}, {"A": A})
    assert "k" in r.alternatives["invalid"] and r.ids == {}


def test_new_branch_id():
    assert ident.new_branch_id("f0123456789ab", 2) == "f0123456789ab#2"
    assert ident.new_branch_id("fast:e1:7:a03", 0) == "fast:e1:7:a03#0"
    with pytest.raises(ValueError):
        ident.new_branch_id("fast", 0)


def test_untriggered_assignment_keeps_its_own_ids_without_a_neighbour_carrying_them():
    # first batch: fast solo ids are per spectrum, so no neighbour carries k's modes; skipped = inherited
    A = held("A", "fA", [("fast:A#0", 10.0)])
    k = held("k", "fast", [("fast:k#0", 10.0), ("fast:k#1", 14.0)])
    a = asg("k", "b:round1", ("fast:k#0", "fast:k#1"), discrete="skipped")
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A, "k": k}, mode="simultaneous", rings=None, sid="b:round1")
    assert [r.ids[ref] for ref in refs("k", 2)] == ["fast:k#0", "fast:k#1"]
    assert [r.status[ref] for ref in refs("k", 2)] == ["matched", "matched"]
    assert r.flags == {}


def test_an_unmatched_mode_keeps_its_own_incoming_id_across_a_count_change():
    # simultaneous: k's own fast:k#0 is carried by no neighbour; neighbours tip k from one mode to two
    A = held("A", "fA", [("p", 12.0)])
    k = held("k", "fast", [("fast:k#0", 10.0)])
    two = run(["k"], [link("A", "k")], {"k": asg("k", "b:round1", (None, "p"), changed=True)},
              {"A": A, "k": k}, mode="simultaneous", rings=None, sid="b:round1")
    assert [two.ids[ref] for ref in refs("k", 2)] == ["fast:k#0", "p"]
    assert two.flags == {}
    three = run(["k"], [link("A", "k")], {"k": asg("k", "b:round1", (None, "p", None), changed=True)},
                {"A": A, "k": k}, mode="simultaneous", rings=None, sid="b:round1")
    assert [three.ids[ref] for ref in refs("k", 3)] == ["fast:k#0", "p", "f_k#2"]   # index 2 had no incoming id


def test_a_carried_mode_keeps_its_id_when_a_lower_mode_is_matched():
    # k's incoming mode sits high; neighbours tip k to two and match the new, lower mode to p (index 0)
    A = held("A", "fA", [("p", 10.0)])
    k = held("k", "fast", [("fast:k#0", 14.0)])
    a = asg("k", "b:round1", ("p", None), changed=True)
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A, "k": k}, mode="simultaneous", rings=None,
            sid="b:round1", energies={"k": [10.0, 14.0]})
    assert [r.ids[ref] for ref in refs("k", 2)] == ["p", "fast:k#0"]
    assert r.flags == {}


def test_an_anchored_unmatched_mode_keeps_its_incoming_id():
    # the ring member's own Held is passed in previous (S1U6); its high mode keeps fast:k#0 as neighbours tip k
    A = held("A", "fA", [("p", 10.0)])
    k = held("k", "fast", [("fast:k#0", 14.0)])
    a = asg("k", "b:ring1", ("p", None), changed=True)
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A, "k": k}, mode="anchored", rings={"k": 1},
            energies={"k": [10.0, 14.0]})
    assert [r.ids[ref] for ref in refs("k", 2)] == ["p", "fast:k#0"]
    assert [r.status[ref] for ref in refs("k", 2)] == ["matched", "new_branch"]
    assert r.flags == {}


def test_a_confident_winner_with_a_close_rival_stays_confident():
    # p = 0.86 >= 0.80 and a rival 1.8 nats behind: a close call goes to review, it does not make the slot ambiguous
    A = held("A", "fA", [("p", 10.0), ("q", 12.0)])
    a = asg("k", "b:ring1", ("p", "q"), cands=[(("p", "q"), 0.0), (("q", "p"), -1.8)])
    r = run(["k"], [link("A", "k")], {"k": a}, {"A": A})
    assert [r.ids[ref] for ref in refs("k", 2)] == ["p", "q"]
    assert [r.status[ref] for ref in refs("k", 2)] == ["matched", "matched"]
    assert r.flags == {}
    assert r.alternatives["by_unit"]["k"]["close"]          # kept for review


def test_each_spectrum_reads_only_its_own_identity_flag_values():
    # two ring-mates each with an ambiguous slot: one stage-wide flag, but each spectrum names only its own slot
    from isar.session.inter_spectra import runner
    A = held("A", "fA", [("p", 10.0), ("q", 12.0)])
    tie = lambda s, t: asg(s, "b:ring1", (t,), cands=[((t,), 0.0), (("q" if t == "p" else "p",), 0.0)])
    r = run(["k1", "k2"], [link("A", "k1"), link("A", "k2")], {"k1": tie("k1", "p"), "k2": tie("k2", "q")},
            {"A": A}, rings={"k1": 1, "k2": 1})
    assert set(r.flags["identity_ambiguous"]["units"]) == {"k1", "k2"}
    (f1,), (f2,) = (runner._Run.identity_flags(None, r, k) for k in ("k1", "k2"))
    assert "\x27p\x27" in f1["text"] and "\x27q\x27" not in f1["text"]
    assert "\x27q\x27" in f2["text"] and "\x27p\x27" not in f2["text"]


def test_dormant_records_follow_the_merged_ids():
    # reconciliation merged q and s into p, and earlier o into q: y's absent records name q, s and t; it observes p
    # and r. Dormant keeps one record per canonical branch and none for a branch y observes.
    from types import SimpleNamespace
    from isar.session.inter_spectra import runner
    aliases = ident.merged_aliases({"o": "q"}, {"q": "p", "s": "p"})
    assert aliases == {"o": "p", "q": "p", "s": "p"}
    absent = {"y": [{"branch": b, "last_seen_fit": f"f{b}", "last_seen_unit": "x"} for b in ("q", "s", "t")]}
    me = SimpleNamespace(aliases=aliases, dormant={})
    ids = SimpleNamespace(absent=absent)
    assert [d.branch for d in runner._Run.note_dormant(me, ids, "y", "f1")] == ["p", "t"]
    assert [d.branch for d in runner._Run.note_dormant(me, ids, "y", "f1", ["p", "r"])] == ["t"]


def test_a_held_fit_carries_its_dormant_ids_through_the_batch_rename():
    # the rename merges q and s into p: a held fit that observes p drops its dormant q; one that does not keeps a
    # single dormant p for q and s; an unrelated dormant id is untouched. Without it a later sweep prices the old
    # id q as a returning branch next to the merged p.
    from isar.session.inter_spectra import runner
    d = lambda b: Dormant(b, f"f{b}", "x")      # noqa: E731
    rename = {"q": "p", "s": "p"}
    sees_p = runner._renamed(held("y", "f1", [("p", 5.0)], [d("q"), d("t")]), rename)
    assert [m.branch for m in sees_p.modes] == ["p"] and [x.branch for x in sees_p.dormant] == ["t"]
    unseen = runner._renamed(held("y", "f1", [("r", 5.0)], [d("q"), d("s"), d("t")]), rename)
    assert [x.branch for x in unseen.dormant] == ["p", "t"]
    same = held("y", "f1", [("r", 5.0)], [d("t")])
    assert runner._renamed(same, rename) is same

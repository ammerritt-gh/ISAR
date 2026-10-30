"""The adaptive candidate search (S3U2 tests 1, 4-11, 19, 21, 24) against a scripted pricer, milliseconds each.

Every spectrum is generated at test time and every mass is a made-up table value (``_search_harness``); the real
``Search`` runs on a real ``Snapshot``, so the pool, the seeds and the shift costs are the core's own.
"""
import math
from dataclasses import replace
from math import comb, factorial

import pytest
from _search_harness import Scripted, job_for, run, snapshot_for

from isar.analyze.coupled import refit as R
from isar.analyze.coupled import search as SE
from isar.analyze.coupled.refit import open_search
from isar.analyze.coupled.types import Start
from test_coupled_refit import S

B12 = ("b1", "b2")
OWN = [(5.6, 0.5, 20.0), (10.6, 0.5, 20.0)]
X12 = ([(5.0, 0.5, 20.0), (10.0, 0.5, 20.0)], ["b1", "b2"])


def happy(**kw):
    return snapshot_for(OWN, ["b1", "b2"], {"x": X12}, **kw)


def labels(job):
    """(count, labels in the energy order of the job's first start)."""
    st = job.starts[0]
    order = sorted(range(job.count), key=lambda i: (st.modes[i][0], str(job.slots[i])))
    return job.count, tuple(job.slots[i] for i in order)


def cands(a):
    return {(c["count"], tuple(c["slots"])): c for c in a.candidates if c["ln_z"] is not None}


# 1 (worked examples 1 and 4) -----------------------------------------------------------------------------------
EX1 = {(2, B12): -100.0, (1, ("b1",)): -112.0, (1, ("b2",)): -130.0, (3, ("b1", "b2", None)): -108.0,
       (2, ("b2", "b1")): -106.0, (2, (None, "b2")): -103.0, (2, ("b1", None)): -104.0}


def test_the_happy_path_seeds_steps_both_ways_and_stops_behind_the_margin():
    s, sc = open_search(happy()), Scripted(EX1)
    sc.M = s.M
    first = s.next_jobs()
    assert s.next_jobs() == first                                   # idempotent without a feed
    assert len(first) == 1 and (first[0].count, first[0].slots) == (2, B12)
    own, nb = first[0].starts
    assert (own.family, nb.family) == ("own", "neighbour")
    assert own.modes == tuple(OWN) and nb.modes == ((5.0, 0.5, None), (10.0, 0.5, None))
    s.feed([sc(j) for j in first])
    second = s.next_jobs()
    assert {labels(j) for j in second} == {(1, ("b1",)), (1, ("b2",)), (3, ("b1", "b2", None)), (2, ("b2", "b1")),
                                           (2, (None, "b2")), (2, ("b1", None))}
    assert next(j for j in second if j.count == 3).starts[0].modes[-1] == (20.0, 0.3, 1.0)   # the residual peak
    asked = {j.key for j in first + second}
    s.feed([sc(j) for j in second])
    while not s.done:
        jobs = s.next_jobs()
        assert not asked & {j.key for j in jobs}                    # a key is never asked twice
        asked |= {j.key for j in jobs}
        s.feed([sc(j) for j in jobs])
    assert s.next_jobs() == ()
    a = s.result()
    assert (a.count, a.slots, a.changed, a.discrete) == (2, B12, False, "priced")
    assert a.coverage["stopped"] == {"down": "behind_margin:1", "up": "behind_margin:3"}
    assert a.search["counts"]["1"]["behind_nats"] == pytest.approx(12.07, abs=0.01)
    assert a.search["counts"]["3"]["behind_nats"] == pytest.approx(8.07, abs=0.01)
    dec = a.coverage["decision"]
    assert dec["count_p"]["2"] == pytest.approx(1.0, abs=1e-3) and not dec["ambiguous"]
    assert dec["label_p"] == pytest.approx(0.9341, abs=1e-4) and dec["label_confident"]
    c = cands(a)
    assert c[(2, B12)]["weight_in_count"] == pytest.approx(0.9341, abs=1e-4) and c[(2, B12)]["ln_z"] == -100.0
    assert set(EX1) <= set(c) and c[(2, B12)]["source"] == "seed:incoming"
    assert a.triggers == ("seed:incoming", "seed:neighbour:x")
    t = a.search["tried"]
    assert t["jobs"] == len(asked) and t["failed"] == 0 and t["starts"] == sum(len(j.starts) for j in sc.asked)


def test_the_protocol_refuses_a_wrong_feed():
    s, sc = open_search(happy()), Scripted(EX1)
    sc.M = s.M
    s.feed([sc(j) for j in s.next_jobs()])
    jobs = s.next_jobs()
    with pytest.raises(ValueError):
        s.feed([sc(jobs[0])])                                       # one price for several jobs
    with pytest.raises(ValueError):
        s.feed([sc(j) for j in reversed(jobs)])                     # another order
    with pytest.raises(RuntimeError):
        s.result()                                                  # not done


# 4 seeds -------------------------------------------------------------------------------------------------------
def test_seeds_are_the_incoming_fit_and_each_neighbour_at_its_own_energies():
    snap = snapshot_for(OWN, ["b1", "b2"], {"x": X12, "w": ([(9.5, 0.6, 5.0)], ["b2"])})
    a, _used, s = run(snap, Scripted({(2, B12): -100.0, (1, ("b2",)): -160.0}))
    seed = [j for j, _p in s.outcomes if s.meta[j.key]["purpose"] == "seed"]
    assert {(j.count, j.slots) for j in seed} == {(2, B12), (1, ("b2",))}
    w = next(j for j in seed if j.count == 1)
    assert w.starts[0].modes == ((9.5, 0.6, None),)                 # its own fitted energy, height to the optimizer
    assert cands(a)[(1, ("b2",))]["ln_z"] == -160.0                 # far behind, still priced and recorded
    assert {sd["source"] for sd in a.search["seeds"]} == {"incoming", "neighbour:x", "neighbour:w"}


def test_one_branch_under_two_merged_names_is_one_pool_branch_and_one_seed():
    snap = snapshot_for([(5.6, 0.5, 20.0)], ["b1"], {"x": ([(5.0, 0.5, 20.0)], ["b1"]),
                                                    "w": ([(5.0, 0.5, 20.0)], ["b1x"])}, aliases={"b1x": "b1"})
    s = open_search(snap)
    assert s.pool == ("b1",)
    seeds = s.next_jobs()
    assert [(j.count, j.slots) for j in seeds] == [(1, ("b1",))] and len(seeds[0].starts) == 2


def test_a_curvature_triple_seeds_each_neighbour_separately_never_their_common_branch():
    xm, zm = [(5.0, 0.5, 20.0), (10.0, 0.5, 20.0)], [(10.4, 0.5, 20.0), (15.0, 0.5, 20.0)]
    snap = snapshot_for(OWN, ["b1", "b2"], {"x": X12}, curvature=((xm, ["b1", "b2"]), (zm, ["b2", "b3"])))
    s = open_search(snap)
    assert set(s.pool) == {"b1", "b2", "b3"} and "b3" not in s.spec.factors     # assignable without a factor (D9)
    seeds = {(j.count, j.slots): j for j in s.next_jobs()}
    assert set(seeds) == {(2, B12), (2, ("b2", "b3"))}
    assert seeds[(2, ("b2", "b3"))].starts[0].modes == ((10.4, 0.5, None), (15.0, 0.5, None))
    # x sits in the pair term and the triple: one seed for it
    assert [sd["source"] for sd in s.seeds].count("neighbour:x") == 1


# 5 the stop reads the count's total, after its first wave ------------------------------------------------------
def three_branches():
    return snapshot_for(OWN, ["b1", "b2"], {"x": X12, "z": ([(15.0, 0.5, 20.0)], ["b3"])})


def test_a_step_count_weak_alone_but_carried_by_its_first_wave_keeps_the_walk_going():
    T = {(2, B12): -100.0, (3, ("b1", "b2", "b3")): -106.0}
    a, used, s = run(three_branches(), Scripted(T, default={3: -106.0}))
    assert any(p.count == 4 for p in used)                          # the walk went on to count 4
    assert a.coverage["stopped"]["up"] != "behind_margin:3" and a.count == 2
    assert a.search["counts"]["3"]["ln_total"] >= -106.0 + math.log(4)


def test_a_step_count_whose_first_wave_is_unproductive_stops_after_exactly_one_wave():
    T = {(2, B12): -100.0, (3, ("b1", "b2", "b3")): -106.0}
    a, used, s = run(three_branches(), Scripted(T))
    assert a.coverage["stopped"]["up"] == "behind_margin:3" and not any(p.count == 4 for p in used)
    assert [len(ks["waves"]) for key, ks in s.keys.items() if key[0] == 3] == [1]


# 6 unresolved steps --------------------------------------------------------------------------------------------
def test_unresolved_steps_end_their_direction_and_are_never_behind():
    why = "Hessian not positive definite"
    sc = Scripted({(2, B12): -100.0}, outcomes={3: ("degenerate_twin", "the extra mode vanished"),
                                               1: ("no_evidence", why)})
    a, _used, _s = run(happy(), sc)
    assert a.coverage["stopped"] == {"down": "unresolved:1", "up": "collapsed:3"}
    assert f"unresolved_step:1:{why}" in a.reasons and not any(":3" in r for r in a.reasons)
    assert a.search_limited and a.coverage["decision"]["ambiguous"]
    rows = [c for c in a.candidates if c["count"] == 1]
    assert len(rows) == 2 and all((c["ln_z"], c["status"], c["reason"]) == (None, "no_evidence", why) for c in rows)
    for k in ("1", "3"):
        assert a.search["counts"][k]["state"] == "unresolved" and a.search["counts"][k]["behind_nats"] is None


def test_a_count_the_card_has_no_structure_for_stops_quietly():
    sc = Scripted({(2, B12): -100.0, (3, ("b1", "b2", None)): -100.5},
                  outcomes={4: ("no_structure", "the card's structures hold no model with 4 modes")})
    a, _used, _s = run(happy(), sc)
    assert a.coverage["stopped"]["up"] == "no_structure:4" and a.search_limited
    assert not any(r.startswith("unresolved_step") or r.startswith("unpriced:4") for r in a.reasons)


# 7 the ledger ---------------------------------------------------------------------------------------------------
def test_a_crossed_second_basin_is_filed_under_its_own_labelling_and_makes_the_labels_uncertain():
    sc = Scripted({(2, B12): -100.0}, crossed={(2, B12): (("b2", "b1"), -100.1, True)})
    a, _used, _s = run(happy(), sc)
    c = cands(a)
    assert c[(2, ("b2", "b1"))]["ln_z"] == pytest.approx(-100.1) and c[(2, ("b2", "b1"))]["source"] == "seed:incoming"
    assert not a.coverage["decision"]["label_confident"]


def test_a_labelling_reached_from_two_parents_is_priced_twice_and_counted_once():
    # z's b1 sits one FWHM (1.5 meV) below the incoming b1: the count-1 seed steps up to (b1, b2) from a new start
    snap = snapshot_for(OWN, ["b1", "b2"], {"x": X12, "z": ([(4.1, 0.5, 20.0)], ["b1"])})
    sc = Scripted({(2, B12): -100.0, (1, ("b1",)): -101.0})       # every start of (b1, b2) reaches one basin
    a, _used, s = run(snap, sc)
    jobs = [j for j in sc.asked if labels(j) == (2, B12)]
    assert len(jobs) >= 2 and len({j.key for j in jobs}) == len(jobs)
    assert s.tried["repeated_basins"] >= 1 and cands(a)[(2, B12)]["ln_z"] == pytest.approx(-100.0)
    assert len(s.ledger[(2, B12)]["basins"]) == 1
    near = Start("move", ((5.7, 0.5, 20.0), (10.7, 0.5, 20.0)))
    assert s._job(2, B12, [near], "move", "move") is None           # within SAME_START_FWHM of an attempted one


# 8 moves ----------------------------------------------------------------------------------------------------------
def test_the_moves_of_a_labelling_are_swaps_new_modes_and_unused_branches_ranked_by_shift_cost():
    s, sc = open_search(three_branches()), Scripted({(2, B12): -100.0})
    sc.M = s.M
    s.feed([sc(j) for j in s.next_jobs()])
    asked = {labels(j)[1] for j in s.next_jobs() if s.meta[j.key]["purpose"] == "move" and j.count == 2}
    s._expand((2, B12))
    cheapest, *ranked = s.keys[(2, B12)]["queue"]
    moves = {("b2", "b1"), (None, "b2"), ("b1", None), ("b3", "b2"), ("b1", "b3")}
    assert cheapest == B12 and set(ranked) == moves
    # every move is asked, except (b1, b3): the count-1 seed's up step already started it from the record's modes
    assert asked == moves - {("b1", "b3")}

    def cost(lab):                                  # the fixed-energy shift cost change, deviance
        return sum(s.spec.cost(e, b) - s.spec.cost(e, o) for e, b, o in zip((5.6, 10.6), lab, B12))
    assert [cost(m) for m in ranked] == sorted(cost(m) for m in ranked)
    while not s.done:
        s.feed([sc(j) for j in s.next_jobs()])
    s._expand((2, ("b1", "b3")))
    assert B12 in s.keys[(2, ("b1", "b3"))]["queue"]               # reassign to an unused branch and back
    s._expand((2, (None, "b2")))
    assert {B12, ("b3", "b2")} <= set(s.keys[(2, (None, "b2"))]["queue"])   # a previously unmatched mode gets matched


def five_branches():
    xm = [(5.0, 0.5, 20.0), (10.0, 0.5, 20.0), (13.0, 0.5, 20.0), (16.0, 0.5, 20.0), (19.0, 0.5, 20.0)]
    return snapshot_for(OWN, ["b1", "b2"], {"x": (xm[:2], ["b1", "b2"]), "z": (xm[2:], ["b3", "b4", "b5"])})


def test_an_unproductive_first_wave_stops_the_key_with_moves_left():
    a, _used, s = run(five_branches(), Scripted({(2, B12): -100.0}, peak=None))
    assert s.keys[(2, B12)]["stopped"] == "wave_behind_margin" and len(s.keys[(2, B12)]["waves"]) == 1
    assert a.search["labelling"]["2"]["stopped"] == "wave_behind_margin"
    assert a.search["labelling"]["2"]["moves_unpriced"] > 0


def test_a_productive_wave_asks_the_next_one():
    a, _used, s = run(five_branches(), Scripted({(2, B12): -100.0}, default={2: -101.0}, peak=None))
    assert len(s.keys[(2, B12)]["waves"]) >= 2
    assert sum(s.meta[j.key]["purpose"] == "move" and j.count == 2 for j, _p in s.outcomes) > SE.MOVE_WAVE


# 9 full search ----------------------------------------------------------------------------------------------------
def full(n, **kw):
    return replace(S, full_search=True, full_search_max_count=n, **kw)


def n_labellings(M, N):
    return sum(comb(k, r) * factorial(M) // factorial(M - r) for k in range(N + 1) for r in range(min(k, M) + 1))


def test_full_search_prices_exactly_the_stated_domain():
    a, used, _s = run(happy(settings=full(3)), Scripted({(2, B12): -100.0}))
    assert len(used) == n_labellings(2, 3) == 24 and a.coverage["stopped"] == {"down": "full_search", "up": "full_search"}
    assert {p.count for p in used} == {0, 1, 2, 3}
    assert a.search["full"] == {"max_count": 3, "jobs": 24, "untried": 0, "truncated": False}


def test_full_search_has_no_job_limit_of_its_own():
    xm = [(3.0 * i, 0.5, 20.0) for i in range(1, 7)]
    snap = snapshot_for(OWN, ["b1", "b2"], {"x": (xm, [f"b{i}" for i in range(1, 7)])}, settings=full(5))
    _a, used, s = run(snap, Scripted({(2, B12): -100.0}))
    assert len(s.in_window) == 6 and len(used) == n_labellings(6, 5) == 5376


def test_full_search_reports_the_seed_counts_it_excludes():
    own5 = [(3.0 + 3.0 * i, 0.5, 20.0) for i in range(5)]
    snap = snapshot_for(own5, [f"f{i}" for i in range(5)], {"x": X12}, settings=full(3))
    a, used, _s = run(snap, Scripted())
    assert a.search["full"]["truncated"] and a.search["full"]["untried"] == 1 and a.coverage["enumeration_truncated"]
    assert a.search_limited and not any(p.count == 5 for p in used)


TABLE9 = {(2, B12): -100.0, (2, ("b2", "b1")): -101.0, (1, ("b1",)): -102.0, (3, ("b1", "b2", None)): -103.0,
          (2, ("b1", None)): -102.5}


def test_full_search_and_the_adaptive_search_decide_alike_when_all_mass_is_within_the_margin():
    ad, _u, _s = run(happy(), Scripted(TABLE9))
    fu, _u, _s = run(happy(settings=full(3)), Scripted(TABLE9))
    assert (ad.count, ad.slots) == (fu.count, fu.slots)
    for k, p in ad.coverage["decision"]["count_p"].items():          # the adaptive walk also visits count 4
        assert fu.coverage["decision"]["count_p"].get(k, 0.0) == pytest.approx(p, abs=1e-6)


SIX = [("b1", "b2", None), ("b1", None, "b2"), ("b2", "b1", None), ("b2", None, "b1"), (None, "b1", "b2"),
       (None, "b2", "b1")]


def test_support_spread_over_six_labellings_is_found_by_moves():
    x = -99.5 - math.log(6)                                         # six equal labellings, 0.5 nats above count 2
    T = {(2, B12): -100.0} | {(3, lab): x for lab in SIX}
    ad, _u, _s = run(happy(), Scripted(T))
    fu, _u, _s = run(happy(settings=full(3)), Scripted(T))
    assert ad.count == fu.count == 3 and all((3, lab) in cands(ad) for lab in SIX)
    assert ad.coverage["decision"]["count_p"]["3"] == pytest.approx(fu.coverage["decision"]["count_p"]["3"], abs=1e-6)


def test_labellings_reachable_only_through_one_far_behind_are_missed_and_the_record_says_so():
    # the accepted risk: (b2, None, None) and (None, None, b1) are one move from (b1, None, None) only, 5.5 nats
    # behind its count's best, so the adaptive search never expands it; the full enumeration finds them
    hidden = [("b2", None, None), (None, None, "b1")]
    T = {(2, B12): -100.0, (3, ("b1", "b2", None)): -101.0, (3, ("b1", None, None)): -106.5}
    T |= {(3, lab): -100.0 for lab in hidden}
    ad, _u, s = run(happy(), Scripted(T))
    fu, _u, _s = run(happy(settings=full(3)), Scripted(T))
    assert (3, ("b1", None, None)) in cands(ad) and (3, ("b1", None, None)) not in s.keys
    assert not any((3, lab) in cands(ad) for lab in hidden) and all((3, lab) in cands(fu) for lab in hidden)
    assert ad.count == 2 and fu.count == 3
    assert ad.search["counts"]["3"]["labellings"] < fu.search["counts"]["3"]["labellings"]


# 10 counts above 6 -----------------------------------------------------------------------------------------------
def test_a_neighbour_with_eight_branches_seeds_count_eight_and_the_search_steps_around_it(monkeypatch):
    b8 = [f"b{i}" for i in range(1, 9)]
    snap = snapshot_for([(2.6, 0.5, 20.0)], ["b1"], {"x": ([(2.0 * i, 0.5, 20.0) for i in range(1, 9)], b8)})
    monkeypatch.setattr(R, "price", lambda *a, **k: pytest.fail("drive priced through refit.price"))
    a, used, _s = run(snap, Scripted({(8, tuple(b8)): -100.0}))
    assert {7, 8, 9} <= {p.count for p in used}
    assert (a.count, a.slots, a.changed) == (8, tuple(b8), True) and len(a.origins) == 8
    assert a.coverage["stopped"]["up"] == "behind_margin:9" and a.search["counts"]["8"]["state"] == "priced"


# 11 the label swap -------------------------------------------------------------------------------------------------
def test_a_label_swap_six_nats_behind_leaves_the_labels_confident():
    a, _u, _s = run(happy(), Scripted({(2, B12): -100.0, (2, ("b2", "b1")): -106.0}))
    dec = a.coverage["decision"]
    assert dec["label_p"] == pytest.approx(1 / (1 + math.exp(-6.0)), abs=1e-4)   # 0.9975
    assert dec["label_confident"] and not dec["ambiguous"] and a.slots == B12


# 19 the variant, decided from the ledger ---------------------------------------------------------------------------
QE = (True, True, True)


@pytest.mark.parametrize("gap,replaced", [(0.05, False), (0.5, True)])
def test_a_variant_ahead_beyond_the_hysteresis_replaces_the_incoming_one(gap, replaced):
    noqe = (True, True, False)                                      # the incoming record carries no QE
    sc = Scripted(variants={(2, B12): [(noqe, -100.0), (QE, -100.0 + gap)]})
    a, _u, _s = run(happy(), sc)
    assert (a.changed, a.mask_only) == (replaced, replaced) and a.slots == B12
    assert a.selected_model[:3] == (QE if replaced else noqe)
    assert a.coverage["decision"]["variant"] == {"incoming": list(noqe), "chosen": list(QE if replaced else noqe)}


# 21 no delta twin in a split basin ---------------------------------------------------------------------------------
def test_a_failed_price_after_a_hidden_pair_parent_is_an_unresolved_step_with_its_reason():
    why = "free width at the floor and its delta alternative did not fit"
    snap = snapshot_for([(8.0, 0.5, 40.0)], ["b1"], {"x": ([(8.0, 0.5, 40.0), (8.8, 0.5, 4.0)], ["b1", "b2"])})
    sc = Scripted({(2, B12): -100.0, (1, ("b1",)): -101.0}, split={(2, B12)}, outcomes={3: ("no_evidence", why)})
    a, _u, s = run(snap, sc)
    assert a.coverage["stopped"]["up"] == "unresolved:3" and f"unresolved_step:3:{why}" in a.reasons
    assert a.search_limited and any(c["reason"] == why for c in a.candidates if c["count"] == 3)
    at3 = [labels(j) for j in sc.asked if j.count == 3]
    assert len(at3) == len(set(at3))                                # no labelling asked twice
    # D4: the two modes of the placed pair are never swapped
    s._expand((2, B12))
    assert ("b2", "b1") not in s.keys[(2, B12)]["queue"]


# 24 a ledger key no job named --------------------------------------------------------------------------------------
def test_a_basin_filed_under_a_labelling_no_job_named_is_a_candidate_and_is_expanded():
    sc = Scripted(crossed={(2, B12): (("b2", "b1"), -100.0, False)})
    a, _u, s = run(happy(), sc)
    seed = next(j for j, _p in s.outcomes if s.meta[j.key]["purpose"] == "seed")
    row = cands(a)[(2, ("b2", "b1"))]
    assert (row["source"], row["key"], row["ln_z"]) == ("seed:incoming", seed.key, pytest.approx(-100.0))
    assert (2, B12) not in s.ledger and (2, ("b2", "b1")) in s.keys
    assert [labels(j) for j in sc.asked].count((2, B12)) == 1       # its swap back is the seed's start: not asked


def test_job_for_builds_the_search_s_own_job():
    snap = happy()
    j = job_for(snap, 2, B12)
    assert j.starts[0].family == "own" and j.starts[0].modes == tuple(OWN)
    j = job_for(snap, 2, ("b2", "b1"))
    assert j.starts[0].modes == ((10.0, 0.5, None), (5.0, 0.5, None))

"""The listed-candidate search (S4U2) against a scripted pricer, milliseconds each.

Every spectrum is generated at test time and every mass is a made-up table value (``_search_harness``); the real
``Search`` runs on a real ``Snapshot``, so the pool, the starts and the decision are the core's own. The link a
spectrum reads (``SnapshotLinks``) is made up too: the search prices only what it lists.
"""
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from _search_harness import (
    Scripted,
    add,
    drop,
    job_for,
    links_for,
    run,
    snapshot_for,
    split_of,
    tie,
)

from isar.analyze.coupled import search as SE
from isar.analyze.coupled.linking import matching_width
from isar.analyze.coupled.refit import open_search
from isar.analyze.coupled.search import change_id, crossing_flags

B12 = ("b1", "b2")
OWN = [(5.6, 0.5, 20.0), (10.6, 0.5, 20.0)]
X12 = ([(5.0, 0.5, 20.0), (10.0, 0.5, 20.0)], ["b1", "b2"])
Z36 = ([(13.0, 0.5, 20.0), (16.0, 0.5, 20.0), (19.0, 0.5, 20.0), (22.0, 0.5, 20.0)], ["b3", "b4", "b5", "b6"])


def snap(links=None, **kw):
    """y holds b1, b2; the neighbours carry b1..b6, so any of b3..b6 can be added."""
    return snapshot_for(OWN, list(B12), {"x": X12, "z": Z36}, links=links, **kw)


def adds(*n):
    """The add changes of b<i> for each i (b3..b6), each at its neighbour's energy."""
    return [add(f"b{i}", 13.0 + 3.0 * (i - 3)) for i in n]


def labels(job):
    """(count, labels in the energy order of the job's first start)."""
    st = job.starts[0]
    order = sorted(range(job.count), key=lambda i: (st.modes[i][0], str(job.slots[i])))
    return job.count, tuple(job.slots[i] for i in order)


def lab(job):
    return labels(job)[1]


def cands(a):
    return {(c["count"], tuple(c["slots"])): c for c in a.candidates if c["ln_z"] is not None}


# (a) no links ----------------------------------------------------------------------------------------------------
def test_without_links_exactly_one_job_the_incoming_fit_is_priced_and_kept():
    s, sc = open_search(snap()), Scripted({(2, B12): -100.0})
    sc.M = s.M
    jobs = s.next_jobs()
    assert len(jobs) == 1 and (jobs[0].count, jobs[0].slots) == (2, B12) and jobs[0].starts[0].family == "own"
    assert jobs[0].starts[0].modes == tuple(OWN)
    s.feed([sc(j) for j in jobs])
    assert s.done and s.next_jobs() == ()
    a = s.result()
    assert (a.count, a.slots, a.changed, a.discrete) == (2, B12, False, "priced")
    assert a.triggers == ("listed:incoming",) and a.search["version"] == 3 and a.search["chosen"] == "incoming"
    assert a.search["applied"] == [] and a.coverage["stopped"] == {"down": "listed", "up": "listed"}
    assert "full_search" not in a.coverage and "enumeration_truncated" not in a.coverage
    assert a.coverage["decision"]["count_p"]["2"] == pytest.approx(1.0)


def test_links_with_nothing_listed_prices_the_incoming_fit_only():
    a, used, _s = run(snap(links_for(2)), Scripted({(2, B12): -100.0}))
    assert len(used) == 1 and a.search["listed"][0]["id"] == "incoming" and len(a.search["listed"]) == 1


# (b) subsets and forward selection ---------------------------------------------------------------------------------
def test_each_subset_of_up_to_four_changes_is_one_candidate_priced_in_one_pass():
    for n, want in ((1, 2), (2, 4), (3, 8), (4, 16)):
        s = open_search(snap(links_for(2, changes=adds(*range(3, 3 + n)))))
        assert len(s.next_jobs()) == want, n
    sc = Scripted({(2, B12): -100.0})
    s = open_search(snap(links_for(2, changes=adds(3, 4))))
    sc.M = s.M
    got = {lab(j) for j in s.next_jobs()}
    assert got == {B12, B12 + ("b3",), B12 + ("b4",), B12 + ("b3", "b4")}
    s.feed([sc(j) for j in s.next_jobs()])
    assert s.done                                                   # one batch, then done


def test_an_add_starts_at_its_predicted_energy_with_a_width_from_the_prediction_and_no_height():
    s = open_search(snap(links_for(2, changes=[add("b3", 13.0, w=4.0)])))
    job = next(j for j in s.next_jobs() if j.count == 3)
    R = s.spec.fwhm
    g0 = SE.gamma_start(4.0, R)
    assert 0.1 * R < g0 and job.starts[0].modes == (OWN[0], OWN[1], (13.0, g0, None))
    assert matching_width(g0, R) == pytest.approx(4.0)


@pytest.mark.parametrize("w", [1.0, 1.5, 2.0, 7.3])
def test_gamma_start_inverts_matching_width_above_the_resolution(w):
    R = 1.5
    assert matching_width(SE.gamma_start(w, R), R) == pytest.approx(max(w, R))


def test_an_add_below_the_incoming_modes_keeps_every_start_ascending_in_the_job_s_slot_order():
    s = open_search(snap(links_for(2, changes=[add("b3", 3.0)])))
    job = next(j for j in s.next_jobs() if j.count == 3)
    assert job.slots == ("b3", "b1", "b2")
    for st in job.starts:
        energies = [m[0] for m in st.modes]
        assert energies == sorted(energies)


def test_a_split_replaces_the_mode_by_two_branches_with_the_predicted_share_of_its_height():
    ch = split_of(0, "b3", 5.2, "b4", 6.1, A1=30.0, A2=10.0)
    s = open_search(snap(links_for(2, changes=[ch])))
    job = next(j for j in s.next_jobs() if lab(j) == ("b3", "b4", "b2"))
    st = job.starts[0]
    assert st.family == "split"
    assert st.modes == ((5.2, 0.5, 20.0 * 0.75), (6.1, 0.5, 20.0 * 0.25), OWN[1])
    # a prediction without intensity falls back to the fixed share
    ch = split_of(0, "b3", 5.2, "b4", 6.1, A1=0.0)
    job = next(j for j in open_search(snap(links_for(2, changes=[ch]))).next_jobs() if j.count == 3)
    assert job.starts[0].modes[0][2] == pytest.approx(20.0 * SE.DEFAULT_SPLIT_FRACTION)


def test_a_drop_removes_its_mode_and_a_drop_and_a_split_of_one_mode_never_share_a_candidate():
    ch = [drop(0), split_of(0, "b3", 5.2, "b4", 6.1)]
    s = open_search(snap(links_for(2, changes=ch)))
    got = [lab(j) for j in s.next_jobs()]
    assert sorted(got, key=str) == sorted([B12, ("b2",), ("b3", "b4", "b2")], key=str)
    # different modes may combine
    s = open_search(snap(links_for(2, changes=[drop(0), drop(1)])))
    assert {lab(j) for j in s.next_jobs()} == {B12, ("b2",), ("b1",), ()}


def five_changes():
    return [*adds(5), drop(1), *adds(3), *adds(6), *adds(4)]       # a listing order of no meaning


def test_above_four_changes_the_best_is_extended_one_change_at_a_time_and_ends_with_the_supported_two():
    L = links_for(2, changes=five_changes())
    s = open_search(snap(L))
    sc = Scripted({(2, B12): -100.0, (3, B12 + ("b3",)): -95.0, (3, B12 + ("b4",)): -95.5,
                   (4, B12 + ("b3", "b4")): -90.0}, default=-120.0)
    sc.M = s.M
    first = s.next_jobs()
    assert {lab(j) for j in first} == {B12, B12 + ("b3",), B12 + ("b4",), B12 + ("b5",), B12 + ("b6",), ("b1",)}
    a, _used = SE.drive(s, sc)
    labs = [lab(j) for j in sc.asked]
    step1, step2 = labs[6:10], labs[10:13]
    assert set(step1) == {B12 + ("b3", "b4"), B12 + ("b3", "b5"), B12 + ("b3", "b6"), ("b1", "b3")}
    assert set(step2) == {B12 + ("b3", "b4", "b5"), B12 + ("b3", "b4", "b6"), ("b1", "b3", "b4")}
    assert len(labs) == 13 and not any({"b3", "b4", "b5", "b6"} <= set(x) for x in labs)    # never all together
    assert [x["best"] for x in a.search["steps"]] == ["add:b3", "add:b3+add:b4"]
    assert (a.count, a.slots, a.changed) == (4, B12 + ("b3", "b4"), True)
    assert [change_id(L.changes[i]) for i in a.search["applied"]] == ["add:b3", "add:b4"]
    assert a.search["chosen"] == "add:b3+add:b4"


def test_forward_selection_stops_when_no_single_change_helps():
    s = open_search(snap(links_for(2, changes=five_changes())))
    sc = Scripted({(2, B12): -100.0}, default=-130.0)
    sc.M = s.M
    a, used = SE.drive(s, sc)
    assert len(used) == 6 and (a.count, a.slots, a.changed) == (2, B12, False) and a.search["steps"] == []


# (c) the listing order changes nothing ---------------------------------------------------------------------------
def test_the_order_of_the_listed_changes_and_ties_changes_neither_jobs_nor_decision():
    ch = [*adds(3, 4), drop(1), split_of(0, "b5", 5.2, "b6", 6.1)]
    ties = [tie("t1", "swap", ("y", "w"), ("b2", "b1")), tie("t0", "competition", ("y", "v"), ("b1", None))]
    table = {(2, B12): -100.0, (3, B12 + ("b3",)): -99.0, (2, ("b2", "b1")): -99.5}

    def go(changes, tiess):
        preds = sorted((p for c in ch for p in c.predictions), key=lambda p: p.branch)    # the link's own order
        sn = snap(links_for(2, changes=changes, ties=tiess, predictions=preds))
        s = open_search(sn)
        first = s.next_jobs()
        a, _u, s = run(sn, Scripted(table))
        return first, a, sn.links.changes

    first1, a1, c1 = go(ch, ties)
    first2, a2, c2 = go(ch[::-1], ties[::-1])
    assert [j.key for j in first1] == [j.key for j in first2] and len(first1) > 5
    assert (a1.count, a1.slots, a1.coverage["decision"]) == (a2.count, a2.slots, a2.coverage["decision"])
    assert [(c["id"], c["jobs"]) for c in a1.search["listed"]] == [(c["id"], c["jobs"]) for c in a2.search["listed"]]
    assert [change_id(c1[i]) for i in a1.search["applied"]] == [change_id(c2[i]) for i in a2.search["applied"]]


# (d) crossing alternatives ---------------------------------------------------------------------------------------
def test_a_swap_tie_gives_a_cross_candidate_and_a_competition_a_compete_candidate_at_the_incoming_count():
    ties = [tie("t3", "swap", ("y", "w"), ("b2", "b1")), tie("t4", "competition", ("y", "v"), ("b1", None))]
    s = open_search(snap(links_for(2, ties=ties)))
    got = {c["id"]: c for c in s.cands}
    assert set(got) == {"incoming", "cross:t3", "compete:t4"}
    jobs = {s.meta[j.key]: j for j in s.next_jobs()}
    assert jobs["cross:t3"].count == 2 and jobs["cross:t3"].slots == ("b2", "b1")
    assert jobs["compete:t4"].slots == ("b1", None)
    assert jobs["cross:t3"].starts[0].modes == tuple(OWN)           # the incoming starts, the other labelling


def test_a_cross_candidate_the_data_prefer_is_chosen_and_the_label_stays_uncertain_when_near_tied():
    ties = [tie("t3", "swap", ("y", "w"), ("b2", "b1"))]
    a, _u, _s = run(snap(links_for(2, ties=ties)), Scripted({(2, B12): -100.0, (2, ("b2", "b1")): -90.0}))
    assert (a.slots, a.search["chosen"], a.search["applied"]) == (("b2", "b1"), "cross:t3", [])
    a, _u, _s = run(snap(links_for(2, ties=ties)), Scripted({(2, B12): -100.0, (2, ("b2", "b1")): -100.05}))
    assert a.slots == B12 and a.search["chosen"] == "incoming" and not a.coverage["decision"]["label_confident"]


def test_a_label_swap_six_nats_behind_leaves_the_labels_confident():
    ties = [tie("t3", "swap", ("y", "w"), ("b2", "b1"))]
    a, _u, _s = run(snap(links_for(2, ties=ties)), Scripted({(2, B12): -100.0, (2, ("b2", "b1")): -106.0}))
    dec = a.coverage["decision"]
    assert dec["label_p"] == pytest.approx(1 / (1 + math.exp(-6.0)), abs=1e-4)   # 0.9975
    assert dec["label_confident"] and not dec["ambiguous"] and a.slots == B12


# (e) what cannot be adopted is recorded --------------------------------------------------------------------------
def test_a_change_the_pool_cannot_carry_is_skipped_with_its_reason_never_dropped_silently():
    ch = [add("b9", 13.0), drop(5), split_of(0, "b3", 5.2, "b9", 6.1), *adds(3)]
    s = open_search(snap(links_for(2, changes=ch)))
    assert {x["id"]: x["why"] for x in s.skipped} == {
        "add:b9": "branch not carried by a conditioning neighbour", "drop:5": "no such mode",
        "split:0:b3+b9": "branch not carried by a conditioning neighbour"}
    assert {lab(j) for j in s.next_jobs()} == {B12, B12 + ("b3",)}
    a, _u, _s = run(snap(links_for(2, changes=ch)), Scripted({(2, B12): -100.0}))
    assert {x["id"] for x in a.search["skipped"]} == {"add:b9", "drop:5", "split:0:b3+b9"}


# (f) count first ---------------------------------------------------------------------------------------------------
def test_a_count_wins_by_the_mass_summed_over_its_listed_labellings_beyond_the_hysteresis():
    eps = 0.1
    third = -math.log(3.0)

    def go(gain, best4=0.0):
        T = {(2, B12): -100.0, (3, B12 + ("b3",)): -100.0 + gain + third, (3, B12 + ("b4",)): -100.0 + gain + third + best4,
             (3, B12 + ("b5",)): -100.0 + gain + third}
        return run(snap(links_for(2, changes=adds(3, 4, 5))), Scripted(T, default=-130.0))[0]

    keep = go(0.5 * eps)
    assert (keep.count, keep.slots, keep.changed) == (2, B12, False)     # each single alone is far below; the sum is not enough
    take = go(3 * eps, best4=0.05)
    assert (take.count, take.changed) == (3, True) and take.slots == B12 + ("b4",)    # the label: the best labelling
    assert take.search["chosen"] == "add:b4" and [change_id(snap(links_for(2, changes=adds(3, 4, 5))).links.changes[i])
                                                    for i in take.search["applied"]] == ["add:b4"]


# (g) protocol ------------------------------------------------------------------------------------------------------
def test_the_protocol_refuses_a_wrong_feed_and_a_result_before_done():
    s = open_search(snap(links_for(2, changes=adds(3, 4))))
    sc = Scripted({(2, B12): -100.0})
    sc.M = s.M
    jobs = s.next_jobs()
    assert s.next_jobs() == jobs                                   # idempotent without a feed
    with pytest.raises(ValueError):
        s.feed([sc(jobs[0])])                                      # one price for several jobs
    with pytest.raises(ValueError):
        s.feed([sc(j) for j in reversed(jobs)])                    # another order
    with pytest.raises(RuntimeError):
        s.result()                                                 # not done
    s.feed([sc(j) for j in jobs])
    assert s.done and s.result() is s.result()


def test_a_failed_listed_price_is_a_row_with_no_price_and_limits_the_search():
    why = "Hessian not positive definite"
    sc = Scripted({(2, B12): -100.0}, outcomes={(3, B12 + ("b3",)): ("no_evidence", why)})
    a, _u, _s = run(snap(links_for(2, changes=adds(3))), sc)
    assert a.search_limited and (a.count, a.slots) == (2, B12) and a.coverage["decision"]["ambiguous"]
    row = next(c for c in a.candidates if c["count"] == 3)
    assert (row["ln_z"], row["status"], row["reason"]) == (None, "no_evidence", why)
    assert a.coverage["counts_unresolved"] == {"3": f"no_evidence: {why}"}
    quiet = Scripted({(2, B12): -100.0}, outcomes={3: ("degenerate_twin", "the extra mode vanished")})
    a, _u, _s = run(snap(links_for(2, changes=adds(3))), quiet)
    assert not a.search_limited and not a.coverage["decision"]["ambiguous"]


# the ledger and the decision kept from the adaptive search ----------------------------------------------------------
def test_a_crossed_second_basin_is_filed_under_its_own_labelling_and_makes_the_labels_uncertain():
    sc = Scripted({(2, B12): -100.0}, crossed={(2, B12): (("b2", "b1"), -100.1, True)})
    a, _used, _s = run(snap(), sc)
    c = cands(a)
    assert c[(2, ("b2", "b1"))]["ln_z"] == pytest.approx(-100.1) and c[(2, ("b2", "b1"))]["source"] == "incoming"
    assert not a.coverage["decision"]["label_confident"]


def test_a_basin_filed_under_a_labelling_no_job_named_is_a_candidate_and_a_relabelling_is_other():
    sc = Scripted(crossed={(2, B12): (("b2", "b1"), -100.0, False)})
    a, _used, s = run(snap(), sc)
    seed = s.outcomes[0][0]
    row = cands(a)[(2, ("b2", "b1"))]
    assert (row["source"], row["key"], row["ln_z"]) == ("incoming", seed.key, pytest.approx(-100.0))
    assert (2, B12) not in s.ledger and a.slots == ("b2", "b1") and a.search["chosen"] == "other"


@pytest.mark.parametrize("gap,replaced", [(0.05, False), (0.5, True)])
def test_a_variant_ahead_beyond_the_hysteresis_replaces_the_incoming_one(gap, replaced):
    noqe, qe = (True, True, False), (True, True, True)             # the incoming record carries no QE
    sc = Scripted(variants={(2, B12): [(noqe, -100.0), (qe, -100.0 + gap)]})
    a, _u, _s = run(snap(), sc)
    assert (a.changed, a.mask_only) == (replaced, replaced) and a.slots == B12
    assert a.selected_model[:3] == (qe if replaced else noqe)
    assert a.coverage["decision"]["variant"] == {"incoming": list(noqe), "chosen": list(qe if replaced else noqe)}


def test_job_for_builds_the_search_s_own_job():
    sn = snap()
    j = job_for(sn, 2, B12)
    assert j.starts[0].family == "own" and j.starts[0].modes == tuple(OWN)
    j = job_for(sn, 2, ("b2", "b1"))               # its starts read in energy order: b1 at 5, then b2 at 10
    assert j.slots == ("b1", "b2") and j.starts[0].modes == ((5.0, 0.5, None), (10.0, 0.5, None))


def test_the_content_key_does_not_depend_on_the_listing_order_of_changes():
    a = snap(links_for(2, changes=adds(3, 4))).links
    b = replace(a, changes=a.changes[::-1])
    k = [open_search(snap(x)).content for x in (a, b)]
    assert k[0] == k[1]
    assert open_search(snap(replace(a, changes=a.changes[:1]))).content != k[0]    # another change, another price key


# (h) the crossing flags ----------------------------------------------------------------------------------------------
def _stage(units, group="t3", kind="swap", anchored=()):
    sn = {}
    for u in units:
        ties = () if u in anchored else (tie(group, kind, units, ("b2", "b1")),)
        sn[u] = replace(snap(links_for(2, ties=ties)), unit=u)
    return sn


def _took(**chosen):
    return {u: SimpleNamespace(search={"chosen": c}) for u, c in chosen.items()}


def test_confident_disagreement_across_a_swap_group_flags_every_spectrum_that_took_a_side():
    snaps = _stage(("a05", "a06"))
    out = crossing_flags(snaps, _took(a05="cross:t3", a06="incoming"))
    assert set(out) == {"a05", "a06"}
    for rows in out.values():
        (r,) = rows
        assert (r["code"], r["severity"]) == ("identity_ambiguous", "serious")
        assert "crossing t3" in r["text"] and "a06 kept the original" in r["text"] and "a05 took the alternative" in r["text"]


def test_a_competition_where_one_spectrum_takes_the_rival_partner_is_flagged_too():
    snaps = _stage(("a05", "a06"), group="t9", kind="competition")
    out = crossing_flags(snaps, _took(a05="compete:t9", a06="incoming"))
    assert set(out) == {"a05", "a06"}


def test_agreement_flags_nothing_and_an_add_choice_leaves_the_unit_out_of_the_group():
    snaps = _stage(("a05", "a06"))
    assert crossing_flags(snaps, _took(a05="incoming", a06="incoming")) == {}
    assert crossing_flags(snaps, _took(a05="cross:t3", a06="cross:t3")) == {}
    assert crossing_flags(snaps, _took(a05="cross:t3", a06="add:b3")) == {}      # one side alone
    out = crossing_flags(_stage(("a05", "a06", "a07")), _took(a05="cross:t3", a06="add:b3", a07="incoming"))
    assert set(out) == {"a05", "a07"}


def test_a_tie_group_with_an_anchored_end_flags_nothing():
    snaps = _stage(("a05", "a06"), anchored=("a06",))                # the anchored end has no alternative
    assert crossing_flags(snaps, _took(a05="cross:t3", a06="incoming")) == {}
    assert crossing_flags(list(snaps.values()), _took(a05="cross:t3")) == {}
    assert crossing_flags({}, {}) == {}


def test_applied_names_the_changes_of_the_candidate_whose_job_found_the_winning_basin():
    # drops of two unlinked modes give one (count, slots) key; the second candidate's job finds the better basin
    L = links_for(2, changes=[drop(0), drop(1)])
    sc = Scripted({(2, (None, None)): -100.0, (0, ()): -120.0}, default=-120.0)
    inner = sc.__call__

    def price(job):
        if job.count == 1:                                  # drop:1 keeps the 5.6 mode and is the better fit
            sc.table[(1, (None,))] = -70.0 if job.starts[0].modes[0][0] < 6.0 else -80.0
        return inner(job)
    s = open_search(snapshot_for(OWN, [None, None], {"x": X12}, links=L))
    sc.M = s.M
    a, _used = SE.drive(s, price)
    assert a.count == 1 and a.search["applied"] == [1]

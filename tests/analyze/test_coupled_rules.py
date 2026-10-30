"""The coupled refit's decision rules (hand-over check_refit_rules.py, ported), one test per rule group."""
import math

import pytest

from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import rules as rr


def test_composition_weight_is_normalised_once_without_q_u():
    M, counts = 3, (1, 2, 3, 4)
    tot = sum(math.comb(M, r) * math.exp(rr.ln_w0(k, r, M, len(counts)))
              for k in counts for r in range(min(k, M) + 1))
    assert abs(tot - 1.0) < 1e-12, tot


def test_pinned_unmatched_entry_pays_once():
    class _E:                                  # stand-in with Entry's B and b_edge only
        b_edge, b_mid = 2.0, 12.0

        def B(self, e):
            return self.b_mid
    assert rr.pinned_entry_charge(_E(), 10.0) == -7.0


def test_decide_hysteresis_basins_and_unpriced():
    inc = (1, ("b1",), ())
    d = rr.decide([(1, ("b1",), (), -100.0), (2, ("b1", "b2"), (), -99.95)], inc)
    assert d["winner"] == inc[:2] and not d["changed"], d                  # 0.05 nat < 0.1 hysteresis
    d = rr.decide([(1, ("b1",), (), -100.0), (2, ("b1", "b2"), (), -100.0), (2, ("b1", "b2"), (), -100.0)], inc)
    assert d["changed"] and d["winner"] == (2, ("b1", "b2")), d              # two basins: ln 2 > 0.1
    assert abs(d["count_p"][2] - 2 / 3) < 1e-12
    d = rr.decide([(1, ("b1",), (), None), (2, ("b1", "b2"), (), -50.0)], inc)
    assert not d["changed"] and "incomplete_comparison" in d["reasons"] and d["ambiguous"]
    d = rr.decide([(1, ("b1",), (), -100.0), (1, ("b2",), (), -101.0)], inc)
    assert d["ambiguous"] and d["close_calls"] == [(1, ("b2",))]            # association p 0.73 < 0.80
    d = rr.decide([(1, ("b1",), (), -100.0), (1, ("b2",), (), -101.5)], inc)
    assert not d["ambiguous"] and d["close_calls"] == [(1, ("b2",))]        # p 0.82, still a 2-nat close call


# R1-R5: the width mask is chosen after the (count, slots), below the labelling -------------------------------
B1, B12 = ("b1",), ("b1", "b2")


def test_r1_a_mask_won_beyond_the_hysteresis_is_a_mask_only_change():
    d = rr.decide([(1, B1, (), -100.0), (1, B1, (0,), -98.7)], (1, B1, ()))
    assert (d["winner"], d["delta"], d["changed"], d["mask_only"]) == ((1, B1), (0,), True, True)
    assert d["count_p"] == {1: 1.0} and d["weights"] == {(1, B1): 1.0}
    assert d["mask_weights"][(1, B1, ())] == pytest.approx(0.2142, abs=1e-4)
    assert d["mask_weights"][(1, B1, (0,))] == pytest.approx(0.7858, abs=1e-4)
    assert sum(d["mask_weights"].values()) == pytest.approx(1.0)


def test_r2_a_mask_gap_under_the_hysteresis_leaves_the_incoming_mask():
    d = rr.decide([(1, B1, (), -100.0), (1, B1, (0,), -99.95)], (1, B1, ()))
    assert (d["delta"], d["changed"], d["mask_only"]) == ((), False, False)


def test_r3_a_new_labelling_takes_its_best_mask_and_is_not_mask_only():
    rows = [(1, B1, (), -100.0), (1, ("b2",), (), -102.0), (1, ("b2",), (0,), -99.0)]
    d = rr.decide(rows, (1, B1, ()))
    assert (d["winner"], d["delta"], d["changed"], d["mask_only"]) == ((1, ("b2",)), (0,), True, False)
    assert d["weights"][(1, B1)] == pytest.approx(0.26, abs=0.005) and d["weights"][(1, ("b2",))] == pytest.approx(0.74, abs=0.005)
    assert d["ambiguous"]                                          # association p 0.74 < 0.80


def test_r4_an_unpriced_incoming_mask_is_replaced_by_the_best_priced_one_with_a_record():
    d = rr.decide([(1, B1, (), -100.0)], (1, B1, (0,)))
    assert (d["winner"], d["delta"], d["changed"], d["mask_only"]) == ((1, B1), (), True, True)
    assert d["reasons"] == ["mask_unpriced:1:('b1',):(0,)"] and not d["ambiguous"]
    # a row of the incoming mask with no evidence is as unpriced as none
    d = rr.decide([(1, B1, (0,), None), (1, B1, (), -100.0)], (1, B1, (0,)))
    assert d["delta"] == () and d["mask_only"] and "mask_unpriced:1:('b1',):(0,)" in d["reasons"]
    assert d["reasons"][0] == "unpriced:1:('b1',)"                 # the existing format, untouched


def test_r5_ties_go_to_the_incoming_mask_then_to_fewer_widths_at_the_floor():
    d = rr.decide([(2, B12, (), -100.0), (2, B12, (1,), -100.0)], (2, B12, (0,)))
    assert (d["delta"], d["mask_only"], d["reasons"]) == ((), True, ["mask_unpriced:2:('b1', 'b2'):(0,)"])
    d = rr.decide([(2, B12, (0,), -100.0), (2, B12, (1,), -100.0)], (2, B12, (1,)))
    assert (d["delta"], d["changed"]) == ((1,), False)             # the incoming mask when it is priced
    d = rr.decide([(2, B12, (0, 1), -100.0), (2, B12, (1,), -100.0), (2, B12, (0,), -100.0)], (2, B12, (0, 1)))
    assert (d["delta"], d["changed"]) == ((0, 1), False)           # priced incoming wins an exact tie
    d = rr.decide([(2, B12, (0, 1), -100.0), (2, B12, (1,), -100.0), (2, B12, (0,), -100.0)], (2, B12, ()))
    assert d["delta"] == (0,)                                      # fewer widths, then lexicographic


def test_a_winner_other_than_the_incoming_takes_the_best_mask_whatever_the_incoming_mask():
    rows = [(1, B1, (0,), -100.0), (1, ("b2",), (), -90.0), (1, ("b2",), (0,), -90.0)]
    d = rr.decide(rows, (1, B1, (0,)))
    assert d["winner"] == (1, ("b2",)) and d["delta"] == () and d["changed"] and not d["mask_only"]


def test_no_priced_row_is_no_winner_and_no_mask_change():
    d = rr.decide([(1, B1, (), None)], (1, B1, (0,)))
    assert d["winner"] is None and d["changed"] is False and d["mask_only"] is False
    assert not any(r.startswith("mask_unpriced") for r in d["reasons"])


def test_summing_the_masks_out_gives_the_old_function():
    # count_p, weights, ambiguous, close_calls, the winner and the label change do not see the mask
    import random
    rng = random.Random(7)
    for _ in range(60):
        rows = [(k, s, d, -100.0 + rng.uniform(-4, 4))
                for k in (1, 2) for s in ({1: [B1, ("b2",)], 2: [B12, ("b2", "b1")]}[k])
                for d in [(), (0,)] if rng.random() < 0.7]
        if not rows:
            continue
        inc = (1, B1)
        flat = {}
        for k, s, _d, z in rows:
            flat.setdefault((k, s), []).append(z)
        old = rr.decide([(k, s, (), rr._lse(v)) for (k, s), v in flat.items()], (*inc, ()))
        for inc_delta in [(), (0,)]:
            new = rr.decide(rows, (*inc, inc_delta))
            assert new["winner"] == old["winner"]
            assert (new["changed"] and not new["mask_only"]) == old["changed"]
            for key in ("weights", "count_p", "close_calls"):
                assert new[key] == pytest.approx(old[key]) if key != "close_calls" else new[key] == old[key]
            assert new["ambiguous"] == old["ambiguous"]


def test_acceptance_order():
    A = dict(any_finite=True, evidence_missing_for_change=False, hard_violation=False, topology_changed=False,
             evidence_won=False, J_old=100.0, J_new=100.0, materially_moved=False)
    assert rr.acceptance(**{**A, "any_finite": False})[0] == "failed"
    assert rr.acceptance(**{**A, "topology_changed": True, "evidence_missing_for_change": True})[0] == "failed"
    assert rr.acceptance(**{**A, "topology_changed": True}) == ("rejected", "no_supported_improvement")
    assert rr.acceptance(**{**A, "topology_changed": True, "evidence_won": True, "J_new": 200.0})[0] == "accepted"
    assert rr.acceptance(**{**A, "J_new": 99.0}) == ("accepted", "objective")
    assert rr.acceptance(**A) == ("unchanged", None)
    assert rr.acceptance(**{**A, "materially_moved": True}) == ("rejected", "material_change_without_gain")


def test_settle_needs_two_quiet_sweeps_and_caps():
    def q(J):
        return {"topology": 1, "max_dE": 0.001, "J": J, "failed": 0}
    assert rr.settled([q(10), q(10)]) == "running"
    assert rr.settled([q(10), q(10), q(10)]) == "settled"
    assert rr.settled([{**q(10), "max_dE": 1.0}] * 20) == "did_not_settle"


def test_total_objective_counts_a_shared_factor_once():
    f = cc.Factor("curv:a|b|c:x", "x", ("a", "b", "c"), (-0.5, 1.0, -0.5), s=1.0, C=12.0, kind="curv")
    E = {(m, f.factor_id): e for m, e in (("a", 10.0), ("b", 11.0), ("c", 10.0))}
    J, P = rr.total_objective([f, f, f], E, {"a": 1.0, "b": 2.0, "c": 3.0}, {}, cc.potential)
    assert abs(J - (6.0 + cc.potential(f, 1.0))) < 1e-12 and len(P) == 1
    other = cc.Factor(f.factor_id, "x", f.members, f.a, s=2.0, C=12.0, kind="curv")
    with pytest.raises(ValueError):
        rr.total_objective([f, other], E, {}, {}, cc.potential)


def test_flag_score_is_none_on_a_failed_release_and_threshold_proposal():
    assert rr.flag_score(None, {"f": 99.0}) is None and rr.flag_score(1.0, {"f": 3.0}) == 4.0
    assert rr.flag_threshold(12.0) == 54.0

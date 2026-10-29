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
    inc = (1, ("b1",))
    d = rr.decide([(1, ("b1",), -100.0), (2, ("b1", "b2"), -99.95)], inc)
    assert d["winner"] == inc and not d["changed"], d                      # 0.05 nat < 0.1 hysteresis
    d = rr.decide([(1, ("b1",), -100.0), (2, ("b1", "b2"), -100.0), (2, ("b1", "b2"), -100.0)], inc)
    assert d["changed"] and d["winner"] == (2, ("b1", "b2")), d              # two basins: ln 2 > 0.1
    assert abs(d["count_p"][2] - 2 / 3) < 1e-12
    d = rr.decide([(1, ("b1",), None), (2, ("b1", "b2"), -50.0)], inc)
    assert not d["changed"] and "incomplete_comparison" in d["reasons"] and d["ambiguous"]
    d = rr.decide([(1, ("b1",), -100.0), (1, ("b2",), -101.0)], inc)
    assert d["ambiguous"] and d["close_calls"] == [(1, ("b2",))]            # association p 0.73 < 0.80
    d = rr.decide([(1, ("b1",), -100.0), (1, ("b2",), -101.5)], inc)
    assert not d["ambiguous"] and d["close_calls"] == [(1, ("b2",))]        # p 0.82, still a 2-nat close call


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

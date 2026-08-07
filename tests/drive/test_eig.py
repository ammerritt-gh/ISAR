"""Phase 5a — cost-normalized EIG acquisition: the scoring primitives, the EIG-ranked target
selection, the value-of-information stop, and the cost accounting."""
import json
import math

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
import numpy as np

from isar.drive.acquisition.eig import (
    counts_eig,
    eig_per_cost,
    fisher_eig,
    predicted_sigma_after,
    sigma_omega0,
    voi_saturated,
    voi_saturated_from_sigmas,
)
from isar.analyze.phonon import omega0_value_error
from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor.stopping import GridQ, evaluate_stop, summarize_grid


# --------------------------------------------------------------------------- scoring primitives
def test_predicted_sigma_follows_sqrt_n():
    # σ_post = σ_now·√(C/(C+ΔC)); doubling the total counts shrinks σ by √2.
    assert predicted_sigma_after(1.0, 1.0, 1.0) == pytest.approx(1.0 / math.sqrt(2.0))
    # No baseline or no added cost → σ unchanged.
    assert predicted_sigma_after(0.5, 0.0, 1.0) == 0.5
    assert predicted_sigma_after(0.5, 1.0, 0.0) == 0.5


def test_counts_eig_diminishing_returns_monotone():
    # Fixed σ/tol/ΔC: the goal-weighted EIG strictly decreases as more counts already accumulated.
    vals = [counts_eig(1.0, 0.2, C, 1e10) for C in (1e9, 1e10, 1e11, 1e12)]
    assert all(a > b for a, b in zip(vals, vals[1:]))
    # No baseline → +inf (scan it first to establish one).
    assert counts_eig(1.0, 0.2, 0.0, 1e10) == float("inf")


def test_counts_eig_is_goal_weighted():
    # A q already at tolerance has ~0 EIG (nothing left to gain); one far over tolerance scores high.
    at_tol = counts_eig(0.20, 0.20, 1e10, 1e10)
    far = counts_eig(1.00, 0.20, 1e10, 1e10)
    assert at_tol == pytest.approx(0.0, abs=1e-9)
    assert far > 0.05


def test_eig_per_cost_prefers_cheap_big_gain():
    # Same EIG, cheaper scan wins; +inf EIG (no baseline) beats any finite score.
    assert eig_per_cost(0.1, 1e9) > eig_per_cost(0.1, 1e10)
    assert eig_per_cost(float("inf"), 1e10) == float("inf")
    assert eig_per_cost(0.1, 0.0) == float("inf")


# --------------------------------------------------------------------------- value-of-information
def test_voi_from_sigmas():
    # Big shrink → not saturated; a scan that can't move σ (Fisher-unidentifiable → σ_post=inf) or that
    # barely moves it → saturated; already at tolerance → not saturated.
    assert not voi_saturated_from_sigmas(1.0, 0.5, 0.2)
    assert voi_saturated_from_sigmas(1.0, float("inf"), 0.2)
    assert voi_saturated_from_sigmas(0.30, 0.2999, 0.20)
    assert not voi_saturated_from_sigmas(0.20, 0.10, 0.20)   # σ ≤ tol → converged, not saturated


def test_voi_saturated_needs_baseline_and_fires_when_pool_huge():
    # Under the √N model VOI fires only once the pool is so large one more scan barely shrinks σ.
    assert not voi_saturated(0.5, 0.2, 0.0, 1e10)            # no baseline → never
    assert not voi_saturated(0.5, 0.2, 1e10, 1e10)           # early, big marginal gain
    assert voi_saturated(0.5, 0.2, 1e14, 1e10)              # huge pool, ΔC negligible


# --------------------------------------------------------------------------- EIG-ranked selection
def _prod_gridq(hkl, q, sigma, *, tol=0.2, C=2e10, dC=1e10, n_prod=2):
    return GridQ(index=0, hkl=hkl, q=q, tolerance=tol, n_production=n_prod,
                 master={"omega0": 3.0, "halfwidth": sigma, "diagnostic_halfwidth": sigma,
                         "usable": True, "reliable": True, "certifiable": True,
                         "n_members": n_prod},
                 counts_spent=C, last_prod_cost=dC)


def test_select_target_ranks_by_eig_per_cost():
    # Two baselined q, equal cost; the one whose ω₀ is least pinned (larger σ over tolerance) has the
    # higher goal-weighted EIG → "count more where it helps most", not merely the least-counted q.
    far = _prod_gridq((2.1, 0.0, 0.0), 0.0, 0.80)
    near = _prod_gridq((2.2, 0.0, 0.0), 0.1, 0.25)
    pick, kind, reason, score = acq._select_target([near, far], model=None)
    assert kind == "production" and pick.hkl == (2.1, 0.0, 0.0)
    assert "EIG-per-cost" in reason and score.method == "counts"


def test_select_target_falls_back_to_deficit_without_cost_history():
    # No recorded counts_spent (hand-built / pre-Phase-5) → the σ-deficit ranking, unchanged.
    a = _prod_gridq((2.1, 0.0, 0.0), 0.0, 0.30, C=0.0, dC=0.0)
    b = _prod_gridq((2.2, 0.0, 0.0), 0.1, 0.90, C=0.0, dC=0.0)   # widest → largest deficit
    pick, kind, reason, score = acq._select_target([a, b], model=None)
    assert pick.hkl == (2.2, 0.0, 0.0) and "largest-deficit" in reason and score.method == "deficit"


def test_fisher_tier_ranks_grounded_candidates_and_falls_back():
    # When a Fisher provider grounds candidates, the tier ranks by fisher_eig/cost and picks the q the
    # scan would pin best (smallest predicted σ_scan at equal cost); an ungrounded set falls to counts.
    class _M:
        def predict(self, q):
            return (3.0, 0.5)
    a = _prod_gridq((2.1, 0.0, 0.0), 0.0, 0.6)
    b = _prod_gridq((2.2, 0.0, 0.0), 0.1, 0.6)
    # provider: b's scan pins ω₀ much tighter than a's → b wins the Fisher tier.
    def fisher(g, w0, sp):
        return 0.30 if g.hkl == (2.1, 0.0, 0.0) else 0.05
    pick, kind, reason, score = acq._select_target([a, b], _M(), fisher=fisher)
    assert pick.hkl == (2.2, 0.0, 0.0) and score.method == "fisher"
    assert score.sigma_scan == pytest.approx(0.05)
    # Provider that grounds nobody → counts tier (method reverts).
    pick2, _, _, score2 = acq._select_target([a, b], _M(), fisher=lambda g, w0, sp: None)
    assert score2.method == "counts"


# --------------------------------------------------------------------------- durable VOI stop
def test_info_saturated_is_visible_but_nonterminal():
    g = _prod_gridq((2.1, 0.0, 0.0), 0.0, 0.30, tol=0.2, C=1e14, dC=1e10)
    assert g.info_saturated and not g.stuck and not g.converged
    assert "diminishing_returns" in g.diagnostics
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.2,
                                budget={"kind": "neutrons", "amount": 1e18})
    dec = evaluate_stop(obj, [g], {"kind": "neutrons", "amount": 1e18, "spent": 0.0})
    assert dec.state == "continue"


def test_info_saturated_dormant_without_cost_history():
    # The same q shape with no cost history is NOT saturated (no baseline to reason from).
    g = _prod_gridq((2.1, 0.0, 0.0), 0.0, 0.30, tol=0.2, C=0.0, dC=0.0)
    assert not g.info_saturated and not g.stuck


# --------------------------------------------------------------------------- cost accounting
def test_summarize_grid_sums_proposal_cost():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [
        {"hkl": (2.1, 0.0, 0.0), "kind": "quicklook", "calibrated_halfwidth": 0.9, "resolved": True,
         "disposition": "resolved", "omega0": 3.0, "proposal_cost": 5e8},
        {"hkl": (2.1, 0.0, 0.0), "kind": "production", "calibrated_halfwidth": 0.8, "resolved": True,
         "disposition": "resolved", "omega0": 3.0, "proposal_cost": 1e10},
    ]
    g = summarize_grid(obj, obs)[0]
    assert g.counts_spent == pytest.approx(5e8 + 1e10)
    assert g.last_prod_cost == pytest.approx(1e10)     # most recent production scan's cost


# --------------------------------------------------------------------------- ProposalScore surface
def test_proposal_carries_json_safe_score():
    # Cold start → an "explore" score (no baseline) that round-trips through strict JSON (no inf).
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    p = acq.propose_scan(obj, acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    score = p.meta["score"]
    assert score["method"] == "explore"
    json.dumps(p.to_dict())     # must not raise (Infinity is not strict JSON)


def test_sigma_omega0_matches_delta_method_on_diagonal():
    # With a diagonal (no nuisance coupling) Fisher matrix, the Schur+delta σ(ω₀) equals the analytic
    # covariance propagation the fit uses (isar.analyze.phonon.omega0_value_error).
    theta = (3.0, 0.3, 100.0, 0.0, 1.0)
    info = np.diag([50.0, 200.0, 1.0, 1.0, 1.0])
    got = sigma_omega0(theta, info)
    cov2 = np.diag([1.0 / 50.0, 1.0 / 200.0])
    _, expect = omega0_value_error(3.0, 0.3, cov2)
    assert got == pytest.approx(expect, rel=1e-6)


def test_sigma_omega0_infinite_when_unidentifiable_never_zero():
    # A rank-deficient Fisher with no Ω_d curvature (singular primary block) → σ = ∞, never 0.
    theta = (3.0, 0.3, 100.0, 0.0, 1.0)
    singular = np.diag([0.0, 0.0, 5.0, 5.0, 5.0])       # Ω_d and Γ both unconstrained
    assert math.isinf(sigma_omega0(theta, singular))
    # A well-posed matrix gives a small positive σ, never zero.
    s = sigma_omega0(theta, np.diag([1e4, 1e4, 1.0, 1.0, 1.0]))
    assert 0.0 < s < 1.0


def test_fisher_eig_edges():
    assert fisher_eig(0.5, float("inf")) == 0.0        # a scan that can't pin ω₀ adds nothing
    assert fisher_eig(float("inf"), 0.5) == float("inf")
    assert fisher_eig(0.5, 0.5) == pytest.approx(0.5 * math.log(2.0))


def test_production_proposal_scored_by_counts_method():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [
        {"hkl": (2.1, 0.0, 0.0), "kind": "quicklook", "calibrated_halfwidth": 0.9, "resolved": True,
         "disposition": "resolved", "omega0": 3.0, "proposal_cost": 5e8},
        {"hkl": (2.1, 0.0, 0.0), "kind": "production", "calibrated_halfwidth": 0.9, "resolved": True,
         "disposition": "resolved", "omega0": 3.0, "proposal_cost": 1e10},
    ]
    master = [{"hkl": (2.1, 0.0, 0.0), "omega0": 3.0, "halfwidth": 0.9, "reliable": True,
               "usable": True, "disposition": "resolved", "n_members": 2}]
    model = acq_model.fit([(g.q, g.best_omega0, g.best_halfwidth)
                           for g in summarize_grid(obj, obs, master) if g.best_omega0 is not None])
    p = acq.propose_scan(obj, model, obs, {"fwhm_dE": 0.8}, {}, master_rows=master)
    assert p is not None and p.kind == "production"
    assert p.meta["score"]["method"] == "counts"
    assert p.meta["score"]["sigma_prior"] == pytest.approx(0.9)
    json.dumps(p.to_dict())


def test_fisher_score_recomputes_for_runtime_clamped_exposure():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "counting_seconds", "amount": 60.0})
    obs = [
        {"hkl": (2.1, 0.0, 0.0), "kind": "production", "calibrated_halfwidth": 0.9,
         "resolved": True, "disposition": "resolved", "omega0": 3.0,
         "proposal_cost": 1e10},
    ]
    master = [{"hkl": (2.1, 0.0, 0.0), "omega0": 3.0, "halfwidth": 0.9,
               "reliable": True, "usable": True, "disposition": "resolved", "n_members": 1}]
    model = acq_model.fit([(g.q, g.best_omega0, g.best_halfwidth)
                           for g in summarize_grid(obj, obs, master) if g.best_omega0 is not None])
    calls = []

    def fisher(_g, _w0, _sp, exposure=None):
        actual = 400_000_000 if exposure is None else int(exposure)
        calls.append(actual)
        return 0.4 * math.sqrt(400_000_000 / actual)

    fisher.supports_exposure = True
    fisher.default_exposure = 400_000_000
    p = acq.propose_scan(
        obj, model, obs, {"fwhm_dE": 0.8}, {}, master_rows=master, fisher=fisher,
        runtime_limits={"max_neutrons_per_point": 100_000_000})
    score = p.meta["score"]
    expected_sigma = 0.8
    expected_eig = fisher_eig(0.9, expected_sigma)
    assert calls == [400_000_000, 100_000_000]
    assert p.ncount == 100_000_000
    assert score["ranking_ncount"] == 400_000_000
    assert score["sigma_scan"] == pytest.approx(expected_sigma)
    assert score["eig"] == pytest.approx(expected_eig)
    assert score["combined_information"] == pytest.approx(expected_eig)
    assert score["eig_per_cost"] == pytest.approx(expected_eig / score["cost"])


def test_bound_legacy_fisher_method_is_not_misdetected_as_exposure_aware():
    class LegacyProvider:
        def fisher(self, _g, _w0, _sp):
            return 0.5

    provider = LegacyProvider()
    assert acq._fisher_supports_exposure(provider.fisher) is False

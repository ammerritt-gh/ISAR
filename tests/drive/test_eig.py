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


# --------------------------------------------------------------------------- durable VOI stop
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



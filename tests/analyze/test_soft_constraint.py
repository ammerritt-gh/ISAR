"""Phase 2a — the fit-time soft-constraint mechanism + the data/posterior-cost split (contract 1).

The load-bearing contract: a soft prior enters the OPTIMISER objective (so the error bar is the
honest data∩prior width) but NEVER the data cost that AIC / cost_per_dof / the verdict read.
"""
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.components import ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.structures import Hypothesis
from isar.schema.steering import ParamConstraints
from isar.synth import estimators

_HYP = Hypothesis("stokes+bg", (PhononDHO, ConstantBackground))


def _ctx(gamma_true=0.5, seed=0, constraints=None):
    x = np.linspace(-6.0, 6.0, 81)
    base = ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, bool), T=100.0,
                       center=0.0, gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute", constraints=constraints)
    comp = _HYP.build()
    theta = np.array([4.0, gamma_true, 500.0, 20.0])       # Omega_d, Gamma, A, BG
    model = comp.evaluate(x, theta, base)
    counts = np.random.default_rng(seed).poisson(np.clip(model, 0, None)).astype(float)
    return replace(base, counts=counts)


# --------------------------------------------------------------------------- penalty mechanics (unit)
def test_soft_penalty_math_and_filtering():
    names = ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]
    pen = loop._soft_penalty(ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)}), names)
    theta = np.array([4.0, 0.5, 500.0, 20.0])
    assert pen(theta) == pytest.approx(((0.5 - 0.3) / 0.1) ** 2)
    # empty / unknown-name / non-positive-sigma → None
    assert loop._soft_penalty(ParamConstraints(), names) is None
    assert loop._soft_penalty(ParamConstraints(soft={"nope.X": (1.0, 0.1)}), names) is None
    assert loop._soft_penalty(ParamConstraints(soft={"phonon.Gamma": (0.3, 0.0)}), names) is None


def test_objective_adds_penalty_but_costv_is_data_only():
    ctx = _ctx()
    ctxc = replace(ctx, constraints=ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)}))
    p0 = loop.build_problem(_HYP, ctx)
    pc = loop.build_problem(_HYP, ctxc)
    x0 = pc.x0
    # data cost is identical with and without the constraint (penalty never enters costv)
    assert pc.costv(x0) == pytest.approx(p0.costv(x0))
    # the objective differs from the data cost by exactly the penalty
    gamma = float(x0[pc.free_names.index("phonon.Gamma")])
    assert pc.objective(x0) - pc.costv(x0) == pytest.approx(((gamma - 0.3) / 0.1) ** 2)
    assert p0.penalty is None and p0.objective(x0) == pytest.approx(p0.costv(x0))


# --------------------------------------------------------------------------- full fit (contract 1)
def test_fit_hypothesis_soft_prior_splits_cost_and_tags():
    ctx = _ctx()
    ctxc = replace(ctx, constraints=ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)}))
    r0 = loop.fit_hypothesis(_HYP, ctx)
    rc = loop.fit_hypothesis(_HYP, ctxc)

    assert r0.fit_kind == "unconstrained" and r0.prior_penalty is None and r0.posterior_cost is None
    assert rc.fit_kind == "soft_prior"

    # rc.cost is DATA-only: recomputing the Baker–Cousins cost at rc's params matches rc.cost, and
    # excludes the penalty (which is strictly positive here since Γ̂ ≠ μ).
    est = estimators.get_estimator("baker_cousins")
    comp = _HYP.build()
    theta = np.array([rc.params[n] for n in comp.param_names()])
    data_cost = float(est.cost(ctxc.counts_used, comp.evaluate(ctxc.x_used, theta, ctxc)))
    assert rc.cost == pytest.approx(data_cost)
    assert rc.prior_penalty > 0.0
    assert rc.posterior_cost == pytest.approx(rc.cost + rc.prior_penalty)
    # cost_per_dof (what the verdict reads) is built from the DATA cost, not the posterior cost
    assert rc.cost_per_dof == pytest.approx(rc.cost / rc.ndof)


def test_soft_prior_pulls_gamma_toward_mu():
    """A tight prior at μ=0.3 pulls the fitted Γ below the unconstrained fit (data Γ_true=0.5)."""
    ctx = _ctx(gamma_true=0.5)
    ctxc = replace(ctx, constraints=ParamConstraints(soft={"phonon.Gamma": (0.3, 0.05)}))
    r0 = loop.fit_hypothesis(_HYP, ctx)
    rc = loop.fit_hypothesis(_HYP, ctxc)
    assert rc.params["phonon.Gamma"] < r0.params["phonon.Gamma"]

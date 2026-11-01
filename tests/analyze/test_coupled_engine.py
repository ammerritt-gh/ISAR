"""The coupled core's own checks (hand-over check_coupled_core.py, ported), against the installed engine.

Check 1 of the hand-over (fast_pass.py byte-identical to the reviewed head) is run by hand, not here: a
legitimate later fast-pass edit would break it; the fast-pass tests running unchanged are the guard.
Check 5 is rebuilt on a synthetic DHO spectrum generated at test time (no measured value).
"""
import math

import numpy as np
import pytest
from scipy.integrate import quad

from isar.analyze import catalogue, fast_pass, prior
from isar.analyze.context import ScanContext
from isar.analyze.coupled import engine as cc

X = np.linspace(-6.0, 24.0, 241)


def ep():
    return prior.EnergyPrior(lo=0.5, hi=30.0, start=2.0, end=0.5 + 0.25, ceiling=7.5, corner=0.05)


def test_one_normaliser_of_the_product():
    e = ep()
    fT = cc.Factor("t1", "b1", ("me", "nT"), (1.0, -1.0), s=1.0, C=12.0, kind="dT")
    fq = cc.Factor("q1", "b1", ("me", "nq"), (1.0, -1.0), s=1.0, C=12.0, kind="dq")
    frozen = {"nT": 12.0, "nq": 15.5}
    d = cc.MatchedDensity(e, "me", [fT, fq], frozen)
    assert d.status == "ok"
    pts = [0.75, 2.0, 12.0, 13.5, 15.5, 17.5]
    tot = sum(quad(lambda x: math.exp(d.ln_pdf(x)), a, b, limit=200)[0]
              for a, b in zip([e.lo] + pts, pts + [e.hi]))
    assert abs(tot - 1.0) < 1e-6, tot
    sep = sum(cc.MatchedDensity(e, "me", [f], frozen).ln_pdf(13.0) for f in (fT, fq))
    assert abs(sep - d.ln_pdf(13.0)) > 0.1, "product of separate normalisers must differ"


def test_birth_price_survives_normalisation():
    e = ep()
    flat = cc.Entry(e, band=1.0, b_edge=0.0, b_mid=0.0)
    for x in (1.0, 7.0, 20.0):
        assert flat.ln_weight(x) == e.log_pdf(x), "zero price must be the solo density"
    const = cc.Entry(e, band=1.0, b_edge=14.0, b_mid=0.0)
    assert abs((const.ln_weight(10.0) - flat.ln_weight(10.0)) + 7.0) < 1e-12
    mid = cc.Entry(e, band=1.0)                      # 2 + 12 in the middle, 2 at a physical edge
    assert abs(mid.ln_weight(10.0) - e.log_pdf(10.0) + 7.0) < 1e-12
    assert abs(mid.ln_weight(e.hi) - e.log_pdf(e.hi) + 1.0) < 1e-12


def _q(s=2.0):
    return cc.Factor("q1", "b1", ("me", "nq"), (1.0, -1.0), s=s, C=12.0, kind="dq")


@pytest.mark.parametrize("u, P", [(0.0, 0.0), (0.1, 0.0150), (0.5, 0.3692), (0.8, 0.9226), (1.0, 1.4100),
                                  (2.0, 4.7216), (2.4, 6.1590), (4.0, 10.3763), (10.0, 12.0)])
def test_the_q_penalty_rises_from_zero_and_caps(u, P):
    assert cc.potential(_q(), u) == pytest.approx(P, rel=1e-3, abs=1e-4)


def test_the_q_penalty_is_monotone_capped_and_steepest_at_its_tolerance():
    f = _q()
    grid = np.linspace(0.0, 8.0, 1601)
    P = np.array([cc.potential(f, float(u)) for u in grid])
    assert np.all(np.diff(P) > 0) and P[-1] <= 12.0
    assert cc.potential(f, 1e3) == pytest.approx(12.0)
    assert grid[np.argmax(np.gradient(P, grid))] == pytest.approx(2.0, abs=0.01)
    assert cc.mismatch(f, 2.0) == pytest.approx(6.0) and cc.mismatch(f, 4.0) == pytest.approx(24.0)


def test_a_narrow_q_tolerance_still_has_a_normalised_density():
    e = ep()
    for s in (0.05, 0.01):
        d = cc.MatchedDensity(e, "me", [_q(s)], {"nq": 15.5})
        assert d.status == "ok"
        tot = quad(lambda x: math.exp(d.ln_pdf(x)), e.lo, e.hi, limit=400, points=[15.5])[0]
        assert abs(tot - 1.0) < 1e-6, (s, tot)


# ---- check 5, rebuilt: zero-price entries reproduce the solo engine ---------------------------------
def _spectrum(modes, seed=7):
    ctx = ScanContext(x=X, counts=np.ones(X.size), mask=np.ones(X.size, bool), T=300.0, center=0.0,
                      gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes))
               if h.label.startswith("elastic+"))
    mu = hyp.build().evaluate(X, [v for m in modes for v in m] + [120.0, 3.0], ctx)
    counts = np.random.default_rng(seed).poisson(mu).astype(float)
    return ScanContext(x=X, counts=counts, mask=np.ones(X.size, bool), T=300.0, center=0.0,
                       gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")


def _priced(engine, k, start):
    variant = engine.variant(k, True, True)
    basin, _cost = engine.fit(variant, start, "check5")
    engine.price(basin)
    return basin


@pytest.mark.parametrize("k", [1, 2])
def test_zero_price_entries_reproduce_the_solo_evidence(k):
    modes = [(6.0, 0.6, 50.0), (13.0, 0.8, 40.0)][:k]
    ctx = _spectrum(modes)
    s = fast_pass.FastPassSettings(mode_ceiling=k)
    start = [(e + 0.3, g, h, False) for e, g, h in modes]
    base = _priced(fast_pass._Engine(ctx, s), k, start)
    assert base.status in ("ok", "ok_boundary")

    def coupled(**kw):
        sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
        slot = cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)
        return _priced(cc.CoupledEngine(ctx, s, [slot] * k, **kw), k, start)

    # the prior is the claim: on the solo engine's own (finite-difference) arithmetic it is exact
    same = coupled(n_exchangeable=(k,), gradients=False)
    assert same.status == base.status
    assert same.ln_z == pytest.approx(base.ln_z, abs=1e-9)
    # the joint gradient's arithmetic stops the optimiser elsewhere by ~1e-5 nats, the same scale as below
    joint = coupled(n_exchangeable=(k,))
    assert joint.status == base.status and joint.ln_z == pytest.approx(base.ln_z, abs=1e-4)
    bare = coupled(gradients=False)
    # a constant shift of the objective moves L-BFGS-B's relative stop by ~1e-5 nats: not a prior difference
    assert bare.ln_z == pytest.approx(base.ln_z - math.lgamma(k + 1), abs=1e-4)


def test_j_includes_the_soft_constraint_the_optimiser_applies():
    from isar.schema.steering import ParamConstraints
    base = _spectrum([(8.0, 0.5, 60.0)])
    ctx = ScanContext(x=base.x, counts=base.counts, mask=base.mask, T=300.0, center=0.0, gauss_fwhm=1.5,
                      lorentz_fwhm=0.0, instrument="S30", energy_status="absolute",
                      constraints=ParamConstraints(soft={"phonon.omega0": (9.0, 0.05)}))
    s = fast_pass.FastPassSettings(mode_ceiling=1)
    sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
    eng = cc.CoupledEngine(ctx, s, [cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)], continuous=True)
    model = eng.model(eng.variant(1, True, True), ())
    assert model.soft
    theta0 = eng._start(model, [(8.0, 0.5, 60.0, False)], None)
    res = eng._trf(model, theta0, "coupled")
    theta = eng.zmap(model).to_theta(res.x, theta0)              # res.x is the z coordinate (widths as t)
    a = model.modes[0][0]
    # the soft pull moves the optimum off the data's 8.0 meV; J, the objective _trf minimised, is stationary there
    assert 8.3 < theta[a] < 9.0
    for d in (-0.02, 0.02):
        moved = theta.copy()
        moved[a] += d
        assert eng.J(model, moved) >= eng.J(model, theta) - 1e-6
    z = sum(((theta[i] - mu) / sd) ** 2 for i, mu, sd in model.soft)
    assert eng.J(model, theta) == pytest.approx(eng.cost(model, theta) + z + sum(p for _, p, _s in eng.penalty_terms(model, theta)))

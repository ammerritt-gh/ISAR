"""MINOS (Tier-1 inline) + the exact ω₀ profile interval (§5.3).

ω₀ is a derived quantity (√(Ω_d²+Γ²)); iminuit MINOS only profiles the fit parameters, so
the ω₀ interval comes from a 1-D profile scan. In the well-counted regime it should match the
bootstrap (both → the asymptotic interval)."""
from types import SimpleNamespace

import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bootstrap as B
from isar.analyze.errors import profile as P
from isar.analyze.structures import enumerate_structures
from isar.synth._ff import convolute_voigt_dho


def _fitted(seed=0):
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 400.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp = {h.label: h for h in enumerate_structures(ctx)}["elastic+stokes+bg"]
    return hyp, ctx, loop.fit_hypothesis(hyp, ctx, minos=False)


def test_minos_intervals_present_and_ordered():
    hyp, ctx, fit = _fitted()
    mi = P.minos_intervals(hyp, ctx, fit)
    assert "phonon.Omega_d" in mi and "phonon.Gamma" in mi
    for lo, hi, valid in mi.values():
        assert lo <= hi


def test_omega0_profile_brackets_fit():
    hyp, ctx, fit = _fitted()
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit)
    assert np.isfinite(lo) and np.isfinite(hi)
    assert lo < fit.omega0 < hi


def test_omega0_profile_executor_preserves_side_order():
    class OrderedExecutor:
        def map(self, fn, tasks):
            return [fn(task) for task in tasks]

    hyp, ctx, fit = _fitted()
    assert P.omega0_profile_interval(hyp, ctx, fit, executor=OrderedExecutor()) == \
        P.omega0_profile_interval(hyp, ctx, fit)


def test_omega0_profile_parallel_path_preserves_unavailable_side(monkeypatch):
    class OrderedExecutor:
        def map(self, fn, tasks):
            return [fn(task) for task in tasks]

    hyp, ctx, fit = _fitted()
    monkeypatch.setattr(P, "_omega0_profile_crossing",
                        lambda _h, _c, _r, direction, _delta=1.0:
                        float("nan") if direction < 0 else 6.0)
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit, executor=OrderedExecutor())
    assert np.isnan(lo) and hi == 6.0


def test_omega0_profile_lower_scan_stops_at_gamma_floor(monkeypatch):
    """A weak fit may step below Gamma's floor before finding a lower cost crossing."""
    best = 0.021
    gamma = loop.GAMMA_FLOOR
    omega_d = float(np.sqrt(best ** 2 - gamma ** 2))
    problem = SimpleNamespace(
        free_names=["phonon.Omega_d", "phonon.Gamma", "phonon.A"],
        bounds=[
            (loop.EPS_OMEGA_D, 10.0),
            (loop.GAMMA_FLOOR, loop.GAMMA_MAX),
            (0.0, 1e4),
        ],
        fixed_pos=[],
        fixed_vals=[],
    )
    fit = SimpleNamespace(
        params={"phonon.Omega_d": omega_d, "phonon.Gamma": gamma, "phonon.A": 1.0},
        omega0=best,
        omega0_err_hesse=0.02,
        posterior_cost=None,
        cost=0.0,
    )
    monkeypatch.setattr(P.loop, "build_problem", lambda _hyp, _ctx: problem)
    monkeypatch.setattr(
        P,
        "_cost_closure",
        lambda _prob, _ctx: lambda theta:
        (float(np.hypot(theta[0], theta[1])) - best) ** 2,
    )

    assert np.isnan(P._omega0_profile_crossing(None, None, fit, -1))


def test_omega0_profile_fixed_gamma_stops_at_omega_d_floor(monkeypatch):
    best = 0.021
    gamma = 0.02
    omega_d = float(np.sqrt(best ** 2 - gamma ** 2))
    problem = SimpleNamespace(
        free_names=["phonon.Omega_d", "phonon.Gamma", "phonon.A"],
        bounds=[
            (loop.EPS_OMEGA_D, 10.0),
            (loop.GAMMA_FLOOR, loop.GAMMA_MAX),
            (0.0, 1e4),
        ],
        fixed_pos=[1],
        fixed_vals=[gamma],
    )
    fit = SimpleNamespace(
        params={"phonon.Omega_d": omega_d, "phonon.Gamma": gamma, "phonon.A": 1.0},
        omega0=best,
        omega0_err_hesse=0.02,
        posterior_cost=None,
        cost=0.0,
    )
    monkeypatch.setattr(P.loop, "build_problem", lambda _hyp, _ctx: problem)
    monkeypatch.setattr(
        P,
        "_cost_closure",
        lambda _prob, _ctx: lambda theta:
        (float(np.hypot(theta[0], theta[1])) - best) ** 2,
    )

    assert np.isnan(P._omega0_profile_crossing(None, None, fit, -1))


def test_omega0_profile_near_bootstrap_when_well_counted():
    hyp, ctx, fit = _fitted()
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit)
    blo, _bmed, bhi = B.parametric_bootstrap(hyp, ctx, fit, M=80, seed=1).omega0_interval
    pw, bw = (hi - lo) / 2.0, (bhi - blo) / 2.0
    assert 0.4 < pw / bw < 2.5            # profile ≈ bootstrap in the asymptotic regime

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
from isar.analyze.phonon import enumerate_phonon_structures
from isar.analyze.lineshapes import convolute_voigt_dho


def _fitted(seed=0):
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 400.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}["elastic+stokes+bg"]
    return hyp, ctx, loop.fit_hypothesis(hyp, ctx, minos=False)


def test_minos_intervals_present_and_ordered():
    hyp, ctx, fit = _fitted()
    mi = P.minos_intervals(hyp, ctx, fit)
    assert "phonon.omega0" in mi and "phonon.Gamma" in mi
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


def test_omega0_profile_lower_scan_stops_at_the_omega0_floor(monkeypatch):
    """A weak fit may reach the .omega0 floor before finding a lower cost crossing."""
    best = 0.021
    gamma = loop.GAMMA_FLOOR
    problem = SimpleNamespace(
        zmap=None,
        free_names=["phonon.omega0", "phonon.Gamma", "phonon.h"],
        bounds=[
            (loop.EPS_OMEGA0, 10.0),
            (loop.GAMMA_FLOOR, loop.GAMMA_MAX),
            (0.0, 1e4),
        ],
        fixed_pos=[],
        fixed_vals=[],
    )
    fit = SimpleNamespace(
        params={"phonon.omega0": best, "phonon.Gamma": gamma, "phonon.h": 1.0},
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
        (float(theta[0]) - best) ** 2,
    )

    assert np.isnan(P._omega0_profile_crossing(None, None, fit, -1))


def test_omega0_profile_fixed_gamma_stops_at_the_omega0_floor(monkeypatch):
    best = 0.021
    gamma = 0.02
    problem = SimpleNamespace(
        zmap=None,
        free_names=["phonon.omega0", "phonon.Gamma", "phonon.h"],
        bounds=[
            (loop.EPS_OMEGA0, 10.0),
            (loop.GAMMA_FLOOR, loop.GAMMA_MAX),
            (0.0, 1e4),
        ],
        fixed_pos=[1],
        fixed_vals=[gamma],
    )
    fit = SimpleNamespace(
        params={"phonon.omega0": best, "phonon.Gamma": gamma, "phonon.h": 1.0},
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
        (float(theta[0]) - best) ** 2,
    )

    assert np.isnan(P._omega0_profile_crossing(None, None, fit, -1))


def test_omega0_profile_with_no_nuisance_left_is_the_cost_at_omega0(monkeypatch):
    # A hard-constrained model leaves ω₀ as the only free parameter: the profile is the cost at ω₀,
    # with nothing to minimise over (scipy used to fail on the empty bounds list).
    best = 0.5
    problem = SimpleNamespace(
        zmap=None,
        free_names=["phonon.omega0", "phonon.Gamma"],
        bounds=[(loop.EPS_OMEGA0, 10.0), (loop.GAMMA_FLOOR, loop.GAMMA_MAX)],
        fixed_pos=[1],
        fixed_vals=[0.5],
    )
    fit = SimpleNamespace(
        params={"phonon.omega0": best, "phonon.Gamma": 0.5},
        omega0=best, omega0_err_hesse=0.02, posterior_cost=None, cost=0.0,
    )
    monkeypatch.setattr(P.loop, "build_problem", lambda _hyp, _ctx: problem)
    monkeypatch.setattr(P, "_cost_closure", lambda _prob, _ctx: lambda theta:
                        (float(theta[0]) - best) ** 2)

    # cost (ω₀ - best)² = 1 at ω₀ = best + 1: the upper Δcost=1 crossing, exactly.
    assert abs(P._omega0_profile_crossing(None, None, fit, +1) - (best + 1.0)) < 1e-6


def test_omega0_profile_near_bootstrap_when_well_counted():
    hyp, ctx, fit = _fitted()
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit)
    blo, _bmed, bhi = B.parametric_bootstrap(hyp, ctx, fit, M=80, seed=1).omega0_interval
    pw, bw = (hi - lo) / 2.0, (bhi - blo) / 2.0
    assert 0.4 < pw / bw < 2.5            # profile ≈ bootstrap in the asymptotic regime


def _edge_problem(*, od_bounds, gamma, best):
    """A fit pinned against its .omega0 box, with a cost so flat that Δcost=1 is unreachable inside it."""
    problem = SimpleNamespace(
        zmap=None,
        free_names=["phonon.omega0", "phonon.Gamma", "phonon.h"],
        bounds=[od_bounds, (loop.GAMMA_FLOOR, loop.GAMMA_MAX), (0.0, 1e4)],
        fixed_pos=[1],
        fixed_vals=[gamma],
    )
    fit = SimpleNamespace(
        params={"phonon.omega0": best, "phonon.Gamma": gamma, "phonon.h": 1.0},
        omega0=best, omega0_err_hesse=0.02, posterior_cost=None, cost=0.0,
    )
    return problem, fit


def test_a_lower_bound_truncated_profile_is_an_explicit_refusal_not_a_bare_nan(monkeypatch):
    # "profile hit a physical bound" and "profile unavailable" were the same nan, so a
    # non-convergence could not be told from a missing diagnostic downstream.
    best = 0.021
    problem, fit = _edge_problem(od_bounds=(loop.EPS_OMEGA0, 10.0), gamma=0.02, best=best)
    monkeypatch.setattr(P.loop, "build_problem", lambda _hyp, _ctx: problem)
    monkeypatch.setattr(P, "_cost_closure", lambda _prob, _ctx: lambda theta:
                        (float(theta[0]) - best) ** 2)

    value = P._omega0_profile_crossing(None, None, fit, -1)
    assert P.is_bound_truncated(value)
    assert np.isnan(value)          # every existing numeric consumer still sees a nan


def test_the_upper_scan_is_capped_by_the_fitted_omega0_bound(monkeypatch):
    # The clamp used to be one-sided: the lower side stopped at the box while the upper side walked
    # straight out of it, so the "1σ" interval came back asymmetric and skewed upward.
    best, od_hi = 0.021, 0.05
    problem, fit = _edge_problem(od_bounds=(loop.EPS_OMEGA0, od_hi), gamma=0.02, best=best)
    seen = []

    def _cost(_prob, _ctx):
        def costv(theta):
            seen.append(float(theta[0]))
            return (float(theta[0]) - best) ** 2
        return costv

    monkeypatch.setattr(P.loop, "build_problem", lambda _hyp, _ctx: problem)
    monkeypatch.setattr(P, "_cost_closure", _cost)

    value = P._omega0_profile_crossing(None, None, fit, +1)
    assert P.is_bound_truncated(value)
    assert max(seen) <= od_hi + 1e-12


def test_the_bound_marker_survives_exactly_the_transports_it_must():
    """Pin both halves of the marker's lifetime, because only the first half is obvious.

    ``BoundTruncated`` is a ``nan`` float subclass, so it is erased by anything that rebuilds the
    number — and preserved by everything that merely moves the object.  It is sound only because
    its single consumer, ``_interval_semantics_for``, runs inside ``attach``/``attach_batch`` in
    the producing process and turns it into a durable ``failure_reason`` before serialisation.
    The negative half is asserted so that a change moving semantics construction past a
    serialisation boundary fails here instead of silently downgrading every bound truncation to
    ``profile_interval_unavailable``.
    """
    import copy
    import pickle

    marker = P.BoundTruncated()

    # Survives: the transports the current call path actually uses.  The sides come back from a
    # worker pool inside a tuple, are deepcopied into the outcome, and are retupled by the driver.
    assert P.is_bound_truncated(pickle.loads(pickle.dumps(marker)))
    assert P.is_bound_truncated(pickle.loads(pickle.dumps((marker, 1.0)))[0])
    assert P.is_bound_truncated(copy.deepcopy(marker))
    assert P.is_bound_truncated(tuple([marker])[0])
    assert P.is_bound_truncated(list((marker,))[0])

    # Erased: every path that reconstructs the float.  ``results.csv`` reads
    # ``omega0_profile_lo``/``_hi`` back through exactly this kind of conversion, which is why the
    # marker must be consumed before it is written, never recovered afterwards.
    assert not P.is_bound_truncated(float(marker))
    assert not P.is_bound_truncated(marker * 1.0)
    assert not P.is_bound_truncated(np.asarray([marker])[0])
    assert not P.is_bound_truncated(float(str(marker)))

    # It is still a nan throughout, so numeric consumers are unaffected either way.
    assert np.isnan(marker) and not np.isfinite(marker)

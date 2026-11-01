"""Recipe /7: a mode's width is a proper density on [g_lo, min(4 meV, omega0)] in every path that fits
or samples a mode (C-width-ceiling, C-zmap, the classic lane's GammaMap). Synthetic spectra generated at
test time; no measured value."""
import math

import numpy as np
import pytest
from scipy.integrate import quad

from isar.analyze import catalogue, evidence, fast_pass, loop, prior
from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.analyze.prior import PriorSettings
from isar.schema.steering import ParamConstraints

FWHM = 1.5
X = np.linspace(-8.0, 20.0, 113)
S = PriorSettings()
K = S.width_knee
G_LO = 1.0e-3


def _ctx(counts, **kw):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float), mask=np.ones(X.size, bool), T=300.0,
                       center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute", **kw)


def _spectrum(modes, *, elastic=150.0, bg=4.0, seed=3, scale=1.0):
    """Poisson counts of elastic + the (omega0, Gamma, h) modes + bg."""
    ctx = _ctx(np.ones(X.size))
    label = "elastic+stokes{}+bg".format(len(modes) if len(modes) > 1 else "")
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == label)
    mean = hyp.build().evaluate(X, [v for m in modes for v in m] + [elastic, bg], ctx) * scale
    return _ctx(np.random.default_rng(seed).poisson(mean).astype(float))


def _sp(**kw):
    return prior.resolve_prior(np.array([0, 3, 250, 7.0]), np.linspace(-16, 20, 50), PriorSettings(**kw),
                               fwhm=1.0)


# ---- C-width-ceiling: the worked-example table ------------------------------------------------------------
def test_ceilings_and_their_derivative():
    c = lambda w, **kw: prior.width_ceiling(w, settings=S, **kw)               # noqa: E731
    d = lambda w, **kw: prior.d_width_ceiling(w, settings=S, **kw)             # noqa: E731
    assert (c(10.0), d(10.0)) == (4.0, 0.0)
    assert (c(2.0), d(2.0)) == (2.0, 1.0)
    assert (c(10.0, lifted=True), d(10.0, lifted=True)) == (20.0, 0.0)
    assert (c(4.0), d(4.0)) == (4.0, 0.0)                    # the kink: the right derivative
    assert (c(3.999), d(3.999)) == (3.999, 1.0)
    assert (c(0.004), d(0.004)) == (0.004, 1.0)              # no floor: [0.001, 0.004] is not empty
    assert prior.width_ceiling(10.0, settings=PriorSettings(legacy_v4=True)) == 5.0
    assert prior.width_ceiling(10.0, lifted=True, settings=PriorSettings(legacy_v4=True)) == 5.0
    assert prior.width_ceiling(10.0, settings=PriorSettings(width_cap=40.0)) == 4.0     # the lone cap is fixed
    assert prior.width_ceiling(10.0, lifted=True, settings=PriorSettings(width_cap=40.0)) == 40.0
    assert prior.width_ceiling(10.0, settings=PriorSettings(width_cap=3.0)) == 3.0      # a smaller cap still wins


@pytest.mark.parametrize("ceiling, ln_l, ln_p03, ln_jac", [(4.0, 1.2607, -0.3932, 1.3860),
                                                         (2.0, 1.0520, -0.1845, 0.6926),
                                                         (20.0, 1.6319, -0.7644, 2.9957)])
def test_the_worked_densities(ceiling, ln_l, ln_p03, ln_jac):
    assert math.log(math.log((ceiling + K) / (G_LO + K))) == pytest.approx(ln_l, abs=1e-4)
    assert prior.ln_width_given(0.3, ceiling, G_LO, S) == pytest.approx(ln_p03, abs=1e-4)
    assert math.log(ceiling - G_LO) == pytest.approx(ln_jac, abs=1e-4)


def test_derivatives_round_trip_and_the_lifted_density():
    assert prior.d_ln_width_given(0.3, 2.0, G_LO, S) == pytest.approx((-1 / 0.42, -0.16473), abs=1e-4)
    assert prior.t_from_gamma(0.5, G_LO, 4.0) == pytest.approx(0.124781, abs=1e-6)
    assert prior.gamma_from_t(0.124781, G_LO, 4.0) == pytest.approx(0.5, abs=1e-5)
    assert prior.gamma_from_t(1.0, G_LO, 4.0) == 4.0
    with pytest.raises(ValueError):
        prior.gamma_from_t(0.5, G_LO, G_LO)
    sp = _sp()
    assert sp.g_lo == G_LO
    # the lifted density is the old 20 meV prior, now normalised on [g_lo, 20]
    assert prior.ln_width_given(G_LO, 20.0, G_LO, S) == pytest.approx(0.4800, abs=1e-4)
    assert sp.ln_width(G_LO) == pytest.approx(prior.ln_width_given(G_LO, 20.0, G_LO, S), abs=1e-12)
    assert sp.ln_width_at(0.3, 10.0, lifted=True) == pytest.approx(sp.ln_width(0.3), abs=1e-12)
    # a numerical derivative
    for omega0 in (1.5, 2.0, 7.0):
        for g in (0.05, 0.3, 1.4):
            h = 1e-6
            fd_g = (sp.ln_width_at(g + h, omega0) - sp.ln_width_at(g - h, omega0)) / (2 * h)
            fd_o = (sp.ln_width_at(g, omega0 + h) - sp.ln_width_at(g, omega0 - h)) / (2 * h)
            assert sp.d_ln_width_at(g, omega0) == pytest.approx((fd_g, fd_o), abs=1e-5)


@pytest.mark.parametrize("kind", ["softlog", "flat"])
@pytest.mark.parametrize("omega0", [0.5, 2.0, 3.999, 4.0, 10.0])
def test_the_width_density_is_proper_on_the_optimisers_own_interval(kind, omega0):
    sp = _sp(width_kind=kind)
    c = prior.width_ceiling(omega0, settings=sp.settings)
    assert c == min(4.0, omega0)
    z = quad(lambda g: math.exp(sp.ln_width_at(g, omega0)), sp.g_lo, c, limit=200)[0]
    assert z == pytest.approx(1.0, abs=1e-9)
    assert sp.ln_width_at(c * 1.0001 + 1e-9, omega0) == -math.inf       # zero above the ceiling
    assert sp.ln_width_at(sp.g_lo / 2, omega0) == -math.inf             # and below g_lo


def test_the_catalogue_width_priors_are_proper_and_sampled_on_the_same_interval():
    rng = np.random.default_rng(1)
    cases = [catalogue.ResolvedPrior("p.Gamma", "softlog", K, None, G_LO, 4.0, "default_softlog"),
             catalogue.ResolvedPrior("p.Gamma", "flat", None, None, G_LO, 4.0, "default_flat_width"),
             catalogue.ResolvedPrior("p.Gamma", "gaussian", 0.5, 0.4, G_LO, 4.0, "transport")]
    for p in cases:
        for ceiling in (0.7, 2.0, 10.0):
            top = min(4.0, ceiling)
            z = quad(lambda g: math.exp(p.logpdf_given(g, ceiling)), G_LO, top, limit=200)[0]
            assert z == pytest.approx(1.0, abs=1e-8), (p.kind, ceiling)
            assert p.logpdf_given(top * 1.001 + 1e-9, ceiling) == -math.inf
            draws = np.array([p.ppf_given(u, ceiling) for u in rng.random(4000)])
            assert draws.max() <= top + 1e-12 and draws.min() >= G_LO - 1e-12
            ecdf = float(np.mean(draws <= 0.5))
            cdf = quad(lambda g: math.exp(p.logpdf_given(g, ceiling)), G_LO, min(0.5, top), limit=200)[0]
            assert ecdf == pytest.approx(cdf, abs=0.03), (p.kind, ceiling)
    # the unconditional softlog is exactly normalised on [lo, hi] now
    soft = cases[0]
    assert quad(lambda g: math.exp(soft.logpdf(g)), G_LO, 4.0, limit=200)[0] == pytest.approx(1.0, abs=1e-9)


# ---- one prior measure in the deep pass's bundle ----------------------------------------------------------------
def _bundle_for_two_modes():
    ctx = _ctx(np.full(X.size, 100.0))
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, 2) if h.label == "elastic+stokes2+bg")
    return ctx, hyp, evidence.candidate_bundle(hyp, ctx)


def test_the_bundle_draws_and_prices_each_width_given_its_own_energy():
    _ctx_, _hyp, bundle = _bundle_for_two_modes()
    names = [p.name for p in bundle.params]
    iw, io = names.index("phonon.Gamma"), names.index("phonon.omega0")
    rng = np.random.default_rng(5)
    draws = np.array([bundle.transform_ordered(u) for u in rng.random((4000, len(names)))])
    for k in ("phonon", "phonon2"):
        w, o = draws[:, names.index(k + ".Gamma")], draws[:, names.index(k + ".omega0")]
        assert np.all(w <= np.minimum(4.0, o) + 1e-12) and np.all(w >= G_LO - 1e-12)
    # the empirical CDF of Gamma at 0.5 for a fixed energy equals the density's integral
    p = bundle.params[iw]
    u = rng.random(4000)
    at = np.array([p.ppf_given(v, 3.0) for v in u])
    expect = quad(lambda g: math.exp(p.logpdf_given(g, 3.0)), G_LO, 0.5)[0]
    assert float(np.mean(at <= 0.5)) == pytest.approx(expect, abs=0.03)
    # and the Laplace-lane prior reads the same conditional density
    sp = prior.resolve_prior_for(_ctx_)
    values = dict(zip(names, draws[0]))
    peaks = sorted([(values[k + ".omega0"], values[k + ".Gamma"], values[k + ".h"]) for k in ("phonon", "phonon2")])
    want = sp.ln_prior(peaks, values["elastic.h"], values["bg.BG"])
    assert bundle.logpdf_ordered(values) == pytest.approx(want, abs=1e-9)
    assert bundle.params[io].lo == pytest.approx(2 * G_LO)               # the energy window follows the floor


def test_a_hard_fixed_omega0_caps_its_own_width_in_the_bundle():
    # omega0 pinned at 2 meV, Gamma free: no draw or price of the bundle goes above min(4, 2)
    ctx = _ctx(np.full(X.size, 100.0), constraints=ParamConstraints(hard={"phonon.omega0": 2.0}))
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, 1) if h.label == "elastic+stokes+bg")
    bundle = evidence.candidate_bundle(hyp, ctx)
    names = [p.name for p in bundle.params]
    assert "phonon.omega0" not in names and "phonon.Gamma" in names
    draws = np.array([bundle.transform_ordered(u) for u in np.random.default_rng(4).random((2000, len(names)))])
    assert draws[:, names.index("phonon.Gamma")].max() <= 2.0 + 1e-12
    named = dict(zip(names, draws[0]))
    assert bundle.logpdf_ordered({**named, "phonon.Gamma": 2.5}) == -math.inf
    assert bundle.logpdf_ordered({**named, "phonon.Gamma": 1.5}) > -math.inf


def test_a_legacy_v4_width_is_not_capped_by_its_energy():
    # the v4 arm's width support is GAMMA_MAX_V4 whatever omega0: a 2 meV mode may be 3 meV wide
    ctx = _ctx(np.full(X.size, 100.0))
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, 1) if h.label == "elastic+stokes+bg")
    bundle = evidence.candidate_bundle(hyp, ctx, prior_settings=PriorSettings(legacy_v4=True))
    names = [p.name for p in bundle.params]
    by, grid = bundle.by_name(), np.linspace(0.0, 1.0, 4001)
    u = np.full(len(names), 0.5)
    u[names.index("phonon.omega0")] = np.interp(2.0, [by["phonon.omega0"].ppf(v) for v in grid], grid)
    g = by["phonon.Gamma"]
    u[names.index("phonon.Gamma")] = (3.0 - g.lo) / (g.hi - g.lo)
    out = dict(zip(names, bundle.transform_ordered(u)))
    assert out["phonon.omega0"] == pytest.approx(2.0, abs=1e-3)
    assert out["phonon.Gamma"] == pytest.approx(3.0, abs=1e-9)
    assert bundle.logpdf_ordered(out) > -math.inf


def test_the_energy_window_keeps_every_width_range_non_empty():
    for fwhm in (1e-3, 0.05, 0.5, 1.5):
        sp = prior.resolve_prior(np.array([0, 3, 250, 7.0]), np.linspace(-16, 20, 50), fwhm=fwhm)
        assert sp.e_lo == max(prior.EPS_OMEGA0, 2 * sp.g_lo)
        assert sp.ceiling_at(sp.e_lo) >= 2 * sp.g_lo > sp.g_lo


def test_the_recipe_version_makes_a_stored_fit_stale():
    assert prior.PRIOR_RECIPE_VERSION == "isar.mode-prior-recipe/7"
    res = fast_pass.run_fast_pass(_spectrum([(10.0, 0.3, 80.0)]), settings=FastPassSettings(mode_ceiling=1))
    assert res.prior_provenance["version"] == "isar.mode-prior-recipe/7"


# ---- C-zmap -----------------------------------------------------------------------------------------------------
def _two_mode_model(constraints=None):
    ctx = _spectrum([(2.5, 0.6, 60.0), (7.0, 0.5, 50.0)], seed=7)
    if constraints is not None:
        ctx = _ctx(ctx.counts, constraints=constraints)
    eng = fast_pass._Engine(ctx, FastPassSettings(mode_ceiling=2))
    model = eng.model(eng.variant(2, True, True), ())
    return eng, model


def test_the_z_map_round_trips_and_keeps_widths_under_their_ceilings():
    eng, model = _two_mode_model()
    zm = eng.zmap(model)
    assert zm.active and [m for m, *_ in zm.mapped] == [0, 1]
    theta = np.array([2.5, 0.9, 60.0, 7.0, 0.5, 50.0, 150.0, 4.0])
    z = zm.to_z(theta)
    assert z[1] == pytest.approx(prior.t_from_gamma(0.9, G_LO, 2.5)) and z[4] == pytest.approx((0.5 - G_LO) / (4 - G_LO))
    assert zm.lo[1] == zm.lo[4] == 0.0 and zm.hi[1] == zm.hi[4] == 1.0 and zm.hi[0] == model.hi[0]
    assert np.allclose(zm.to_theta(z, theta), theta, rtol=1e-12)
    # omega0 is taken AFTER the assignment: moving it moves the width at fixed t
    z2 = z.copy()
    z2[0] = 2.0
    assert zm.to_theta(z2, theta)[1] == pytest.approx(prior.gamma_from_t(z[1], G_LO, 2.0))
    # t = 1 is exactly the ceiling, so a capped mode below 4 meV has Gamma == omega0 (read as overdamped)
    assert zm.to_theta(np.array([2.5, 1.0, 60.0, 7.0, 1.0, 50.0, 150.0, 4.0]), theta)[[1, 4]].tolist() == [2.5, 4.0]
    # a start above the ceiling is pulled under it, and a clipped t is 1
    over = theta.copy()
    over[1] = 3.9
    assert zm.clip(over.copy())[1] == pytest.approx(2.5) and zm.to_z(over)[1] == 1.0
    assert zm.at_cap(np.array([2.5, 2.5, 60, 7.0, 0.5, 50, 150, 4]), 0) and not zm.at_cap(theta, 0)


@pytest.mark.parametrize("omega1, omega2", [(2.5, 7.0), (1.2, 3.2)])
def test_grad_to_z_matches_central_differences_of_the_z_objective(omega1, omega2):
    eng, model = _two_mode_model()
    zm = eng.zmap(model)
    theta = np.array([omega1, 0.4, 60.0, omega2, 0.5, 50.0, 150.0, 4.0])

    def F(t):                      # the theta-space objective: cost/2 - sat - ln prior (no Jacobian)
        return 0.5 * eng.cost(model, t) - eng.sat - eng.ln_prior(model, t)

    def dF(t, h=1e-6):
        g = np.zeros(t.size)
        for i in model.free:
            up, dn = t.copy(), t.copy()
            step = h * max(1.0, abs(t[i]))
            up[i] += step
            dn[i] -= step
            g[i] = (F(up) - F(dn)) / (2 * step)
        return g

    f, lo, hi = eng.objective(model, theta)
    z0 = zm.to_z(theta)
    fd = np.zeros(z0.size)
    for i in range(z0.size):
        h = 1e-6
        up, dn = z0.copy(), z0.copy()
        up[i] += h
        dn[i] -= h
        fd[i] = (f(up) - f(dn)) / (2 * h)
    assert zm.grad_to_z(theta, dF(theta)) == pytest.approx(fd, rel=2e-4, abs=2e-3)
    # the objective is F_theta - ln_jac
    assert f(z0) == pytest.approx(F(theta) - zm.ln_jac(theta), abs=1e-9)


def test_cov_to_theta_is_the_delta_method_on_a_linear_toy():
    gm = prior.GammaMap([(0, 1, G_LO, False)], S)
    theta = np.array([2.0, 0.8])
    cov_z = np.array([[0.04, 0.003], [0.003, 0.01]])
    J = np.array([[1.0, 0.0], [(0.8 - G_LO) / (2.0 - G_LO), 2.0 - G_LO]])
    assert gm.jac(theta) == pytest.approx(J)
    assert gm.cov_to_theta(theta, cov_z) == pytest.approx(J @ cov_z @ J.T)
    # a Monte Carlo of the transform: draws in z land inside [g_lo, ceiling] whatever the energy
    rng = np.random.default_rng(2)
    z = np.column_stack([rng.uniform(0.5, 9.0, 3000), rng.random(3000)])
    th = np.array([gm.to_theta(r) for r in z])
    assert np.all(th[:, 1] <= np.minimum(4.0, th[:, 0]) + 1e-12) and np.all(th[:, 1] >= G_LO - 1e-12)


def test_a_soft_constrained_width_is_mapped_and_cut_at_its_ceiling():
    soft = ParamConstraints(soft={"phonon2.Gamma": (0.5, 0.2)})
    eng, model = _two_mode_model(soft)
    zm = eng.zmap(model)
    assert [m for m, *_ in zm.mapped] == [0, 1] and model.soft_priors
    p = model.soft_priors[0][1]
    for omega0 in (0.9, 2.5, 7.0):
        top = min(p.hi, omega0)
        assert quad(lambda g: math.exp(p.logpdf_given(g, top)), p.lo, top)[0] == pytest.approx(1.0, abs=1e-8)
    theta = np.array([2.5, 0.4, 60.0, 1.2, 0.5, 50.0, 150.0, 4.0])
    f, lo, hi = eng.objective(model, theta)
    assert math.isfinite(f(zm.to_z(theta)))
    # its prior factor is the truncated Gaussian on [lo, min(hi, ceiling)], the same measure as the rest
    assert eng._ln_soft(model, theta) == pytest.approx(p.logpdf_given(0.5, 1.2))


# ---- behaviour --------------------------------------------------------------------------------------------------
def test_a_lone_spectrum_fits_no_mode_wider_than_the_cap():
    res = fast_pass.run_fast_pass(_spectrum([(10.0, 8.0, 40.0)], scale=3.0), settings=FastPassSettings(mode_ceiling=2))
    assert res.peaks and all(p.Gamma <= min(4.0, p.omega0) + 1e-9 for p in res.peaks)
    assert any(p.at_width_cap for p in res.peaks)


def test_a_sharp_mode_is_unchanged_and_is_not_marked():
    res = fast_pass.run_fast_pass(_spectrum([(10.0, 0.3, 120.0)]), settings=FastPassSettings(mode_ceiling=1))
    assert [p.at_width_cap for p in res.peaks] == [False] * len(res.peaks) and res.peaks
    peak = min(res.peaks, key=lambda p: abs(p.omega0 - 10.0))
    assert peak.Gamma == pytest.approx(0.3, abs=0.12)


def test_a_wide_mode_planted_at_low_energy_stays_under_its_own_energy():
    res = fast_pass.run_fast_pass(_spectrum([(2.5, 5.0, 60.0)], scale=2.0), settings=FastPassSettings(mode_ceiling=2))
    assert res.peaks and all(p.Gamma <= p.omega0 + 1e-9 for p in res.peaks)


# ---- the classic maximum-likelihood lane ------------------------------------------------------------------------
def _classic(modes, **kw):
    ctx = _spectrum(modes, **kw)
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, 1) if h.label == "elastic+stokes+bg")
    return ctx, hyp


def test_the_classic_fit_keeps_a_wide_mode_under_its_ceiling_and_its_evidence_is_finite():
    ctx, hyp = _classic([(2.5, 5.0, 60.0)], scale=2.0)
    fit = loop.fit_hypothesis(hyp, ctx)
    om, g = fit.params["phonon.omega0"], fit.params["phonon.Gamma"]
    assert g <= min(4.0, om) + 1e-9
    prob = loop.build_problem(hyp, ctx)
    assert prob.zmap is not None and prob.zmap.pairs == ((prob.free_names.index("phonon.omega0"),
                                                          prob.free_names.index("phonon.Gamma"), G_LO, False),)
    bundle = evidence.candidate_bundle(hyp, ctx)
    rec = evidence.structure_evidence(hyp, ctx, fit, bundle, escalation="defer")
    assert math.isfinite(rec.log_evidence) or rec.evidence_method == "deferred_escalation"
    if rec.evidence_method == "deferred_escalation":          # a Gamma at its ceiling escalates, never truncates
        assert math.isfinite(rec.validity["log_evidence_upper_bound"])


def test_the_classic_lane_reports_the_same_errors_in_the_z_coordinate_as_in_meV(monkeypatch):
    # an interior width: the z fit and the plain meV fit are one posterior, so the delta-method covariance
    # J cov_z J^T must reproduce the meV Hessian's errors
    ctx, hyp = _classic([(8.0, 0.8, 100.0)], scale=4.0)
    with_z = loop.fit_hypothesis(hyp, ctx)
    real = loop.build_problem

    def no_zmap(*a, **k):
        prob = real(*a, **k)
        prob.zmap = None
        return prob

    monkeypatch.setattr(loop, "build_problem", no_zmap)
    plain = loop.fit_hypothesis(hyp, ctx)
    assert with_z.covariance_ok and plain.covariance_ok
    for name in ("phonon.omega0", "phonon.Gamma"):
        assert with_z.params[name] == pytest.approx(plain.params[name], rel=2e-2)
        assert with_z.param_errors[name] == pytest.approx(plain.param_errors[name], rel=0.1)


def test_one_bootstrap_replicate_of_a_wide_mode_stays_under_its_ceiling():
    from isar.analyze.errors import bootstrap
    ctx, hyp = _classic([(2.5, 5.0, 60.0)], scale=2.0)
    fit = loop.fit_hypothesis(hyp, ctx)
    boot = bootstrap.parametric_bootstrap(hyp, ctx, fit, M=6, seed=1)
    gs = np.asarray(boot.gamma_samples, dtype=float)
    oms = np.asarray(boot.omega0_samples, dtype=float)
    assert gs.size and np.all(gs <= np.minimum(4.0, oms) + 1e-9)


def test_minos_offsets_are_in_mev_and_the_profile_scan_keeps_widths_under_the_ceiling():
    from isar.analyze.errors import profile
    ctx, hyp = _classic([(8.0, 0.8, 100.0)], scale=2.0)
    fit = loop.fit_hypothesis(hyp, ctx)
    out = profile.minos_intervals(hyp, ctx, fit)
    g = fit.params["phonon.Gamma"]
    if "phonon.Gamma" in out:
        lo, hi, _ok = out["phonon.Gamma"]
        assert lo <= g <= hi and hi <= 4.0 + 1e-9 and lo >= G_LO - 1e-9
    lo, hi = profile.omega0_profile_interval(hyp, ctx, fit)
    assert not (isinstance(lo, float) and math.isfinite(lo)) or lo < fit.omega0


def test_the_nested_sampler_draws_widths_under_their_ceiling_without_a_bundle():
    from isar.analyze.errors import bayes
    ctx, hyp = _classic([(2.5, 5.0, 60.0)], scale=2.0)
    fit = loop.fit_hypothesis(hyp, ctx)
    res = bayes.bayesian_posterior(hyp, ctx, fit, n_live=40, dlogz=5.0, max_iter=400)
    assert res.parameter_samples.size, "the sampler returned no samples"
    names = list(res.parameter_names)
    g = res.parameter_samples[:, names.index("phonon.Gamma")]
    om = res.parameter_samples[:, names.index("phonon.omega0")]
    assert np.all(g <= np.minimum(4.0, om) + 1e-9)


def test_the_transitional_flag_gives_the_old_coordinate():
    ctx = _spectrum([(2.5, 0.6, 60.0)])
    eng = fast_pass._Engine(ctx, FastPassSettings(mode_ceiling=1))
    eng.width_ceiling_active = False
    model = eng.model(eng.variant(1, True, True), ())
    zm = eng.zmap(model)
    assert not zm.active and not zm.mapped and model.hi[model.modes[0][1]] == 20.0     # the lifted box
    theta = np.array([2.5, 6.0, 60.0, 150.0, 4.0])
    assert np.array_equal(zm.to_z(theta), theta) and zm.ln_jac(theta) == 0.0
    assert eng.ln_prior(model, theta) == pytest.approx(eng.sp.ln_energies([2.5]) + eng.sp.ln_width(6.0)
                                                       + eng.sp.ln_height(60.0) + eng.sp.ln_height(150.0)
                                                       + eng.sp.ln_background(4.0))
    assert CompositeModel and PhononDHO and Elastic and ConstantBackground

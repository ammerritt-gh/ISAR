"""The mode prior recipe: v5 bounds (heights from the spectrum's highest count, the empirical-Bayes
amplitude rule; widths and energies declared), the v4 recipe as the legacy arm, and the mode
energies ordered once."""
import dataclasses
import math
import warnings

import numpy as np
import pytest

from isar.analyze import prior
from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.phonon import EPS_OMEGA0, GAMMA_FLOOR, GAMMA_MAX, GAMMA_MAX_V4


def _ctx(**kw):
    x = np.linspace(-10.0, 10.0, 81)
    d = dict(x=x, counts=np.full(81, 100.0), mask=np.ones(81, dtype=bool), T=100.0,
             center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
             energy_status="absolute")
    d.update(kw)
    return ScanContext(**d)


NAMES = ["phonon.omega0", "phonon.Gamma", "phonon.h", "phonon2.omega0", "phonon2.Gamma",
         "phonon2.h", "elastic.h", "qe.QE", "bg.BG"]


# --------------------------------------------------------------------------- the recipe's bounds
def test_amplitude_bounds_follow_max_count():
    # The operator's ruling (2026-10-25), replacing "bounds ignore observed counts": heights are
    # flat on [0, 1.2 x highest count], the background on [0, highest count]; the rest is declared.
    base = _ctx(exposure_scale=np.ones(81), exposure_reference=3.0e5, exposure_source="monitor")
    counts = base.counts.copy()
    counts[40] = 400.0
    loud = dataclasses.replace(base, counts=counts)
    b, l = (dict(zip(NAMES, prior.mode_bounds(NAMES, c))) for c in (base, loud))
    assert l["phonon.h"] == l["phonon2.h"] == l["elastic.h"] == (0.0, pytest.approx(480.0))
    assert l["bg.BG"] == (0.0, pytest.approx(400.0)) and b["bg.BG"] == (0.0, pytest.approx(100.0))
    for n in ("phonon.omega0", "phonon.Gamma", "qe.QE"):          # not read from the counts
        assert l[n] == b[n]
    # a masked point does not count, and the exposure rescales: counts per reference exposure
    masked = dataclasses.replace(loud, mask=np.arange(81) != 40)
    assert dict(zip(NAMES, prior.mode_bounds(NAMES, masked)))["bg.BG"] == (0.0, pytest.approx(100.0))
    halved = dataclasses.replace(loud, exposure_scale=np.where(np.arange(81) == 40, 0.5, 1.0))
    assert dict(zip(NAMES, prior.mode_bounds(NAMES, halved)))["bg.BG"] == (0.0, pytest.approx(800.0))


def test_mode_bounds_and_spectrum_prior_are_one_source():
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    sp = prior.resolve_prior_for(ctx)
    got = dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))
    assert got["phonon.omega0"] == sp.bounds("omega0") and got["phonon.h"] == sp.bounds("h")
    assert got["bg.BG"] == sp.bounds("bg") and got["phonon.Gamma"][1] == sp.bounds("Gamma")[1]
    assert prior.recipe_provenance(ctx) == sp.provenance()


def test_bounds_follow_declared_exposure_and_resolution():
    a = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    b = dataclasses.replace(a, exposure_reference=2.0e5)
    c = dataclasses.replace(a, gauss_fwhm=2.0)
    # re-pin: v4 is now the legacy arm; the quasi-elastic area rule is v4's in both
    amp = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx, prior.PriorSettings(legacy_v4=True))))["phonon.h"][1]
    area = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))["qe.QE"][1]
    assert amp(a) == pytest.approx(prior.AMPLITUDE_SCALE_PER_MONITOR * 1.0e5)
    assert amp(b) == pytest.approx(2.0 * amp(a))
    assert amp(c) == pytest.approx(amp(a))        # a height ceiling does not scale with the width
    assert area(c) == pytest.approx(2.0 * area(a))  # an area ceiling does
    bg = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx, prior.PriorSettings(legacy_v4=True))))["bg.BG"][1]
    assert bg(b) == pytest.approx(2.0 * bg(a))


def test_bounds_energy_width_amplitude_shapes():
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    got = dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))
    assert got["phonon.omega0"] == got["phonon2.omega0"] == (EPS_OMEGA0, 10.0)  # fit window
    assert got["phonon.Gamma"] == (GAMMA_FLOOR, 20.0)            # resolution 1.0 meV: floor wins
    # re-pin (recipe /5): heights 1.2 x max count, background the max count (counts are 100)
    assert got["phonon.h"] == got["phonon2.h"] == got["elastic.h"] == (0.0, pytest.approx(120.0))
    assert got["bg.BG"] == (0.0, pytest.approx(100.0))
    assert got["qe.QE"][0] == 0.0 and got["qe.QE"][1] == pytest.approx(1.0e5)   # v4 area rule
    v4 = dict(zip(NAMES, prior.mode_bounds(NAMES, ctx, prior.PriorSettings(legacy_v4=True))))
    assert v4["phonon.Gamma"] == (GAMMA_FLOOR, GAMMA_MAX_V4)
    assert v4["phonon.h"] == v4["elastic.h"] == v4["bg.BG"] == (0.0, pytest.approx(1.0e5))


def test_width_floor_scales_with_a_very_sharp_resolution():
    sharp = _ctx(gauss_fwhm=1.0e-3)
    lo, hi = dict(zip(NAMES, prior.mode_bounds(NAMES, sharp)))["phonon.Gamma"]
    assert lo == pytest.approx(prior.WIDTH_FLOOR_FRACTION * 1.0e-3) and lo < GAMMA_FLOOR
    assert hi == GAMMA_MAX


def test_no_declared_exposure_uses_the_nominal_one_not_the_counts():
    a = _ctx()                                    # exposure_reference None
    b = dataclasses.replace(a, counts=a.counts * 1.0e3)
    assert prior.recipe_exposure(a) == prior.NOMINAL_EXPOSURE
    legacy = prior.PriorSettings(legacy_v4=True)                                   # re-pin: the declared-exposure rule is the v4 arm
    assert prior.mode_bounds(NAMES, a, legacy) == prior.mode_bounds(NAMES, b, legacy)


def test_time_and_monitor_exposures_get_their_own_amplitude_scale():
    # Equal numeric exposure, different source: seconds are a count rate times time, not monitor
    # counts, so the two ceilings must differ.
    mon = _ctx(exposure_scale=np.ones(81), exposure_reference=30.0, exposure_source="monitor")
    tim = dataclasses.replace(mon, exposure_source="time")
    width = prior.resolution_fwhm(mon)
    bounds = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx, prior.PriorSettings(legacy_v4=True))))   # re-pin: v4 arm
    assert bounds(mon)["phonon.h"][1] == pytest.approx(prior.AMPLITUDE_SCALE_PER_MONITOR * 30.0 * width)
    assert bounds(tim)["phonon.h"][1] == pytest.approx(prior.AMPLITUDE_SCALE_PER_SECOND * 30.0 * width)
    assert bounds(tim)["bg.BG"][1] == pytest.approx(prior.AMPLITUDE_SCALE_PER_SECOND * 30.0)


def test_a_declared_exposure_with_no_source_is_refused():
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=4.0e5)
    with pytest.raises(ValueError, match="declared source"):
        prior.mode_bounds(NAMES, ctx)


def test_unusable_resolution_is_refused_not_defaulted():
    with pytest.raises(ValueError):
        prior.mode_bounds(NAMES, _ctx(gauss_fwhm=0.0, lorentz_fwhm=0.0))


def test_unknown_parameter_is_refused():
    with pytest.raises(ValueError):
        prior.mode_bounds(["phonon.Nonsense"], _ctx())


def test_provenance_records_the_empirical_bayes_quantity():
    # re-pin (recipe /6): the provenance names the data-derived max count, as the ruling requires,
    # and the near-zero energy penalty it placed, in meV and at the resolution FWHM it read
    p = prior.recipe_provenance(_ctx(exposure_scale=np.ones(81), exposure_reference=2.0e5,
                                     exposure_source="time"))
    assert p["version"] == prior.PRIOR_RECIPE_VERSION == "isar.mode-prior-recipe/6"
    assert p["energy_penalty"] == {"lo": EPS_OMEGA0, "hi": 10.0, "start": 1.0, "end": 0.25,
                                   "ceiling": 7.5, "corner": 0.05, "fwhm_meV": 1.0}
    assert p["settings"]["energy_ramp_ceiling"] == 7.5
    assert p["empirical_bayes"] is True and p["empirical_quantity"] == "max_count"
    assert p["max_count"] == 100.0 and p["height_hi"] == pytest.approx(120.0)
    assert p["settings"] == dataclasses.asdict(prior.PriorSettings())
    import json
    json.dumps(p)                                                  # JSON-able, recorded as is
    legacy = prior.recipe_provenance(_ctx(exposure_scale=np.ones(81), exposure_reference=2.0e5,
                                          exposure_source="time"), prior.PriorSettings(legacy_v4=True))
    assert legacy["settings"]["legacy_v4"] is True
    assert legacy["height_hi"] == pytest.approx(prior.AMPLITUDE_SCALE_PER_SECOND * 2.0e5)


# --------------------------------------------------------------------------- the energy density
def _ep(w, lo=EPS_OMEGA0, hi=20.0, **kw):
    return prior.energy_prior(lo, hi, w, prior.PriorSettings(**kw))


def _breakpoints(ep):
    return [x for x in (ep.end - ep.corner, ep.end + ep.corner, ep.start - ep.corner,
                        ep.start + ep.corner) if ep.lo < x < ep.hi]


WINDOWS = ((1.0, EPS_OMEGA0, 20.0), (1.5, EPS_OMEGA0, 20.0), (0.8, EPS_OMEGA0, 0.9))


def test_energy_prior_worked_examples():
    # C-energy-prior's table (adaptive quadrature at planning): 0 at and above 1 FWHM, 10 nats per
    # FWHM down to a 7.5-nat ceiling at 1/4 FWHM, each corner rounded over 0.05 FWHM.
    ep = _ep(1.0)
    assert [ep.penalty(e) for e in (0.1, 0.25, 0.5, 0.75, 1.0, 1.5)] == \
        pytest.approx([7.5, 7.375, 5.0, 2.5, 0.125, 0.0], abs=1e-12)
    assert ep.penalty(-3.0) == pytest.approx(7.5) and ep.penalty(40.0) == 0.0     # any real e
    assert ep.ln_z == pytest.approx(2.9494946237, abs=1e-8)
    assert ep.log_pdf(0.5) == pytest.approx(-7.94949462, abs=1e-8)
    assert ep.log_pdf(1.5) == pytest.approx(-2.94949462, abs=1e-8)
    assert list(ep.cdf([0.25, 0.5, 1.0])) == pytest.approx([7.2744e-6, 3.97334e-5, 5.1480102e-3],
                                                          abs=1e-10)
    assert prior.ordered_energy_log_prior([5, 9], ep) == pytest.approx(-5.20584207, abs=1e-8)
    assert prior.ordered_energy_log_prior([0.5, 5], ep) == pytest.approx(-10.20584207, abs=1e-8)
    wide = _ep(1.5)
    assert wide.ln_z == pytest.approx(2.9255485675, abs=1e-8)
    assert wide.log_pdf(0.75) == pytest.approx(-7.92554857, abs=1e-8)
    short = _ep(0.8, hi=0.9)                                    # the window ends inside the ramp
    assert short.ln_z == pytest.approx(-1.7313780752, abs=1e-8)
    assert short.log_pdf(0.8) == pytest.approx(1.60637808, abs=1e-8)
    assert float(short.cdf(0.4)) == pytest.approx(3.4280258e-3, abs=1e-10)
    assert ep.log_pdf(EPS_OMEGA0 / 2) == ep.log_pdf(20.01) == -math.inf
    assert ep.spec() == {"lo": EPS_OMEGA0, "hi": 20.0, "start": 1.0, "end": 0.25, "ceiling": 7.5,
                         "corner": 0.05}


def test_flat_energy_prior_is_recipe_5():
    lo, hi = EPS_OMEGA0, 20.0
    for ep in (prior.EnergyPrior.flat(lo, hi), _ep(1.0, energy_ramp_ceiling=0.0)):
        for n in range(4):
            e = list(np.linspace(1.0, 9.0, n))
            assert prior.ordered_energy_log_prior(e, ep) == \
                pytest.approx(math.lgamma(n + 1) - n * math.log(hi - lo), abs=1e-12)
        u = np.array([0.0, 0.3, 1.0])
        assert prior.ordered_energy_transform(u, ep) == \
            pytest.approx(prior.ordered_energy_transform(u, prior.EnergyPrior.flat(lo, hi)))


def test_the_ramp_is_validated():
    with pytest.raises(ValueError):
        prior.PriorSettings(energy_ramp_ceiling=-1.0)
    with pytest.raises(ValueError):
        prior.PriorSettings(energy_ramp_end_fwhm=0.05)          # end - corner must stay above 0
    with pytest.raises(ValueError):
        prior.PriorSettings(energy_ramp_start_fwhm=0.3)         # start - end < 2 corner
    prior.PriorSettings(energy_ramp_ceiling=0.0, energy_ramp_corner_fwhm=0.0)   # off: not read
    with pytest.raises(ValueError):
        prior.energy_prior(EPS_OMEGA0, 20.0, 0.0, prior.PriorSettings())
    with pytest.raises(ValueError):
        prior.EnergyPrior(2.0, 1.0, 1.0, 0.25, 7.5, 0.05)


def test_energy_density_is_normalised_on_each_window():
    from scipy import integrate
    for w, lo, hi in WINDOWS:
        ep = _ep(w, lo, hi)
        total = integrate.quad(lambda e: math.exp(ep.log_pdf(e)), lo, hi, points=_breakpoints(ep),
                               epsabs=1e-13, epsrel=1e-13, limit=200)[0]
        assert total == pytest.approx(1.0, abs=1e-10)
    # N = 2: N! g(e1) g(e2) integrates to 1 over the ordered region e1 < e2 of a 3 meV window
    ep = _ep(1.5, hi=3.0)
    total = integrate.dblquad(
        lambda e1, e2: math.exp(prior.ordered_energy_log_prior([e1, e2], ep)),
        ep.lo, ep.hi, lambda e2: ep.lo, lambda e2: e2, epsabs=1e-11, epsrel=1e-11)[0]
    assert total == pytest.approx(1.0, abs=1e-8)


def test_cdf_and_ppf_invert_each_other():
    for w, lo, hi in WINDOWS:
        ep = _ep(w, lo, hi)
        corners = [np.linspace(k - ep.corner, k + ep.corner, 401) for k in (ep.end, ep.start)]
        e = np.clip(np.concatenate([np.linspace(lo, hi, 4001), *corners, [lo, hi]]), lo, hi)
        assert np.max(np.abs(ep.ppf(ep.cdf(e)) - e)) <= 1e-10
        u = np.concatenate([np.linspace(0.0, 1.0, 4001), *(ep.cdf(c) for c in corners),
                            [0.0, 1e-15, 1.0 - 1e-15, 1.0]])
        assert np.max(np.abs(ep.cdf(ep.ppf(u)) - u)) <= 1e-12
        assert ep.ppf(0.0) == lo and ep.ppf(1.0) == hi


def test_ordered_transform_samples_the_penalised_density():
    from scipy import special, stats
    ep = _ep(1.5, hi=3.0)                     # the ramp (below 1.5 meV) holds 9% of the mass
    assert 0.05 < float(ep.cdf(ep.start)) < 0.5
    flat = prior.EnergyPrior.flat(ep.lo, ep.hi)
    rng = np.random.default_rng(11)
    for n in (1, 3):
        draws = np.array([prior.ordered_energy_transform(u, ep) for u in rng.random((20000, n))])
        assert np.all(np.diff(draws, axis=1) >= 0.0) and draws.min() >= ep.lo
        for k in range(1, n + 1):             # the k-th order statistic of n draws from g
            law = lambda x, k=k, d=ep: special.betainc(k, n - k + 1, d.cdf(x))
            assert stats.kstest(draws[:, k - 1], law).pvalue > 1e-3, (n, k)
        # the test can fail: the same draws are far from the unpenalised law
        unpenalised = lambda x, d=flat: special.betainc(1, n, d.cdf(x))
        assert stats.kstest(draws[:, 0], unpenalised).pvalue < 1e-6


def test_ordered_prior_counts_swapped_labels_once():
    # The labelled prior is prod g(E_i); the ordered one is N! prod g(E_i): the factor appears once.
    ep = _ep(1.0, hi=4.0)
    e = [0.5, 1.5, 3.0]
    labelled = sum(ep.log_pdf(v) for v in e)
    assert prior.ordered_energy_log_prior(e, ep) == pytest.approx(labelled + math.log(6.0))
    for perm in ((0, 2, 1), (1, 0, 2), (2, 1, 0), (1, 2, 0), (2, 0, 1)):
        assert prior.ordered_energy_log_prior([e[i] for i in perm], ep) == \
            prior.ordered_energy_log_prior(e, ep)
    assert prior.ordered_energy_log_prior([], ep) == 0.0


def test_ordered_prior_outside_the_window_is_impossible():
    ep = prior.EnergyPrior.flat(0.0, 4.0)
    assert prior.ordered_energy_log_prior([0.5, 4.5], ep) == -math.inf
    assert prior.ordered_energy_log_prior([-0.1, 1.0], ep) == -math.inf


def test_ordered_transform_endpoints():
    ep = _ep(1.0, lo=EPS_OMEGA0, hi=3.0)
    assert prior.ordered_energy_transform([0.0, 0.0], ep) == pytest.approx([EPS_OMEGA0] * 2)
    assert prior.ordered_energy_transform([1.0, 1.0], ep) == pytest.approx([3.0, 3.0])


def test_laplace_is_accurate_for_a_well_determined_energy_in_the_ramp():
    # Measured limit (STATS_PRIMER 2.1): a Gaussian likelihood times the penalised prior is not a
    # Gaussian, so the fast pass's Laplace evidence errs for a poorly determined low mode (up to
    # about 1.7 nats at sd = 1 FWHM). Asserted here only where it is promised: sd <= 0.05 FWHM
    # agrees with quadrature to 0.05 nats across the ramp, corners included.
    from scipy import integrate, optimize
    ep = _ep(1.0)
    pts = _breakpoints(ep)
    for s in (0.02, 0.05):
        for m in np.linspace(0.1, 1.5, 57):
            f = lambda e: 0.5 * ((e - m) / s) ** 2 - ep.log_pdf(e)
            grid = np.linspace(ep.lo, m + 10 * s + 2.0, 4001)
            k = int(np.argmin([f(e) for e in grid]))
            mode = optimize.minimize_scalar(f, bounds=(grid[max(k - 1, 0)], grid[k + 1]),
                                            method="bounded", options={"xatol": 1e-12}).x
            lap = prior.laplace_log_evidence(lambda t: f(t[0]), [mode], [ep.lo], [ep.hi])
            a, b = max(ep.lo, m - 12 * s), m + 12 * s
            inside = sorted(p for p in pts + [m] if a < p < b)
            with warnings.catch_warnings():   # quad's roundoff note on the near-delta case
                warnings.simplefilter("ignore", integrate.IntegrationWarning)
                exact = math.log(integrate.quad(lambda e: math.exp(-f(e)), a, b, points=inside,
                                                epsabs=0, epsrel=1e-11, limit=400)[0])
            assert lap.status in ("ok", "ok_boundary")
            assert abs(exact - lap.ln_z) <= 0.05, (s, m)


# --------------------------------------------------------------------------- model-level ordering
def test_mode_order_puts_the_lower_energy_first_and_keeps_the_curve():
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), Elastic(),
                           ConstantBackground()])
    names = comp.param_names()
    swapped = np.array([7.0, 0.4, 30.0,      # phonon  (higher)
                        3.0, 0.2, 50.0,      # phonon2 (lower)
                        100.0, 2.0])
    perm = comp.mode_order(swapped)
    ordered = swapped[perm]
    assert ordered[names.index("phonon.omega0")] == 3.0
    assert ordered[names.index("phonon2.omega0")] == 7.0
    assert ordered[names.index("phonon.h")] == 50.0 and ordered[names.index("phonon2.h")] == 30.0
    # already ordered input is untouched
    assert np.array_equal(comp.mode_order(ordered), np.arange(len(names)))
    ctx = _ctx()
    x = np.linspace(0.5, 9.0, 40)
    assert np.allclose(comp.evaluate(x, swapped, ctx), comp.evaluate(x, ordered, ctx))


# --------------------------------------------------------------------------- the bundle's ordered variants
def _two_mode_bundle():
    from isar.analyze import catalogue
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), ConstantBackground()])
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    names = comp.param_names()
    return names, catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx))


def test_bundle_ordered_logpdf_adds_the_factorial_once_and_ignores_labels():
    names, bundle = _two_mode_bundle()
    theta = dict(zip(names, [2.0, 0.3, 50.0, 6.0, 0.4, 30.0, 5.0]))
    swapped = dict(theta, **{"phonon.omega0": 6.0, "phonon2.omega0": 2.0})
    assert bundle.logpdf_ordered(theta) - bundle.logpdf(theta) == pytest.approx(math.log(2.0))
    assert bundle.logpdf_ordered(swapped) == pytest.approx(bundle.logpdf_ordered(theta))


def test_both_ordered_lanes_share_the_recipe_window():
    # Laplace (logpdf_ordered) and nested sampling (transform_ordered) must share the window the
    # recipe bounds, [EPS_OMEGA0, x_max], so the N! prod g normalisation is the same in each lane.
    names, bundle = _two_mode_bundle()
    top = 10.0
    i1, i2 = names.index("phonon.omega0"), names.index("phonon2.omega0")
    assert (bundle.params[i1].lo, bundle.params[i1].hi) == (EPS_OMEGA0, top)
    theta = dict(zip(names, [2.0, 0.3, 50.0, 6.0, 0.4, 30.0, 5.0]))
    lane = bundle.logpdf_ordered(theta) - bundle.logpdf(theta)
    assert lane == pytest.approx(math.log(2.0))                  # the same N! over the same W
    above = dict(theta, **{"phonon.omega0": top - 0.01})
    beyond = dict(theta, **{"phonon.omega0": top + 0.01})
    assert np.isfinite(bundle.logpdf_ordered(above))
    assert bundle.logpdf_ordered(beyond) == -math.inf
    u = np.random.default_rng(5).random((2000, len(names)))
    draws = np.array([bundle.transform_ordered(row) for row in u])
    assert draws[:, [i1, i2]].min() >= EPS_OMEGA0 and draws[:, [i1, i2]].max() <= top


def test_bundle_ordered_transform_is_the_ordered_prior_and_keeps_the_evidence():
    # A swap-symmetric likelihood has the same evidence under the labelled prior and under the
    # ordered one: the sampler for the ordered prior must not lose or double any mass.
    names, bundle = _two_mode_bundle()
    i1, i2 = names.index("phonon.omega0"), names.index("phonon2.omega0")
    g = lambda e: np.exp(-0.5 * ((e - 2.0) / 0.5) ** 2) + np.exp(-0.5 * ((e - 6.0) / 0.5) ** 2)
    rng = np.random.default_rng(3)
    u = rng.random((40000, len(names)))
    ordered = np.array([bundle.transform_ordered(row) for row in u])
    assert np.all(ordered[:, i1] <= ordered[:, i2])
    labelled = np.array([[p.ppf(v) for p, v in zip(bundle.params, row)] for row in u])
    z_ordered = (g(ordered[:, i1]) * g(ordered[:, i2])).mean()
    z_labelled = (g(labelled[:, i1]) * g(labelled[:, i2])).mean()
    assert z_ordered == pytest.approx(z_labelled, rel=0.06)


def test_the_bundle_and_the_fast_pass_price_one_prior():
    # The deep pass and the campaign path (PriorBundle) and the fast pass (SpectrumPrior) must give
    # the same log prior, the near-zero penalty included, or their evidences differ by a constant.
    from isar.analyze import catalogue
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), Elastic(),
                           ConstantBackground()])
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor",
               gauss_fwhm=1.3)
    names = comp.param_names()
    bundle = catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx))
    sp = prior.resolve_prior_for(ctx)
    omega = [p for p in bundle.params if p.name.endswith(".omega0")]
    assert {(p.kind, p.origin) for p in omega} == {("energy_ramp", "default_energy")}
    assert all(p.energy == sp.energy for p in omega)
    for e1, e2 in ((0.4, 6.0), (1.2, 1.31), (7.0, 2.0)):
        theta = dict(zip(names, [e1, 0.3, 50.0, e2, 0.4, 30.0, 80.0, 5.0]))
        want = sp.ln_prior([(e1, 0.3, 50.0), (e2, 0.4, 30.0)], 80.0, 5.0)
        assert bundle.logpdf_ordered(theta) == pytest.approx(want, abs=1e-10)


def test_bundle_leaves_an_informative_energy_prior_alone():
    from isar.analyze import catalogue
    from isar.schema.steering import ParamConstraints
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), ConstantBackground()])
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor",
               constraints=ParamConstraints(soft={"phonon.omega0": (3.0, 0.5)},
                                            origins={"phonon.omega0": "transport"}))
    names = comp.param_names()
    bundle = catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx))
    theta = dict(zip(names, [3.0, 0.3, 50.0, 6.0, 0.4, 30.0, 5.0]))
    assert bundle.logpdf_ordered(theta) == pytest.approx(bundle.logpdf(theta))


def test_a_mode_with_its_own_amplitude_or_width_prior_is_not_ordered():
    # The N! count needs exchangeable modes: a prior that singles out one mode's A or Gamma makes
    # the labels distinguishable, so the energies must stay unordered.
    from isar.analyze import catalogue
    from isar.schema.steering import ParamConstraints
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), ConstantBackground()])
    names = comp.param_names()
    theta = dict(zip(names, [2.0, 0.3, 50.0, 6.0, 0.4, 30.0, 5.0]))
    for name, (mu, sigma) in (("phonon.h", (50.0, 20.0)), ("phonon.Gamma", (0.3, 0.1))):
        ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor",
                   constraints=ParamConstraints(soft={name: (mu, sigma)}, origins={name: "test"}))
        bundle = catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx))
        assert np.isfinite(bundle.logpdf(theta))                  # not -inf == -inf
        assert bundle.logpdf_ordered(theta) == pytest.approx(bundle.logpdf(theta)), name


def test_a_delta_function_mode_is_ordered_with_the_others():
    # Re-pin (delta-mode twin rule, C-prior example 9): a degenerate twin pins one mode's Gamma at
    # the floor (no width factor), and its energy is still one of the k exchangeable energies the
    # k! counts: all three omega0 are ordered together, one 3!, one ordered transform. (Before the
    # rule the delta mode was left out and only the DHO pair was ordered, ln 2.)
    from isar.analyze import catalogue
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), PhononDHO(prefix="phonon3"),
                           ConstantBackground()])
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    names = comp.param_names()
    bundle = catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx),
                                      exclude=["phonon2.Gamma"])
    theta = dict(zip(names, [2.0, 0.3, 50.0, 4.0, GAMMA_FLOOR, 20.0, 6.0, 0.4, 30.0, 5.0]))
    assert bundle.logpdf_ordered(theta) - bundle.logpdf(theta) == pytest.approx(math.log(6.0))
    free = [p.name for p in bundle.params]
    i1, i2, i3 = (free.index(f"{p}.omega0") for p in ("phonon", "phonon2", "phonon3"))
    u = np.random.default_rng(7).random((2000, len(free)))
    draws = np.array([bundle.transform_ordered(row) for row in u])
    assert np.all(draws[:, i1] <= draws[:, i2]) and np.all(draws[:, i2] <= draws[:, i3])

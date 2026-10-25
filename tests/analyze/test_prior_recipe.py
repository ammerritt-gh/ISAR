"""The declared-quantity prior recipe (S1U1): bounds from the energy window, the resolution width
and the exposure, never from the spectrum's own counts; mode energies ordered once."""
import dataclasses
import itertools
import math

import numpy as np
import pytest

from isar.analyze import prior
from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.phonon import EPS_OMEGA0, GAMMA_FLOOR, GAMMA_MAX


def _ctx(**kw):
    x = np.linspace(-10.0, 10.0, 81)
    d = dict(x=x, counts=np.full(81, 10.0), mask=np.ones(81, dtype=bool), T=100.0,
             center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
             energy_status="absolute")
    d.update(kw)
    return ScanContext(**d)


NAMES = ["phonon.omega0", "phonon.Gamma", "phonon.h", "phonon2.omega0", "phonon2.Gamma",
         "phonon2.h", "elastic.h", "qe.QE", "bg.BG"]


# --------------------------------------------------------------------------- the recipe's bounds
def test_bounds_ignore_observed_counts():
    base = _ctx(exposure_scale=np.ones(81), exposure_reference=3.0e5, exposure_source="monitor")
    loud = dataclasses.replace(base, counts=base.counts * 1.0e4 + 7.0)
    assert prior.mode_bounds(NAMES, base) == prior.mode_bounds(NAMES, loud)


def test_bounds_follow_declared_exposure_and_resolution():
    a = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    b = dataclasses.replace(a, exposure_reference=2.0e5)
    c = dataclasses.replace(a, gauss_fwhm=2.0)
    amp = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))["phonon.h"][1]
    area = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))["qe.QE"][1]
    assert amp(a) == pytest.approx(prior.AMPLITUDE_SCALE_PER_MONITOR * 1.0e5)
    assert amp(b) == pytest.approx(2.0 * amp(a))
    assert amp(c) == pytest.approx(amp(a))        # a height ceiling does not scale with the width
    assert area(c) == pytest.approx(2.0 * area(a))  # an area ceiling does
    bg = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))["bg.BG"][1]
    assert bg(b) == pytest.approx(2.0 * bg(a))


def test_bounds_energy_width_amplitude_shapes():
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    got = dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))
    assert got["phonon.omega0"] == got["phonon2.omega0"] == (EPS_OMEGA0, 10.0)  # fit window
    assert got["phonon.Gamma"] == (GAMMA_FLOOR, GAMMA_MAX)       # resolution 1.0 meV: floor wins
    for n in ("phonon.h", "phonon2.h", "elastic.h", "qe.QE"):
        assert got[n][0] == 0.0 and got[n][1] > 0.0 and np.isfinite(got[n][1])
    assert got["phonon.h"] == got["elastic.h"] == got["qe.QE"]


def test_width_floor_scales_with_a_very_sharp_resolution():
    sharp = _ctx(gauss_fwhm=1.0e-3)
    lo, hi = dict(zip(NAMES, prior.mode_bounds(NAMES, sharp)))["phonon.Gamma"]
    assert lo == pytest.approx(prior.WIDTH_FLOOR_FRACTION * 1.0e-3) and lo < GAMMA_FLOOR
    assert hi == GAMMA_MAX


def test_no_declared_exposure_uses_the_nominal_one_not_the_counts():
    a = _ctx()                                    # exposure_reference None
    b = dataclasses.replace(a, counts=a.counts * 1.0e3)
    assert prior.recipe_exposure(a) == prior.NOMINAL_EXPOSURE
    assert prior.mode_bounds(NAMES, a) == prior.mode_bounds(NAMES, b)


def test_time_and_monitor_exposures_get_their_own_amplitude_scale():
    # Equal numeric exposure, different source: seconds are a count rate times time, not monitor
    # counts, so the two ceilings must differ.
    mon = _ctx(exposure_scale=np.ones(81), exposure_reference=30.0, exposure_source="monitor")
    tim = dataclasses.replace(mon, exposure_source="time")
    width = prior.resolution_fwhm(mon)
    bounds = lambda ctx: dict(zip(NAMES, prior.mode_bounds(NAMES, ctx)))
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


def test_provenance_names_the_version_and_the_declared_quantities():
    p = prior.recipe_provenance(_ctx(exposure_scale=np.ones(81), exposure_reference=2.0e5,
                                     exposure_source="time"))
    assert p["version"] == prior.PRIOR_RECIPE_VERSION
    assert p["exposure"] == 2.0e5 and p["exposure_source"] == "time"
    assert p["amplitude_scale"] == prior.AMPLITUDE_SCALE_PER_SECOND


# --------------------------------------------------------------------------- ordered energies
def _ordered_cells(n, lo, hi, m):
    """Midpoint cells of [lo, hi]^n inside the ordered region e_1 < ... < e_n, with the share of
    each cell that lies inside it (a cell whose indices tie in a run of k is 1/k! inside)."""
    mids = lo + (np.arange(m) + 0.5) * (hi - lo) / m
    cell = ((hi - lo) / m) ** n
    for idx in itertools.combinations_with_replacement(range(m), n):
        share = 1.0
        for _, run in itertools.groupby(idx):
            share /= math.factorial(len(list(run)))
        yield tuple(mids[i] for i in idx), cell * share


def test_ordered_prior_is_normalised_on_the_ordered_region():
    lo, hi = -2.0, 5.0
    for n, m in ((1, 1), (2, 120), (3, 40)):
        total = sum(math.exp(prior.ordered_energy_log_prior(p, lo, hi)) * w
                    for p, w in _ordered_cells(n, lo, hi, m))
        assert total == pytest.approx(1.0, rel=1e-9)


def test_ordered_prior_counts_swapped_labels_once():
    # The labelled flat prior is 1/W^N; the ordered density is N!/W^N: the factor appears once.
    lo, hi = 0.0, 4.0
    e = [0.5, 1.5, 3.0]
    flat = -3 * math.log(hi - lo)
    assert prior.ordered_energy_log_prior(e, lo, hi) == pytest.approx(flat + math.log(6.0))
    for perm in ((0, 2, 1), (1, 0, 2), (2, 1, 0), (1, 2, 0), (2, 0, 1)):
        assert prior.ordered_energy_log_prior([e[i] for i in perm], lo, hi) == \
            prior.ordered_energy_log_prior(e, lo, hi)


def test_ordered_prior_outside_the_window_is_impossible():
    assert prior.ordered_energy_log_prior([0.5, 4.5], 0.0, 4.0) == -math.inf
    assert prior.ordered_energy_log_prior([-0.1, 1.0], 0.0, 4.0) == -math.inf


def test_ordered_transform_gives_sorted_order_statistics():
    rng = np.random.default_rng(7)
    n, lo, hi = 3, -1.0, 3.0
    draws = np.array([prior.ordered_energy_transform(u, lo, hi) for u in rng.random((20000, n))])
    assert np.all(np.diff(draws, axis=1) >= 0.0) and draws.min() >= lo and draws.max() <= hi
    # order statistics of n uniforms on [lo, hi]: E[e_(i)] = lo + (hi - lo) i / (n + 1)
    expect = lo + (hi - lo) * np.arange(1, n + 1) / (n + 1)
    assert draws.mean(axis=0) == pytest.approx(expect, abs=0.03)
    # and the joint law matches the ordered density: P(e_(1) > t) = ((hi - t)/(hi - lo))^n
    t = 0.5
    assert (draws[:, 0] > t).mean() == pytest.approx(((hi - t) / (hi - lo)) ** n, abs=0.01)


def test_ordered_transform_endpoints():
    assert prior.ordered_energy_transform([0.0, 0.0], 1.0, 3.0) == pytest.approx([1.0, 1.0])
    assert prior.ordered_energy_transform([1.0, 1.0], 1.0, 3.0) == pytest.approx([3.0, 3.0])


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
    # recipe bounds, [EPS_OMEGA0, x_max], so the N!/W^N normalisation is the same in each lane.
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
    # A swap-symmetric likelihood has the same evidence under the flat labelled prior and under
    # the ordered one: the sampler for the ordered prior must not lose or double any mass.
    names, bundle = _two_mode_bundle()
    i1, i2 = names.index("phonon.omega0"), names.index("phonon2.omega0")
    g = lambda e: np.exp(-0.5 * ((e - 2.0) / 0.5) ** 2) + np.exp(-0.5 * ((e - 6.0) / 0.5) ** 2)
    rng = np.random.default_rng(3)
    u = rng.random((40000, len(names)))
    ordered = np.array([bundle.transform_ordered(row) for row in u])
    assert np.all(ordered[:, i1] <= ordered[:, i2])
    lo = np.array([p.lo for p in bundle.params])
    hi = np.array([p.hi for p in bundle.params])
    flat = lo + u * (hi - lo)
    z_ordered = (g(ordered[:, i1]) * g(ordered[:, i2])).mean()
    z_flat = (g(flat[:, i1]) * g(flat[:, i2])).mean()
    assert z_ordered == pytest.approx(z_flat, rel=0.06)


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
        assert bundle.logpdf_ordered(theta) == pytest.approx(bundle.logpdf(theta)), name


def test_a_delta_function_mode_leaves_the_other_modes_ordered_among_themselves():
    # A degenerate twin pins one mode's Gamma at the floor (no prior factor): that delta-function
    # mode is distinguishable from the DHO modes, but the two DHO modes are still exchangeable, so
    # they alone are ordered (2!, once) in both lanes; the delta mode's energy keeps its flat prior.
    from isar.analyze import catalogue
    comp = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), PhononDHO(prefix="phonon3"),
                           ConstantBackground()])
    ctx = _ctx(exposure_scale=np.ones(81), exposure_reference=1.0e5, exposure_source="monitor")
    names = comp.param_names()
    bundle = catalogue.resolve_priors(comp, ctx, prior.mode_bounds(names, ctx),
                                      exclude=["phonon2.Gamma"])
    theta = dict(zip(names, [2.0, 0.3, 50.0, 4.0, GAMMA_FLOOR, 20.0, 6.0, 0.4, 30.0, 5.0]))
    assert bundle.logpdf_ordered(theta) - bundle.logpdf(theta) == pytest.approx(math.log(2.0))
    free = [p.name for p in bundle.params]
    i1, i2, i3 = (free.index(f"{p}.omega0") for p in ("phonon", "phonon2", "phonon3"))
    u = np.random.default_rng(7).random((2000, len(free)))
    draws = np.array([bundle.transform_ordered(row) for row in u])
    assert np.all(draws[:, i1] <= draws[:, i3])                  # the DHO pair, ordered
    assert np.any(draws[:, i2] < draws[:, i1]) and np.any(draws[:, i2] > draws[:, i3])

"""Exact evaluator work is reused without changing spectra, likelihoods, or ordered priors."""
import math

import numpy as np
import pytest

from isar.analyze import catalogue, components, estimators, evidence, lineshapes as L, loop, prior
from isar.analyze.context import ScanContext
from isar.analyze.errors.bayes import EvidenceProblem
from isar.analyze.resolution import TabulatedResolution


def _context(tabulated=False, T=300.0):
    x = np.linspace(-8.0, 20.0, 113)
    curve = None
    if tabulated:
        grid = np.arange(-80, 101) * 0.1
        values = np.exp(-0.5 * (grid / np.where(grid < 0.0, 0.4, 0.7)) ** 2)
        curve = TabulatedResolution(grid, values, "synthetic", None, "generated")
    return ScanContext(x=x, counts=np.full(x.size, 20.0), mask=np.ones(x.size, bool),
                       T=T, center=0.13, gauss_fwhm=1.1, lorentz_fwhm=0.3,
                       instrument="S30", energy_status="absolute", resolution_curve=curve)


def _legacy_average(z, h):
    # The former four-stencil expression is an independent rounding reference.
    f = L._third_antiderivative
    return (f(z + 1.5 * h) - 3.0 * f(z + 0.5 * h)
            + 3.0 * f(z - 0.5 * h) - f(z - 1.5 * h)) / h ** 3


@pytest.mark.parametrize("tabulated", [False, True])
def test_height_only_columns_keep_full_public_peak_areas(tabulated):
    ctx = _context(tabulated)
    modes = [(5.0, L.GAMMA_CLIP), (7.0, 0.001), (9.0, 1.5), (3.0, 5.0), (0.001, 0.02)]
    peaks = ["stokes+astokes"] * len(modes)
    full, peaks_full = components.phonon_unit_columns(ctx.x, ctx, modes, peaks)
    fast, peaks_fast = components.phonon_unit_columns(ctx.x, ctx, modes, peaks, areas=False)
    np.testing.assert_array_equal(fast, full)
    for complete, height_only in zip(peaks_full, peaks_fast):
        assert complete.height == height_only.height and complete.position == height_only.position
        assert complete.area_stokes > 0.0 and complete.area_both > 0.0
        assert math.isnan(height_only.area_stokes) and math.isnan(height_only.area_both)


@pytest.mark.parametrize("T", [None, 0.0, -1.0, float("nan"), 10.0, 300.0])
def test_prepared_grid_and_bose_values_keep_temperature_and_sides(T):
    ctx = _context(True, T)
    conv = components._convolver(ctx.x, ctx)
    prepared = L._dho_grid_values(conv, T)
    assert prepared is L._dho_grid_values(conv, T)
    assert all(not values.flags.writeable for values in prepared)
    for peak in ("stokes", "astokes", "stokes+astokes"):
        direct = L.dho_signal(conv.grid, 3.0, 0.001, 1.0, T, peak)
        cached = L.dho_signal(conv.grid, 3.0, 0.001, 1.0, T, peak, _prepared=prepared)
        np.testing.assert_array_equal(cached, direct)
    other = L._dho_grid_values(conv, 150.0)
    assert other[0] is prepared[0]
    assert not np.array_equal(other[1], prepared[1])


@pytest.mark.parametrize("tabulated", [False, True])
@pytest.mark.parametrize("energy, width, T", [
    (5.0, L.GAMMA_CLIP, 300.0), (5.01, 0.001, 300.0), (5.0, 0.075, 300.0),
    (5.0, 1.5, 300.0), (3.0, 5.0, 300.0), (0.001, 0.02, 300.0),
    (5.0, 0.001, None), (5.0, 0.001, 0.0), (5.0, 0.001, 10.0),
])
def test_shared_stencil_preserves_poisson_likelihood(monkeypatch, tabulated, energy, width, T):
    ctx = _context(tabulated, T)
    composite = components.CompositeModel([components.PhononDHO(), components.ConstantBackground()])
    theta = [energy, width, 40.0, 2.0]
    shared = composite.evaluate(ctx.x, theta, ctx)
    monkeypatch.setattr(L, "_pole_average", _legacy_average)
    reference = composite.evaluate(ctx.x, theta, ctx)
    np.testing.assert_allclose(shared, reference, rtol=2e-8, atol=1e-7)
    counts = np.random.default_rng(734).poisson(reference).astype(float)
    counts[::3] = 0.0
    cost = estimators.get_estimator("baker_cousins").cost
    for observation in (counts, np.zeros_like(counts)):
        assert abs(0.5 * (cost(observation, shared) - cost(observation, reference))) < 1e-5


@pytest.mark.parametrize("delta", [False, True])
def test_cached_prior_groups_preserve_hard_fixed_ordered_problem(monkeypatch, delta):
    ctx = _context()
    plain = next(h for h in catalogue.enumerate_count_structures(ctx, 2)
                 if h.label == "elastic+stokes2+bg")
    hyp = loop.delta_twin_hypothesis(plain, ["phonon2"], ctx) if delta else plain
    bundle = evidence.candidate_bundle(hyp, ctx)
    names = [p.name for p in bundle.params]
    group = [names.index("phonon.omega0"), names.index("phonon2.omega0")]
    original = catalogue.PriorBundle._mode_priors
    calls = []

    def count_grouping(self, index):
        calls.append(index)
        return original(self, index)

    monkeypatch.setattr(catalogue.PriorBundle, "_mode_priors", count_grouping)
    problem = EvidenceProblem(hyp, loop.candidate_context(hyp, ctx), bundle)
    loglike, transform, ndim = problem()
    for u in (np.zeros(ndim), np.full(ndim, 0.3), np.ones(ndim)):
        expected = np.array([p.ppf(v) for p, v in zip(bundle.params, u)])
        first = bundle.params[group[0]]
        assert first.kind == "energy_ramp"
        expected[group] = prior.ordered_energy_transform(u[group], first.energy)
        actual = transform(u)
        np.testing.assert_array_equal(actual, expected)
        values = dict(zip(names, actual))
        reference = sum(p.logpdf(values[p.name]) for i, p in enumerate(bundle.params) if i not in group)
        reference += prior.ordered_energy_log_prior(actual[group], first.energy)
        assert bundle.logpdf_ordered(values) == reference
        assert math.isfinite(loglike(actual))
        if delta:
            assert problem.full_params(actual)["phonon2.Gamma"] == loop.GAMMA_FLOOR
    assert calls == group
    listed = bundle._energy_groups()
    listed[0].clear()
    assert bundle._energy_groups() == [group]

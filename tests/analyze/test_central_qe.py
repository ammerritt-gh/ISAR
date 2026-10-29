"""The BL43LXU central quasi-elastic component: its width density, the lanes that share it, its
line shape and its seeds (S1U1). No real data: the committed resolution curve and a synthetic window.
"""
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.integrate import quad, trapezoid

from isar.analyze import catalogue, components, phonon, prior
from isar.analyze.components import (CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO,
                                     central_qe_columns)
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import KB
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
T = 97.0


def _ctx(instrument="BL43LXU", x=None, counts=None):
    x = np.arange(-6.0, 12.0, 0.25) if x is None else x
    counts = np.full(x.size, 20.0) if counts is None else counts
    return ScanContext(x=x, counts=counts, mask=np.ones(x.size, bool), T=T, center=0.0,
                       gauss_fwhm=0.0, lorentz_fwhm=0.0, instrument=instrument,
                       energy_status="absolute", resolution_curve=CURVE)


# ----------------------------------------------------------------------------------- the density
def test_the_width_density_is_a_normalised_closed_form_distribution():
    p = prior.qe_width_prior(1.13)
    pieces = [(p.lo, p.knee - p.corner), (p.knee - p.corner, p.knee + p.corner),
              (p.knee + p.corner, p.hi)]
    total = sum(quad(lambda w: math.exp(p.log_pdf(w)), a, b, epsabs=1e-13, epsrel=1e-12)[0]
                for a, b in pieces)
    assert total == pytest.approx(1.0, abs=1e-9)
    w = np.linspace(p.lo, 15.0, 301)       # beyond ~25 meV the CDF is 1 to double precision
    np.testing.assert_allclose(p.ppf(p.cdf(w)), w, atol=1e-9)
    assert p.cdf(p.lo) == 0.0 and p.cdf(p.hi) == 1.0
    # the support starts at one resolution FWHM (operator, 2026-10-29: a narrower QE can stand in
    # for the elastic line), flat to 2 FWHM, then 1 nat per FWHM (slope 1/fwhm per meV)
    assert p.lo == pytest.approx(1.13) and p.log_pdf(0.99 * 1.13) == -math.inf
    assert p.log_pdf(p.lo) == pytest.approx(p.log_pdf(p.knee - 1.5 * p.corner))
    assert p.log_pdf(8.0) - p.log_pdf(5.0) == pytest.approx(-(8.0 - 5.0) / 1.13)
    assert p.log_pdf(p.lo - 1e-6) == -math.inf and p.log_pdf(p.hi + 1e-6) == -math.inf
    flat = p.log_pdf(p.lo)
    assert flat - p.log_pdf(6.0) == pytest.approx(3.3, abs=0.05)
    assert flat - p.log_pdf(23.0) == pytest.approx(18.35, abs=0.05)
    assert flat - p.log_pdf(p.hi) == pytest.approx(40.0, abs=1e-9)    # the support ends at 40 nats


def test_the_width_settings_refuse_nonsense():
    for bad in ({"slope": 0.0}, {"floor_fwhm": 1.99}, {"tail_nats": float("inf")}):
        with pytest.raises(ValueError):
            prior.QEWidthSettings(**bad)


# --------------------------------------------------------------------------------- lane agreement
def test_the_fast_pass_density_and_the_sampler_transform_are_one_prior():
    ctx = _ctx()
    sp = prior.resolve_prior_for(ctx)
    model = CompositeModel([Elastic(), CentralQE(), ConstantBackground()])
    names = model.param_names()
    bounds = prior.mode_bounds(names, ctx)
    bundle = catalogue.resolve_priors(model, ctx, bounds)
    qe = bundle.by_name()["qe.width"]
    assert qe.kind == "qe_width" and qe not in bundle.nontrivial
    assert bounds[names.index("qe.width")] == sp.bounds("qe_width")
    assert (qe.lo, qe.hi) == (sp.qe_width.lo, sp.qe_width.hi)
    for w in (0.4 * ctx.gauss_fwhm, 1.5 * ctx.gauss_fwhm, 3.0, 12.0):
        assert qe.logpdf(w) == sp.ln_qe_width(w)
    for u in (0.0, 0.1, 0.5, 0.93, 1.0):
        assert qe.ppf(u) == float(sp.qe_width.ppf(u))
    assert bundle.transform_ordered(np.full(len(names), 0.5)).shape == (len(names),)
    assert sp.provenance()["qe_width_prior"]["recipe"] == prior.CENTRAL_QE_RECIPE_VERSION


def test_a_spectrum_off_bl43lxu_has_no_qe_width_prior():
    ctx = _ctx(instrument="S30")
    sp = prior.resolve_prior_for(ctx)
    assert sp.qe_width is None and "qe_width_prior" not in sp.provenance()
    assert "qe_width_prior" not in prior.recipe_provenance(ctx)
    assert not phonon.central_qe_admissible(ctx) and phonon.central_qe_admissible(_ctx())
    with pytest.raises(ValueError):
        sp.bounds("qe_width")


# ------------------------------------------------------------------------------------ line shape
def test_the_relaxational_factor_is_detailed_balance_and_finite_at_zero():
    r = components._relaxational_factor
    ratio = float(r(-3.0, T) / r(3.0, T))
    assert ratio == pytest.approx(math.exp(-3.0 / (KB * T)))
    assert 0.65 < ratio < 0.75
    assert float(r(0.0, T)) == 1.0
    assert float(r(2.0, None)) == 1.0 and float(r(2.0, -5.0)) == 1.0     # unknown T: factor 1


def test_the_unit_column_and_the_composite_agree_with_the_basis():
    x = np.arange(-6.0, 12.0, 0.05)               # on the convolver's nodes, so the peak is hit
    ctx = _ctx(x=x)
    cols, _ = central_qe_columns(x, ctx, [0.5, 2.0, 9.0])
    np.testing.assert_allclose(cols.max(axis=1), 1.0, atol=1e-12)
    model = CompositeModel([Elastic(), CentralQE(), ConstantBackground()])
    assert model.param_names() == ["elastic.h", "qe.width", "qe.h", "bg.BG"]
    theta = np.array([30.0, 2.0, 12.0, 3.0])
    basis = model.basis(x, theta, ctx)
    np.testing.assert_allclose(model.evaluate(x, theta, ctx), basis @ theta[[0, 2, 3]], rtol=1e-12)
    pinned = CentralQE(width=2.0)
    assert pinned.param_names() == ["h"]
    np.testing.assert_allclose(pinned.unit_columns(x, {}, ctx)["h"],
                               CentralQE().unit_columns(x, {"width": 2.0}, ctx)["h"], rtol=1e-12)
    assert CompositeModel([pinned]).static_columns(x, ctx)


def test_the_spectral_weight_is_the_intrinsic_integral_over_the_window():
    ctx = _ctx()
    width, h = 2.5, 40.0
    weight = CentralQE().spectral_weight({"width": width, "h": h}, ctx)
    # independent: the convolution on a fine grid for the peak, a fine trapezoid for the integral
    dx = 0.005
    grid = np.arange(-80.0, 80.0 + dx / 2, dx)
    shape = (width / (2 * np.pi)) / (grid ** 2 + (0.5 * width) ** 2) \
        * (grid / (KB * T)) / (-np.expm1(-np.where(grid == 0, 1.0, grid) / (KB * T)))
    shape[grid == 0] = (width / (2 * np.pi)) / (0.5 * width) ** 2
    kernel = CURVE.profile(np.arange(-40.0, 40.0 + dx / 2, dx))
    peak = np.convolve(shape, kernel * dx, mode="same").max()
    lo, hi = ctx.window
    inside = (grid >= lo) & (grid <= hi)
    area = trapezoid(shape[inside], grid[inside])
    assert weight == pytest.approx(h * area / peak, rel=2e-3)


def test_the_spectral_weight_needs_no_numpy_two(monkeypatch):
    # pyproject allows NumPy 1.26, which has no np.trapezoid: the weight must compute without it.
    ctx = _ctx()
    expected = CentralQE().spectral_weight({"width": 2.5, "h": 40.0}, ctx)
    monkeypatch.delattr(np, "trapezoid", raising=False)
    assert CentralQE().spectral_weight({"width": 2.5, "h": 40.0}, ctx) == pytest.approx(expected, rel=1e-12)


# --------------------------------------------------------------------------------------- the seeds
def _synthetic_two_mode_composite():
    model = CompositeModel([PhononDHO(), PhononDHO(prefix="phonon2"), Elastic(), CentralQE(),
                            ConstantBackground()])
    x = np.arange(-6.0, 12.0, 0.25)
    theta = dict(zip(model.param_names(), [3.0, 0.5, 60.0, 7.0, 0.8, 40.0, 100.0, 2.0, 30.0, 4.0]))
    mean = model.evaluate(x, [theta[n] for n in model.param_names()], _ctx(x=x))
    counts = np.random.default_rng(0).poisson(mean).astype(float)
    return model, _ctx(x=x, counts=counts)


def test_every_seed_path_holds_the_qe_width_at_the_resolution():
    model, ctx = _synthetic_two_mode_composite()
    width = prior.resolution_fwhm(ctx)
    names = model.param_names()
    ic = phonon.phonon_structure_ic(model, ctx)
    seeds = {"structure": dict(zip(names, ic)),
             "greedy": phonon.greedy_multimode_ic(model, ctx, None),
             "placed": phonon.placed_modes_ic(model, ctx, [3.0, 7.0])}
    prev = SimpleNamespace(params={"phonon.omega0": 3.0, "phonon.Gamma": 0.5, "phonon.h": 60.0,
                                   "elastic.h": 100.0, "qe.h": 30.0, "bg.BG": 4.0})
    split = phonon.split_mode_ic(model, ctx, prev, "phonon", 1.0, heavier="low")
    assert split is not None
    seeds["split"] = split
    for kind, seed in seeds.items():
        assert set(seed) == set(names), kind
        assert all(np.isfinite(v) for v in seed.values()), kind
        assert seed["qe.width"] == width, kind


def test_a_phonon_less_qe_composite_gets_one_nnls_seed():
    model = CompositeModel([Elastic(), CentralQE(), ConstantBackground()])
    ctx = _ctx(counts=np.full(72, 15.0))
    seed = dict(zip(model.param_names(), phonon.phonon_structure_ic(model, ctx)))
    assert seed["qe.width"] == prior.resolution_fwhm(ctx)
    assert all(np.isfinite(v) and v >= 0 for v in seed.values())


# ------------------------------------------------------------------------------- the reducing helper
def test_without_component_drops_one_prefix_and_its_label_token():
    from isar.analyze import loop
    from isar.analyze.structures import Hypothesis
    hyp = Hypothesis("elastic+stokes+qe+bg@acoustic",
                     (PhononDHO(), Elastic, CentralQE, ConstantBackground))
    assert loop.without_component(hyp, "qe").label == "elastic+stokes+bg@acoustic"
    assert loop.without_component(hyp, "elastic").label == "stokes+qe+bg@acoustic"
    both = loop.without_component(loop.without_component(hyp, "elastic"), "qe")
    assert both.label == "stokes+bg@acoustic" and len(both.components) == 2
    assert loop.without_background(hyp).label == loop.without_component(hyp, "bg").label
    assert loop.without_component(Hypothesis("bg", (ConstantBackground,)), "bg") is None

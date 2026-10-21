"""Tabulated resolution at the fit seam (isar.analyze.resolution, lineshapes, components, context).

Curves are generated here, never read from files: an asymmetric split Gaussian with long tails
on a 0.1 meV grid (the shape and spacing of a measured curve) and a Gaussian on a fine grid (to
compare against the Voigt path with Lorentz width 0).
"""
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.optimize import brentq

from isar.analyze import context as CTX
from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic, PhononDHO,
                                     QuasiElastic)
from isar.analyze.lineshapes import CONVOLUTION_DX, Voigt, convolute_voigt_dho
from isar.analyze.resolution import ResolutionLibrary, TabulatedResolution
from isar.schema import axes

DX = CONVOLUTION_DX
SIGMA_L, SIGMA_R = 0.4, 0.7


def _asym(e):
    """Split Gaussian (narrow left, broad right) plus weak exponential tails, long on the right."""
    e = np.asarray(e, dtype=float)
    core = np.where(e < 0, np.exp(-0.5 * (e / SIGMA_L) ** 2), np.exp(-0.5 * (e / SIGMA_R) ** 2))
    return core + 0.02 * np.where(e > 0, np.exp(-e / 3.0), np.exp(e / 0.5))


def _asym_curve(detector="d1", setting="60x60"):
    e = np.round(np.arange(-8.0, 12.0 + 1e-9, 0.1), 10)
    return TabulatedResolution(e, 1000.0 * _asym(e), detector, setting, "generated")


def _gauss_curve(fwhm=1.2):
    e = np.round(np.arange(-8.0, 8.0 + 1e-9, 0.01), 10)
    return TabulatedResolution(e, Voigt(e, 1.0, fwhm, 0.0, 0.0), "d1", None, "gaussian")


def _half_max(x, y):
    """(left crossing, right crossing) of a sampled peak by linear interpolation."""
    i = int(np.argmax(y))
    h = y[i] / 2
    lo = np.nonzero(y[:i] < h)[0][-1]
    hi = i + 1 + np.nonzero(y[i + 1:] < h)[0][0]
    left = x[lo] + (h - y[lo]) * (x[lo + 1] - x[lo]) / (y[lo + 1] - y[lo])
    right = x[hi - 1] + (y[hi - 1] - h) * (x[hi] - x[hi - 1]) / (y[hi - 1] - y[hi])
    return left, right


# --- the curve itself ------------------------------------------------------------------------

def test_curve_is_unit_area_and_fwhm_matches_the_continuous_shape():
    curve = _asym_curve()
    assert np.trapezoid(curve.values, curve.energy) == pytest.approx(1.0, abs=1e-12)
    # Independent half-maximum crossings of the generating function (peak at 0 by construction).
    half = 0.5 * _asym(0.0)
    left = brentq(lambda e: _asym(e) - half, -3.0, 0.0)
    right = brentq(lambda e: _asym(e) - half, 0.0, 3.0)
    # 0.1 meV linear interpolation of this shape misplaces each crossing by well under 5 meV.
    assert curve.fwhm == pytest.approx(right - left, abs=0.005)
    # Asymmetry survives: the right half-width is the broad side.
    assert -left < right


def test_profile_is_zero_outside_the_table_and_covers_says_so():
    curve = _asym_curve()
    assert curve.covers(-8.0, 12.0) and curve.covers(-3.0, 5.0)
    assert not curve.covers(-8.01, 0.0) and not curve.covers(0.0, 12.01)
    assert np.all(curve.profile([-8.5, -20.0, 12.5, 40.0]) == 0.0)
    assert curve.profile(0.0) > 0.0


@pytest.mark.parametrize("dx", [DX, 0.07, 0.03])
def test_kernel_keeps_unit_area_and_puts_zero_energy_on_its_centre_node(dx):
    curve = _asym_curve()
    kern, centre = curve.kernel(dx)
    assert kern.sum() * dx == pytest.approx(1.0, abs=1e-12)
    # Nodes are k*dx inside the table: the centre is k = 0, so the node count either side is fixed.
    assert centre == int(np.floor(8.0 / dx + 1e-9))
    assert kern.size - 1 - centre == int(np.floor(12.0 / dx + 1e-9))
    scale = kern[centre] / curve.profile(0.0)
    assert kern[centre + 3] == pytest.approx(scale * curve.profile(3 * dx), rel=1e-12)


def test_malformed_tables_are_refused():
    e = np.arange(-1.0, 1.01, 0.1)
    with pytest.raises(ValueError):
        TabulatedResolution(e[::-1], np.ones_like(e), "d1")
    with pytest.raises(ValueError):
        TabulatedResolution(e, np.zeros_like(e), "d1")
    with pytest.raises(ValueError):  # never falls to half maximum: no width to report
        TabulatedResolution(e, np.ones_like(e), "d1")
    with pytest.raises(ValueError):  # a kernel must have the curve's zero energy as a node
        TabulatedResolution(e + 5.0, np.exp(-e ** 2), "d1").kernel(DX)


def test_unit_area_needs_no_numpy_two(monkeypatch):
    # pyproject allows NumPy 1.26, which has no np.trapezoid: the curve must normalise without it.
    monkeypatch.delattr(np, "trapezoid")
    curve = TabulatedResolution(np.array([-1.0, 0.0, 1.0]), np.array([0.0, 2.0, 0.0]), "d1")  # triangle, area 2
    assert curve.values[1] == pytest.approx(1.0) and curve.values.sum() == pytest.approx(1.0)
    assert curve.fwhm == pytest.approx(1.0)


def test_library_keys_by_detector_and_setting_and_refuses_duplicates():
    a, b = _asym_curve("a03", "60x60"), _asym_curve("a03", "20x20")
    lib = ResolutionLibrary([a, b, _asym_curve("a04", "60x60")])
    assert len(lib) == 3
    assert lib.settings() == ["20x20", "60x60"]
    assert lib.get("a03", "60x60") is a and lib.get("a03", "20x20") is b
    assert lib.get("a04", "20x20") is None and lib.get("a99", "60x60") is None
    with pytest.raises(ValueError, match="a03"):
        ResolutionLibrary([a, _asym_curve("a03", "60x60")])


# --- the fit seam ----------------------------------------------------------------------------

@pytest.mark.parametrize("window", [(-4.0, 18.0), (-4.013, 18.037)])  # dx multiple, and not
@pytest.mark.parametrize("center", [0.0, 0.123])
def test_near_delta_dho_recovers_the_curve(window, center):
    """A DHO far narrower than the grid, with its peak on a node, is a discrete delta: the model
    must return the curve itself, at the right place, whatever the data window. The convolution
    grid (window + 20 meV pad) is wider than the 20 meV table."""
    curve = _asym_curve()
    gamma, omega_d = 5e-4, 5.0
    x = np.arange(*window, 0.002)
    y = convolute_voigt_dho(x, np.hypot(omega_d, gamma), center, 9.9, 9.9, gamma, 1.0,
                            0.0, 0.0, 10.0, peak="stokes", resolution_curve=curve)
    weight = DX / gamma ** 2   # the on-node sample times dx: the discrete delta's area
    # Off-node Lorentzian samples add sum_k gamma^2/(k dx)^2 ~ 3e-4 of the weight.
    assert np.trapezoid(y, x) / weight == pytest.approx(1.0, abs=1e-3)
    c_left, c_right = _half_max(curve.energy, curve.values)
    y_left, y_right = _half_max(x, y)
    assert (y_left + y_right) / 2 - omega_d - center == pytest.approx(
        (c_left + c_right) / 2, abs=DX / 10)
    assert y_right - y_left == pytest.approx(curve.fwhm, abs=1e-3)


def test_gaussian_curve_reproduces_the_voigt_path():
    """On a window whose span is a dx multiple the Voigt grid holds 0 exactly, so the two paths
    must agree; elsewhere the Voigt path carries its documented sub-dx offset and this one not."""
    curve, fwhm = _gauss_curve(), 1.2
    x = np.arange(-4.0, 10.0, 0.1)
    args = (x, 4.0, 0.2, fwhm, 0.0, 0.5, 1.0)
    dho_tab = convolute_voigt_dho(*args, 0.0, 0.0, 100.0, resolution_curve=curve)
    dho_voigt = convolute_voigt_dho(*args, 0.0, 0.0, 100.0)
    # Only the 8 meV table edge and 0.01 meV sampling separate them.
    assert np.max(np.abs(dho_tab - dho_voigt)) <= 1e-6 * np.max(dho_voigt)
    x_off = x + 0.037
    ctx_tab = SimpleNamespace(center=0.2, gauss_fwhm=fwhm, lorentz_fwhm=0.0, T=100.0,
                              quasielastic_fwhm=1.5, resolution_curve=curve)
    ctx_voigt = SimpleNamespace(center=0.2, gauss_fwhm=fwhm, lorentz_fwhm=0.0, T=100.0,
                                quasielastic_fwhm=1.5)
    el_tab = Elastic().evaluate(x_off, {"E": 1.0}, ctx_tab)
    el_voigt = Elastic().evaluate(x_off, {"E": 1.0}, ctx_voigt)
    assert np.max(np.abs(el_tab - el_voigt)) <= 1e-4 * np.max(el_voigt)
    # The quasi-elastic Lorentzian is convolved numerically on the grid: its far tails beyond the
    # 20 meV pad and the 8 meV table edge are lost, about 3e-4 of the peak here.
    qe_tab = QuasiElastic().evaluate(x_off, {"QE": 1.0}, ctx_tab)
    qe_voigt = QuasiElastic().evaluate(x_off, {"QE": 1.0}, ctx_voigt)
    assert np.max(np.abs(qe_tab - qe_voigt)) <= 2e-3 * np.max(qe_voigt)


def test_asymmetric_elastic_line_is_the_curve_at_the_zero():
    curve = _asym_curve()
    ctx = SimpleNamespace(center=-0.31, gauss_fwhm=0.0, lorentz_fwhm=0.0, T=100.0,
                          resolution_curve=curve)
    x = np.arange(-9.0, 13.0, 0.001)   # the whole table, shifted by the zero
    y = Elastic().evaluate(x, {"E": 250.0}, ctx)
    assert np.array_equal(y, 250.0 * curve.profile(x + 0.31))
    left, right = _half_max(x, y)
    assert right - left == pytest.approx(curve.fwhm, abs=1e-3)
    peak = x[np.argmax(y)]
    assert right - peak > peak - left   # the broad side stays on the energy-loss side
    assert np.trapezoid(y, x) == pytest.approx(250.0, rel=1e-3)


def test_composite_model_fits_the_curve_shape_through_the_nnls_basis():
    curve = _asym_curve()
    ctx = CTX.ScanContext(x=np.arange(-5.0, 15.0, 0.2), counts=np.zeros(100),
                          mask=np.ones(100, dtype=bool), T=200.0, center=0.1, gauss_fwhm=9.0,
                          lorentz_fwhm=9.0, instrument="BL43LXU", energy_status="needs_zero",
                          resolution_curve=curve)
    model = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    theta = [6.0, 0.8, 3.0, 40.0, 0.5]
    truth = model.evaluate(ctx.x, theta, ctx)
    phi, names = model.nnls_basis(ctx.x, {"phonon.Omega_d": 6.0, "phonon.Gamma": 0.8}, ctx)
    amps, *_ = np.linalg.lstsq(phi, truth, rcond=None)
    assert names == ["phonon.A", "elastic.E", "bg.BG"]
    assert amps == pytest.approx([3.0, 40.0, 0.5], rel=1e-9)
    # Anti-Stokes stays derived from detailed balance: removing it changes the gain side only.
    stokes = PhononDHO(peak="stokes").evaluate(ctx.x, {"Omega_d": 6.0, "Gamma": 0.8, "A": 3.0}, ctx)
    both = PhononDHO().evaluate(ctx.x, {"Omega_d": 6.0, "Gamma": 0.8, "A": 3.0}, ctx)
    gain = ctx.x < -3.0
    assert np.all(both[gain] > stokes[gain])


# --- the context -----------------------------------------------------------------------------

def _record(spectra, record_monitor):
    egrid = np.linspace(-5.0, 15.0, 21)
    return SimpleNamespace(
        scan_axes=[axes.energy_axis(egrid), *axes.constant_q_axes(2.0, 0.0, 0.0, 21)],
        spectra=spectra,
        temperature=SimpleNamespace(setpoint=np.full(21, 300.0)),
        energy_calibration=SimpleNamespace(status="needs_zero"),
        normalization=SimpleNamespace(counting_mode="time", monitor=record_monitor,
                                      time=np.full(21, np.nan), preset=10.0,
                                      monitor_correction=False),
    )


def _spectrum(monitor=None, time=None):
    return SimpleNamespace(intensity=np.arange(21.0), mask=np.ones(21, dtype=bool),
                           monitor=monitor, time=time, q=None)


CARD = SimpleNamespace(policy=SimpleNamespace(instrument="BL43LXU", quasielastic_fwhm=None))


def test_context_with_a_curve_hands_width_only_consumers_its_fwhm():
    curve = _asym_curve()
    rec = _record({"a03": _spectrum()}, np.full(21, 100.0))
    ctx = CTX.ScanContext.from_scan(rec, "a03", CARD, center=0.05, gauss_fwhm=2.0,
                                    lorentz_fwhm=0.4, resolution_curve=curve)
    assert ctx.resolution_curve is curve
    assert (ctx.gauss_fwhm, ctx.lorentz_fwhm) == (curve.fwhm, 0.0)
    plain = CTX.ScanContext.from_scan(rec, "a03", CARD, center=0.05, gauss_fwhm=2.0,
                                      lorentz_fwhm=0.4)
    assert plain.resolution_curve is None and (plain.gauss_fwhm, plain.lorentz_fwhm) == (2.0, 0.4)


def test_context_uses_each_spectrums_own_exposure():
    own = np.linspace(50.0, 150.0, 21)
    rec = _record({"a03": _spectrum(monitor=own, time=np.full(21, 10.0)),
                   "a04": _spectrum()}, np.full(21, np.nan))
    ctx = CTX.ScanContext.from_scan(rec, "a03", CARD, center=0.0, gauss_fwhm=1.0,
                                    lorentz_fwhm=0.0)
    assert np.allclose(ctx.exposure_scale, own / np.median(own))
    assert ctx.exposure_reference == pytest.approx(np.median(own))
    # Without its own exposure the spectrum falls to the record's, here NaN: no usable measure,
    # so the fit takes the uniform-exposure path rather than a wrong number.
    other = CTX.ScanContext.from_scan(rec, "a04", CARD, center=0.0, gauss_fwhm=1.0,
                                      lorentz_fwhm=0.0)
    assert other.exposure_scale is None

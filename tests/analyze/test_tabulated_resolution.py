"""Tabulated resolution at the fit seam (isar.analyze.resolution, lineshapes, components, context).

Curves are generated here, never read from files: an asymmetric split Gaussian with long tails
on a 0.1 meV grid (the shape and spacing of a measured curve) and a Gaussian on a fine grid (to
compare against the Voigt path with Lorentz width 0).
"""
import numpy as np
import pytest
from scipy.optimize import brentq

from isar.analyze.lineshapes import CONVOLUTION_DX
from isar.analyze.resolution import ResolutionLibrary, TabulatedResolution

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


def test_library_keys_by_detector_and_setting_and_refuses_duplicates():
    a, b = _asym_curve("a03", "60x60"), _asym_curve("a03", "20x20")
    lib = ResolutionLibrary([a, b, _asym_curve("a04", "60x60")])
    assert len(lib) == 3
    assert lib.settings() == ["20x20", "60x60"]
    assert lib.get("a03", "60x60") is a and lib.get("a03", "20x20") is b
    assert lib.get("a04", "20x20") is None and lib.get("a99", "60x60") is None
    with pytest.raises(ValueError, match="a03"):
        ResolutionLibrary([a, _asym_curve("a03", "60x60")])

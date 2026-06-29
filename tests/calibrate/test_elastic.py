"""The shared elastic-line fit (promoted from the docs measure_resolution): a Voigt+flat-BG
fit near the energy zero -> (center, gauss_fwhm, lorentz_fwhm, ok). ``ok`` is False when there
is no significant elastic, so a flat spectrum cannot yield a bogus zero."""
import numpy as np

from isar.calibrate import elastic
from isar.synth._ff import Voigt


def test_fit_elastic_recovers_synthetic_center_and_width():
    x = np.linspace(-10.0, 10.0, 121)
    counts = Voigt(x, 300.0, 1.2, 0.3, 0.4) + 10.0      # center 0.4, gfw 1.2, lfw 0.3
    c, g, l, ok = elastic.fit_elastic(x, counts)
    assert ok is True
    assert abs(c - 0.4) < 0.1
    assert abs(g - 1.2) < 0.4


def test_fit_elastic_flat_spectrum_is_not_ok():
    rng = np.random.default_rng(0)
    x = np.linspace(-10.0, 10.0, 121)
    _c, _g, _l, ok = elastic.fit_elastic(x, rng.poisson(20.0, 121).astype(float))
    assert ok is False

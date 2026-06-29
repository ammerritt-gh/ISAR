"""Energy-zero calibration (§7, D8): fit E0 from a significant elastic; key a zero series by
acquisition time (parsed from the raw header, falling back to scan_number); interpolate to a
scan's time; a missing zero (flat elastic or out of coverage) resolves to None -> held."""
from types import SimpleNamespace

import numpy as np

from isar.calibrate import energy_zero as ez
from isar.synth._ff import Voigt


def _elastic(x, center):
    return Voigt(x, 300.0, 1.2, 0.3, center) + 10.0


def test_fit_zero_recovers_center_and_none_on_flat():
    x = np.linspace(-10.0, 10.0, 121)
    assert abs(ez.fit_zero(x, _elastic(x, -0.6)) - (-0.6)) < 0.1
    rng = np.random.default_rng(1)
    assert ez.fit_zero(x, rng.poisson(20.0, 121).astype(float)) is None


def test_zero_series_interpolates_and_bounds_coverage():
    s = ez.ZeroSeries.build([(0.0, -0.5), (10.0, -1.5)])
    assert abs(s.interpolate(5.0) - (-1.0)) < 1e-9
    assert s.interpolate(-1.0) is None                 # outside coverage -> no zero
    assert s.interpolate(11.0) is None
    assert ez.ZeroSeries.build([]).interpolate(5.0) is None


def test_zero_series_recovers_linear_drift():
    def true(t):
        return -0.5 - 0.01 * t
    s = ez.ZeroSeries.build([(t, true(t)) for t in np.arange(0.0, 100.0, 10.0)])
    for t in (5.0, 33.0, 87.0):
        assert abs(s.interpolate(t) - true(t)) < 1e-6


def test_scan_time_parses_both_formats_and_falls_back():
    spec = SimpleNamespace(raw_header={"D": "Fri Mar 07 16:40:15 2025"}, scan_number=84)
    in8 = SimpleNamespace(raw_header={"keywords": {"DATE_": "09-Sep-25 00:00:00"}}, scan_number=55)
    none = SimpleNamespace(raw_header={}, scan_number=42)
    assert ez.scan_time(spec) > 0 and ez.scan_time(in8) > 0
    assert ez.scan_time(spec) != ez.scan_time(in8)
    assert ez.scan_time(none) == 42.0                  # fallback to scan_number


def test_resolve_zero_own_interpolated_held():
    x = np.linspace(-10.0, 10.0, 121)
    series = ez.ZeroSeries.build([(0.0, -0.5), (100.0, -0.7)])
    rec = SimpleNamespace(raw_header={}, scan_number=50)          # time -> 50 (within [0,100])
    rng = np.random.default_rng(2)
    flat = rng.poisson(20.0, 121).astype(float)

    own = ez.resolve_zero(x, _elastic(x, -0.6), rec, series)
    assert own.source == "own" and abs(own.value - (-0.6)) < 0.1

    interp = ez.resolve_zero(x, flat, rec, series)                # no elastic -> interpolate at 50
    assert interp.source == "interpolated" and abs(interp.value - (-0.6)) < 1e-6

    far = SimpleNamespace(raw_header={}, scan_number=500)         # out of coverage
    held = ez.resolve_zero(x, flat, far, series)
    assert held.value is None and held.source is None

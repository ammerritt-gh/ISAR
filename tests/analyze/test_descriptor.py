"""L0 model-free descriptor — the statistical contract (docs/MEASUREMENT_STEERING.md §4.3).

The load-bearing test is the **null false-positive rate**: on pure flat Poisson background the
per-scan rate of spurious peaks must stay near the target alpha. Plus positive detection, elastic
exclusion, and blend/merge behaviour.
"""
import numpy as np
import pytest

from isar.analyze.descriptor import describe_spectrum
from isar.schema.steering import ScanDescriptor


def _grid(lo=-6.0, hi=6.0, n=61):
    return np.linspace(lo, hi, n)


def _gauss(x, area, e0, fwhm):
    s = fwhm / 2.3548
    return area / (s * np.sqrt(2 * np.pi)) * np.exp(-0.5 * ((x - e0) / s) ** 2)


# --------------------------------------------------------------------------- null false-positive rate
def test_null_false_positive_rate_within_alpha():
    """Pure flat Poisson background: fraction of scans with ≥1 spurious peak ≈ alpha (contract 3)."""
    rng = np.random.default_rng(0)
    x = _grid()
    alpha, bg, M = 0.05, 40.0, 400
    n_false = 0
    for _ in range(M):
        y = rng.poisson(bg, size=x.size).astype(float)
        d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5, alpha=alpha)
        if d.n_peaks > 0:
            n_false += 1
    fpr = n_false / M
    # Bonferroni over resolution elements is conservative → expect ≤ alpha, allow Monte-Carlo slack.
    assert fpr <= alpha + 0.04, f"per-scan FPR {fpr:.3f} exceeds target {alpha}"


# --------------------------------------------------------------------------- positive detection
def test_finds_a_planted_peak():
    rng = np.random.default_rng(1)
    x = _grid()
    bg = 30.0
    y = rng.poisson(bg + _gauss(x, area=300.0, e0=4.0, fwhm=1.5), size=x.size).astype(float)
    d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5, alpha=0.05)
    near = [p for p in d.peaks if abs(p.energy - 4.0) <= 1.0]
    assert len(near) == 1
    p = near[0]
    assert p.significance > 4.0
    assert p.integrated_intensity > 0.0
    assert p.width >= 1.5 - 1e-9          # floored at the resolution


# --------------------------------------------------------------------------- elastic exclusion
def test_elastic_line_excluded():
    rng = np.random.default_rng(2)
    x = _grid()
    # A strong elastic line at 0 and nothing else → no reported peaks (elastic is excluded).
    y = rng.poisson(20.0 + _gauss(x, area=800.0, e0=0.0, fwhm=1.5), size=x.size).astype(float)
    d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5, alpha=0.05, elastic_excl_fwhm=1.5)
    assert d.n_peaks == 0


# --------------------------------------------------------------------------- blends
def test_two_separated_peaks_are_two():
    rng = np.random.default_rng(3)
    x = _grid()
    peaks = _gauss(x, 300, -3.0, 1.5) + _gauss(x, 300, 4.0, 1.5)
    y = rng.poisson(25.0 + peaks, size=x.size).astype(float)
    d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5, alpha=0.05)
    energies = sorted(p.energy for p in d.peaks)
    assert d.n_peaks == 2
    assert energies[0] == pytest.approx(-3.0, abs=1.0) and energies[1] == pytest.approx(4.0, abs=1.0)


def test_within_resolution_merges_to_one():
    rng = np.random.default_rng(4)
    x = _grid()
    # Two centres 0.8 meV apart, closer than the 1.5 meV resolution → NMS collapses to one.
    peaks = _gauss(x, 300, 3.2, 1.5) + _gauss(x, 300, 4.0, 1.5)
    y = rng.poisson(25.0 + peaks, size=x.size).astype(float)
    d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5, alpha=0.05)
    assert d.n_peaks == 1


# --------------------------------------------------------------------------- edges + schema
def test_too_few_points_is_empty():
    x = np.array([0.0, 1.0, 2.0])
    d = describe_spectrum(x, np.array([10.0, 40.0, 10.0]), center=0.0, gauss_fwhm=1.5)
    assert d.n_peaks == 0 and d.n_points == 3


def test_descriptor_dict_roundtrip():
    rng = np.random.default_rng(5)
    x = _grid()
    y = rng.poisson(30.0 + _gauss(x, 300, 4.0, 1.5), size=x.size).astype(float)
    d = describe_spectrum(x, y, center=0.0, gauss_fwhm=1.5)
    rt = ScanDescriptor.from_dict(d.to_dict())
    assert rt.n_peaks == d.n_peaks and rt.peaks[0].energy == pytest.approx(d.peaks[0].energy)


def test_descriptor_uses_exposure_in_poisson_mean_without_rescaling_counts():
    rng = np.random.default_rng(91)
    x = _grid()
    exposure = np.linspace(0.5, 2.0, x.size)
    rate = 25.0 + _gauss(x, 240.0, 4.0, 1.5)
    counts = rng.poisson(rate * exposure).astype(float)
    original = counts.copy()
    d = describe_spectrum(x, counts, center=0.0, gauss_fwhm=1.5,
                          exposure_scale=exposure)
    assert np.array_equal(counts, original)
    assert any(abs(p.energy - 4.0) <= 1.0 for p in d.peaks)

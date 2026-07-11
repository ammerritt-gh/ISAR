"""Per-system lattice refinement from peak |Q| magnitudes (metric-LSQ + fallback)."""
import numpy as np
import pytest

from isar.geometry.ub_matrix import compute_B_matrix, refine_lattice_from_peaks


class StubPeak:
    """Duck-typed peak: refine_lattice_from_peaks only reads q_lab / hkl / is_valid."""

    def __init__(self, hkl, q_lab):
        self.hkl = hkl
        self._q_lab = np.asarray(q_lab, dtype=float)

    @property
    def q_lab(self):
        return self._q_lab

    @property
    def is_valid(self):
        return True


def _make_peaks(true_lattice, hkls):
    """Synthetic peaks: q_obs = |B_true @ hkl| for a KNOWN lattice."""
    B = compute_B_matrix(*true_lattice)
    return [StubPeak(hkl, B @ np.array(hkl, dtype=float)) for hkl in hkls]


def test_metric_cubic_recovery():
    true = (4.1, 4.1, 4.1, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (1, 1, 0), (1, 1, 1), (2, 0, 0)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 4.0, 90.0, 90.0, 90.0), "cubic")
    assert out["method"] == "metric_lsq"
    assert out["lattice"][0] == pytest.approx(4.1, abs=1e-6)


def test_metric_tetragonal_recovery_anisotropic():
    # The case a single global scale CANNOT recover: a and c err in opposite directions.
    true = (4.0, 4.0, 6.6, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 0, 1), (2, 0, 0), (0, 0, 2),
                               (1, 1, 0), (1, 0, 1)])
    out = refine_lattice_from_peaks(peaks, (4.05, 4.05, 6.5, 90.0, 90.0, 90.0), "tetragonal")
    assert out["method"] == "metric_lsq"
    a, b, c, al, be, ga = out["lattice"]
    assert a == pytest.approx(4.0, abs=1e-6)
    assert c == pytest.approx(6.6, abs=1e-6)


def test_metric_orthorhombic_recovery():
    true = (3.8, 4.6, 5.9, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0),
                               (0, 1, 1), (1, 0, 1), (1, 1, 1)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 6.0, 90.0, 90.0, 90.0), "orthorhombic")
    assert out["method"] == "metric_lsq"
    a, b, c = out["lattice"][:3]
    assert (a, b, c) == pytest.approx((3.8, 4.6, 5.9), abs=1e-6)


def test_metric_hexagonal_recovery():
    true = (3.2, 3.2, 5.1, 90.0, 90.0, 120.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 0, 1), (1, 1, 0), (2, 0, 0),
                               (1, 0, 1), (0, 0, 2)])
    out = refine_lattice_from_peaks(peaks, (3.0, 3.0, 5.0, 90.0, 90.0, 120.0), "hexagonal")
    assert out["method"] == "metric_lsq"
    a, b, c, al, be, ga = out["lattice"]
    assert a == pytest.approx(3.2, abs=1e-6)
    assert c == pytest.approx(5.1, abs=1e-6)
    assert ga == pytest.approx(120.0)


def test_metric_tetragonal_underdetermined_falls_back():
    # Only (h,k,0) peaks carry no c* information -> rank-deficient -> uniform fallback.
    true = (4.0, 4.0, 6.6, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (2, 0, 0), (1, 1, 0)])
    out = refine_lattice_from_peaks(peaks, (4.05, 4.05, 6.5, 90.0, 90.0, 90.0), "tetragonal")
    assert out["method"] == "uniform_scale_fallback"


def test_general_system_uniform_scales():
    true = (4.1, 4.1, 4.1, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (1, 1, 0), (1, 1, 1)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 4.0, 90.0, 90.0, 90.0), None)
    assert out["method"] == "uniform_scale"

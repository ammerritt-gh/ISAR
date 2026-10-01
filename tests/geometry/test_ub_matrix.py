"""Lattice refinement by crystal system, as TAVI 010cec6d refines it.

Ported from TAVI's tests/test_orientation.py (Refine Lattice by crystal
system): each system refines exactly its own reciprocal-metric parameters, and
a set of peaks that cannot decide them is refused naming what is missing.
"""
import math

import numpy as np
import pytest

from isar.geometry.ub_matrix import (
    compute_B_matrix,
    lattice_crystal_system,
    refine_lattice_from_peaks,
)


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


def _rot(axis, deg):
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = math.radians(deg)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * k + (1 - math.cos(t)) * (k @ k)


U_MOUNT = _rot(np.random.default_rng(53).normal(size=3), 7.0)


def _make_peaks(true_lattice, hkls):
    """Synthetic peaks of a KNOWN lattice under a seeded mount: q = U B_true hkl."""
    B = compute_B_matrix(*true_lattice)
    return [StubPeak(hkl, U_MOUNT @ B @ np.array(hkl, dtype=float)) for hkl in hkls]


def test_metric_cubic_recovery():
    true = (4.1, 4.1, 4.1, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (1, 1, 0), (1, 1, 1), (2, 0, 0)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 4.0, 90.0, 90.0, 90.0), "cubic")
    assert out["crystal_system"] == "cubic"
    assert out["lattice"][0] == pytest.approx(4.1, abs=1e-6)


def test_metric_tetragonal_recovery_anisotropic():
    # The case a single global scale CANNOT recover: a and c err in opposite directions.
    true = (4.0, 4.0, 6.6, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 0, 1), (2, 0, 0), (0, 0, 2),
                               (1, 1, 0), (1, 0, 1)])
    out = refine_lattice_from_peaks(peaks, (4.05, 4.05, 6.5, 90.0, 90.0, 90.0), "tetragonal")
    a, b, c, al, be, ga = out["lattice"]
    assert a == pytest.approx(4.0, abs=1e-6)
    assert c == pytest.approx(6.6, abs=1e-6)


def test_metric_orthorhombic_recovery():
    true = (3.8, 4.6, 5.9, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0),
                               (0, 1, 1), (1, 0, 1), (1, 1, 1)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 6.0, 90.0, 90.0, 90.0), "orthorhombic")
    a, b, c = out["lattice"][:3]
    assert (a, b, c) == pytest.approx((3.8, 4.6, 5.9), abs=1e-6)


def test_metric_hexagonal_recovery():
    true = (3.2, 3.2, 5.1, 90.0, 90.0, 120.0)
    peaks = _make_peaks(true, [(1, 0, 0), (0, 0, 1), (1, 1, 0), (2, 0, 0),
                               (1, 0, 1), (0, 0, 2)])
    out = refine_lattice_from_peaks(peaks, (3.0, 3.0, 5.0, 90.0, 90.0, 120.0), "hexagonal")
    a, b, c, al, be, ga = out["lattice"]
    assert a == pytest.approx(3.2, abs=1e-6)
    assert c == pytest.approx(5.1, abs=1e-6)
    assert ga == pytest.approx(120.0)


def test_tetragonal_hk0_set_is_refused_naming_what_it_cannot_decide():
    # Only (h,k,0) peaks carry no c* information: refused, never a uniform-scale guess.
    true = (4.0, 4.0, 6.6, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (2, 0, 0), (1, 1, 0)])
    with pytest.raises(ValueError, match="tetragonal refinement fits 2 parameters but the 3 "
                                         "valid peaks give 1 independent"):
        refine_lattice_from_peaks(peaks, (4.05, 4.05, 6.5, 90.0, 90.0, 90.0), "tetragonal")


def test_no_system_refines_the_fields_highest_symmetry():
    true = (4.1, 4.1, 4.1, 90.0, 90.0, 90.0)
    peaks = _make_peaks(true, [(1, 0, 0), (1, 1, 0), (1, 1, 1)])
    out = refine_lattice_from_peaks(peaks, (4.0, 4.0, 4.0, 90.0, 90.0, 90.0), None)
    assert out["crystal_system"] == "cubic"
    assert out["lattice"][0] == pytest.approx(4.1, abs=1e-6)
    assert "method" not in out


# (system, the lattice refined from, its free parameters perturbed 1 % each).
REFINE_CASES = {
    "cubic": ("cubic", (4.05, 4.05, 4.05, 90, 90, 90), (4.0905, 4.0905, 4.0905, 90, 90, 90)),
    "tetragonal": ("tetragonal", (4.0, 4.0, 6.0, 90, 90, 90), (4.04, 4.04, 5.94, 90, 90, 90)),
    "orthorhombic": ("orthorhombic", (4.0, 5.0, 6.0, 90, 90, 90),
                     (4.04, 4.95, 6.06, 90, 90, 90)),
    "hexagonal": ("hexagonal", (3.21, 3.21, 5.21, 90, 90, 120),
                  (3.2421, 3.2421, 5.1579, 90, 90, 120)),
    "trigonal-hexagonal-axes": ("trigonal", (3.21, 3.21, 5.21, 90, 90, 120),
                                (3.1779, 3.1779, 5.2621, 90, 90, 120)),
    "trigonal-rhombohedral-axes": ("trigonal", (5.0, 5.0, 5.0, 80, 80, 80),
                                   (5.05, 5.05, 5.05, 80.8, 80.8, 80.8)),
    "monoclinic-b": ("monoclinic", (5.1, 6.3, 7.2, 90, 103.5, 90),
                     (5.151, 6.237, 7.272, 90, 104.535, 90)),
    "monoclinic-a": ("monoclinic", (5.1, 6.3, 7.2, 98, 90, 90),
                     (5.049, 6.363, 7.128, 98.98, 90, 90)),
    "monoclinic-c": ("monoclinic", (5.1, 6.3, 7.2, 90, 90, 112),
                     (5.151, 6.363, 7.128, 90, 90, 110.88)),
    "triclinic": ("triclinic", (5.1, 6.3, 7.2, 85, 95, 100),
                  (5.151, 6.237, 7.272, 85.85, 94.05, 101.0)),
}
REFINE_HKLS = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, -1, 1)]


@pytest.mark.parametrize("case", list(REFINE_CASES))
def test_refine_lattice_recovers_every_crystal_system(case):
    """Refined from the unperturbed lattice, peaks of the perturbed one give it
    back to 1e-6: the constraint per system and the (2 pi)^2 factor from G* to
    the direct metric (the round trip through compute_B_matrix)."""
    system, start, perturbed = REFINE_CASES[case]
    result = refine_lattice_from_peaks(_make_peaks(perturbed, REFINE_HKLS), start, system)
    assert result["crystal_system"] == system
    assert np.allclose(result["lattice"], perturbed, rtol=0.0, atol=1e-6), result["lattice"]
    assert result["rms_error"] < 1e-9 and "method" not in result


@pytest.mark.parametrize(("system", "start", "hkls", "words"), [
    ("tetragonal", (4.0, 4.0, 6.0, 90, 90, 90), [(1, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)],
     "tetragonal refinement fits 2 parameters but the 4 valid peaks give 1 independent"),
    ("hexagonal", (3.21, 3.21, 5.21, 90, 90, 120), [(1, 0, 0), (1, 1, 0)],
     "hexagonal refinement fits 2 parameters but the 2 valid peaks give 1 independent"),
    ("monoclinic", (5.1, 6.3, 7.2, 90, 103.5, 90), [(1, 0, 0), (0, 1, 0), (0, 0, 1)],
     "monoclinic refinement fits 4 parameters but the 3 valid peaks give 3 independent"),
    ("cubic", (4.0, 4.0, 5.0, 90, 90, 90), [(1, 0, 0), (0, 0, 1)],
     "do not have the cubic metric"),
    ("trigonal", (5.0, 5.0, 6.0, 80, 80, 80), [(1, 0, 0), (0, 0, 1)],
     "do not have the trigonal metric"),
], ids=["tetragonal-hk0", "hexagonal-hk0-pair", "monoclinic-three", "cubic-fields",
        "trigonal-fields"])
def test_refine_lattice_refuses_what_the_peaks_or_fields_cannot_decide(system, start, hkls,
                                                                      words):
    with pytest.raises(ValueError, match=words):
        refine_lattice_from_peaks(_make_peaks(start, hkls), start, system)


def test_with_no_system_the_fields_highest_symmetry_is_refined():
    """Hexagonal axes read as hexagonal (the same metric as trigonal on them),
    rhombohedral axes as trigonal."""
    named = {name: lattice_crystal_system(start) for name, (_, start, _) in REFINE_CASES.items()}
    assert named == {"cubic": "cubic", "tetragonal": "tetragonal",
                     "orthorhombic": "orthorhombic", "hexagonal": "hexagonal",
                     "trigonal-hexagonal-axes": "hexagonal",
                     "trigonal-rhombohedral-axes": "trigonal", "monoclinic-b": "monoclinic",
                     "monoclinic-a": "monoclinic", "monoclinic-c": "monoclinic",
                     "triclinic": "triclinic"}
    _, start, perturbed = REFINE_CASES["tetragonal"]
    result = refine_lattice_from_peaks(_make_peaks(perturbed, REFINE_HKLS), start)
    assert result["crystal_system"] == "tetragonal"
    assert np.allclose(result["lattice"], perturbed, rtol=0.0, atol=1e-6)

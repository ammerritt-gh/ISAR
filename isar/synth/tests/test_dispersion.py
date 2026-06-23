"""Tests for the acoustic test dispersion ω₀(H) = 6·|sin(π(H−2))| meV."""
import numpy as np

from isar.synth import dispersion


def test_omega0_zero_at_zone_centers():
    # H = 2.00 and H = 3.00 are zone-center/Bragg positions: ω₀ = 0 (unmeasurable).
    assert dispersion.omega0_of_H(2.0) == 0.0
    assert dispersion.omega0_of_H(3.0) < 1e-12


def test_omega0_max_at_zone_boundary():
    # H = 2.5 is the zone boundary: maximum 6 meV.
    assert dispersion.omega0_of_H(2.5) == 6.0


def test_omega0_known_interior_value():
    # H = 2.25 → 6·sin(π·0.25) = 6/√2.
    assert dispersion.omega0_of_H(2.25) == 6.0 * np.sin(np.pi * 0.25)


def test_omega0_vectorized():
    H = np.array([2.0, 2.25, 2.5, 3.0])
    out = dispersion.omega0_of_H(H)
    assert out.shape == H.shape
    assert out[2] == 6.0


def test_h_values_grid():
    H = dispersion.h_values()
    assert len(H) == 21
    assert H[0] == 2.0
    assert H[-1] == 3.0
    # uniform 0.05 step
    assert np.allclose(np.diff(H), 0.05)


def test_classify_midzone():
    # H = 2.5: ω₀ = 6 ≫ 2Γ = 1.0 → midzone.
    assert dispersion.classify_cell(2.5, gamma=0.5, eps_omega_d=1e-3) == "midzone"


def test_classify_unmeasurable_at_endpoints():
    assert dispersion.classify_cell(2.0, gamma=0.5, eps_omega_d=1e-3) == "unmeasurable"
    assert dispersion.classify_cell(3.0, gamma=0.5, eps_omega_d=1e-3) == "unmeasurable"


def test_classify_boundary_near_zone_center():
    # H = 2.05: ω₀ = 6·sin(π·0.05) ≈ 0.937 meV — above ε, below 2Γ = 1.0 → boundary.
    assert dispersion.classify_cell(2.05, gamma=0.5, eps_omega_d=1e-3) == "boundary"

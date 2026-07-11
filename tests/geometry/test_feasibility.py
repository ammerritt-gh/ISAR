"""Kinematic feasibility behavior (ported/adapted from TAS_MCP tests/test_feasibility.py).

Adapted to the stateless ``isar.geometry`` API: ``check_feasibility`` / ``accessible_range``
take a ``SampleState`` plus explicit fixed-mode / fixed-k arguments (default = PUMA).
"""
import math

import pytest

from isar.geometry import SampleState, feasibility


def cubic_sample(a=4.0) -> SampleState:
    return SampleState(a, a, a, 90.0, 90.0, 90.0)


def test_elastic_feasible_point():
    s = cubic_sample(4.0)  # default kf=2.6634
    out = feasibility.check_feasibility(s, hkl=(1, 0, 0), e_transfer_meV=0.0)
    assert out["feasible"] is True
    assert out["triangle_closes"] is True
    # Elastic: ki == kf.
    assert out["ki_inv_angstrom"] == pytest.approx(out["kf_inv_angstrom"])
    # |Q|=2*pi/4=1.571; stt = 2*asin(|Q|/2k) magnitude.
    expected = 2 * math.degrees(math.asin((2 * math.pi / 4.0) / (2 * 2.6634)))
    assert abs(out["stt_deg"]) == pytest.approx(expected, abs=1e-6)


def test_triangle_does_not_close_large_Q():
    s = cubic_sample(4.0)
    # |Q| larger than ki+kf -> cannot close.
    out = feasibility.check_feasibility(s, q_inv_angstrom=[100.0, 0, 0], e_transfer_meV=0.0)
    assert out["feasible"] is False
    assert out["reason"] == "scattering_triangle_does_not_close"
    assert out["triangle_closes"] is False


def test_feasibility_without_sample_via_q_vector():
    # No SampleState needed when the target is given as a Q magnitude vector.
    out = feasibility.check_feasibility(None, q_inv_angstrom=[1.0, 0, 0], e_transfer_meV=0.0)
    assert out["feasible"] is True
    assert out["in_plane"] is None


def test_energy_loss_raises_ki_in_kf_fixed_mode():
    s = cubic_sample(4.0)
    loss = feasibility.check_feasibility(s, hkl=(1, 0, 0), e_transfer_meV=10.0,
                                         fixed_mode="kf", fixed_k_inv_angstrom=2.6634)
    gain = feasibility.check_feasibility(s, hkl=(1, 0, 0), e_transfer_meV=-10.0,
                                         fixed_mode="kf", fixed_k_inv_angstrom=2.6634)
    assert loss["ki_inv_angstrom"] > loss["kf_inv_angstrom"]   # energy loss -> ki>kf
    assert gain["ki_inv_angstrom"] < gain["kf_inv_angstrom"]   # energy gain -> ki<kf


def test_energy_gain_exceeds_fixed_kf():
    s = cubic_sample(4.0)
    # Energy gain beyond kf^2*2.0717 makes ki imaginary.
    big_gain = -(2.6634 ** 2 * 2.0717) - 5.0
    out = feasibility.check_feasibility(s, hkl=(1, 0, 0), e_transfer_meV=big_gain,
                                        fixed_mode="kf", fixed_k_inv_angstrom=2.6634)
    assert out["feasible"] is False
    assert out["reason"] == "energy_gain_exceeds_fixed_kf"


def test_in_plane_null_without_orientation():
    s = cubic_sample(4.0)
    out = feasibility.check_feasibility(s, hkl=(1, 0, 0), e_transfer_meV=0.0)
    assert out["in_plane"] is None
    assert any("in_plane not determined" in w for w in out["warnings"])


def test_accessible_range_band():
    s = cubic_sample(4.0)
    out = feasibility.accessible_range(s, hkl=(1, 0, 0),
                                       e_min_meV=0, e_max_meV=10, e_step_meV=2,
                                       fixed_mode="kf", fixed_k_inv_angstrom=2.6634)
    assert out["n_total"] == 6
    assert out["n_feasible"] >= 1
    assert out["feasible_band_meV"] is not None

"""HKL<->Q conversion + d-spacing behavior (ported/adapted from TAS_MCP tests/test_feasibility.py).

Adapted to the stateless ``isar.geometry`` API: functions take a ``SampleState`` per call
instead of a mutable ``TASSession``.
"""
import math

import pytest

from isar.geometry import SampleState, convert


def cubic_sample(a=4.0) -> SampleState:
    return SampleState(a, a, a, 90.0, 90.0, 90.0)


def test_d_spacing_cubic():
    s = cubic_sample(4.0)
    out = convert.d_spacing(s, 1, 0, 0)
    # |Q| = 2*pi/a for (100); d = a.
    assert out["q_magnitude_inv_angstrom"] == pytest.approx(2 * math.pi / 4.0)
    assert out["d_spacing_angstrom"] == pytest.approx(4.0)


def test_hkl_to_q_unoriented_is_crystal_frame():
    s = cubic_sample(4.0)
    out = convert.hkl_to_q(s, 1, 0, 0)
    assert out["frame"] == "crystal"
    assert out["q_magnitude_inv_angstrom"] == pytest.approx(2 * math.pi / 4.0)


def test_q_to_hkl_roundtrip_unoriented():
    s = cubic_sample(4.0)
    q = convert.hkl_to_q(s, 1, 2, 0)["q_inv_angstrom"]
    back = convert.q_to_hkl(s, *q)["hkl_rlu"]
    assert back == pytest.approx([1.0, 2.0, 0.0], abs=1e-9)


def test_d_spacing_zero_hkl_raises():
    with pytest.raises(ValueError):
        convert.d_spacing(cubic_sample(4.0), 0, 0, 0)

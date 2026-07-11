"""Pin the frame convention and the component<->instrument bridge.

``isar.geometry`` works in the INSTRUMENT frame (scattering plane horizontal, +z out of
plane). The mounted-component (McStas) frame has +y out of plane. The bridge functions swap
those two axes. This test pins (a) the round-trip identity on reference vectors and (b) a
known-answer computed from the ORIGINAL TAVI code, so a future edit to the swap is caught.
"""
import math

import numpy as np
import pytest

from isar.geometry import SampleState, compute_B_matrix
from isar.geometry.tas_geometry import (
    component_q_to_instrument_q,
    instrument_q_to_component_q,
)


REFERENCE_VECTORS = [
    np.array([1.0, 0.0, 0.0]),
    np.array([0.0, 1.0, 0.0]),
    np.array([0.0, 0.0, 1.0]),
    np.array([0.3, 1.7, 2.9]),
    np.array([-2.5, 0.4, -1.1]),
]


@pytest.mark.parametrize("v", REFERENCE_VECTORS)
def test_component_instrument_roundtrip(v):
    back = instrument_q_to_component_q(component_q_to_instrument_q(v))
    assert back == pytest.approx(v)
    back2 = component_q_to_instrument_q(instrument_q_to_component_q(v))
    assert back2 == pytest.approx(v)


def test_known_answer_from_original_tavi_code():
    # Known answer produced from the ORIGINAL TAVI implementation
    # (tavi/tas_geometry.py: component_q_to_instrument_q returns np.array([q[0], q[2], q[1]]),
    # i.e. it swaps the y and z components). For component q = [0.3, 1.7, 2.9] the instrument
    # vector is therefore [0.3, 2.9, 1.7].
    q_component = np.array([0.3, 1.7, 2.9])
    got = component_q_to_instrument_q(q_component)
    assert got == pytest.approx([0.3, 2.9, 1.7])


def test_known_answer_cubic_hkl_to_instrument_q():
    # End-to-end through the vendored B-matrix + bridge, against a hand value from the
    # original convention. Cubic a=4 A: a* = 2*pi/4 = 1.5707963 A^-1 along x, so the mounted
    # (2,0,0) component Q is [3.1415927, 0, 0]; the y<->z swap leaves it unchanged.
    B = compute_B_matrix(4.0, 4.0, 4.0, 90.0, 90.0, 90.0)
    q_component = B @ np.array([2.0, 0.0, 0.0])
    q_instrument = component_q_to_instrument_q(q_component)
    assert q_instrument == pytest.approx([2.0 * math.pi / 2.0, 0.0, 0.0], abs=1e-9)


def test_in_plane_reflection_has_zero_out_of_plane_component():
    # After orienting the (H,0,L) plane, an in-plane reflection has ~0 instrument-z (vertical).
    s = SampleState(4.0, 4.0, 4.0, 90.0, 90.0, 90.0)
    from isar.geometry import set_orientation_from_plane
    set_orientation_from_plane(s, [1, 0, 0], [0, 0, 1])
    q_component = np.asarray(s.ub().hkl_to_q(2, 0, 1))
    q_instrument = component_q_to_instrument_q(q_component)
    assert q_instrument[2] == pytest.approx(0.0, abs=1e-9)  # instrument-z = out of plane

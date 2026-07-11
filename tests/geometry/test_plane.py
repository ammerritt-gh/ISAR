"""Scattering-plane orientation behavior (ported/adapted from TAS_MCP tests/test_feasibility.py).

Adapted to the stateless ``isar.geometry`` API: ``set_orientation_from_plane`` establishes U
on the passed ``SampleState``.
"""
import pytest

from isar.geometry import SampleState, plane


def cubic_sample(a=4.0) -> SampleState:
    return SampleState(a, a, a, 90.0, 90.0, 90.0)


def test_orientation_from_plane_in_plane_and_out():
    s = cubic_sample(4.0)
    # (H,0,L)-style plane spanned by [1,0,0] and [0,0,1].
    plane.set_orientation_from_plane(s, [1, 0, 0], [0, 0, 1])
    in_plane = plane.scattering_plane_check(s, 2, 0, 1)
    out_plane = plane.scattering_plane_check(s, 0, 1, 0)  # along the plane normal
    assert in_plane["in_plane"] is True
    assert out_plane["in_plane"] is False
    assert out_plane["out_of_plane_angle_deg"] == pytest.approx(90.0, abs=1e-3)


def test_scattering_plane_info_reports_normal():
    s = cubic_sample(4.0)
    info = plane.set_orientation_from_plane(s, [1, 0, 0], [0, 0, 1])
    # The plane normal points along the crystal [0,1,0] direction for the (H,0,L) plane.
    # It is returned as the hkl whose Q is the out-of-plane unit vector, so its magnitude is
    # 1/|b*| (= a/2π for cubic), not 1 — pin the DIRECTION only.
    normal = info["plane_normal_hkl"]
    assert normal[0] == pytest.approx(0.0, abs=1e-9)
    assert normal[2] == pytest.approx(0.0, abs=1e-9)
    assert abs(normal[1]) > 1e-6


def test_check_requires_orientation():
    with pytest.raises(ValueError):
        plane.scattering_plane_check(cubic_sample(4.0), 1, 0, 0)

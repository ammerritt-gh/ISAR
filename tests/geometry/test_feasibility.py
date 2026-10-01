"""Kinematic feasibility behavior (ported/adapted from TAS_MCP tests/test_feasibility.py).

Adapted to the stateless ``isar.geometry`` API: ``check_feasibility`` / ``accessible_range``
take a ``SampleState`` plus explicit fixed-mode / fixed-k arguments (default = PUMA).

Reach is the instrument's sample stage: on PUMA, TAVI's turntable plus two +/-20 deg arcs,
checked against TAVI 010cec6d's own per-point stage solve (``tavi_parity_010cec6d.json``).
"""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from isar.geometry import SampleState, compute_B_matrix, feasibility
from isar.geometry.instruments import PUMA, tas_goniometer
from isar.geometry.tas_geometry import component_q_to_instrument_q, solve_instrument_angles

FIXTURE = Path(__file__).with_name("tavi_parity_010cec6d.json")


def cubic_sample(a=4.0) -> SampleState:
    return SampleState(a, a, a, 90.0, 90.0, 90.0)


def _tavi_point(tilt_deg):
    """TAVI's stage solve on PUMA for (1 0 tan(tilt)) of a 4.05 A cubic crystal at U =
    identity, where (h k 0) is horizontal and l vertical; elastic at kf = 2.6634."""
    points = json.loads(FIXTURE.read_text(encoding="utf-8"))["feasibility"]
    return next(p for p in points if p["tilt_deg"] == tilt_deg)


def _check_tilted(point):
    s = SampleState(*point["lattice"])
    s.orientation_set = True                  # U = identity, as TAVI's case
    assert np.allclose(s.ub().U, point["u"])
    assert point["kf"] == PUMA.default_fixed_k_inv_angstrom == point["ki"]
    return feasibility.check_feasibility(s, hkl=point["hkl"], e_transfer_meV=0.0)


def test_puma_profile_carries_tavis_stage_and_sense():
    assert PUMA.sense_sample == -1
    assert PUMA.goniometer == tas_goniometer(20.0)


def test_puma_reflection_tilted_10_deg_is_reached_as_tavi_solves_it():
    point = _tavi_point(10.0)
    out = _check_tilted(point)
    assert out["feasible"] is True and out["in_plane"] is False
    got = [out["stt_deg"], out["sth_deg"], out["sgl_deg"], out["sgu_deg"]]
    want = [point["stt"], point["sth"], point["sgl"], point["sgu"]]
    assert np.allclose(got, want, rtol=0.0, atol=1e-9), (got, want)
    assert abs(out["sgu_deg"]) == pytest.approx(10.0, abs=1e-6)


def test_puma_reflection_tilted_25_deg_is_refused_out_of_travel():
    point = _tavi_point(25.0)
    out = _check_tilted(point)
    assert out["feasible"] is False
    assert out["reason"] == "stage_out_of_travel"
    assert out["detail"] == point["error"]
    assert "sgu" in out["detail"] and "[-20, 20]" in out["detail"]
    span = feasibility.accessible_range(SampleState(*point["lattice"], orientation_set=True),
                                        hkl=point["hkl"], e_min_meV=0, e_max_meV=2,
                                        e_step_meV=1)
    assert span["n_feasible"] == 0
    assert {p["reason"] for p in span["points"]} == {"stage_out_of_travel"}


def test_in_plane_reflection_reports_zero_tilts_and_the_legacy_angles():
    point = _tavi_point(0.0)
    out = _check_tilted(point)
    assert out["feasible"] is True and out["in_plane"] is True
    assert (out["sgl_deg"], out["sgu_deg"]) == (0.0, 0.0)
    # The angles the single-tilt solve gave before the stage: unchanged in plane.
    q = component_q_to_instrument_q(compute_B_matrix(*point["lattice"]) @ np.array(point["hkl"]))
    legacy = solve_instrument_angles(q, point["ki"], point["kf"])
    assert np.allclose([out["stt_deg"], out["sth_deg"]], [legacy.stt, legacy.sth],
                       rtol=0.0, atol=1e-9)
    assert np.allclose([out["stt_deg"], out["sth_deg"]], [point["stt"], point["sth"]],
                       rtol=0.0, atol=1e-9)


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


def test_accessible_range_scalar_q_echoed_in_list():
    # A scalar q_inv_angstrom must serialize without raising (was list(scalar) -> TypeError).
    out = feasibility.accessible_range(None, q_inv_angstrom=1.0,
                                       e_min_meV=0, e_max_meV=2, e_step_meV=1)
    assert out["q_inv_angstrom"] == [1.0]


def test_accessible_range_grid_never_overshoots_emax():
    # e_min=0, e_max=1, step=0.4 -> [0, 0.4, 0.8]; NOT 1.2.
    s = cubic_sample(4.0)
    out = feasibility.accessible_range(s, hkl=(1, 0, 0),
                                       e_min_meV=0, e_max_meV=1, e_step_meV=0.4)
    grid = [p["e_transfer_meV"] for p in out["points"]]
    assert grid == [0.0, 0.4, 0.8]


def test_accessible_range_emax_below_emin_raises():
    s = cubic_sample(4.0)
    with pytest.raises(ValueError):
        feasibility.accessible_range(s, hkl=(1, 0, 0), e_min_meV=5, e_max_meV=1, e_step_meV=1)


def test_accessible_range_single_point_when_emin_equals_emax():
    s = cubic_sample(4.0)
    out = feasibility.accessible_range(s, hkl=(1, 0, 0),
                                       e_min_meV=0, e_max_meV=0, e_step_meV=0.4)
    assert out["n_total"] == 1
    assert [p["e_transfer_meV"] for p in out["points"]] == [0.0]


def test_accessible_range_disjoint_runs_not_merged(monkeypatch):
    # Force feasibility to be feasible-infeasible-feasible across the grid. The gap energy
    # must be in NO band; feasible_band_meV is None; feasible_bands_meV has two runs.
    s = cubic_sample(4.0)

    def fake_check(sample, *, e_transfer_meV, **kwargs):
        feasible = e_transfer_meV != 2.0  # carve a single-point infeasible gap at e=2
        return {
            "feasible": feasible,
            "reason": "ok" if feasible else "carved_gap",
            "ki_inv_angstrom": 2.6634, "kf_inv_angstrom": 2.6634, "stt_deg": -10.0,
        }

    monkeypatch.setattr(feasibility, "check_feasibility", fake_check)
    out = feasibility.accessible_range(s, hkl=(1, 0, 0),
                                       e_min_meV=0, e_max_meV=4, e_step_meV=1)
    # e=2 is feasible in no band.
    gap_pt = next(p for p in out["points"] if p["e_transfer_meV"] == 2.0)
    assert gap_pt["feasible"] is False
    assert out["feasible_band_meV"] is None
    bands = out["feasible_bands_meV"]
    assert len(bands) == 2
    assert bands[0] == {"e_min_meV": 0.0, "e_max_meV": 1.0}
    assert bands[1] == {"e_min_meV": 3.0, "e_max_meV": 4.0}

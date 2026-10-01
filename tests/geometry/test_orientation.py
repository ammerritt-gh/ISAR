"""The sample stage, its solver and the UB fit on ISAR's copy of TAVI's geometry.

Ported from TAVI's tests/test_orientation.py, test_tas_geometry.py and
test_sign_conventions.py, without TAVI's instrument models: peaks are solved on
each TAVI instrument's stage (the table in isar/geometry/VENDORED.md) directly.
The round trip and the stage tests read no fixture, so a sign error shared by
TAVI and this copy cannot hide behind parity; ``test_parity_with_tavi_010cec6d``
then recomputes TAVI's own answers (``tavi_parity_010cec6d.json``, written by
``make_tavi_parity.py``) on the same inputs.
"""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from isar.geometry import SampleState
from isar.geometry.feasibility import check_feasibility
from isar.geometry.instruments import PUMA, tas_goniometer
from isar.geometry.orientation import (
    TRAVEL_TOLERANCE_DEG,
    StageAxis,
    StageUnreachable,
    check_travel,
    legacy_triple,
    lock_plane,
    q_mount_from_stage,
    sample_arm_euler,
    solve_stage,
    stage_record,
    stage_rotation,
)
from isar.geometry.sample_mount import reciprocal_basis_tas
from isar.geometry.tas_geometry import (
    component_q_to_instrument_q,
    lab_q_from_stt,
    mccode_rotation_matrix,
    solve_instrument_angles,
    stt_from_q_norm,
)
from isar.geometry.ub_matrix import (
    ObservedPeak,
    UBMatrix,
    lattice_crystal_system,
    refine_lattice_from_peaks,
)

FIXTURE = Path(__file__).with_name("tavi_parity_010cec6d.json")

# TAVI 010cec6d's stage and sample sense per instrument (instruments/<x>/plugin.py).
STAGES = {
    "puma": (tas_goniometer(20.0), -1),
    "in8": (tas_goniometer(), 1),
    "in12": (tas_goniometer(20.0), 1),
    "panda": (tas_goniometer(15.0), 1),
}
LATTICES = {
    "cubic": (4.05, 4.05, 4.05, 90.0, 90.0, 90.0),
    "hexagonal": (3.21, 3.21, 5.21, 90.0, 90.0, 120.0),
    "monoclinic": (5.10, 6.30, 7.20, 90.0, 103.5, 90.0),
    "triclinic": (5.10, 6.30, 7.20, 85.0, 95.0, 100.0),
}
CUBIC = LATTICES["cubic"]
K = 2.662
HKL_POOL = [(h, k, l) for h in range(-3, 4) for k in range(-3, 4) for l in (-1, 0, 1)
            if (h, k, l) != (0, 0, 0)]
U_IN_PLANE = mccode_rotation_matrix(0.0, 23.0, 0.0)   # about the vertical mount y


def _rot(axis, deg):
    """Right-handed rotation about an axis, written out in the test."""
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = math.radians(deg)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * k + (1 - math.cos(t)) * (k @ k)


def _fit(lattice, peaks):
    ub = UBMatrix(*lattice)
    ub.peaks = peaks
    ub.calculate_U_from_peaks()
    return ub.U


def _solved_peaks(gonio, sense, lattice, u_true, rng, count=3):
    """Peaks of ``u_true`` solved on ``gonio``: (hkl, readouts, stt, record)."""
    B = reciprocal_basis_tas(*lattice)
    sign = -1.0 if sense > 0 else 1.0          # vTAS: +1 puts -U B hkl on the lab Q
    found = []
    for index in rng.permutation(len(HKL_POOL)):
        hkl = HKL_POOL[index]
        q = u_true @ B @ np.array(hkl, dtype=float)
        if not 0.5 < np.linalg.norm(q) < 4.8:
            continue
        stt = stt_from_q_norm(float(np.linalg.norm(q)), K, K, sense)
        assert np.sign(stt) == sense
        q_lab = lab_q_from_stt(K, K, stt)
        try:
            readouts = solve_stage(gonio, sign * q, q_lab)
        except StageUnreachable:
            continue
        # The stage turns U B hkl onto the signed lab Q (arm by arm, in the test).
        r = (_rot((0, 1, 0), readouts["A3"]) @ _rot((1, 0, 0), readouts["sgl"])
             @ _rot((0, 0, 1), readouts["sgu"]))
        assert np.linalg.norm(r @ (sign * q) - q_lab) < 1e-9 * np.linalg.norm(q_lab)
        # Read back through the stage: the crystal's own Q, sign undone.
        back = q_mount_from_stage(gonio, readouts, stt, K, K, sense)
        assert np.linalg.norm(back - q) < 1e-9 * np.linalg.norm(q)
        found.append((hkl, readouts, stt, stage_record(gonio, readouts, ki=K, kf=K, sense=sense)))
        if len(found) == count:
            return found
    raise AssertionError("too few reachable reflections to test")


# --- lens 1: the round trip on every TAVI stage and both senses ---------------------

@pytest.mark.parametrize("lattice", list(LATTICES))
@pytest.mark.parametrize("sense", [-1, 1])
@pytest.mark.parametrize("name", list(STAGES))
def test_fit_recovers_known_u_for_every_stage_and_sense(name, sense, lattice):
    gonio = STAGES[name][0]
    rng = np.random.default_rng(7 * list(STAGES).index(name) + 3 * (sense > 0)
                                + 11 * list(LATTICES).index(lattice))
    u_true = _rot(rng.normal(size=3), rng.uniform(0.5, 10.0))
    found = _solved_peaks(gonio, sense, LATTICES[lattice], u_true, rng)
    stage_peaks = [ObservedPeak(hkl=tuple(hkl), angles=(r["A3"], r["sgl"], stt), ki=K, kf=K,
                                stage=record) for hkl, r, stt, record in found]
    # The legacy triple saved beside the record, read as an older reader does
    # (no record, no sense): the sense comes from the sign of stt.
    legacy_peaks = [ObservedPeak.from_dict({"hkl": list(hkl), "ki": K, "kf": K,
                                            "angles": list(legacy_triple(record, stt, K, K, sense))})
                    for hkl, _, stt, record in found]
    assert all(p.is_legacy and p.sense_sample == sense for p in legacy_peaks)
    for peaks in (stage_peaks, legacy_peaks):
        for n in (2, 3):
            assert np.allclose(_fit(LATTICES[lattice], peaks[:n]), u_true, rtol=0.0, atol=1e-9)


def test_tilted_legacy_peaks_on_the_positive_sense_fit_back():
    """Peaks without a stage record, taken on the +1 sense with a nonzero
    legacy tilt (saz) and saved without a sense: the fit reads them through the
    signed legacy inverse and recovers the tilted U (the base read it half a
    circle off)."""
    u_true = mccode_rotation_matrix(-30, 10, 20)
    B = reciprocal_basis_tas(*CUBIC)
    saved = []
    for hkl in [(1, 0, 0), (0, 1, 0), (1, 1, 1)]:
        q = component_q_to_instrument_q(u_true @ B @ np.array(hkl, dtype=float))
        a = solve_instrument_angles(q, 2.0, 2.0, sense_sample=1)
        saved.append({"hkl": list(hkl), "angles": [a.sth, a.saz, a.stt], "ki": 2.0, "kf": 2.0})
    assert all(d["angles"][2] > 0 for d in saved)
    assert max(abs(d["angles"][1]) for d in saved) > 5.0

    ub = UBMatrix.from_dict({"lattice": list(CUBIC), "peaks": saved})
    assert [(p.sense_sample, p.stage) for p in ub.peaks] == [(1, None)] * 3
    ub.calculate_U_from_peaks()
    assert np.allclose(ub.U, u_true, rtol=0.0, atol=1e-9)


def test_legacy_peak_dict_with_positive_stt_fits_correctly():
    """In-plane peaks on IN8's stage (+1 sense) saved without sense or record:
    (A3, sgl, stt) is then the legacy triple, and the fit reads it right."""
    gonio = STAGES["in8"][0]
    B = reciprocal_basis_tas(*CUBIC)
    saved = []
    for hkl in [(1, 0, 0), (0, 1, 0)]:
        q = U_IN_PLANE @ B @ np.array(hkl, dtype=float)
        stt = stt_from_q_norm(float(np.linalg.norm(q)), K, K, 1)
        r = solve_stage(gonio, -q, lab_q_from_stt(K, K, stt))
        record = stage_record(gonio, r, ki=K, kf=K, sense=1)
        d = ObservedPeak(hkl=tuple(hkl), angles=(r["A3"], r["sgl"], stt), ki=K, kf=K,
                         stage=record).to_dict()
        d.pop("sense_sample")
        d.pop("stage")
        assert d["angles"][2] > 0
        saved.append(d)
    ub = UBMatrix.from_dict({"lattice": list(CUBIC), "peaks": saved})
    assert [p.sense_sample for p in ub.peaks] == [1, 1]
    assert [p.stage for p in ub.peaks] == [None, None]
    ub.calculate_U_from_peaks()
    assert np.allclose(ub.U, U_IN_PLANE, rtol=0.0, atol=1e-9)


def test_peak_sense_and_stage_round_trip_through_dict():
    peak = ObservedPeak(hkl=(1, 0, 0), angles=(10.0, 0.0, 40.0), ki=K, kf=K,
                        sense_sample=-1, stage={"A3": 10.0})
    back = ObservedPeak.from_dict(peak.to_dict())
    assert back.sense_sample == -1
    assert back.stage == {"A3": 10.0}


# --- the stage solver ------------------------------------------------------------

@pytest.mark.parametrize("azimuth", [77.0, 182.0, 224.0])
def test_a_point_exactly_at_the_travel_corner_solves(azimuth):
    """Q levelled by exactly (sgl, sgu) = (-17.3, 17.3), the corner of +/-17.3
    deg travel: the root solved at one arc's end node lands ~1e-14 deg past the
    other's limit, which TRAVEL_TOLERANCE_DEG absorbs."""
    travel = 17.3
    gonio = (StageAxis("A3", (0.0, 1.0, 0.0)),
             StageAxis("sgl", (1.0, 0.0, 0.0), -travel, travel),
             StageAxis("sgu", (0.0, 0.0, 1.0), -travel, travel))
    a = math.radians(azimuth)
    q_lab = 2.0 * np.array([math.cos(a), 0.0, math.sin(a)])
    q = (_rot((1, 0, 0), -travel) @ _rot((0, 0, 1), travel)).T @ q_lab

    angles = solve_stage(gonio, q, q_lab)
    assert (angles["sgl"], angles["sgu"]) == pytest.approx((-travel, travel), abs=1e-9)
    assert all(abs(angles[name]) <= travel + TRAVEL_TOLERANCE_DEG for name in ("sgl", "sgu"))
    assert np.allclose(stage_rotation(gonio, angles) @ q, q_lab, rtol=0.0, atol=1e-9)


def test_locked_mode_never_moves_a_tilt_and_refuses_out_of_plane():
    gonio = STAGES["in12"][0]
    B = reciprocal_basis_tas(*CUBIC)
    u = _rot((1, 0, 0), 3.0) @ _rot((0, 0, 1), -2.0)
    ub = u @ B
    lock = lock_plane(gonio, ub, (1, 0, 0), (0, 1, 0))
    inner = stage_rotation(gonio[1:], lock)
    for hkl in ((1, 0, 0), (0, 1, 0)):
        q = ub @ np.array(hkl, dtype=float)
        assert abs((inner @ q)[1]) < 1e-12 * np.linalg.norm(q)
    assert max(abs(v) for v in lock.values()) < 4.0

    for hkl in ((2, 0, 0), (1, 1, 0), (-1, 2, 0), (0, -2, 0)):
        q = ub @ np.array(hkl, dtype=float)
        stt = stt_from_q_norm(float(np.linalg.norm(q)), 2.0, 2.0, -1)
        q_lab = lab_q_from_stt(2.0, 2.0, stt)
        angles = solve_stage(gonio, q, q_lab, locked=lock)
        assert {name: angles[name] for name in lock} == lock
        assert np.allclose(stage_rotation(gonio, angles) @ q, q_lab, rtol=0.0, atol=1e-9)

    q = ub @ np.array([1.0, 0.0, 1.0])
    stt = stt_from_q_norm(float(np.linalg.norm(q)), 2.0, 2.0, -1)
    out = math.degrees(math.asin((inner @ q)[1] / np.linalg.norm(q)))
    with pytest.raises(StageUnreachable, match="out of the locked scattering plane") as err:
        solve_stage(gonio, q, lab_q_from_stt(2.0, 2.0, stt), locked=lock)
    assert f"{out:+.4g}°" in str(err.value)


@pytest.mark.parametrize(("angles", "named"), [
    ({"A3": 1.0, "sgl": 2.0}, "sgu"),
    ({"A3": 1.0, "sgl": 2.0, "sgu": 3.0, "chi": 4.0}, "chi"),
    ({"omega": 1.0, "chi": 2.0, "phi": 3.0}, "A3"),
], ids=["missing", "unknown", "other-stage"])
def test_stage_readers_refuse_angles_that_do_not_match_the_stage(angles, named):
    """A missing or unknown axis name is an error naming it, never 0 deg."""
    gonio = tas_goniometer()
    for read in (lambda: stage_rotation(gonio, angles),
                 lambda: sample_arm_euler(gonio, angles, np.eye(3)),
                 lambda: stage_record(gonio, angles)):
        with pytest.raises(ValueError, match=named):
            read()
    if named == "chi":                                   # check_travel allows a subset
        with pytest.raises(ValueError, match=named):
            check_travel(gonio, {"chi": 1.0})


# --- parity with TAVI 010cec6d on the same inputs -----------------------------------

@pytest.fixture(scope="module")
def tavi():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _close(a, b):
    assert np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float),
                       rtol=0.0, atol=1e-9), (a, b)


def _same_readouts(got, want):
    assert set(got) == set(want)
    _close([got[n] for n in sorted(got)], [want[n] for n in sorted(want)])


def _parity_stages(tavi):
    for name, data in tavi["stages"].items():
        gonio, sense = STAGES[name]
        assert data["sense"] == sense
        want = [(n, tuple(axis), -math.inf if lo is None else lo, math.inf if hi is None else hi)
                for n, axis, lo, hi in data["axes"]]
        assert [tuple(ax) for ax in gonio] == want


def _parity_stage_sets(tavi):
    for case in tavi["stage_sets"]:
        gonio, _ = STAGES[case["stage"]]
        sense, k, lattice = case["sense"], case["k"], case["lattice"]
        B = reciprocal_basis_tas(*lattice)
        u_true = np.array(case["u_true"])
        stage_peaks, legacy_peaks = [], []
        for point in case["points"]:
            hkl = point["hkl"]
            _close(u_true @ B @ np.array(hkl, dtype=float), point["q_mount"])
            stt = stt_from_q_norm(float(np.linalg.norm(point["q_mount"])), k, k, sense)
            _close(stt, point["stt"])
            _close(lab_q_from_stt(k, k, point["stt"]), point["q_lab"])
            q = np.array(point["q_mount"])
            signed = -q if sense > 0 else q
            if "error" in point:
                with pytest.raises(StageUnreachable) as err:
                    solve_stage(gonio, signed, np.array(point["q_lab"]))
                assert str(err.value) == point["error"]
                continue
            _same_readouts(solve_stage(gonio, signed, np.array(point["q_lab"])),
                           point["readouts"])
            record = stage_record(gonio, point["readouts"], ki=k, kf=k, sense=sense)
            _close(legacy_triple(record, point["stt"], k, k, sense), point["legacy"])
            r = point["readouts"]
            stage_peaks.append(ObservedPeak(hkl=tuple(hkl), angles=(r["A3"], r["sgl"], point["stt"]),
                                            ki=k, kf=k, stage=record))
            legacy_peaks.append(ObservedPeak.from_dict({"hkl": hkl, "angles": point["legacy"],
                                                        "ki": k, "kf": k}))
        for key, peaks in (("stage", stage_peaks), ("legacy", legacy_peaks)):
            for n in (2, 3):
                _close(_fit(lattice, peaks[:n]), case["fit"][f"{key}_{n}"])


def _parity_locked(tavi):
    for case in tavi["locked"]:
        gonio, _ = STAGES[case["stage"]]
        ub = np.array(case["ub"])
        hkl_u, hkl_v = (tuple(v) for v in case["plane"])
        if "lock_error" in case:
            with pytest.raises(StageUnreachable) as err:
                lock_plane(gonio, ub, hkl_u, hkl_v)
            assert str(err.value) == case["lock_error"]
            continue
        lock = lock_plane(gonio, ub, hkl_u, hkl_v)
        _same_readouts(lock, case["lock"])
        for point in case["points"]:
            q, q_lab = np.array(point["q_mount"]), np.array(point["q_lab"])
            _close(ub @ np.array(point["hkl"], dtype=float), q)
            if "error" in point:
                with pytest.raises(StageUnreachable) as err:
                    solve_stage(gonio, q, q_lab, locked=case["lock"], plane=(hkl_u, hkl_v))
                assert str(err.value) == point["error"]
            else:
                _same_readouts(solve_stage(gonio, q, q_lab, locked=case["lock"],
                                           plane=(hkl_u, hkl_v)), point["readouts"])


class _QPeak:
    """What refine_lattice_from_peaks reads: hkl, q_lab, is_valid."""
    is_valid = True

    def __init__(self, hkl, q):
        self.hkl, self.q_lab = tuple(hkl), np.array(q)


def _parity_refine(tavi):
    for case in tavi["refine"]:
        peaks = [_QPeak(p["hkl"], p["q"]) for p in case["peaks"]]
        start = tuple(case["start"])
        if "error" in case:
            with pytest.raises(ValueError) as err:
                refine_lattice_from_peaks(peaks, start, case["system"])
            assert str(err.value) == case["error"]
            continue
        result = refine_lattice_from_peaks(peaks, start, case["system"])
        assert result["crystal_system"] == case["crystal_system"]
        _close(result["lattice"], case["lattice"])
        _close(result["rms_error"], case["rms_error"])
    starts = {entry["case"]: tuple(entry["start"]) for entry in tavi["refine"]}
    assert {case: lattice_crystal_system(starts[case])
            for case in tavi["crystal_systems"]} == tavi["crystal_systems"]


def _parity_feasibility(tavi):
    """``check_feasibility`` on ISAR's PUMA profile against TAVI's per-point stage-solve
    excerpt (instruments/tas_runtime.py 920-934, re-run by the generator with TAVI's own
    primitives, not ``calculate_stage_angles`` itself) for the same reflection, crystal and k."""
    for point in tavi["feasibility"]:
        assert point["stage"] == "puma" and point["sense"] == PUMA.sense_sample
        sample = SampleState(*point["lattice"], orientation_set=True)
        sample.ub().set_U(np.array(point["u"]))
        assert point["ki"] == point["kf"]
        out = check_feasibility(sample, hkl=point["hkl"], e_transfer_meV=0.0, fixed_mode="kf",
                                fixed_k_inv_angstrom=point["kf"], instrument=PUMA)
        _close(out["stt_deg"], point["stt"])
        if "error" in point:
            assert (out["reason"], out["detail"]) == ("stage_out_of_travel", point["error"])
        else:
            _close([out["sth_deg"], out["sgl_deg"], out["sgu_deg"]],
                   [point["sth"], point["sgl"], point["sgu"]])


PARITY = {
    "stages": _parity_stages,
    "stage_sets": _parity_stage_sets,
    "locked": _parity_locked,
    "refine": _parity_refine,
    "feasibility": _parity_feasibility,
}


@pytest.mark.parametrize("section", list(PARITY))
def test_parity_with_tavi_010cec6d(tavi, section):
    assert tavi["tavi_sha"].startswith("010cec6d")
    PARITY[section](tavi)

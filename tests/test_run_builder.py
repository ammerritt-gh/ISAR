"""Synthetic run builder (§4): wrap Phase 1 ground truth into a multi-analyzer facility run.

The signal is fanned across nine analyzers at nine Q values (each its own Phase-1 GroundTruth),
sharing the energy grid + monitor/time. This re-uses Phase 1's `make_ground_truth`/`draw_counts`
(do not re-implement scan generation — wrap it).
"""
import numpy as np
import pytest

from isar.synth_format import run_builder


def test_build_run_has_nine_detectors_on_a_shared_grid():
    run = run_builder.build_s30_run("SYN", n_scans=2)
    assert run.experiment_id == "SYN"
    assert len(run.scans) == 2
    sc = run.scans[0]
    assert len(sc.detectors) == 9
    nP = len(sc.energy)
    for d in sc.detectors:
        assert d.counts.shape == (nP,)
        assert len(d.q) == 3
        assert d.counts.min() >= 0          # Poisson counts are non-negative
    assert sc.monitor.shape == (nP,) and np.all(sc.monitor > 0)
    assert sc.time.shape == (nP,) and np.all(sc.time > 0)


def test_central_detector_carries_central_q():
    sc = run_builder.build_s30_run("SYN", n_scans=1).scans[0]
    # ana5 (index 4) is the central analyzer; its Q is the scan's central Q (== future #Q / #V8).
    assert sc.detectors[4].detector_id == "ana5"
    assert sc.detectors[4].q == sc.central_q


def test_counts_are_seeded_and_reproducible():
    a = run_builder.build_s30_run("SYN", n_scans=1, seed0=7).scans[0]
    b = run_builder.build_s30_run("SYN", n_scans=1, seed0=7).scans[0]
    assert np.array_equal(a.detectors[0].counts, b.detectors[0].counts)
    c = run_builder.build_s30_run("SYN", n_scans=1, seed0=8).scans[0]
    assert not np.array_equal(a.detectors[0].counts, c.detectors[0].counts)


def test_scan_numbers_are_assigned_in_order():
    run = run_builder.build_s30_run("SYN", n_scans=3)
    assert [s.scan_number for s in run.scans] == [1, 2, 3]


# --- S30 calibration set (measurement run + companion elastic scans + gold table) --------------
def _cal_set(tmp_path, name, **kw):
    return run_builder.build_s30_calibration_set(tmp_path / name, **kw)


def test_s30_calibration_set_is_byte_reproducible_from_its_seed(tmp_path):
    a, b = _cal_set(tmp_path, "a", seed=11), _cal_set(tmp_path, "b", seed=11)
    c = _cal_set(tmp_path, "c", seed=12)
    for attr in ("measurement_path", "companion_path", "gold_path"):
        assert getattr(a, attr).read_bytes() == getattr(b, attr).read_bytes()
    assert a.companion_path.read_bytes() != c.companion_path.read_bytes()
    assert [e.zero for e in a.elastic] != [e.zero for e in c.elastic]


def test_s30_calibration_set_elastic_scans_read_back_and_zeros_are_recovered(tmp_path):
    from isar.calibrate import bragg
    from isar.calibrate.energy_zero import build_energy_calibration

    cs = _cal_set(tmp_path, "s", seed=21)
    es = bragg.read_elastic_scans(cs.companion_path)
    # only the herixE ascans come back (the companion's measurement fscans are not elastic scans)
    assert [(e.scan_number, e.slit) for e in es] == [(i.scan_number, i.slit) for i in cs.elastic]
    assert len({i.slit for i in cs.elastic}) >= 2

    cal = build_energy_calibration(es)
    for i in cs.elastic:   # observed error ~0.003 meV; 0.02 leaves room for platform fit noise
        assert cal.zero(i.scan_number) == pytest.approx(i.zero, abs=0.02)
    for slit in {i.slit for i in cs.elastic}:
        gfw, _lfw = cal.resolution(slit)
        inj = next(i for i in cs.elastic if i.slit == slit)
        assert gfw == pytest.approx(inj.gauss_fwhm, abs=0.1)
    # held scans lie outside the elastic span, the rest inside it
    assert cs.held_scans and all(cal.zero(s) is None for s in cs.held_scans)
    assert all(cal.zero(s) is not None for s in cs.measurement_scans if s not in cs.held_scans)


def test_s30_calibration_set_measurement_and_gold_files_load(tmp_path):
    from isar.analyze import gold
    from isar.parsers import spec_s30

    cs = _cal_set(tmp_path, "m", seed=31)
    recs = spec_s30.parse_file(cs.measurement_path, spec_s30.card_template())
    assert [r.scan_number for r in recs] == cs.measurement_scans
    rows = gold.load_gold(cs.gold_path)
    assert len(rows) == cs.n_gold_matched < len(cs.measurement_scans)
    assert all(r.slit == cs.measurement_slits[r.scan] and r.analyzer == "ana7" for r in rows)

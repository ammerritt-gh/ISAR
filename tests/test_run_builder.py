"""Synthetic run builder (§4): wrap Phase 1 ground truth into a multi-analyzer facility run.

The signal is fanned across nine analyzers at nine Q values (each its own Phase-1 GroundTruth),
sharing the energy grid + monitor/time. This re-uses Phase 1's `make_ground_truth`/`draw_counts`
(do not re-implement scan generation — wrap it).
"""
import numpy as np

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

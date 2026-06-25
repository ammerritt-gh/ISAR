"""Injectable quirks (§4) as RECORDING hooks: each defect mutates a RunScan AND logs what it did
into `scan.truth["quirks"]`, so the QC layer (next push) can score "masked exactly the injected
points and only those." This push builds them but the round-trip runs clean (no quirks).

The last test is the key cross-check: a quirked run still parses (quirks change data, not format),
and a monitor-dropout point is **masked, not dropped** (§0.4) — the mask-never-delete principle.
"""
import numpy as np

from isar.parsers import spec_s30
from isar.synth_format import quirks, run_builder


def _one_scan(seed0=1):
    return run_builder.build_s30_run("Q", n_scans=1, seed0=seed0).scans[0]


def test_inject_spike_mutates_and_records():
    sc = _one_scan()
    before = sc.detectors[0].counts[10]
    quirks.inject_spike(sc, "ana1", 10, magnitude=5000.0)
    assert sc.detectors[0].counts[10] == before + 5000.0
    q = sc.truth["quirks"][-1]
    assert q["type"] == "spike" and q["detector"] == "ana1" and q["index"] == 10


def test_inject_monitor_dropout_zeros_monitor_and_records():
    sc = _one_scan()
    quirks.inject_monitor_dropout(sc, 5)
    assert sc.monitor[5] == 0
    assert sc.truth["quirks"][-1]["type"] == "monitor_dropout"


def test_inject_truncation_shortens_all_arrays():
    sc = _one_scan()
    quirks.inject_truncation(sc, n_keep=20)
    assert len(sc.energy) == 20
    assert all(len(d.counts) == 20 for d in sc.detectors)
    assert len(sc.monitor) == 20 and len(sc.time) == 20
    assert sc.truth["quirks"][-1]["type"] == "truncated"


def test_inject_dead_detector_records():
    sc = _one_scan()
    quirks.inject_dead_detector(sc, "ana3")
    q = sc.truth["quirks"][-1]
    assert q["type"] == "dead_detector" and q["detector"] == "ana3"


def test_inject_sensor_divergence_sets_sensor_far_from_setpoint():
    sc = _one_scan()
    quirks.inject_sensor_divergence(sc, value=298.0)
    assert abs(sc.temperature_sensor - sc.temperature_setpoint) > 5
    assert sc.truth["quirks"][-1]["type"] == "sensor_divergence"


def test_quirked_run_still_parses_and_masks_the_dropout(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("Q", n_scans=1)
    quirks.inject_spike(run.scans[0], "ana1", 10, 5000.0)
    quirks.inject_monitor_dropout(run.scans[0], 5)

    path = tmp_path / "q.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    recs = spec_s30.parse_file(path, card)

    assert len(recs) == 1
    sp = recs[0].spectra["ana1"]
    # The monitor==0 point is MASKED, not deleted: the array is full length, the point is flagged.
    assert len(sp.mask) == len(run.scans[0].energy)
    assert not sp.mask[5]
    assert any(pf.index == 5 and pf.type == "monitor_dropout" for pf in sp.point_flags)

"""Deterministic scan/spectrum QC (§7.2): truncated / empty / dead_detector / sensor_divergence.

These checks only ever APPEND flags (and, for a divergent sensor, mark that sensor's `valid_mask`) —
they never touch the signal, never delete a point (§0.4), and must survive a malformed record without
throwing (§0.5: a QC failure is a benign over-flag or a "couldn't check", never a crashed batch). The
synthetic quirk injectors give us known truth to score against; we parse via the supplementary S30
path, which produces intensity at parse time (no reduce step needed).
"""
import numpy as np

from isar.parsers import spec_s30
from isar.reduce import qc_scans
from isar.synth_format import quirks, run_builder


def _parse(run, card, tmp_path):
    path = tmp_path / f"{run.experiment_id}.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    return spec_s30.parse_file(path, card)


def _clean(seed0=7, target=200.0):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QC", n_scans=1, seed0=seed0, target_peak_counts=target)
    return run, card


def _types(flags):
    return {f.type for f in flags}


# ---------------------------------------------------------------------------
# truncated (scan-level)
# ---------------------------------------------------------------------------
def test_truncated_below_min_points_flags_scan(tmp_path):
    run, card = _clean()
    quirks.inject_truncation(run.scans[0], n_keep=3)        # < min_points (5)
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    trunc = [f for f in rec.scan_flags if f.type == "truncated"]
    assert len(trunc) == 1
    assert trunc[0].severity == "error"
    assert "3" in trunc[0].reason


def test_clean_run_is_not_truncated(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    assert len(rec.spectra["ana5"].intensity) >= card.policy.min_points   # clears the floor
    qc_scans.qc_scans(rec, card)
    assert "truncated" not in _types(rec.scan_flags)


def test_truncated_uses_commanded_np_when_present(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    rec.raw_header["S"] = "bs qh 4 -0.2 0 1 dqh 0 0 0 0.5 np 999 mn 100000"   # claims 999 points
    qc_scans.qc_scans(rec, card)
    trunc = [f for f in rec.scan_flags if f.type == "truncated"]
    assert len(trunc) == 1
    assert trunc[0].severity == "warning"                  # above the floor, but short of commanded
    assert "999" in trunc[0].reason


# ---------------------------------------------------------------------------
# empty (scan-level) vs dead_detector (spectrum-level)
# ---------------------------------------------------------------------------
def test_all_flat_spectra_flag_scan_empty(tmp_path):
    run, card = _clean()
    for d in run.scans[0].detectors:
        quirks.inject_flat(run.scans[0], d.detector_id)    # every analyzer flat → beam down
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    assert "empty" in _types(rec.scan_flags)
    # when EVERYTHING is flat it is not mislabeled as a per-analyzer dead_detector
    assert all("dead_detector" not in _types(sp.spectrum_flags) for sp in rec.spectra.values())


def test_clean_run_is_not_empty(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    assert "empty" not in _types(rec.scan_flags)


def test_one_flat_among_live_siblings_flags_dead_detector(tmp_path):
    run, card = _clean()
    quirks.inject_dead_detector(run.scans[0], "ana3")
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    dead = [f for f in rec.spectra["ana3"].spectrum_flags if f.type == "dead_detector"]
    assert len(dead) == 1 and dead[0].severity == "warning"
    assert "dead_detector" not in _types(rec.spectra["ana5"].spectrum_flags)   # a live sibling
    assert "empty" not in _types(rec.scan_flags)                              # not the whole scan


def test_clean_run_has_no_dead_detector(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    assert all("dead_detector" not in _types(sp.spectrum_flags) for sp in rec.spectra.values())


# ---------------------------------------------------------------------------
# sensor_divergence (scan-level + sensor valid_mask)
# ---------------------------------------------------------------------------
def test_divergent_sensor_flags_scan_and_marks_valid_mask(tmp_path):
    run, card = _clean()
    quirks.inject_sensor_divergence(run.scans[0], value=350.0)   # 60 K from the 290 setpoint
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    assert "sensor_divergence" in _types(rec.scan_flags)
    sensor = rec.temperature.sensors["T_Sample"]
    assert not sensor.valid_mask.any()                  # every divergent point marked invalid
    assert np.allclose(rec.temperature.setpoint, 290.0)  # provenance untouched: setpoint stays truth


def test_clean_sensor_within_tol_is_not_flagged(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    qc_scans.qc_scans(rec, card)
    assert "sensor_divergence" not in _types(rec.scan_flags)
    assert rec.temperature.sensors["T_Sample"].valid_mask.all()


# ---------------------------------------------------------------------------
# invariants: flag-never-delete (§0.4) and survive-mess (§0.5)
# ---------------------------------------------------------------------------
def test_qc_scans_never_touches_the_signal(tmp_path):
    run, card = _clean()
    quirks.inject_dead_detector(run.scans[0], "ana3")
    rec = _parse(run, card, tmp_path)[0]
    before_I = {k: sp.intensity.copy() for k, sp in rec.spectra.items()}
    before_mask = {k: sp.mask.copy() for k, sp in rec.spectra.items()}
    qc_scans.qc_scans(rec, card)
    for k, sp in rec.spectra.items():
        assert np.array_equal(sp.intensity, before_I[k])     # signal byte-identical
        assert np.array_equal(sp.mask, before_mask[k])       # point mask untouched (scan-level)


def test_qc_scans_survives_a_degenerate_record(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    rec.spectra["ana1"].intensity = np.array([])             # a zero-length spectrum
    rec.spectra["ana1"].mask = np.array([], dtype=bool)
    qc_scans.qc_scans(rec, card)                             # must not raise
    rec.spectra = {}                                         # no spectra at all
    qc_scans.qc_scans(rec, card)                             # must not raise

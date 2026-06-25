"""qc.qc_record runs the whole deterministic QC layer: point-level then scan/spectrum-level."""
from isar.parsers import spec_s30
from isar.reduce import qc
from isar.synth_format import quirks, run_builder


def _parse(run, card, tmp_path):
    path = tmp_path / f"{run.experiment_id}.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    return spec_s30.parse_file(path, card)


def test_qc_record_runs_point_and_scan_checks(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCALL", n_scans=1, seed0=7, target_peak_counts=200.0)
    quirks.inject_stale_head(run.scans[0], "ana5", n=3)         # point-level
    quirks.inject_dead_detector(run.scans[0], "ana3")           # spectrum-level
    quirks.inject_sensor_divergence(run.scans[0], value=350.0)  # scan-level
    rec = _parse(run, card, tmp_path)[0]
    qc.qc_record(rec, card)
    assert any(pf.type == "stale_head" for pf in rec.spectra["ana5"].point_flags)
    assert any(f.type == "dead_detector" for f in rec.spectra["ana3"].spectrum_flags)
    assert any(f.type == "sensor_divergence" for f in rec.scan_flags)
    assert not rec.spectra["ana5"].mask[:3].any()              # the stale run was masked

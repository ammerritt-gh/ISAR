"""The report model: the removal ledger is the heart — every rejected pass / masked point listed
with a stable id, magnitude, rule, and the reversibility note. Nothing is ever deleted (§0.4)."""
import numpy as np

from isar.parsers import ill_in8, spec_s30, spec_s30_raw
from isar.reduce import collate, combine, qc
from isar.report import model as report_model
from isar.synth_format import quirks, run_builder


def _reduced(run, card, tmp_path):
    path = tmp_path / f"{run.experiment_id}.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    records = spec_s30_raw.parse_file(path, card)
    for r in records:
        if r.scan_type == "measurement":
            combine.reduce_record(r, card)
    return records


def _build(records, card):
    return report_model.build_report_model(records, collate.collate(records, card), card)


def test_model_header_and_catalogue(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=5, include_ascans=True)
    model = _build(_reduced(run, card, tmp_path), card)
    assert model.experiment_id == "RPT.spec"
    assert (model.facility, model.instrument) == ("APS", "S30")
    assert model.rule == "poisson"
    assert model.n_measurements == 1
    assert len(model.catalogue) == 9                  # nine analyzer-Q groups
    assert model.coverage["component"] == "h"
    # the two ascans are acknowledged, not given full sections
    assert {s["scan_type"] for s in model.other_scans} == {"calibration", "alignment"}


def test_removal_ledger_lists_exactly_the_injected_cosmic_ray(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=11, include_ascans=False)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    model = _build(_reduced(run, card, tmp_path), card)

    removals = [rm for s in model.scans for sp in s.spectra for rm in sp.removals]
    cosmic = [rm for rm in removals if rm.kind == "cosmic_ray_rejected"]
    assert len(cosmic) == 1
    rm = cosmic[0]
    assert rm.detector_id == "ana5" and rm.point == p0
    assert rm.passes == [7]
    assert rm.magnitude > 9000
    assert rm.rule == "poisson"
    assert rm.stable_id == f"RPT.spec:1:ana5:p{p0}"
    assert "reversible" in rm.reversible.lower()


def test_nothing_deleted_invariant(tmp_path):
    # Building the report must never mutate or shrink the raw arrays — the point stays in the file.
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=11, include_ascans=False)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    records = _reduced(run, card, tmp_path)
    rec = next(r for r in records if r.scan_type == "measurement")
    sp = rec.spectra["ana5"]
    nP, n_mini = sp.miniscan_counts.shape

    _build(records, card)

    assert sp.miniscan_counts.shape == (nP, n_mini)    # ensemble intact
    assert sp.intensity.shape == (nP,)                 # rate array full length
    assert sp.miniscan_reject[p0, 7]                   # the rejected pass is still marked, not gone


def test_masked_monitor_dropout_point_is_in_the_ledger(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=5, include_ascans=False)
    sc = run.scans[0]
    sc.miniscan_monitor[3, :] = 0.0                    # kill the monitor at point 3 (all passes)
    sc.monitor = sc.miniscan_monitor.sum(axis=1)
    model = _build(_reduced(run, card, tmp_path), card)
    masked = [rm for s in model.scans for sp in s.spectra for rm in sp.removals
              if rm.kind == "masked"]
    assert any(rm.point == 3 for rm in masked)


def _qc_model(run, card, tmp_path):
    # Supplementary path (intensity at parse time) is the simplest substrate for QC flags.
    path = tmp_path / f"{run.experiment_id}.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    for r in records:
        qc.qc_record(r, card)
    return report_model.build_report_model(records, collate.collate(records, card), card)


def test_qc_scan_and_spectrum_flags_surface_in_the_model(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCM", n_scans=1, seed0=7, target_peak_counts=200.0)
    quirks.inject_dead_detector(run.scans[0], "ana3")
    quirks.inject_sensor_divergence(run.scans[0], value=350.0)
    model = _qc_model(run, card, tmp_path)
    s = model.scans[0]
    assert "sensor_divergence" in {f["type"] for f in s.scan_flags}   # scan-level flag rendered
    ana3 = next(sp for sp in s.spectra if sp.detector_id == "ana3")
    assert "dead_detector" in {f["type"] for f in ana3.flags}          # spectrum-level flag rendered


def test_truncated_scan_flag_surfaces_in_the_model(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCT", n_scans=1, seed0=7)
    quirks.inject_truncation(run.scans[0], n_keep=3)                  # < min_points
    model = _qc_model(run, card, tmp_path)
    assert "truncated" in {f["type"] for s in model.scans for f in s.scan_flags}


def test_stale_head_masked_point_named_in_ledger_not_zero_monitor(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCS", n_scans=1, seed0=7, target_peak_counts=200.0)
    quirks.inject_stale_head(run.scans[0], "ana5", n=3)
    model = _qc_model(run, card, tmp_path)
    ana5 = next(sp for sp in model.scans[0].spectra if sp.detector_id == "ana5")
    stale = [rm for rm in ana5.removals if rm.kind == "stale_head"]
    assert {rm.point for rm in stale} == {0, 1, 2}                    # the masked leading run
    assert all("zero kept monitor" not in rm.reason for rm in stale)  # flag-aware reason, not default


def test_in8_merged_spectra_appear_in_the_model(tmp_path):
    card = ill_in8.card_template()
    run = run_builder.build_in8_run("DIR-RPT", n_scans=2, seed0=7)
    path = tmp_path / "DIR-RPT.numor"
    path.write_bytes(ill_in8.emit_file(run, card))
    records = ill_in8.parse_file(path, card)
    model = report_model.build_report_model(records, collate.collate(records, card), card)

    assert len(model.merged) == 1                         # both scans at one (Q,T) -> one merged
    m = model.merged[0]
    assert m.n_members == 2
    assert {mem[1] for mem in m.members} == {55000, 55001}   # both numors named
    assert m.n_points > 0 and m.fig_id
    assert len(model.scans) == 2                          # ...and each contributing scan still shown


def test_model_is_json_serializable(tmp_path):
    import json
    from isar.schema import _json
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=5, include_ascans=True)
    model = _build(_reduced(run, card, tmp_path), card)
    blob = json.dumps(_json.to_jsonable(model))        # the machine-readable sidecar (§8 hook)
    assert "cosmic_ray_rejected" in blob or "removal" in blob.lower() or model.n_measurements == 1

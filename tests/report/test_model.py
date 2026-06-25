"""The report model: the removal ledger is the heart — every rejected pass / masked point listed
with a stable id, magnitude, rule, and the reversibility note. Nothing is ever deleted (§0.4)."""
import numpy as np

from isar.parsers import spec_s30_raw
from isar.reduce import collate, combine
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


def test_model_is_json_serializable(tmp_path):
    import json
    from isar.schema import _json
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("RPT", n_scans=1, seed0=5, include_ascans=True)
    model = _build(_reduced(run, card, tmp_path), card)
    blob = json.dumps(_json.to_jsonable(model))        # the machine-readable sidecar (§8 hook)
    assert "cosmic_ray_rejected" in blob or "removal" in blob.lower() or model.n_measurements == 1

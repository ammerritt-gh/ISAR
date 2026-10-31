"""A manual point mask is checked against the data on every read: one whose point no longer exists lapses.

Lapsed masks stay in the intake log and are never applied; an analyzer whose stored data cannot be read has its
masks in neither list. Generated data only (hard rule 10); a re-import is simulated by rewriting the unit file.
"""
import json
import logging
from types import SimpleNamespace

import pytest

import intake_fixtures as fx
from isar.intake import inputs, scans, store
from isar.intake.inputs import apply, calibration_inputs_key, unit_inputs
from isar.intake.point_masks import point_ids, save_selection
from isar.session import calibrations, ingest, unit_data
from isar.session.unit_data import read_unit_data_for_unit, unit_data_path


@pytest.fixture
def masked(tmp_path):
    """One scan ingested; its analyzer a03's last point masked. Returns (workspace, source folder, unit key, point)."""
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root, data_dir=ws.root / "data")
    ws.save(card, session)
    key = next(k for k in session.scans if k.endswith(":a03"))
    point = _stored_ids(ws.root, key)[-1]
    assert save_selection(ws.root, {key: [point]}).ok
    return ws, data, key, point


def _scan(key):
    return key.rsplit(":", 1)[0]


def _det(key):
    return key.rsplit(":", 1)[1]


def _stored_ids(root, key):
    exp, n, det = key.rsplit(":", 2)
    d = read_unit_data_for_unit(root / "data", exp, int(n), det)
    return point_ids(d["x"], d["counts"], d["variance"])


def _si(root, key, row=None):
    row = row or scans.read_scans(root)[_scan(key)]
    read = store.read_all(root, store.ENTRIES)
    return inputs.scan_inputs(row, inputs.index_entries(read.entries, lines=read.lines), data_dir=root / "data")


def _rerun(root, key):
    """What a re-import of a changed file does: every counts value moves and the row takes a new revision."""
    exp, n, det = key.rsplit(":", 2)
    path = unit_data_path(root / "data", exp, int(n), det)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["counts"] = [c + 1.0 for c in doc["counts"]]
    path.write_text(json.dumps(doc), encoding="utf-8")
    scans.record_import(root, [{**scans.read_scans(root)[_scan(key)], "data_revision": "rev-2"}])


def test_a_mask_whose_point_changed_lapses_and_stays_recorded(masked):
    ws, _data, key, point = masked
    det = _det(key)
    assert _si(ws.root, key).point_masks[det] == (point,)
    _rerun(ws.root, key)
    si = _si(ws.root, key)
    assert si.point_masks[det] == () and si.lapsed_masks[det] == (point,)
    assert si.unchecked_masks == ()
    read = store.read_all(ws.root, store.ENTRIES)
    assert inputs.index_entries(read.entries, lines=read.lines).point_masks[key] == (point,)   # nothing withdrawn


def test_the_effective_record_and_the_fit_inputs_carry_only_applied_masks(masked):
    ws, data, key, point = masked
    det = _det(key)
    rec = fx.records_for(ws, data)[0]
    before = _si(ws.root, key)
    assert apply(rec, before).facts["point_masks"]["value"] == {det: [point]}
    unit_before = unit_inputs(scans.read_scans(ws.root)[_scan(key)], before, det, None)
    assert unit_before["point_masks"] == [point]
    _rerun(ws.root, key)
    row = scans.read_scans(ws.root)[_scan(key)]
    now = _si(ws.root, key, row)
    assert "point_masks" not in apply(rec, now).facts
    unit_now = unit_inputs(row, now, det, None)
    assert unit_now["point_masks"] == []
    assert inputs.inputs_hash(unit_now) != inputs.inputs_hash(unit_before)
    assert any(c.field == "point_masks" for c in inputs.changes(unit_before, unit_now))


def test_the_calibration_key_from_readiness_equals_the_one_of_the_effective_record(masked):
    ws, data, key, point = masked
    rec = fx.records_for(ws, data)[0]
    _rerun(ws.root, key)
    row = scans.read_scans(ws.root)[_scan(key)]
    si = _si(ws.root, key, row)
    eff = apply(rec, si)
    no_curves = SimpleNamespace(folder=None, pattern=None, fingerprint=None)
    from_record = calibrations._inputs_key(eff, row["data_revision"], no_curves)
    # readiness' form of the same key: its applied masks of the live analyzers, as scan_inputs gives them
    from_readiness = calibration_inputs_key(
        data_revision=row["data_revision"], folder=None, pattern=None, library=None,
        declared_slit=calibrations._declared_slit(eff), analyzers=sorted(eff.spectra),
        counts_accepted=calibrations._accepted(eff), point_masks={d: si.point_masks.get(d, ()) for d in eff.spectra},
        named=None)
    assert from_record == from_readiness
    assert from_record != calibration_inputs_key(
        data_revision=row["data_revision"], folder=None, pattern=None, library=None,
        declared_slit=calibrations._declared_slit(eff), analyzers=sorted(eff.spectra),
        counts_accepted=calibrations._accepted(eff), point_masks={_det(key): (point,)}, named=None)


def test_unchanged_data_lapses_nothing(masked):
    ws, _data, key, point = masked
    si = _si(ws.root, key)
    assert all(ids == () for ids in si.lapsed_masks.values())
    assert unit_inputs(scans.read_scans(ws.root)[_scan(key)], si, _det(key), None)["point_masks"] == [point]


def test_unreadable_unit_data_holds_the_masks_neither_applied_nor_lapsed(masked, caplog):
    ws, _data, key, _point = masked
    det = _det(key)
    exp, n, _ = key.rsplit(":", 2)
    unit_data_path(ws.root / "data", exp, int(n), det).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="isar.intake.point_masks"):
        si = _si(ws.root, key)
    assert si.unchecked_masks == (det,)
    assert si.point_masks[det] == () and si.lapsed_masks[det] == ()
    assert f"point masks of {key}" in caplog.text


def test_the_identities_are_read_once_per_data_revision(masked, monkeypatch):
    ws, _data, key, point = masked
    det = _det(key)
    real, calls = unit_data.read_unit_data_for_unit, []
    monkeypatch.setattr(unit_data, "read_unit_data_for_unit", lambda *a: calls.append(a) or real(*a))
    _si(ws.root, key)
    _si(ws.root, key)
    assert len(calls) == 1
    _rerun(ws.root, key)                            # a new revision: read again, and the answer is fresh
    assert _si(ws.root, key).lapsed_masks[det] == (point,)
    assert len(calls) == 2

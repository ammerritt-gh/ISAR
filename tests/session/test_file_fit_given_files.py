"""A fit reads only the files the user gave (S1U6, operator ruling 2026-10-30): the import row records every
member file, and the queued fit, the batch, the preview and the tools re-parse from those, never from a folder.

Artificial BL43 copies in ``tmp_path``; nothing reads ``real_data/``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import isar
from artificial import BL43_A03, BL43_A04, BL43_VAR
from intake_fixtures import workspace
from isar import sandbox_api
from isar.intake import scans as intake_scans
from isar.parsers import sp8_bl43
from isar.parsers.base import FileSet
from isar.session import file_fit, ingest
from isar.session.file_fit import FileItem, fit_file
from isar.session.state import content_hash


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


def _copy(folder, *srcs):
    folder.mkdir(parents=True, exist_ok=True)
    for src in srcs:
        shutil.copy(src, folder / src.name)
    return folder


def _import(ws, *paths):
    card, session = ws.load()
    ingest.ingest_batch(card, session, [str(p) for p in paths], workspace_root=ws.root)
    ws.save(card, session)


def _row(ws) -> dict:
    (row,) = intake_scans.read_scans(ws.root).values()
    return row


def _prepared(ws):
    """``(record, source_by_name)`` or the reason, as the queued fit prepares the (only) scan."""
    frozen = file_fit.freeze(ws.root)
    (key,) = {k.rsplit(":", 1)[0] for k in frozen.session.scans}
    exp, scan = key.rsplit(":", 1)
    prev = next(e for k, e in frozen.session.scans.items() if k.startswith(key + ":"))
    return file_fit.prepare_scan(frozen, exp, int(scan), prev)


def _subset(tmp_path):
    """A03 given as a file, A04 and the .var beside it: the workspace, its folder and the import row."""
    ws = workspace(tmp_path / "w")
    data = _copy(tmp_path / "data", BL43_A03, BL43_A04, BL43_VAR)
    _import(ws, data / BL43_A03.name)
    return ws, data


def _spy_discover(monkeypatch):
    calls = []
    real = ingest.discover

    def spy(data_paths, card):
        calls.append(list(data_paths))
        return real(data_paths, card)
    monkeypatch.setattr(ingest, "discover", spy)
    return calls


def test_a_fit_of_a_subset_reads_the_given_files_only_and_reproduces_the_data_revision(tmp_path):
    ws, data = _subset(tmp_path)
    row = _row(ws)
    assert sorted(row["import"]["paths"]) == ["a03"]
    rec, _src = _prepared(ws)
    assert sorted(rec.spectra) == ["a03"]                       # a04 sits in the folder but was not given
    assert rec.raw_header["sidecar"] == BL43_VAR.name           # the card's companion beside the data
    assert content_hash(rec) == row["data_revision"]


def test_the_import_row_records_every_member_given_in_order_and_no_revision_moves(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _copy(tmp_path / "data", BL43_A03, BL43_A04, BL43_VAR)
    _import(ws, data / BL43_A04.name, data / BL43_A03.name)
    members = _row(ws)["import"]["members"]
    assert members == [str((data / BL43_A04.name).resolve()), str((data / BL43_A03.name).resolve())]
    rec, _src = _prepared(ws)
    with_members = content_hash(rec)
    assert with_members == _row(ws)["data_revision"]
    del rec.raw_header["members"]                                # no stored revision moves
    assert content_hash(rec) == with_members


def test_a_left_out_member_reparses_to_the_same_revision(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _copy(tmp_path / "data", BL43_A03, BL43_VAR)
    (data / BL43_A04.name).write_bytes(b"")
    _import(ws, data / BL43_A03.name, data / BL43_A04.name)
    row = _row(ws)
    assert row["import"]["left_out"]["a04"]["file"] == BL43_A04.name
    rec, _src = _prepared(ws)
    assert content_hash(rec) == row["data_revision"]


def test_a_duplicate_member_is_recorded(tmp_path):
    other = _copy(tmp_path / "other", BL43_A03)
    here = _copy(tmp_path / "here", BL43_A03, BL43_A04, BL43_VAR)
    fs = FileSet(key="CsV3Sb5_302K_0p5_0p5_3_sum", folder=here, sidecars=[here / BL43_VAR.name],
                 measurements=[(here / BL43_A03.name, {"analyzer": "03"}), (here / BL43_A04.name, {"analyzer": "04"}),
                               (other / BL43_A03.name, {"analyzer": "03"})])
    ws = workspace(tmp_path / "w")
    card, _session = ws.load()
    (rec,) = sp8_bl43.parse_set(fs, card)
    assert rec.raw_header["members"] == [str(p.resolve()) for p, _g in fs.measurements]
    assert "a03" not in rec.raw_header["paths"]                  # the duplicate removed the analyzer, not the record


def test_an_old_row_with_members_removed_rebuilds_from_paths_and_left_out(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _copy(tmp_path / "data", BL43_A03, BL43_VAR)
    (data / BL43_A04.name).write_bytes(b"")
    _import(ws, data / BL43_A03.name, data / BL43_A04.name)
    path = ws.root / intake_scans.INTAKE_SCANS
    doc = json.loads(path.read_text(encoding="utf-8"))
    (row,) = doc["scans"].values()
    del row["import"]["members"]
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    rec, _src = _prepared(ws)
    assert content_hash(rec) == row["data_revision"]


def test_without_members_or_paths_the_ledger_lists_the_files_and_never_the_folder(tmp_path):
    ws, data = _subset(tmp_path)
    path = ws.root / intake_scans.INTAKE_SCANS
    doc = json.loads(path.read_text(encoding="utf-8"))
    (row,) = doc["scans"].values()
    row["import"].pop("members")
    row["import"]["paths"] = {}
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    rec, _src = _prepared(ws)
    assert sorted(rec.spectra) == ["a03"]


def _forget(ws, *, ledger=True, row=True):
    if row:
        path = ws.root / intake_scans.INTAKE_SCANS
        doc = json.loads(path.read_text(encoding="utf-8"))
        for r in doc["scans"].values():
            r.pop("import", None)
        path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    if ledger:
        card, session = ws.load()
        session.files = {}
        ws.save(card, session)


def test_no_record_fails_with_a_reason_and_never_reads_the_folder(tmp_path, monkeypatch):
    ws, data = _subset(tmp_path)
    _forget(ws)
    calls = _spy_discover(monkeypatch)
    got = _prepared(ws)
    assert isinstance(got, str) and "no record" in got and "CsV3Sb5_302K_0p5_0p5_3_sum" in got
    assert all(Path(p).is_file() for call in calls for p in call)


def test_a_recorded_file_that_is_gone_is_named(tmp_path):
    ws, data = _subset(tmp_path)
    (data / BL43_A03.name).unlink()
    got = _prepared(ws)
    assert isinstance(got, str) and BL43_A03.name in got


def test_fit_file_reports_the_no_record_reason(tmp_path):
    ws, data = _subset(tmp_path)
    _forget(ws)
    unit = next(iter(ws.load()[1].scans))
    out = []
    fit_file(ws.root, FileItem("q1", unit, "fast", 1), progress=None, emit=out.append)
    (res,) = out
    assert res.status == "failed" and "no record" in res.reason


def test_given_files_lists_the_recorded_files_and_the_preview_never_walks_the_folder(tmp_path, monkeypatch):
    ws, data = _subset(tmp_path)
    card, session = ws.load()
    session.data_paths = [str(data)]                             # the folder is remembered, a04 is in it
    ws.save(card, session)
    card, session = ws.load()
    rows = intake_scans.read_scans(ws.root)
    files = file_fit.given_files(session, rows)
    names = sorted(Path(f).name for f in files)
    assert BL43_A03.name in names and BL43_A04.name not in names
    calls = _spy_discover(monkeypatch)
    unit = next(iter(session.scans))
    sandbox_api.preview_fit(ws.root, unit)
    assert calls and all(Path(p).is_file() for call in calls for p in call)
    assert all(Path(p).name != BL43_A04.name for call in calls for p in call)


def test_given_files_with_an_empty_ledger_gives_the_reason_and_the_preview_is_held(tmp_path):
    ws, data = _subset(tmp_path)
    _forget(ws)
    card, session = ws.load()
    reason = file_fit.given_files(session, intake_scans.read_scans(ws.root))
    assert isinstance(reason, str) and "no record" in reason
    unit = next(iter(session.scans))
    out = sandbox_api.preview_fit(ws.root, unit)
    assert out.status == "held" and "no record" in out.reason


def test_all_recorded_files_leaves_out_a_file_withheld_at_a_later_import(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _copy(tmp_path / "data", BL43_A03, BL43_A04, BL43_VAR)
    _import(ws, data / BL43_A03.name, data / BL43_A04.name)
    _import(ws, data / BL43_A03.name)
    card, session = ws.load()
    assert any(Path(p).name == BL43_A04.name for p in session.files)          # the manifest still holds it
    files = intake_scans.all_recorded_files(intake_scans.read_scans(ws.root), session.files)
    names = [Path(f).name for f in files]
    assert BL43_A03.name in names and BL43_A04.name not in names


def test_all_recorded_files_without_rows_lists_the_manifest():
    files = {"/x/a": {"format": "measurement", "scan_ids": ["e:1"]}, "/x/b": {"format": "companion"},
             "/x/c": {"format": "foreign"}}
    assert intake_scans.all_recorded_files(None, files) == ["/x/a", "/x/b"]
    assert intake_scans.recorded_files(None, files, "e:1") == ["/x/a"]

"""`isar ingest` CLI — exit codes, --json shape, no-workspace error JSON (Phase B)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from isar.cli import ingest_cmd, init_cmd
from isar.parsers import spec_s30
from isar.synth_format import run_builder


def _emit_s30(path: Path, n_scans=2, seed0=1000):
    card = spec_s30.card_template()
    path.write_bytes(spec_s30.emit_file(run_builder.build_s30_run("SYN", n_scans=n_scans, seed0=seed0), card))


def _init_ws(tmp_path) -> tuple[Path, Path]:
    """Emit an S30 file and scaffold a workspace over it via `isar init`; return (data, workspace)."""
    data = tmp_path / "run.spec_supplementary.dat"
    _emit_s30(data)
    ws = tmp_path / "ws"
    code = init_cmd.run(argparse.Namespace(data=str(data), out=str(ws), json=False))
    assert code == 0
    return data, ws


def test_ingest_human_success(tmp_path, capsys):
    data, ws = _init_ws(tmp_path)
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws), json=False))
    out = capsys.readouterr()
    assert code == 0
    assert "measurement=1" in out.out
    assert "added=" in out.out


def test_ingest_json_shape(tmp_path, capsys):
    data, ws = _init_ws(tmp_path)
    capsys.readouterr()                          # drain `isar init`'s human output first
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws), json=True))
    out = capsys.readouterr()
    assert code == 0
    obj = json.loads(out.out.strip())            # exactly one JSON object on stdout
    assert obj["workspace"] == str(ws)
    assert set(obj["scans"]) == {"added", "updated", "unchanged"}
    assert obj["scans"]["added"]                 # a fresh ingest added scans
    assert any(f["class"] == "measurement" for f in obj["files"])
    assert "gaps" in obj and "deduped" in obj
    assert obj["last_ingest"]["logical_scan_count"] == len(obj["scans"]["added"])
    assert obj["last_ingest"]["analyzer_unit_count"] > 0

    from isar.session.state import SessionState
    persisted = SessionState.load(ws / "session.json").last_ingest
    assert persisted["failed_file_count"] == 0


def test_ingest_writes_unit_files_under_workspace_data_dir(tmp_path):
    """The CLI passes ``data_dir = <ws>/data``: per-unit files land under ``<ws>/data/ingest/``,
    one per ledger entry (9 S30 analyzers × scans), never colliding with drive ``data/j-*.json``."""
    from isar.session.state import SessionState

    data, ws = _init_ws(tmp_path)
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws), json=False))
    assert code == 0
    unit_files = sorted((ws / "data" / "ingest").glob("*.json"))
    assert unit_files and all(f.name.startswith("ingest-") for f in unit_files)
    session = SessionState.load(ws / "session.json")
    assert len(unit_files) == len(session.scans)


def test_ingest_no_workspace_json_error(tmp_path, capsys):
    empty = tmp_path / "not_a_ws"
    empty.mkdir()
    code = ingest_cmd.run(argparse.Namespace(data=["x"], workspace=str(empty), json=True))
    out = capsys.readouterr()
    assert code == 2
    obj = json.loads(out.out.strip())
    assert obj["error"]["kind"] == "no_workspace"
    assert "no workspace" in out.err


def test_ingest_internal_error_exits_2_with_clean_cause(tmp_path, capsys, monkeypatch):
    """The backstop: if ingest_batch itself raises, the CLI must not exit with a bare traceback on
    stdout. Exit 2, a machine-readable ``{"error": {"kind": "internal", ...}}`` on stdout under
    --json, and an ``error:`` cause line on stderr for the GUI's extractor."""
    data, ws = _init_ws(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(ingest_cmd.ingest, "ingest_batch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))

    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws), json=True))
    out = capsys.readouterr()

    assert code == 2
    # the JSON error object is the last JSON line on stdout (traceback text may precede it)
    obj = None
    for line in reversed([ln for ln in out.out.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
            break
        except ValueError:
            continue
    assert obj is not None and obj["error"]["kind"] == "internal"
    assert "kaboom" in obj["error"]["message"]
    assert "error: RuntimeError: kaboom" in out.err


def test_ingest_no_workspace_human(tmp_path, capsys):
    empty = tmp_path / "not_a_ws"
    empty.mkdir()
    code = ingest_cmd.run(argparse.Namespace(data=["x"], workspace=str(empty), json=False))
    out = capsys.readouterr()
    assert code == 2
    assert out.out == ""                         # no JSON on stdout in human mode
    assert "no workspace" in out.err


def test_bl43_ingest_writes_the_import_record_and_prints_damaged_intake_lines(tmp_path, capsys):
    """A BL43 workspace gains report/intake_scans.json; a damaged line in the user's entries file is
    reported, never a stop (R14)."""
    from intake_fixtures import bl43_workspace
    from isar.intake import store

    ws, data = bl43_workspace(tmp_path)
    (ws.root / "intake").mkdir(exist_ok=True)       # the fixture's saved resolution folder made it
    (ws.root / store.ENTRIES).write_bytes(b"[1, 2]\n")
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=False))
    out = capsys.readouterr().out
    assert code == 0 and (ws.root / "report" / "intake_scans.json").exists()
    assert "intake: intake/entries.jsonl line 1 skipped: not a JSON object" in out
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=True))
    obj = json.loads(capsys.readouterr().out.strip())
    assert code == 0 and obj["intake_damaged"] == ["intake/entries.jsonl line 1 skipped: not a JSON object"]
    assert obj["import_record_error"] is None


def test_bl43_ingest_says_when_the_import_record_could_not_be_written(tmp_path, capsys, monkeypatch):
    from intake_fixtures import bl43_workspace
    from isar.intake import scans

    def locked(*_a, **_k):
        raise PermissionError("intake_scans.json is held by another process")
    monkeypatch.setattr(scans, "record_import", locked)
    ws, data = bl43_workspace(tmp_path)
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=False))
    out = capsys.readouterr().out
    assert code == 0 and "intake: report/intake_scans.json was not written (PermissionError: " in out
    assert "file values may be stale" in out
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=True))
    obj = json.loads(capsys.readouterr().out.strip())
    assert code == 0 and "was not written" in obj["import_record_error"]


def test_bl43_json_carries_companions_and_a_non_bl43_card_carries_null(tmp_path, capsys):
    from intake_fixtures import bl43_workspace
    ws, data = bl43_workspace(tmp_path)
    code = ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=True))
    obj = json.loads(capsys.readouterr().out.strip())
    assert code == 0 and set(obj["companions"]) == {"sidecar", "gpi", "curves"}
    assert len(obj["companions"]["sidecar"]["missing"]) == 2      # the fixture writes no .var
    (tmp_path / "s30").mkdir()
    s30, s30_ws = _init_ws(tmp_path / "s30")
    capsys.readouterr()
    ingest_cmd.run(argparse.Namespace(data=[str(s30)], workspace=str(s30_ws), json=True))
    assert json.loads(capsys.readouterr().out.strip())["companions"] is None

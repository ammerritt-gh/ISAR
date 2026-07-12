"""`isar ingest` CLI — exit codes, --json shape, no-workspace error JSON (Phase B).

authored-not-run: no `isar` env on the build machine (AGENTS.md). py_compile only.
"""
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


def test_ingest_no_workspace_json_error(tmp_path, capsys):
    empty = tmp_path / "not_a_ws"
    empty.mkdir()
    code = ingest_cmd.run(argparse.Namespace(data=["x"], workspace=str(empty), json=True))
    out = capsys.readouterr()
    assert code == 2
    obj = json.loads(out.out.strip())
    assert obj["error"]["kind"] == "no_workspace"
    assert "no workspace" in out.err


def test_ingest_no_workspace_human(tmp_path, capsys):
    empty = tmp_path / "not_a_ws"
    empty.mkdir()
    code = ingest_cmd.run(argparse.Namespace(data=["x"], workspace=str(empty), json=False))
    out = capsys.readouterr()
    assert code == 2
    assert out.out == ""                         # no JSON on stdout in human mode
    assert "no workspace" in out.err

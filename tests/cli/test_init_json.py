"""`isar init --json` — success shape and all four exit-2 error kinds (Phase B).

The stderr line and exit code are unchanged from the non-JSON path; --json only adds the machine
object on stdout. authored-not-run (no `isar` env on the build machine).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from isar.cli import init_cmd
from isar.parsers import spec_s30
from isar.synth_format import run_builder


def _emit_s30(path: Path):
    card = spec_s30.card_template()
    path.write_bytes(spec_s30.emit_file(run_builder.build_s30_run("SYN", n_scans=1), card))


def _run(data, out, capsys, as_json=True):
    code = init_cmd.run(argparse.Namespace(data=str(data), out=(str(out) if out else None), json=as_json))
    return code, capsys.readouterr()


def test_init_json_success(tmp_path, capsys):
    data = tmp_path / "run.spec_supplementary.dat"
    _emit_s30(data)
    ws = tmp_path / "ws"
    code, out = _run(data, ws, capsys)
    assert code == 0
    obj = json.loads(out.out.strip())
    assert obj["workspace"] == str(ws)
    assert obj["facility"] and obj["instrument"] and obj["fmt"] and obj["plugin"]


def test_init_json_bad_path(tmp_path, capsys):
    code, out = _run(tmp_path / "nope", tmp_path / "ws", capsys)
    assert code == 2
    assert json.loads(out.out.strip())["error"]["kind"] == "bad_path"
    assert "no such path" in out.err


def test_init_json_no_recognizable_data(tmp_path, capsys):
    (tmp_path / "readme.txt").write_text("nothing", encoding="utf-8")
    code, out = _run(tmp_path, tmp_path / "ws", capsys)
    assert code == 2
    assert json.loads(out.out.strip())["error"]["kind"] == "no_recognizable_data"
    assert "could not detect" in out.err


def test_init_json_no_card_template(tmp_path, capsys, monkeypatch):
    data = tmp_path / "run.spec_supplementary.dat"
    _emit_s30(data)
    # Force the "detected but no shipped template" branch.
    monkeypatch.setattr("isar.parsers.registry.default_plugin_name", lambda *a, **k: None)
    code, out = _run(data, tmp_path / "ws", capsys)
    assert code == 2
    assert json.loads(out.out.strip())["error"]["kind"] == "no_card_template"
    assert "no shipped card template" in out.err


def test_init_json_card_exists(tmp_path, capsys):
    data = tmp_path / "run.spec_supplementary.dat"
    _emit_s30(data)
    ws = tmp_path / "ws"
    code1, _ = _run(data, ws, capsys)
    assert code1 == 0
    code2, out = _run(data, ws, capsys)
    assert code2 == 2
    assert json.loads(out.out.strip())["error"]["kind"] == "card_exists"
    assert "already has a card" in out.err

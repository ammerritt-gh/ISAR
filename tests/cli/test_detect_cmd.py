"""`isar detect` — JSON + human output, and the not-found path (retained for a planned GUI Files-dock detect-preview)."""
from __future__ import annotations

import argparse
import json

from isar.cli import detect_cmd


def _run(data, as_json, capsys):
    code = detect_cmd.run(argparse.Namespace(data=str(data), json=as_json))
    out = capsys.readouterr()
    return code, out


def test_detect_missing_path_json_is_empty_object(tmp_path, capsys):
    code, out = _run(tmp_path / "nope", True, capsys)
    assert code == 2
    assert json.loads(out.out.strip()) == {}


def test_detect_unrecognized_json_is_empty_object(tmp_path, capsys):
    (tmp_path / "readme.txt").write_text("nothing", encoding="utf-8")
    code, out = _run(tmp_path, True, capsys)
    assert code == 2
    assert json.loads(out.out.strip()) == {}


def test_detect_human_missing_path(tmp_path, capsys):
    code, out = _run(tmp_path / "nope", False, capsys)
    assert code == 2
    assert "no such path" in out.err

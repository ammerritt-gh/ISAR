"""``isar report --scene`` — the offline rebuild of a scene from persisted state (SCENE-3 PR 3c).

Lives beside the adapter tests because it reuses their stamped-workspace fixtures: the flag's
whole job is to run the same adapter over a workspace nobody is driving. The report-writing seam
is faked out, so what is exercised here is the scene gate and nothing else.
"""
import types

import pytest

from isar.cli import main as main_mod
from isar.cli import report_cmd
from isar.scene import store

from .test_workspace_api_scene import COLUMNS, _candidate, _row
from synthetic_ws import stamp_ws


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A stamped workspace with one scoreable arena, and no real report rendering."""
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    stamp_ws(root, rows=[_row(1, candidates=[_candidate("phonon+bg")])], columns=COLUMNS)
    monkeypatch.setattr(report_cmd.run_report, "write_run_report", lambda *a, **k: {})
    return root


def _args(root, **kw):
    return types.SimpleNamespace(workspace=str(root), formats="markdown", **kw)


def test_the_flag_is_off_by_default_and_writes_no_scene(ws, capsys):
    """Without the flag, ``isar report`` is exactly the command it was: nothing under
    ``report/scene/`` is created, so a workspace never acquires a scene by accident."""
    assert main_mod.build_parser().parse_args(["report", "--workspace", "w"]).scene is False
    assert report_cmd.run(_args(ws, scene=False)) == 0
    assert not (ws / "report" / "scene").exists()
    assert "scene:" not in capsys.readouterr().out


def test_the_flag_publishes_a_verifiable_revision_and_says_where(ws, capsys):
    assert report_cmd.run(_args(ws, scene=True)) == 0
    pointer, payload = store.read_current(ws / "report")
    assert [o["scan_ref"] for o in payload["observations"]] == ["tavi:1:det"]
    out = capsys.readouterr().out
    assert f"scene: {ws / 'report' / 'scene' / (pointer['digest'] + '.json')}" in out
    assert f"({pointer['digest'][:12]}, new)" in out

    # Re-running rebuilds the same revision: same digest, pointer unmoved, and the operator is
    # told it was unchanged rather than being shown a second identical "new" scene.
    assert report_cmd.run(_args(ws, scene=True)) == 0
    assert "unchanged)" in capsys.readouterr().out
    assert store.read_current(ws / "report")[0] == pointer


def test_a_workspace_the_adapter_refuses_fails_visibly(ws, capsys):
    """H1: the operator asked for a scene by name, so a refusal is an error with the re-deliver
    message on stderr and a nonzero exit — never a silent empty scene that reads as "this
    campaign measured nothing". The report itself still wrote."""
    columns = [c for c in COLUMNS if c != "candidates"]
    stamp_ws(ws, rows=[_row(1, candidates=[_candidate("phonon+bg")])], columns=columns)

    assert report_cmd.run(_args(ws, scene=True)) == 3
    captured = capsys.readouterr()
    assert "re-deliver" in captured.err
    # No revision was published — and the refusal is recorded where a later reader will see it.
    assert list((ws / "report" / "scene").glob("*.json")) == \
        [ws / "report" / "scene" / "last_attempt.json"]
    attempt = store.read_last_attempt(ws / "report")
    assert attempt["status"] == "failed" and "re-deliver" in attempt["reason"]

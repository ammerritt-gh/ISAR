"""``isar scene-build`` — the lean build the campaign parent supervises (SCENE-4.3 C5, D1).

Beside ``test_cli_report_scene.py`` because it reuses the same stamped-workspace fixtures: this
command is the SAME canonical ``scene_hook.build_and_store``, with a machine-readable surface
instead of an operator-readable one. What is exercised here is the report line and the exit
vocabulary the parent branches on — and, once, the real subprocess, so the fake seam
``tests/drive/test_program_runner.py`` uses cannot drift away from the child it stands in for.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

from isar.cli import main as main_mod
from isar.cli import scene_build_cmd
from isar.drive.program_runner import SceneBuildFailed, build_scene_revision
from isar.scene import store

from .test_workspace_api_scene import COLUMNS, _candidate, _row, _write_measurements
from synthetic_ws import stamp_ws


@pytest.fixture
def ws(tmp_path):
    """A stamped workspace with one scoreable arena — the same fixture the report-CLI test uses."""
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    rows = [_row(1, candidates=[_candidate("phonon+bg")])]
    stamp_ws(root, rows=rows, columns=COLUMNS)
    _write_measurements(root, rows)
    return root


def _args(root, source="campaign"):
    return types.SimpleNamespace(workspace=str(root), source=source)


def _report(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one report line, got {lines!r}"
    return json.loads(lines[0])


# ---------------------------------------------------------------------------- the contract
def test_a_successful_build_prints_one_json_line_naming_the_digest_it_flipped_to(ws, capsys):
    assert scene_build_cmd.run(_args(ws)) == 0
    report = _report(capsys)

    assert report["schema"] == scene_build_cmd.REPORT_SCHEMA
    assert report["pointer_flipped"] is True and report["unchanged"] is False
    pointer, _payload = store.read_current(ws / "report")
    assert report["digest"] == pointer["digest"]


def test_a_rebuild_of_an_unchanged_workspace_reports_unchanged_and_flips_nothing(ws, capsys):
    assert scene_build_cmd.run(_args(ws)) == 0
    first = _report(capsys)
    assert scene_build_cmd.run(_args(ws)) == 0
    second = _report(capsys)

    assert second["digest"] == first["digest"]
    assert second["unchanged"] is True and second["pointer_flipped"] is False


def test_a_workspace_the_adapter_refuses_exits_3_with_no_report_line(ws, capsys):
    """Exit 3 means "there is NO durable revision" — the parent classifies it as a controller
    fault and must never fall back onto whatever the pointer currently names."""
    columns = [c for c in COLUMNS if c != "candidates"]
    stamp_ws(ws, rows=[_row(1, candidates=[_candidate("phonon+bg")])], columns=columns)

    assert scene_build_cmd.run(_args(ws)) == 3
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "re-deliver" in captured.err


def test_a_lost_pointer_flip_is_reported_as_a_durable_digest_and_exits_zero(ws, capsys,
                                                                           monkeypatch):
    """The ``ConcurrentSceneWrite`` window. ``write_revision`` writes the content-addressed blob
    BEFORE it takes the pointer lock, so a racing publisher that wins the CAS leaves this build's
    revision durable and addressable. Reporting that as a failure would let a read-only
    ``isar report --scene`` stop a live campaign — the false machine failure the four-reasons stop
    law forbids."""
    from isar.session import scene_hook

    real = store.RevisionStore.write_revision
    seen: dict[str, str] = {}

    def racing_write(self, report_dir, revision, *, expected_parent=store.UNSET, source="driver"):
        # Publish a DIFFERENT revision first, so this writer's expected_parent no longer holds.
        if self.artifact_label == "scene" and "rival" not in seen:
            seen["rival"] = "done"
            real(self, report_dir, _Rival(), source="rival")
        seen["digest"] = revision.digest
        return real(self, report_dir, revision, expected_parent=expected_parent, source=source)

    class _Rival:
        canonical = b'{"observations":[]}'
        digest = "0" * 64

    monkeypatch.setattr(store.RevisionStore, "write_revision", racing_write)
    assert scene_build_cmd.run(_args(ws)) == 0
    report = _report(capsys)

    assert report["digest"] == seen["digest"]
    assert report["pointer_flipped"] is False and report["unchanged"] is False
    # The blob really is durable at the reported address, which is the whole claim.
    assert store.read_revision(ws / "report", report["digest"])
    # …and the pointer still names the rival, which is exactly why the pin, not the pointer, is
    # what the campaign steers from.
    assert store.pointer_digest(ws / "report") == "0" * 64
    assert scene_hook.build_and_store is not None      # the one canonical operation, unchanged


def test_the_command_is_registered_and_takes_a_workspace():
    parsed = main_mod.build_parser().parse_args(["scene-build", "--workspace", "w"])
    assert parsed.command == "scene-build" and parsed.workspace == "w"
    assert parsed.source == "campaign"


# ---------------------------------------------------------------------------- the real child
def test_the_supervised_subprocess_seam_talks_to_the_real_child(ws):
    """The one REAL spawn. Everything else fakes the seam for speed, so this is what pins the
    fake to the child's actual contract: a digest to pin, the pointer state, and exit 0."""
    report = build_scene_revision(ws, source="campaign")

    pointer, _payload = store.read_current(ws / "report")
    assert report.digest == pointer["digest"]
    assert report.pointer_flipped is True and report.unchanged is False
    assert report.seconds > 0.0

    # A second call over the same workspace is the idempotent path, and it still yields a pin.
    again = build_scene_revision(ws, source="campaign")
    assert again.digest == report.digest and again.unchanged is True


def test_a_failing_child_becomes_a_named_machine_failure_never_a_stale_pin(tmp_path):
    """No workspace at all: the child exits nonzero, and the seam raises rather than handing back
    a digest nobody built."""
    with pytest.raises(SceneBuildFailed) as excinfo:
        build_scene_revision(tmp_path / "nowhere")
    assert "scene build exited" in str(excinfo.value)


def test_the_child_prints_its_report_on_stdout_and_nothing_else(ws):
    """The parent parses the LAST stdout line; this pins that the child writes exactly one."""
    completed = subprocess.run(
        [sys.executable, "-m", "isar.cli.main", "scene-build", "--workspace", str(ws)],
        capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["schema"] == scene_build_cmd.REPORT_SCHEMA

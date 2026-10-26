"""Run / Re-run wiring: enablement, command assembly, remembered state, finish status.

The real ``isar run`` subprocess is exercised in the end-to-end walkthrough; here ProcessRunner is
faked so the wiring is tested without launching the engine.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import isar_gui.operation_coordinator as operations
from isar_gui.process import ProcessRunner
from isar.workspace_api import Workspace
from isar_gui.commands import RunOptions
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


class _FakeRunner(ProcessRunner):
    """A ProcessRunner that records the argv but never launches a subprocess."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started_cmd = None
        self.started_log = None

    def start(self, argv, *, cwd=None, log_path=None, env=None):
        self.started_cmd = argv
        self.started_log = log_path
        self.started_env = env

    def is_running(self):
        return False


def _win(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    return w, root


def test_run_actions_disabled_when_empty(qtbot):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert not w._run_action.isEnabled()
    assert not w._rerun_action.isEnabled()


def test_run_enabled_but_rerun_needs_history(qtbot, make_ws):
    w, _ = _win(qtbot, make_ws)
    assert w._run_action.isEnabled()
    assert not w._rerun_action.isEnabled()  # nothing remembered yet


def test_launch_builds_command_remembers_and_sets_status(qtbot, make_ws, monkeypatch):
    w, root = _win(qtbot, make_ws)
    monkeypatch.setattr(operations, "ProcessRunner", _FakeRunner)
    opts = RunOptions(errors="full", M=200)
    w._launch_run([r"D:\data"], opts)

    cmd = w._operations._run_runner.started_cmd
    assert cmd[1:4] == ["-m", "isar", "run"]
    assert r"D:\data" in cmd
    assert cmd[cmd.index("--workspace") + 1] == str(root)
    assert cmd[cmd.index("--errors") + 1] == "full"  # non-default tier propagated
    # The invocation is remembered, but Re-run remains disabled until this single-writer run ends.
    assert w._run_state() == {"data": [r"D:\data"], "opts": {"llm": False, "force": False,
                                                             "M": 200, "errors": "full",
                                                             "detectors": None}}
    assert not w._rerun_action.isEnabled()
    assert "running" in w._run_status.text()
    # The retained-log path lands under the workspace report dir.
    assert "gui-runs" in w._operations._run_runner.started_log
    # The LLM backend settings ride along as env overrides (default profile = llmm).
    assert w._operations._run_runner.started_env["ISAR_LLM_PROFILE"] == "llmm"


def test_rerun_replays_remembered_invocation(qtbot, make_ws, monkeypatch):
    w, root = _win(qtbot, make_ws)
    captured = []
    monkeypatch.setattr(w, "_launch_run", lambda data, opts: captured.append((data, opts)))
    w._remember_run([r"D:\data"], RunOptions(errors="bayes"))
    w._sync_run_actions()
    w.rerun()
    assert captured == [([r"D:\data"], RunOptions(errors="bayes"))]


def test_finish_status_success_and_failure(qtbot, make_ws):
    w, _ = _win(qtbot, make_ws)
    w._on_run_finished(0, True)
    assert "OK" in w._run_status.text()
    assert "2e7d32" in w._run_status.styleSheet()
    w._on_run_finished(2, True)
    assert "FAILED" in w._run_status.text()
    assert "c62828" in w._run_status.styleSheet()

"""ProcessRunner: run a real subprocess, stream its lines, retain a log, report exit state."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from isar_gui.process import ProcessRunner


def test_runs_and_streams_lines_and_finishes_zero(qtbot, tmp_path):
    runner = ProcessRunner()
    lines: list[str] = []
    runner.line.connect(lines.append)
    log_path = tmp_path / "run.log"

    with qtbot.waitSignal(runner.finished, timeout=15000) as blocker:
        runner.start([sys.executable, "-c", "print('hello'); print('world')"], log_path=str(log_path))
    assert blocker.args == [0]
    assert not runner.is_running()
    joined = "\n".join(lines)
    assert "hello" in joined and "world" in joined
    # The retained log captured the same output plus a command header.
    text = log_path.read_text(encoding="utf-8")
    assert "hello" in text and "world" in text
    assert "-c" in text  # the command line was recorded in the header


def test_nonzero_exit_is_reported(qtbot, tmp_path):
    runner = ProcessRunner()
    with qtbot.waitSignal(runner.finished, timeout=15000) as blocker:
        runner.start([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert blocker.args == [3]


def test_failure_to_start_reports_nonzero_and_a_line(qtbot):
    runner = ProcessRunner()
    lines: list[str] = []
    runner.line.connect(lines.append)
    with qtbot.waitSignal(runner.finished, timeout=15000) as blocker:
        runner.start(["this_executable_does_not_exist_isar_xyz"])
    assert blocker.args != [0]                 # a non-zero (failure) code
    assert any("could not" in ln.lower() or "fail" in ln.lower() for ln in lines)


def test_is_running_true_during_and_stop_kills(qtbot):
    runner = ProcessRunner()
    runner.start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert runner.is_running()
    with qtbot.waitSignal(runner.finished, timeout=15000):
        runner.stop()
    assert not runner.is_running()


def test_stderr_is_merged_into_the_stream(qtbot):
    runner = ProcessRunner()
    lines: list[str] = []
    runner.line.connect(lines.append)
    with qtbot.waitSignal(runner.finished, timeout=15000):
        runner.start([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n')"])
    assert any("boom" in ln for ln in lines)

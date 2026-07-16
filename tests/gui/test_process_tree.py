"""Real-process tests for ProcessRunner's Job-Object tree lifetime (win32 only).

These spawn a genuine python parent that spawns a genuine sleeper child, then assert the whole
tree dies on every stop path: explicit ``kill_tree``, runner destruction, job-assignment failure
(taskkill fallback), and a root that exits leaving its child behind. Liveness is probed with
``tasklist`` (no psutil dependency); every wait is bounded and a ``finally`` sweep taskkills any
leftover pid so a failing assertion can never leak a 2-minute sleeper.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from isar_gui.process import ProcessRunner, _WinJob

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")

# The parent prints its own pid and its child's pid on one line, then either parks ("stay") or
# returns immediately ("exit"), leaving the child running.
_PARENT = """\
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(f"PIDS {os.getpid()} {child.pid}", flush=True)
if sys.argv[1] == "stay":
    time.sleep(120)
"""


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, check=False)
    return str(pid) in out.stdout


def _sweep(pids) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)


def _assert_dead(qtbot, pids, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return
        qtbot.wait(250)
    raise AssertionError(f"processes still alive: {[p for p in pids if _alive(p)]}")


@pytest.fixture
def parent_script(tmp_path):
    p = tmp_path / "parent.py"
    p.write_text(_PARENT, encoding="utf-8")
    return p


def _launch(qtbot, script, mode: str):
    runner = ProcessRunner()
    pids: list[int] = []
    runner.line.connect(lambda ln: pids.extend(int(x) for x in ln.split()[1:])
                        if ln.startswith("PIDS ") else None)
    runner.start([sys.executable, "-u", str(script), mode])
    qtbot.waitUntil(lambda: len(pids) == 2, timeout=15000)
    return runner, pids


def test_normal_completion_no_leak(qtbot):
    runner = ProcessRunner()
    with qtbot.waitSignal(runner.finished, timeout=15000) as blocker:
        runner.start([sys.executable, "-c", "print('ok')"])
    assert blocker.args == [0]
    assert not runner.is_running()


def test_kill_tree_kills_parent_and_child(qtbot, parent_script):
    runner, pids = _launch(qtbot, parent_script, "stay")
    try:
        assert all(_alive(p) for p in pids)
        with qtbot.waitSignal(runner.finished, timeout=15000):
            runner.kill_tree()
        _assert_dead(qtbot, pids)
    finally:
        _sweep(pids)


def test_runner_destruction_reaps_tree(qtbot, parent_script):
    runner, pids = _launch(qtbot, parent_script, "stay")
    try:
        # deleteLater → destroyed → the job handle closes → kill-on-close reaps the whole tree.
        # (Qt prints a "Destroyed while process is still running" warning here — expected: this
        # test exercises the no-cleanup teardown path on purpose.)
        runner.deleteLater()
        _assert_dead(qtbot, pids)
    finally:
        _sweep(pids)


def test_assignment_failure_falls_back_to_taskkill(qtbot, parent_script, monkeypatch):
    monkeypatch.setattr(_WinJob, "create", classmethod(lambda cls: None))
    runner, pids = _launch(qtbot, parent_script, "stay")
    try:
        assert runner._job is None   # the job path is really off; kill_tree must use taskkill
        with qtbot.waitSignal(runner.finished, timeout=15000):
            runner.kill_tree()
        _assert_dead(qtbot, pids)
    finally:
        _sweep(pids)


def test_root_exit_reaps_surviving_child(qtbot, parent_script):
    runner, pids = _launch(qtbot, parent_script, "exit")
    try:
        with qtbot.waitSignal(runner.finished, timeout=15000):
            pass   # the parent returns on its own right after printing
        _assert_dead(qtbot, pids)   # child must not survive the root's normal exit
    finally:
        _sweep(pids)

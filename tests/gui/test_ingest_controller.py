"""IngestController tests — FIFO serialization + bootstrap sequencing + failure path (D2).

A fake :class:`ProcessRunner` (monkeypatched into the controller module) records launched argvs and
lets the test drive completion; a fake WorkspaceManager records state transitions. No real
subprocess, no filesystem — just the controller's scheduling logic.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from isar_gui import ingest_controller
from isar_gui.ingest_controller import IngestController


class _FakeSignal:
    def __init__(self):
        self._cbs = []

    def connect(self, cb):
        self._cbs.append(cb)

    def disconnect(self):
        self._cbs.clear()

    def emit(self, *args):
        for cb in list(self._cbs):
            cb(*args)


class _FakeRunner:
    """Stand-in for ProcessRunner: records argv, exposes line/finished signals to drive by hand."""

    instances: list = []

    def __init__(self, parent=None):
        self.line = _FakeSignal()
        self.finished = _FakeSignal()
        self.argv = None
        self.log_path = None
        _FakeRunner.instances.append(self)

    def start(self, argv, log_path=None):
        self.argv = argv
        self.log_path = log_path

    def deleteLater(self):
        pass

    # convenience for the test
    def complete(self, code, lines=()):
        for ln in lines:
            self.line.emit(ln)
        self.finished.emit(code)


class _FakeManager:
    def __init__(self, state="ready", root=r"D:\ws"):
        self._state = state
        self._root = root
        self.opened = []
        self.new_temp_calls = 0

    def state(self):
        return self._state

    def root(self):
        return self._root

    def new_temp(self):
        self.new_temp_calls += 1
        self._state = "pending"
        self._root = r"D:\tmp\ws"
        return self._root

    def open(self, root):
        self.opened.append(root)
        self._state = "ready"
        return True


def _success_line(added=(), updated=()):
    return json.dumps({
        "workspace": r"D:\ws", "files": [],
        "scans": {"added": list(added), "updated": list(updated), "unchanged": []},
        "deduped": [], "gaps": []})


@pytest.fixture
def patched_runner(monkeypatch):
    _FakeRunner.instances = []
    monkeypatch.setattr(ingest_controller, "ProcessRunner", _FakeRunner)
    return _FakeRunner


def test_fifo_serializes_concurrent_requests(qtbot, patched_runner):
    """A request arriving while one is running is queued, not run in parallel; it starts only after
    the first completes — two isar processes must never race on session.json."""
    mgr = _FakeManager(state="ready")
    started, finished = [], []
    ctrl = IngestController("py", mgr, log_sink=lambda _m: None)
    ctrl.ingest_started.connect(lambda p: started.append(p))
    ctrl.ingest_finished.connect(lambda r: finished.append(r))

    ctrl.request_ingest(["a"])
    ctrl.request_ingest(["b"])            # arrives mid-run
    assert len(patched_runner.instances) == 1   # only ONE process running
    assert started == [["a"]]

    patched_runner.instances[0].complete(0, [_success_line(added=["exp:1"])])
    # First finished -> report emitted -> second batch dequeued and started.
    assert len(finished) == 1
    assert len(patched_runner.instances) == 2
    assert "b" in patched_runner.instances[1].argv
    assert started == [["a"], ["b"]]


def test_bootstrap_runs_init_then_ingest_then_open(qtbot, patched_runner):
    """From the pending state a request runs `isar init` (first path) then `isar ingest` (all paths),
    then opens the scaffolded workspace — strictly in that order."""
    mgr = _FakeManager(state="pending", root=r"D:\tmp\ws")
    ctrl = IngestController("py", mgr, log_sink=lambda _m: None)

    ctrl.request_ingest(["d1", "d2"])
    assert "init" in patched_runner.instances[0].argv
    assert "d1" in patched_runner.instances[0].argv
    assert mgr.opened == []                       # not opened before ingest

    patched_runner.instances[0].complete(0)       # init OK
    assert "ingest" in patched_runner.instances[1].argv
    assert "d1" in patched_runner.instances[1].argv and "d2" in patched_runner.instances[1].argv
    assert mgr.opened == []                        # still not opened until ingest done

    patched_runner.instances[1].complete(0, [_success_line(added=["exp:1"])])
    assert mgr.opened == [r"D:\tmp\ws"]             # opened exactly once, after ingest


def test_none_state_reserves_temp_first(qtbot, patched_runner):
    """A request with no workspace bound reserves a temp workspace (new_temp) then bootstraps it."""
    mgr = _FakeManager(state="none", root=None)
    ctrl = IngestController("py", mgr, log_sink=lambda _m: None)

    ctrl.request_ingest(["d1"])
    assert mgr.new_temp_calls == 1
    assert "init" in patched_runner.instances[0].argv


def test_failure_emits_ingest_failed_with_json_cause(qtbot, patched_runner):
    """An ingest exiting non-zero surfaces the engine's JSON error message via ingest_failed."""
    mgr = _FakeManager(state="ready")
    failures = []
    ctrl = IngestController("py", mgr, log_sink=lambda _m: None)
    ctrl.ingest_failed.connect(lambda code, cause: failures.append((code, cause)))

    ctrl.request_ingest(["x"])
    err = json.dumps({"error": {"kind": "no_workspace", "message": "run init first"}})
    patched_runner.instances[0].complete(2, [err])
    assert failures == [(2, "no_workspace: run init first")]


def test_bootstrap_init_failure_does_not_open(qtbot, patched_runner):
    """If bootstrap `isar init` fails, the controller reports failure and never opens/ingests —
    the pending workspace is left intact for a retry."""
    mgr = _FakeManager(state="pending", root=r"D:\tmp\ws")
    failures = []
    ctrl = IngestController("py", mgr, log_sink=lambda _m: None)
    ctrl.ingest_failed.connect(lambda code, cause: failures.append((code, cause)))

    ctrl.request_ingest(["d1"])
    patched_runner.instances[0].complete(2, ["error: bad card"])
    assert len(patched_runner.instances) == 1      # no ingest step launched
    assert mgr.opened == []
    assert failures and failures[0][0] == 2

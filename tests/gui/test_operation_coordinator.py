"""WorkspaceOperationCoordinator tests — one writer across ingest and analysis."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar_gui import operation_coordinator
from isar_gui.commands import RunOptions
from isar_gui.operation_coordinator import WorkspaceOperationCoordinator


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def disconnect(self, callback=None):
        if callback is None:
            self._callbacks.clear()
        else:
            self._callbacks.remove(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


class _Manager:
    def __init__(self):
        self._root = r"D:\ws"
        self._generation = 7

    def root(self):
        return self._root

    def generation(self):
        return self._generation

    def state(self):
        return "ready"


class _Ingest:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.ingest_started = _Signal()
        self.ingest_finished = _Signal()
        self.ingest_failed = _Signal()
        self.job_finished = _Signal()
        self.bootstrap_bound = _Signal()
        self.started = []
        _Ingest.instances.append(self)

    def start_ingest(self, paths):
        self.started.append(list(paths))
        self.ingest_started.emit(list(paths))

    def runner(self):
        return None

    def complete(self, report=None):
        self.ingest_finished.emit(report if report is not None else object())
        self.job_finished.emit()

    def bootstrap(self, root, generation):
        self.bootstrap_bound.emit(root, generation)


class _Runner:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.line = _Signal()
        self.finished = _Signal()
        self.argv = None
        self.running = False
        self.terminated = False
        self.killed = False
        _Runner.instances.append(self)

    def start(self, argv, **_kwargs):
        self.argv = argv
        self.running = True

    def is_running(self):
        return self.running

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def deleteLater(self):
        pass

    def complete(self, code=0):
        self.running = False
        self.finished.emit(code)


@pytest.fixture
def coordinator(monkeypatch):
    _Ingest.instances = []
    _Runner.instances = []
    monkeypatch.setattr(operation_coordinator, "IngestController", _Ingest)
    monkeypatch.setattr(operation_coordinator, "ProcessRunner", _Runner)
    return WorkspaceOperationCoordinator("python", _Manager(), lambda _line: None)


def test_ingest_batches_drain_before_queued_run(qtbot, coordinator):
    """A run waits for every previously queued ingest, preventing concurrent session writers."""
    coordinator.request_ingest(["first.dat"])
    coordinator.request_ingest(["second.dat"])
    coordinator.request_run(["analysis.dat"], RunOptions())
    ingest = _Ingest.instances[0]
    assert ingest.started == [["first.dat"]]

    ingest.complete()
    qtbot.waitUntil(lambda: ingest.started == [["first.dat"], ["second.dat"]])
    ingest.complete()
    qtbot.waitUntil(lambda: len(_Runner.instances) == 1)
    assert "run" in _Runner.instances[0].argv
    assert coordinator.is_running_analysis()


def test_stale_ingest_completion_never_reaches_the_new_workspace(qtbot, coordinator):
    """A root/generation change suppresses completion effects rather than rebinding their result."""
    reports = []
    coordinator.ingest_finished.connect(reports.append)
    coordinator.request_ingest(["first.dat"])
    manager = coordinator._wsm
    manager._root = r"D:\other"
    manager._generation += 1

    _Ingest.instances[0].complete()
    qtbot.waitUntil(lambda: not coordinator.is_busy())
    assert reports == []


def test_none_bootstrap_adopts_its_own_generation_and_reports_success(qtbot, coordinator):
    """none -> temp -> ready is a known controller transition, not a stale workspace switch."""
    manager = coordinator._wsm
    manager._root = None
    manager._generation = 0
    reports = []
    coordinator.ingest_finished.connect(reports.append)
    coordinator.request_ingest(["first.dat"])
    ingest = _Ingest.instances[0]

    manager._root = r"D:\tmp\ws"
    manager._generation = 1
    ingest.bootstrap(manager._root, manager._generation)  # controller's new_temp()
    manager._generation = 2
    ingest.bootstrap(manager._root, manager._generation)  # controller's open(root)
    report = object()
    ingest.complete(report)

    qtbot.waitUntil(lambda: not coordinator.is_busy())
    assert reports == [report]


def test_bootstrap_rebases_multiple_queued_ingests(qtbot, coordinator):
    """Drops queued while a new temporary workspace bootstraps stay on that workspace."""
    manager = coordinator._wsm
    manager._root = None
    manager._generation = 0
    coordinator.request_ingest(["first.dat"])
    coordinator.request_ingest(["second.dat"])
    coordinator.request_ingest(["third.dat"])
    ingest = _Ingest.instances[0]

    manager._root = r"D:\tmp\ws"
    manager._generation = 1
    ingest.bootstrap(manager._root, manager._generation)
    manager._generation = 2
    ingest.bootstrap(manager._root, manager._generation)
    ingest.complete()
    qtbot.waitUntil(lambda: ingest.started == [["first.dat"], ["second.dat"]])
    ingest.complete()
    qtbot.waitUntil(lambda: ingest.started == [["first.dat"], ["second.dat"], ["third.dat"]])
    ingest.complete()
    qtbot.waitUntil(lambda: not coordinator.is_busy())


def test_pending_bootstrap_adopts_open_generation(qtbot, coordinator):
    """An operator-created pending temp workspace also survives its controller open() generation."""
    manager = coordinator._wsm
    reports = []
    coordinator.ingest_finished.connect(reports.append)
    coordinator.request_ingest(["first.dat"])
    manager._generation += 1
    ingest = _Ingest.instances[0]
    ingest.bootstrap(manager._root, manager._generation)
    report = object()
    ingest.complete(report)
    qtbot.waitUntil(lambda: not coordinator.is_busy())
    assert reports == [report]


def test_cancel_requests_graceful_run_termination(coordinator):
    coordinator.request_run(["analysis.dat"], RunOptions())
    runner = _Runner.instances[0]
    assert coordinator.cancel() is True
    assert runner.terminated is True
    assert coordinator.queued_count() == 0

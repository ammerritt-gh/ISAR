"""WorkspaceOperationCoordinator tests — one writer across ingest and analysis."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar_gui import operation_coordinator
import json

from isar_gui.commands import DriveOptions, RunOptions
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
        self.tree_killed = False
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

    def kill_tree(self):
        self.tree_killed = True

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


# ---- campaigns (drive job kind) ----------------------------------------------
def _drive_opts(**kw):
    return DriveOptions(goal="map the acoustic branch", **kw)


def test_request_drive_stamps_token_and_starts(coordinator):
    assert coordinator.request_drive(_drive_opts()) is True
    runner = _Runner.instances[0]
    assert "drive" in runner.argv
    i = runner.argv.index("--control-token")
    token = runner.argv[i + 1]
    assert len(token) == 32          # uuid4().hex, minted per invocation
    assert coordinator.is_running_campaign()


def test_request_drive_allowed_in_pending_refused_in_none(coordinator):
    # A pending (reserved, card-less) root is a first-class campaign target — the drive engine
    # initializes the workspace itself. Only "none" (no root at all) refuses.
    coordinator._wsm.state = lambda: "none"
    coordinator._wsm.root = lambda: None
    assert coordinator.request_drive(_drive_opts()) is False
    assert not coordinator.is_busy()

    coordinator._wsm.state = lambda: "pending"
    coordinator._wsm.root = lambda: r"D:\ws"
    assert coordinator.request_drive(_drive_opts()) is True
    assert coordinator.is_running_campaign()


def test_adopt_drive_binding_rebases_the_active_campaign(qtbot, coordinator):
    # The pending->ready promotion of the campaign's OWN workspace bumps the generation; adoption
    # rebases the running job so its completion still classifies as live. The payload (control
    # token included) is untouched.
    coordinator._wsm.state = lambda: "pending"
    assert coordinator.request_drive(_drive_opts())
    token = coordinator._active.payload[0].control_token
    finishes = []
    coordinator.drive_finished.connect(lambda code, live: finishes.append((code, live)))

    coordinator._wsm._generation = 8            # the manager's open() bumped the generation
    coordinator._wsm.state = lambda: "ready"
    assert coordinator.adopt_drive_binding(r"D:\ws", 8) is True
    assert coordinator._active.generation == 8
    assert coordinator._active.payload[0].control_token == token

    _Runner.instances[0].complete(0)
    assert finishes == [(0, True)]              # NOT stale after the promotion


def test_adopt_drive_binding_normalizes_windows_paths(coordinator):
    import sys

    coordinator._wsm.state = lambda: "pending"
    assert coordinator.request_drive(_drive_opts())
    coordinator._wsm._generation = 8
    variant = "d:/WS" if sys.platform == "win32" else r"D:\ws"
    assert coordinator.adopt_drive_binding(variant, 8) is True


def test_adopt_drive_binding_refusals(coordinator):
    # No active campaign at all.
    assert coordinator.adopt_drive_binding(r"D:\ws", 8) is False
    # Wrong root: the active campaign runs elsewhere.
    coordinator._wsm.state = lambda: "pending"
    assert coordinator.request_drive(_drive_opts())
    assert coordinator.adopt_drive_binding(r"D:\other", 8) is False
    # Stale generation: the manager moved on again after the promotion.
    coordinator._wsm._generation = 9
    assert coordinator.adopt_drive_binding(r"D:\ws", 8) is False
    # A refused adoption leaves the job on its original generation -> completion will be stale.
    assert coordinator._active.generation == 7


def test_drive_gates_run_and_ingest_until_finished(qtbot, coordinator):
    coordinator.request_drive(_drive_opts())
    coordinator.request_ingest(["late.dat"])
    coordinator.request_run(["analysis.dat"], RunOptions())
    assert coordinator.queued_count() == 2
    ingest = _Ingest.instances[0]
    assert ingest.started == []                       # everything waits behind the campaign

    _Runner.instances[0].complete(0)
    qtbot.waitUntil(lambda: ingest.started == [["late.dat"]])


def test_drive_stop_writes_control_file_to_captured_root(qtbot, coordinator, tmp_path):
    coordinator._wsm._root = str(tmp_path)
    coordinator.request_drive(_drive_opts())
    coordinator.request_ingest(["queued.dat"])
    runner = _Runner.instances[0]
    token = runner.argv[runner.argv.index("--control-token") + 1]

    # A rebind between start and stop must not redirect the stop request.
    coordinator._wsm._root = r"D:\other"

    assert coordinator.request_drive_stop() is True
    marker = tmp_path / "report" / "control" / f"stop-{token}.json"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["token"] == token and payload["by"] == "gui" and payload["requested_at"]
    first_stat = marker.stat().st_mtime_ns

    assert coordinator.request_drive_stop() is True   # idempotent: no rewrite
    assert marker.stat().st_mtime_ns == first_stat
    assert coordinator.queued_count() == 1            # cooperative stop never clears the queue


def test_drive_stop_refused_for_non_drive_jobs(coordinator):
    coordinator.request_run(["analysis.dat"], RunOptions())
    assert coordinator.request_drive_stop() is False


def test_cancel_drive_tree_kills_and_preserves_queue(qtbot, coordinator):
    coordinator.request_drive(_drive_opts())
    coordinator.request_ingest(["queued.dat"])
    runner = _Runner.instances[0]

    assert coordinator.cancel_drive() is True
    assert runner.tree_killed is True and runner.terminated is False
    assert coordinator.queued_count() == 1

    runner.complete(4)                                # the tree kill surfaces as finished
    ingest = _Ingest.instances[0]
    qtbot.waitUntil(lambda: ingest.started == [["queued.dat"]])


def test_legacy_cancel_on_drive_uses_kill_tree_and_clears_queue(coordinator):
    coordinator.request_drive(_drive_opts())
    coordinator.request_ingest(["queued.dat"])
    runner = _Runner.instances[0]
    assert coordinator.cancel() is True
    assert runner.tree_killed is True and runner.terminated is False
    assert coordinator.queued_count() == 0            # Run-menu contract: clear-all


def test_drive_finished_classifies_operator_stop(qtbot, coordinator):
    lines = []
    coordinator._log = lines.append
    results = []
    coordinator.drive_finished.connect(lambda code, live: results.append((code, live)))
    coordinator.request_drive(_drive_opts())
    _Runner.instances[0].complete(4)
    qtbot.waitUntil(lambda: results == [(4, True)])
    assert any("stopped by operator" in ln for ln in lines)
    assert not any("failed" in ln for ln in lines)


def test_force_stop_exit_code_is_not_logged_as_failure(qtbot, coordinator):
    lines = []
    coordinator._log = lines.append
    coordinator.request_drive(_drive_opts())
    coordinator.cancel_drive()
    _Runner.instances[0].complete(1)   # TerminateJobObject exits with an arbitrary code
    qtbot.waitUntil(lambda: not coordinator.is_busy())
    assert any("force-stopped by operator" in ln for ln in lines)
    assert not any("failed" in ln for ln in lines)


def test_drive_stop_refused_once_runner_finished(coordinator, tmp_path):
    coordinator._wsm._root = str(tmp_path)
    coordinator.request_drive(_drive_opts())
    runner = _Runner.instances[0]
    runner.running = False             # process gone; finished signal not yet delivered
    assert coordinator.request_drive_stop() is False
    assert not (tmp_path / "report" / "control").exists()


def test_stale_generation_drive_completion_is_not_live(qtbot, coordinator):
    results = []
    coordinator.drive_finished.connect(lambda code, live: results.append((code, live)))
    coordinator.request_drive(_drive_opts())
    coordinator._wsm._generation += 1
    _Runner.instances[0].complete(0)
    qtbot.waitUntil(lambda: results == [(0, False)])

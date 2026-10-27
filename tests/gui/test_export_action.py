"""ExportController over a real WorkspaceOperationCoordinator (offscreen Qt; the engine process is faked)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from isar_gui import operation_coordinator
from isar_gui.export_action import ExportController
from isar_gui.operation_coordinator import WorkspaceOperationCoordinator

ROOT = Path(r"D:\ws")
SUMMARY = "export: 2 spectra, 4 mode rows (2 fast, 2 deep), 1 resolution curves, 0 problems (listed in fits.json)"


class _Manager:
    def root(self):
        return str(ROOT)

    def generation(self):
        return 7

    def state(self):
        return "ready"


class _Runner(QObject):
    """Stand-in for ProcessRunner: records argv and exposes line/finished signals to drive by hand."""
    instances = []

    line = Signal(str)
    finished = Signal(int)

    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.argv = None
        self.running = False
        self.terminated = False
        _Runner.instances.append(self)

    def start(self, argv, **_kwargs):
        self.argv = argv
        self.running = True

    def is_running(self):
        return self.running

    def terminate(self):
        self.terminated = True

    def deleteLater(self):
        pass

    def complete(self, code):
        self.running = False
        self.finished.emit(code)


def _make(monkeypatch):
    _Runner.instances = []
    monkeypatch.setattr(operation_coordinator, "ProcessRunner", _Runner)
    coordinator = WorkspaceOperationCoordinator("python", _Manager(), lambda _line: None)
    return ExportController(coordinator), coordinator


def test_success_reports_progress_then_the_new_folder(qtbot, tmp_path, monkeypatch):
    folder = tmp_path / "report" / "exports" / "export-20261027-120000"
    folder.mkdir(parents=True)
    controller, _ = _make(monkeypatch)
    progress, finished, failed, started = [], [], [], []
    controller.progress.connect(progress.append)
    controller.finished.connect(finished.append)
    controller.failed.connect(lambda *args: failed.append(args))
    controller.started.connect(lambda: started.append(True))

    assert controller.start(ROOT) is True
    assert started == [True]
    runner = _Runner.instances[0]
    for line in ("export: 1/2 spectra", "export: 2/2 spectra", SUMMARY, str(folder),
                 json.dumps({"export": {"folder": str(folder), "spectra": 2}})):
        runner.line.emit(line)
    runner.complete(0)

    assert progress == ["Exporting: spectrum 1 of 2", "Exporting: spectrum 2 of 2", SUMMARY]
    assert finished == [folder]
    assert failed == []
    assert controller.is_running() is False
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox)]


def test_failure_names_the_reason_and_the_log(qtbot, monkeypatch):
    controller, _ = _make(monkeypatch)
    failed = []
    controller.failed.connect(lambda reason, log: failed.append((reason, log)))

    assert controller.start(ROOT) is True
    runner = _Runner.instances[0]
    runner.line.emit(json.dumps({"error": {"kind": "export_failed", "message": "OSError: disk full"}}))
    runner.complete(1)

    (reason, log), = failed
    assert reason == "export_failed: OSError: disk full"
    # The log path is captured inside the request, so it survives only if running is set before it.
    assert Path(log).parent == ROOT / "report" / "gui-runs" and Path(log).name.startswith("export-")


def test_refused_request_starts_nothing(qtbot, monkeypatch):
    controller, coordinator = _make(monkeypatch)
    monkeypatch.setattr(coordinator._wsm, "state", lambda: "pending")
    started = []
    controller.started.connect(lambda: started.append(True))

    assert controller.start(ROOT) is False
    assert started == []
    assert controller.is_running() is False
    assert _Runner.instances == []


def test_start_refuses_while_an_export_runs(qtbot, monkeypatch):
    controller, _ = _make(monkeypatch)
    assert controller.start(ROOT) is True
    assert controller.start(ROOT) is False
    assert len(_Runner.instances) == 1


def test_start_refuses_while_the_coordinator_is_busy(qtbot, monkeypatch):
    controller, coordinator = _make(monkeypatch)
    monkeypatch.setattr(coordinator, "is_busy", lambda: True)
    assert controller.start(ROOT) is False
    assert _Runner.instances == []

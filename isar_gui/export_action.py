"""ExportController -- starts ``isar export`` through the operation coordinator.

The engine writes the export folder. This controller queues the command as a coordinator job, relays
its progress lines, and reports the new folder or the failure by signal. It shows no message box: the
Output bar names the folder or the reason, and the log path travels with a failure.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from isar_gui.commands import last_error_line, last_exception_line, parse_last_json_object

if TYPE_CHECKING:
    from isar_gui.operation_coordinator import WorkspaceOperationCoordinator

_PROGRESS = re.compile(r"export: (\d+)/(\d+) spectra$")


class ExportController(QObject):
    started = Signal()
    progress = Signal(str)              # one human line per engine progress line
    finished = Signal(object)           # Path of the new export folder under <root>/report/
    failed = Signal(str, str)           # reason, log path ("" when there is none)

    def __init__(self, operations: "WorkspaceOperationCoordinator",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._operations = operations
        self._running = False
        self._log_path = ""
        self._lines: list[str] = []
        operations.export_started.connect(self._on_started)
        operations.export_line.connect(self._on_line)
        operations.export_finished.connect(self._on_finished)

    def start(self, root: Path) -> bool:
        """True = queued; False = refused (already running, or the coordinator is busy). Never raises."""
        if self._running or self._operations.is_busy():
            return False
        # Set before the request: an idle coordinator starts the export inside the call and emits
        # its start then, and _on_started acts only while running.
        self._running = True
        self._lines = []
        self._log_path = ""
        if not self._operations.request_export(root):
            self._running = False
            return False
        return True

    def is_running(self) -> bool:
        return self._running

    def _on_started(self, log_path: str) -> None:
        if self._running:
            self._log_path = log_path
            self.started.emit()

    def _on_line(self, line: str) -> None:
        if not self._running:
            return
        self._lines.append(line)
        match = _PROGRESS.match(line)
        if match:
            self.progress.emit(f"Exporting: spectrum {match[1]} of {match[2]}")
        elif line.startswith("export: "):
            self.progress.emit(line)

    def _on_finished(self, code: int, _live: bool) -> None:
        self._running = False
        if code == 0:
            export = (parse_last_json_object(self._lines) or {}).get("export") or {}
            folder = export.get("folder")
            if folder and Path(folder).is_dir():
                self.finished.emit(Path(folder))
                return
        self.failed.emit(self._reason(code), self._log_path)

    def _reason(self, code: int) -> str:
        error = (parse_last_json_object(self._lines) or {}).get("error")
        if isinstance(error, dict):
            return f"{error.get('kind')}: {error.get('message')}"
        return (last_error_line(self._lines) or last_exception_line(self._lines)
                or f"export failed (exit {code})")

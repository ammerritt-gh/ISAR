"""ExportController -- starts ``isar export <experiment>`` through the operation coordinator.

Foundation: the signals and the two methods, as a stub that refuses to start. A later unit queues the
engine command as a coordinator job of kind ``"export"`` and reports progress, the new folder or the
failure through these signals; the signatures do not change. The controller shows no message box of
its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from isar_gui.operation_coordinator import WorkspaceOperationCoordinator


class ExportController(QObject):
    started = Signal()
    progress = Signal(str)              # one human line per engine progress line
    finished = Signal(object)           # Path of the new export folder under <root>/report/
    failed = Signal(str, str)           # reason, log path ("" when there is none)

    def __init__(self, operations: "WorkspaceOperationCoordinator",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._operations = operations

    def start(self, root: Path) -> bool:
        """True = queued; False = refused (already running). Never raises. Foundation: refuses."""
        return False

    def is_running(self) -> bool:
        """Queued or running. Foundation: never."""
        return False

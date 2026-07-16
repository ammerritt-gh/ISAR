"""LogDock — a read-only text log dock for status messages and workspace warnings.

Shows GUI status lines the window pushes in, plus new entries from ``Workspace.warnings`` on each
watch tick (the facade appends a string there whenever a tolerant parse degrades). Only the *delta*
since the last tick is shown, so a warning is not repeated every refresh.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDockWidget, QPlainTextEdit


class LogDock(QDockWidget):
    """A dock wrapping a read-only :class:`QPlainTextEdit`; ``append(str)`` adds a line."""

    #: Every appended line, for lightweight mirrors (the compact monitor's ticker).
    appended = Signal(str)

    def __init__(self, parent=None):
        super().__init__("Log", parent)
        self.setObjectName("log_dock")  # stable name so saveState()/restoreState() can find it
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(5000)  # bound memory on a long-running campaign view
        # A dropped file must reach MainWindow.dropEvent, not be pasted as text into the log.
        self._view.setAcceptDrops(False)
        self.setWidget(self._view)
        self._warnings_seen = 0
        self._last_generation = None   # last per-tick generation seen (None => none yet)
        self._last_batch: list | None = None  # the previous tick's full warnings batch

    def append(self, msg: str) -> None:
        self._view.appendPlainText(str(msg))
        self.appended.emit(str(msg))

    def reset_warnings(self) -> None:
        """Clear the per-tick warning high-water bookkeeping.

        The dock now persists across workspace rebinds (its scrollback is kept), so its
        generation/seen/batch state must be reset explicitly on each rebind — otherwise a stale
        ``_last_generation`` or ``_last_batch`` from the previous workspace could suppress the new
        workspace's first warnings batch.
        """
        self._warnings_seen = 0
        self._last_generation = None
        self._last_batch = None

    def sync_warnings(self, warnings, generation=None) -> None:
        """Append only the warnings not shown yet.

        With a ``generation`` (a per-bridge-tick counter from the window), a new tick resets the
        high-water mark: ``Workspace.refresh()`` clears and repopulates ``warnings`` from scratch, so
        a same-length-or-longer batch cannot be told apart by length alone (the bug this replaces).
        A batch identical to the previous tick's is NOT re-printed, so a steady-state repeated parse
        failure does not spam the log every tick.

        ``generation=None`` keeps the legacy length-based behavior for any caller that predates the
        generation counter.
        """
        warnings = list(warnings or [])
        if generation is None:
            if len(warnings) < self._warnings_seen:
                self._warnings_seen = 0
            for w in warnings[self._warnings_seen:]:
                self.append(f"warning: {w}")
            self._warnings_seen = len(warnings)
            return

        if generation != self._last_generation:
            self._warnings_seen = 0
            self._last_generation = generation
        if warnings == self._last_batch:
            # Steady state: the same batch re-reported on a new tick — nothing new to show.
            self._warnings_seen = len(warnings)
            return
        for w in warnings[self._warnings_seen:]:
            self.append(f"warning: {w}")
        self._warnings_seen = len(warnings)
        self._last_batch = list(warnings)

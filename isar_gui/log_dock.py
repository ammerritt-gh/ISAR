"""LogDock — a read-only text log dock for status messages and workspace warnings.

Shows GUI status lines the window pushes in, plus new entries from ``Workspace.warnings`` on each
watch tick (the facade appends a string there whenever a tolerant parse degrades). Only the *delta*
since the last tick is shown, so a warning is not repeated every refresh.
"""
from __future__ import annotations

from PySide6.QtWidgets import QDockWidget, QPlainTextEdit


class LogDock(QDockWidget):
    """A dock wrapping a read-only :class:`QPlainTextEdit`; ``append(str)`` adds a line."""

    def __init__(self, parent=None):
        super().__init__("Log", parent)
        self.setObjectName("log_dock")  # stable name so saveState()/restoreState() can find it
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(5000)  # bound memory on a long-running campaign view
        self.setWidget(self._view)
        self._warnings_seen = 0

    def append(self, msg: str) -> None:
        self._view.appendPlainText(str(msg))

    def sync_warnings(self, warnings) -> None:
        """Append only the warnings not shown yet.

        ``Workspace.refresh()`` clears ``warnings``, which would shrink the list; when that happens
        we reset the high-water mark so the next batch shows in full rather than being skipped.
        """
        warnings = list(warnings or [])
        if len(warnings) < self._warnings_seen:
            self._warnings_seen = 0
        for w in warnings[self._warnings_seen:]:
            self.append(f"warning: {w}")
        self._warnings_seen = len(warnings)

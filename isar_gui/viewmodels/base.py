"""ViewModel base — the tiny common denominator for isar_gui viewmodels.

Holds the read-only :class:`~isar.workspace_api.Workspace` and exposes a single ``refreshed``
signal a view connects to. The window calls :meth:`on_workspace_changed` from its watch slot (on the
Qt thread, after the facade has already been refreshed by the bridge); the base just re-emits
``refreshed`` so subclasses/views recompute. Subclasses add their own derived-state accessors and
signals — they must not re-fit or write (the facade is read-only by contract).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class ViewModel(QObject):
    """Base viewmodel: owns the workspace, re-emits ``refreshed`` on a workspace change."""

    #: Emitted (on the Qt thread) whenever the underlying workspace may have changed.
    refreshed = Signal()

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self._ws = ws

    @property
    def workspace(self):
        return self._ws

    def on_workspace_changed(self, changed=None) -> None:
        """Slot for the window's watch signal. The facade is already refreshed; just re-emit."""
        self.refreshed.emit()

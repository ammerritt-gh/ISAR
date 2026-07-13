"""The GUI's single source of truth for the unit currently being inspected.

Selection is intentionally *not* kept here.  A scientist may select a collection of
units to make a display group or drag it somewhere while continuing to inspect one
particular detector.  Keeping those two concepts separate prevents a group selection
from quietly changing the spectrum in the central inspector.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class ActiveUnitController(QObject):
    """Own the canonical full unit key shown by the inspector and companion views."""

    active_changed = Signal(object)  # str | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key: str | None = None

    @property
    def key(self) -> str | None:
        return self._key

    def set_active(self, key: str | None) -> None:
        normalized = str(key) if key else None
        if normalized == self._key:
            return
        self._key = normalized
        self.active_changed.emit(self._key)

    def retain(self, available_keys) -> None:
        """Keep the active identity through a refresh only while it still exists."""
        if self._key is not None and self._key not in set(available_keys):
            self.set_active(None)

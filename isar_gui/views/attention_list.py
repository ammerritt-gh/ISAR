"""AttentionList -- the needs-attention list in the left column, under the setup section.

Stub (S2U1): the widget exists with its final signature and is wired by the window; S2U2 fills it
with the backend's needs-attention groups and the ticked-scan value entry.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget


class AttentionList(QWidget):
    expanded_changed = Signal(bool)

    def __init__(self, intake, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("attention_list")
        self._intake = intake
        self._expanded = False

    def reveal(self, group_key: str) -> None:
        """Expand the list and that group, and scroll to it."""

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)

"""FactTable -- the scan inspector's table of facts: the file's value, the user's, and the one used.

Stub (S2U1): the widget exists with its final signature; S2U3 fills it and places it in the scan
inspector through ``ScanInspectorPanel.set_intake``.
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


class FactTable(QWidget):
    def __init__(self, intake, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fact_table")
        self._intake = intake

    def set_unit(self, unit_key: str | None) -> None:
        """Show the facts of spectrum ``unit_key`` (``None``: a pooled row or nothing picked)."""

"""ReadinessLine -- the Processing panel's line above *Run analysis…*: what can start, and what is missing.

Stub (S2U1): the widget exists with its final signature and is placed and wired by the window; S2U4
fills it from the backend's readiness read.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget


class ReadinessLine(QWidget):
    reveal_gap = Signal(str)         # group key -> AttentionList.reveal
    locate_requested = Signal()      # -> locate_resolution_folder
    run_requested = Signal()         # -> MainWindow.run_analysis

    def __init__(self, intake, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("readiness_line")
        self._intake = intake

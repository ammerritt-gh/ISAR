"""ScanListPanel -- the scan list, the main thing in the window's left column.

Foundation: a "No scans yet" placeholder over the :class:`~isar_gui.viewmodels.scan_list.
ScanListViewModel`. The tree, filter, sort and columns are filled by a later unit; the constructor
signature does not change. The panel acts only through the view model and accepts no drops: a drop
on it reaches the main window by Qt's propagation, which is the one drop route.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from isar_gui.viewmodels.scan_list import ScanListViewModel


class ScanListPanel(QWidget):           # no public signals; it acts through the VM
    def __init__(self, vm: ScanListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scan_list")
        self.setAcceptDrops(False)
        self._vm = vm
        self._empty = QLabel("No scans yet", self)
        self._empty.setObjectName("scan_list_empty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._empty)

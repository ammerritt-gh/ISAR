"""ScanInspectorPanel -- the optional inspector column between the scan list and the display.

Foundation: the header with its close button. The window owns the column's visibility and width; the
facts and the raw-counts plot of the selected analyzer are filled by a later unit (``show_key``), the
constructor signature does not change. Accepts no drops: a drop reaches the main window by Qt's
propagation, which is the one drop route.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QVBoxLayout, QWidget

from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.elided_label import ElidedLabel


class ScanInspectorPanel(QWidget):
    close_requested = Signal()          # the ✕ button in its header

    def __init__(self, vm: ScanListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scan_inspector")
        self.setAcceptDrops(False)
        self._vm = vm
        title = ElidedLabel("Scan inspector", self)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        self._close = QToolButton(self)
        self._close.setObjectName("scan_inspector_close")
        self._close.setText("✕")
        self._close.setToolTip("Close the inspector")
        self._close.setAccessibleName("Close the scan inspector")
        self._close.clicked.connect(self.close_requested)
        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(self._close, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(header)
        layout.addStretch(1)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def show_key(self, key: str | None) -> None:
        """Show the facts of spectrum ``key`` (``None`` = nothing selected). Foundation: no-op."""

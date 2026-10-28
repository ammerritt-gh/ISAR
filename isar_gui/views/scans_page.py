"""Scan(s) centre page shell; curve drawing is added by the scans display unit."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.group_chips import GroupChips

EMPTY, PLOT, LIST = 0, 1, 2
EMPTY_TEXT = "No spectra selected. Select them in the scan list."


class ScansPage(QWidget):
    """The shared-selection page and stable mounts for the scans display controls."""

    store_dirty = Signal()

    def __init__(self, scan_list_vm: ScanListViewModel, ws, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scan_list_vm = scan_list_vm
        self.ws = ws
        self._store: GuiStore | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        self.fit_mount = self._mount("scans_page_fit", header)
        self.group_mount = self._mount("scans_page_groups", header)
        header_layout.addWidget(self.fit_mount)
        header_layout.addWidget(self.group_mount, 1)
        outer.addWidget(header)

        self.groups = GroupChips(scan_list_vm, self.group_mount)
        self.group_mount.layout().addWidget(self.groups, 1)
        self.groups.store_dirty.connect(self._on_group_store_dirty)

        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        self.layout_mount = self._mount("scans_page_layout", controls)
        self.offset_mount = self._mount("scans_page_offset", controls)
        self.scale_mount = self._mount("scans_page_scale", controls)
        controls_layout.addWidget(self.layout_mount)
        controls_layout.addWidget(self.offset_mount)
        controls_layout.addWidget(self.scale_mount)
        controls_layout.addStretch(1)
        outer.addWidget(controls)

        self.not_drawn = QLabel(self)
        self.not_drawn.setObjectName("scansPageNotDrawn")
        self.not_drawn.hide()
        outer.addWidget(self.not_drawn)

        self.body = QStackedWidget(self)
        self.body.setObjectName("scansPageBody")
        self.empty_label = QLabel(EMPTY_TEXT, self.body)
        self.empty_label.setObjectName("scansPageEmptyText")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.body.addWidget(self.empty_label)

        self.plot_area = QWidget(self.body)
        self.plot_area.setObjectName("scansPagePlotArea")
        plot_layout = QVBoxLayout(self.plot_area)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(0)
        self._glw = pg.GraphicsLayoutWidget(self.plot_area)
        self._glw.setObjectName("scansPageGraphics")
        plot_layout.addWidget(self._glw)
        self.body.addWidget(self.plot_area)

        self.list_area = QWidget(self.body)
        self.list_area.setObjectName("scansPageListArea")
        list_layout = QVBoxLayout(self.list_area)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.list_area)
        self.body.setCurrentIndex(EMPTY)
        outer.addWidget(self.body, 1)

        self.selection.changed.connect(self._on_selection_changed)

    @staticmethod
    def _mount(name: str, parent: QWidget) -> QWidget:
        mount = QWidget(parent)
        mount.setObjectName(name)
        layout = QHBoxLayout(mount)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        return mount

    @property
    def selection(self):
        return self.scan_list_vm.selection

    @property
    def store(self) -> GuiStore | None:
        return self._store

    def set_store(self, store: GuiStore | None) -> None:
        self._store = store
        self.groups.set_store(store)

    def refresh(self) -> None:
        """The drawing unit refreshes the page when it is shown."""

    def load_view_state(self, session) -> None:
        """The drawing unit owns the Scan(s) session settings."""

    def save_view_state(self, session) -> None:
        """The drawing unit owns the Scan(s) session settings."""

    def _on_selection_changed(self, change) -> None:
        self.body.setCurrentIndex(PLOT if change.keys else EMPTY)

    def _on_group_store_dirty(self) -> None:
        self.store_dirty.emit()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, max(240, super().minimumSizeHint().height()))

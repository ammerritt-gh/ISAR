"""ScanTableDock — the tabular twin of the physical Scans dock (Phase E).

Same model, same selection: this is a :class:`~PySide6.QtWidgets.QTableView` bound to the VM's
:class:`~isar_gui.viewmodels.scans.ScanTableModel` and its shared
:class:`~PySide6.QtCore.QItemSelectionModel`, so selecting a row here highlights the matching chips
(and vice-versa). Rows drag out as ``MIME_SCAN_IDS`` (the model builds the payload for the selected
rows), the identical payload chips emit.

Sorting is deliberately OFF in v1 (see the report): the shared selection model is built on the
SOURCE model and chips map ``scan_key`` -> source row via ``UserRole``. A ``QSortFilterProxyModel``
would force the selection model onto the proxy and an index remap in every chip lookup, for little
gain when rows already arrive in ledger order at hundreds of rows. Double-clicking a fitted scan
routes it to the C2 inspector through the VM.
"""
from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QDockWidget, QTableView


class ScanTableDock(QDockWidget):
    """A read/select/drag table over the shared scan model."""

    def __init__(self, vm, parent=None):
        super().__init__("Scan Table", parent)
        self.setObjectName("scan_table_dock")
        self._vm = vm

        view = QTableView(self)
        view.setModel(vm.model)
        view.setSelectionModel(vm.selection_model)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setAlternatingRowColors(True)
        view.setSortingEnabled(False)   # v1: selection stays on the source model (see module docs)
        view.verticalHeader().setVisible(False)
        view.horizontalHeader().setStretchLastSection(True)
        # Drag out selected rows as MIME_SCAN_IDS (payload built by the model's mimeData override).
        view.setDragEnabled(True)
        view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        view.doubleClicked.connect(self._on_double_clicked)
        self._view = view
        self.setWidget(view)

    def _on_double_clicked(self, index) -> None:
        from PySide6.QtCore import Qt
        key = index.data(Qt.UserRole)
        if key:
            self._vm.request_inspect(key)

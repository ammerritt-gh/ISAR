"""Inspectable, reversible manual exclusions; E/I are displayed without waterfall offsets."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)


class _Number(QTableWidgetItem):
    def __init__(self, value):
        super().__init__(f'{value:.2f}')
        self.value = value
        self.setToolTip(f'{value:.12g}')

    def __lt__(self, other):
        return self.value < other.value


class MaskedPoints(QDialog):
    restore_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Masked data points')
        self.resize(620, 380)
        layout = QVBoxLayout(self)
        self.caption = QLabel(self)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(['Scan / analyzer', 'E (meV)', 'I'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setToolTip('Masked points in the displayed spectra. E and I exclude waterfall offsets; click a heading to sort.')
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for title, tip, callback in (
            ('Restore selected', 'Restore the selected manual exclusions. Independent QC exclusions remain.', self.restore_selected),
            ('Clear masks', 'Clear manual exclusions on the displayed spectra. Independent QC exclusions remain.', self.restore_all),
            ('Close', 'Close this list.', self.close)):
            button = QPushButton(title, self)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def show_points(self, rows, unit):
        self.caption.setText(f'{len(rows)} manually masked points in displayed spectra. Intensities: {unit}.')
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(['Scan / analyzer', 'E (meV)', f'I ({unit})'])
        for row, (key, point_id, label, x, y) in enumerate(rows):
            item = QTableWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, (key, point_id))
            item.setToolTip(key)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, _Number(x))
            self.table.setItem(row, 2, _Number(y))
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

    def _restore(self, rows):
        selected = {}
        for row in rows:
            key, point_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            selected.setdefault(key, []).append(point_id)
        self.restore_requested.emit(selected)

    def restore_selected(self):
        self._restore(sorted({item.row() for item in self.table.selectedItems()}))

    def restore_all(self):
        self._restore(range(self.table.rowCount()))

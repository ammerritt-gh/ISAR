"""RecoveryDialog — offer leftover temporary workspaces for recovery at startup (Phase C).

When ISAR launches with no explicit workspace and no openable recent, any content-bearing temporary
workspaces left behind by an earlier session (a crash, or a close where the operator chose neither
Save As nor Discard) are offered here: **Open** the selected one, **Discard selected** (rmtree, temp
only), or **Later** (leave them for next time). Qt land — built only when there is something to
recover, so it never adds latency to the common no-candidates path.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from isar_gui.workspace_manager import discard_dir

_PATH_ROLE = int(Qt.ItemDataRole.UserRole)


class RecoveryDialog(QDialog):
    """Pick a leftover temporary workspace to recover. After ``exec``:

      * :attr:`chosen` is the selected :class:`~pathlib.Path` if the operator clicked **Open**, else
        ``None`` (Discard selected removes the row and leaves ``chosen`` None; Later closes with
        ``chosen`` None).
    """

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recover a temporary workspace?")
        self.chosen: Path | None = None
        self._candidates = [Path(c) for c in candidates]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "These temporary workspaces from an earlier session were never saved.\n"
            "Open one to keep working, discard the ones you don't need, or decide later."))

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for path in self._candidates:
            item = QListWidgetItem(path.name, self._list)
            item.setToolTip(str(path))
            item.setData(_PATH_ROLE, str(path))
        if self._list.count():
            self._list.setCurrentRow(0)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(self)
        open_btn = QPushButton("Open", self)
        discard_btn = QPushButton("Discard selected", self)
        later_btn = QPushButton("Later", self)
        buttons.addButton(open_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(discard_btn, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton(later_btn, QDialogButtonBox.ButtonRole.RejectRole)
        open_btn.clicked.connect(self._on_open)
        discard_btn.clicked.connect(self._on_discard)
        later_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _selected_path(self) -> Path | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return Path(item.data(_PATH_ROLE))

    def _on_open(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        self.chosen = path
        self.accept()

    def _on_discard(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        discard_dir(path)                 # temp-only guard lives in the manager helper
        row = self._list.currentRow()
        self._list.takeItem(row)
        if self._list.count() == 0:
            self.reject()                 # nothing left to recover — close

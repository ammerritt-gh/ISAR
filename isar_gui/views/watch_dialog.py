"""WatchDialog -- the watched-folder controls, in their own non-modal dialog.

Opened from the files strip's *Watch…*. It holds the watched folders (several allowed), *Add…*,
*Remove*, *Pause watching*, the watcher's attention lines and the last check that found new files.
Every change goes through the FilesViewModel, so the window persists the store and re-seeds the
watcher exactly as the Files dock's controls do; the dialog never touches either itself.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget)

from isar_gui.folder_watcher import WatchStatus
from isar_gui.viewmodels.files import FilesViewModel


class WatchDialog(QDialog):
    def __init__(self, files_vm: FilesViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("watch_dialog")
        self.setWindowTitle("Watched folders")
        self.setModal(False)
        self._vm = files_vm
        self._status = WatchStatus()
        self._last_dir = str(Path.home())

        layout = QVBoxLayout(self)
        heading = QLabel("Watched folders (top-level files only)", self)
        heading.setObjectName("watch_heading")
        layout.addWidget(heading)
        self._list = QListWidget(self)
        self._list.setObjectName("watch_list")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self._list)

        row = QHBoxLayout()
        add = QPushButton("Add…", self)
        add.setObjectName("watch_add")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove", self)
        remove.setObjectName("watch_remove")
        remove.clicked.connect(self._remove)
        self._pause = QCheckBox("Pause watching", self)
        self._pause.setObjectName("watch_pause")
        self._pause.toggled.connect(self._vm.set_paused)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(self._pause)
        layout.addLayout(row)

        self._attention = QLabel(self)
        self._attention.setObjectName("watch_attention")
        self._attention.setWordWrap(True)
        self._last = QLabel(self)
        self._last.setObjectName("watch_last_check")
        self._last.setWordWrap(True)
        self._cadence = QLabel(self)
        self._cadence.setObjectName("watch_cadence")
        self._cadence.setWordWrap(True)
        for label in (self._attention, self._last, self._cadence):
            layout.addWidget(label)

        self._vm.refreshed.connect(self._render)
        self._vm.watch_config_changed.connect(self._render)
        self._render()

    def show_status(self, status: WatchStatus) -> None:
        self._status = status
        self._render()

    def _render(self) -> None:
        self._list.clear()
        for folder in self._vm.watched():
            self._list.addItem(folder)
        # Reflect the persisted pause flag without re-emitting a toggle.
        self._pause.blockSignals(True)
        self._pause.setChecked(self._vm.watch_paused())
        self._pause.blockSignals(False)

        status = self._status
        messages = status.messages()
        self._attention.setText("\n".join(messages))
        self._attention.setVisible(bool(messages))
        if status.found_at is None:
            self._last.setText("No new files found since watching started")
        else:
            noun = "file" if status.found == 1 else "files"
            self._last.setText(f"Last check that found files: {status.found_at:%H:%M:%S} · "
                               f"{status.found} new {noun}")
        self._cadence.setText("Watching is paused" if status.paused
                              else "Checks for new files every second while watching is on")

    def _add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Watch a folder for new data", self._last_dir)
        if path:
            self._last_dir = path
            self._vm.add_watch(path)

    def _remove(self) -> None:
        for item in self._list.selectedItems():
            self._vm.remove_watch(item.text())

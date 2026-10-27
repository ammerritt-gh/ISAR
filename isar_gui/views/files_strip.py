"""FilesStrip -- the window's one drop zone, under the experiment strip.

With no experiment open it is a large labelled drop zone ("Drop data files or a folder here") that
says ISAR creates the experiment and reads the files, with *Add files…* and *Add folder…*. It is the
only mount that accepts drops: every dropped or chosen path leaves through ``paths_dropped``, and the
window decides (create-or-ingest, a lone experiment folder opens it).

The two add buttons sit on separate rows so the strip holds the left column's minimum width whatever
the font.

This is the foundation's version: the drop zone, the two add buttons and the signals. The one-line
form with an experiment open, the *Watch…* dialog and the arrival highlight are filled by a later
unit; the signatures do not change.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from isar_gui.commands import GuiIngestReport
from isar_gui.folder_watcher import WatchStatus
from isar_gui.viewmodels.files import FilesViewModel

_FIRST_LAUNCH = "Drop data files or a folder here"
_FIRST_LAUNCH_NOTE = ("ISAR creates the experiment and reads the files. You can rename it and add "
                      "details afterwards.")
_OPEN = "Drop more data files here"


class FilesStrip(QWidget):
    paths_dropped = Signal(list)        # list[str] of absolute local paths: a drop on the strip,
                                        # or the paths chosen through "Add files…"/"Add folder…"/"Add…"
    log_message = Signal(str)           # one line for the Log (what arrived)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("files_strip")
        self.setAcceptDrops(True)
        self._files_vm: FilesViewModel | None = None
        self._experiment_open = False
        self._last_dir = ""

        zone = QFrame(self)
        zone.setObjectName("files_drop_zone")
        zone.setFrameShape(QFrame.Shape.StyledPanel)
        self._title = QLabel(_FIRST_LAUNCH, zone)
        self._title.setObjectName("files_title")
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._title.setWordWrap(True)
        self._note = QLabel(_FIRST_LAUNCH_NOTE, zone)
        self._note.setObjectName("files_note")
        self._note.setWordWrap(True)
        self._add_files = QPushButton("Add files…", zone)
        self._add_files.setObjectName("files_add_files")
        self._add_files.clicked.connect(self._choose_files)
        self._add_folder = QPushButton("Add folder…", zone)
        self._add_folder.setObjectName("files_add_folder")
        self._add_folder.clicked.connect(self._choose_folder)
        inner = QVBoxLayout(zone)
        inner.setContentsMargins(8, 4, 8, 4)
        inner.setSpacing(3)
        inner.addWidget(self._title)
        inner.addWidget(self._note)
        for button in (self._add_files, self._add_folder):
            row = QHBoxLayout()
            row.addWidget(button, 0)
            row.addStretch(1)
            inner.addLayout(row)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 2, 8, 4)
        outer.addWidget(zone)

    # ---- state ---------------------------------------------------------------------------
    def bind(self, files_vm: FilesViewModel, experiment_open: bool) -> None:
        """Called on every bind; the strip drops ``files_vm`` on the next bind."""
        self._files_vm = files_vm
        self._experiment_open = bool(experiment_open)
        self._title.setText(_OPEN if self._experiment_open else _FIRST_LAUNCH)
        self._note.setVisible(not self._experiment_open)

    def set_watch_status(self, status: WatchStatus) -> None:
        """Foundation: watched-folder status is shown by a later unit."""

    def show_arrival(self, report: GuiIngestReport) -> None:
        """Foundation: the arrival highlight and its Log line come from a later unit."""

    # ---- choosing paths ------------------------------------------------------------------
    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add data files", self._last_dir)
        if paths:
            self._last_dir = str(Path(paths[0]).parent)
            self.paths_dropped.emit([str(Path(p)) for p in paths])

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add data folder", self._last_dir)
        if path:
            self._last_dir = path
            self.paths_dropped.emit([str(Path(path))])

    # ---- drag-drop -----------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        md = event.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # str(Path(...)) gives OS-native separators: QUrl always yields forward slashes, which would
        # diverge from the resolved paths in the session manifest on Windows.
        paths = [str(Path(u.toLocalFile())) for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        event.acceptProposedAction()
        self.paths_dropped.emit(paths)

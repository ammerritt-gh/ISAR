"""FilesStrip -- the window's drop target and watch line, under the experiment strip.

Two forms, one mount. With no experiment open, or an experiment holding no files, it is the large drop
zone ("Drop data files or a folder here") with *Add data files…* and *Add data folder…*. Once the experiment
holds files it is one line: the counts, the watch state, *Add data ▾* (files or a folder) and
*Watch a folder…*, which opens the WatchDialog. Both forms take drops. The strip never ingests: every path leaves through
``paths_dropped``, and the window decides (create-or-ingest, a lone experiment folder opens it).

The two add buttons sit on separate rows so the strip holds the left column's minimum width whatever
the font.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QVBoxLayout, QWidget)

from isar_gui.commands import GuiIngestReport
from isar_gui.folder_watcher import WatchStatus
from isar_gui.theme import TONE_COLOR, Tone, dot_icon
from isar_gui.viewmodels.files import FilesViewModel
from isar_gui.views.elided_label import ElidedButton, ElidedLabel
from isar_gui.views.files_dock import _HIGHLIGHT_COLOR, _HIGHLIGHT_MS
from isar_gui.views.watch_dialog import WatchDialog

_FIRST_LAUNCH = "Drop data files or a folder here"
_FIRST_LAUNCH_NOTE = ("ISAR creates the experiment for you and reads the files. You can rename it afterwards. "
                      "Or use New experiment… to make an empty one first.")
_ADD_FILES_TIP = "Read the chosen data files into this experiment. They stay where they are and must stay available."
_ADD_FOLDER_TIP = "Read every data file of the chosen folder into this experiment. They stay where they are."
_WATCH_TIP = ("Choose a folder (for example where the beamline writes scans); new files appearing in it are "
              "read into this experiment automatically.")
_IN_EXPERIMENT_NOTE = "Files you drop are read into this experiment."
_NEEDS_EXPERIMENT = "needs an experiment with data"


class FilesStrip(QWidget):
    paths_dropped = Signal(list)        # list[str] of absolute local paths: a drop on the strip,
                                        # or the paths chosen through "Add files…"/"Add folder…"/"Add…"
    log_message = Signal(str)           # one line for the Log (what arrived)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("files_strip")
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)   # the arrival tint paints
        self._files_vm: FilesViewModel | None = None
        self._experiment_open = False
        self._status = WatchStatus()
        self._last_dir = ""
        self._watch_dialog: WatchDialog | None = None

        # ---- the drop zone: no experiment, or one with no files yet ----
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
        self._add_files = QPushButton("Add data files…", zone)
        self._add_files.setObjectName("files_add_files")
        self._add_files.setToolTip(_ADD_FILES_TIP)
        self._add_files.clicked.connect(self.choose_files)
        self._add_folder = QPushButton("Add data folder…", zone)
        self._add_folder.setObjectName("files_add_folder")
        self._add_folder.setToolTip(_ADD_FOLDER_TIP)
        self._add_folder.clicked.connect(self.choose_folder)
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
        self._zone = zone

        # ---- the one line: counts and watch state, then Add… and Watch… ----
        line = QFrame(self)
        line.setObjectName("files_line")
        self._line_text = ElidedLabel("", line, Qt.TextElideMode.ElideRight)    # the counts stay; the watch tail goes
        self._line_text.setObjectName("files_line_text")
        self._attention = QWidget(line)
        self._attention.setObjectName("files_attention")
        self._attention_dot = QLabel(self._attention)
        self._attention_dot.setPixmap(dot_icon(Tone.WARN).pixmap(10, 10))
        self._attention_word = ElidedLabel("watch needs attention", self._attention, Qt.TextElideMode.ElideRight)
        self._attention_word.setStyleSheet(f"color: {TONE_COLOR[Tone.WARN]};")
        attention_row = QHBoxLayout(self._attention)
        attention_row.setContentsMargins(0, 0, 0, 0)
        attention_row.setSpacing(4)
        attention_row.addWidget(self._attention_dot, 0)
        attention_row.addWidget(self._attention_word, 1)
        self._attention.hide()
        self._add = ElidedButton("Add data ▾", line)
        self._add.setObjectName("files_add")
        self._add.setToolTip("Read more data files, or a whole folder, into this experiment. The originals stay "
                             "where they are and must stay available.")
        add_menu = QMenu(self._add)
        add_menu.addAction("Data files…").triggered.connect(self.choose_files)
        add_menu.addAction("Data folder…").triggered.connect(self.choose_folder)
        self._add.setMenu(add_menu)
        self._watch = ElidedButton("Watch a folder…", line)
        self._watch.setObjectName("files_watch")
        self._watch.setToolTip(_WATCH_TIP)
        self._watch.clicked.connect(self._open_watch)
        self._watch_reason = QLabel(_NEEDS_EXPERIMENT, line)      # why Watch is off, as text
        self._watch_reason.setObjectName("files_watch_reason")
        self._watch_reason.setWordWrap(True)
        self._watch_reason.setStyleSheet("color: #6b7280;")
        self._watch_reason.hide()
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._line_text, 1)          # elides; widens nothing, so the strip fits 280 px
        row.addWidget(self._add, 0)
        row.addWidget(self._watch, 0)
        line_layout = QVBoxLayout(line)
        line_layout.setContentsMargins(8, 4, 8, 4)
        line_layout.setSpacing(3)
        line_layout.addLayout(row)
        line_layout.addWidget(self._watch_reason)
        line_layout.addWidget(self._attention)     # a second row only while the watch needs attention
        self._line = line

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 2, 8, 4)
        outer.addWidget(zone)
        outer.addWidget(line)
        line.hide()

    # ---- state ---------------------------------------------------------------------------
    def bind(self, files_vm: FilesViewModel, experiment_open: bool) -> None:
        """Called on every bind; the strip drops ``files_vm`` on the next bind."""
        if files_vm is not self._files_vm:
            self._close_watch()
            files_vm.refreshed.connect(self._render)
            files_vm.watch_config_changed.connect(self._render)
        self._files_vm = files_vm
        self._experiment_open = bool(experiment_open)
        self._render()

    def set_watch_status(self, status: WatchStatus) -> None:
        self._status = status
        if self._watch_dialog is not None:
            self._watch_dialog.show_status(status)
        self._render()

    def show_arrival(self, report: GuiIngestReport) -> None:
        """Tint the strip briefly and log what arrived; a report with no files does nothing."""
        if not report.files:
            return
        self.setStyleSheet(f"#files_strip {{ background: {_HIGHLIGHT_COLOR.name()}; }}")
        # Context-object form: a rebind during the highlight cannot fire into a deleted strip.
        QTimer.singleShot(_HIGHLIGHT_MS, self, self._clear_arrival)
        root = self._files_vm.workspace.root
        name = Path(root).name if root else "no experiment"
        summary = report.last_ingest or {}
        logical, units = summary.get("logical_scan_count"), summary.get("analyzer_unit_count")
        if logical is not None and units is not None:
            what = f"{logical} scan set(s), {units} analyzer(s)"
        else:
            what = f"+{len(report.added)} added, ~{len(report.updated)} updated"
        not_read = sum(1 for f in report.files if f.get("error"))
        tail = f"; {not_read} not read" if not_read else ""
        self.log_message.emit(f"Read {len(report.files)} file(s) into {name}: {what}{tail}")

    def _clear_arrival(self) -> None:
        self.setStyleSheet("")

    # ---- rendering -----------------------------------------------------------------------
    def _render(self) -> None:
        vm = self._files_vm
        if vm is None:
            return
        files, sets = vm.counts
        drop_zone = not self._experiment_open or files == 0
        self._zone.setVisible(drop_zone)
        self._line.setVisible(not drop_zone)
        if drop_zone:
            self._note.setText(_IN_EXPERIMENT_NOTE if self._experiment_open else _FIRST_LAUNCH_NOTE)
            return
        text = (f"{files} file{'s' if files != 1 else ''} · {sets} scan set{'s' if sets != 1 else ''}"
                " · drop more here" + self._watch_text())
        self._line_text.set_full_text(text)
        self._watch.setEnabled(vm.can_watch)
        self._watch.setToolTip(_WATCH_TIP if vm.can_watch else f"Watching is off: {_NEEDS_EXPERIMENT}.")
        self._watch_reason.setVisible(not vm.can_watch)
        messages = list(self._status.messages())
        self._attention.setVisible(self._status.needs_attention)
        self._attention_word.set_full_text("Watch problem: " + messages[0] if messages else "watch needs attention")
        tip = "\n".join(messages)
        self._attention.setToolTip(tip)
        self._attention_dot.setToolTip(tip)
        self._attention_word.setToolTip(tip or self._attention_word.full_text())

    def _watch_text(self) -> str:
        folders = self._files_vm.watched()
        if not folders:
            return ""
        if self._status.paused:
            return " · watching paused"
        if len(folders) == 1:
            return f" · watching {folders[0]}"
        return f" · watching {len(folders)} folders"

    # ---- the Watch dialog ----------------------------------------------------------------
    def _open_watch(self) -> None:
        if self._watch_dialog is None:
            self._watch_dialog = WatchDialog(self._files_vm, self)
            self._watch_dialog.show_status(self._status)
        self._watch_dialog.show()
        self._watch_dialog.raise_()
        self._watch_dialog.activateWindow()

    def _close_watch(self) -> None:
        if self._watch_dialog is not None:
            self._watch_dialog.close()
            self._watch_dialog.deleteLater()
            self._watch_dialog = None

    # ---- choosing paths ------------------------------------------------------------------
    def choose_files(self) -> None:
        """Pick data files to read into this experiment (also the needs-attention list's *Add data…*)."""
        paths, _ = QFileDialog.getOpenFileNames(self, "Add data files", self._last_dir)
        if paths:
            self._last_dir = str(Path(paths[0]).parent)
            self.paths_dropped.emit([str(Path(p)) for p in paths])

    def choose_folder(self) -> None:
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

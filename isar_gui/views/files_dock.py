"""FilesDock — the data-file entry point and manifest view (Phase D).

The one dock that is useful *before* a workspace exists: it is where the operator drops data (or
picks it via Add Files… / Add Folder…) to begin, so it stays enabled and drop-accepting in the
empty/pending state with a "Drop data files here to begin." hint. Everything it does routes through
the :class:`~isar_gui.ingest_controller.IngestController` (drops, browse buttons) or the
:class:`~isar_gui.viewmodels.files.FilesViewModel` (the manifest rows + watched-folder config).

Presentational only: the viewmodel computes ``status_text``; this widget lays out a Name | Status
tree, tooltips the full path, transiently highlights the files a just-finished ingest added, and
renders the watched-folder list with Add/Remove and a Pause checkbox.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_HINT = "Drop data files here to begin."
_HIGHLIGHT_MS = 2000
_HIGHLIGHT_COLOR = QColor(255, 243, 176)  # a soft amber; fades after ~2 s


class FilesDock(QDockWidget):
    """Data-file drop target + ingest manifest + watched-folder controls."""

    def __init__(self, vm, controller, parent=None):
        super().__init__("Files", parent)
        self.setObjectName("files_dock")
        self._vm = vm
        self._controller = controller
        self._last_dir = str(Path.home())
        self._highlight: set[str] = set()
        # Drops land on the dock itself; child widgets that decline drops (the tree, below) let the
        # event propagate up here. Overriding the QDockWidget's own dragEnter/dropEvent is the only
        # reliable hook — PySide6 dispatches virtual events to the subclass, never to an instance
        # attribute monkeypatched onto a plain QWidget.
        self.setAcceptDrops(True)

        body = QWidget(self)
        layout = QVBoxLayout(body)

        # ---- top row: add files / add folder ----
        top = QHBoxLayout()
        add_files = QPushButton("Add Files…", body)
        add_files.clicked.connect(self._add_files)
        add_folder = QPushButton("Add Folder…", body)
        add_folder.clicked.connect(self._add_folder)
        top.addWidget(add_files)
        top.addWidget(add_folder)
        top.addStretch(1)
        layout.addLayout(top)

        self._hint = QLabel(_HINT, body)
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet("color: #888; padding: 6px;")
        layout.addWidget(self._hint)

        # ---- file manifest ----
        self._tree = QTreeWidget(body)
        self._tree.setHeaderLabels(["Name", "Status"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAcceptDrops(False)   # let drops fall through to the dock body
        layout.addWidget(self._tree, 1)

        # ---- watched folders ----
        layout.addWidget(QLabel("Watched folders", body))
        self._watch_list = QListWidget(body)
        self._watch_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._watch_list.setMaximumHeight(110)
        layout.addWidget(self._watch_list)

        watch_row = QHBoxLayout()
        add_watch = QPushButton("Add…", body)
        add_watch.clicked.connect(self._add_watch)
        remove_watch = QPushButton("Remove", body)
        remove_watch.clicked.connect(self._remove_watch)
        self._pause_check = QCheckBox("Pause watching", body)
        self._pause_check.toggled.connect(self._on_pause_toggled)
        watch_row.addWidget(add_watch)
        watch_row.addWidget(remove_watch)
        watch_row.addStretch(1)
        watch_row.addWidget(self._pause_check)
        layout.addLayout(watch_row)

        self.setWidget(body)

        vm.refreshed.connect(self.refresh)
        vm.watch_config_changed.connect(self._render_watch)
        self.refresh()

    # ---- rendering -------------------------------------------------------
    def refresh(self) -> None:
        self._render_files()
        self._render_watch()

    def _render_files(self) -> None:
        rows = self._vm.rows
        self._hint.setVisible(not rows)
        self._tree.clear()
        for row in rows:
            item = QTreeWidgetItem([row["name"], row["status_text"]])
            item.setToolTip(0, row["path"])
            item.setToolTip(1, row["path"])
            if row["path"] in self._highlight:
                for col in (0, 1):
                    item.setBackground(col, QBrush(_HIGHLIGHT_COLOR))
            self._tree.addTopLevelItem(item)

    def _render_watch(self) -> None:
        self._watch_list.clear()
        for folder in self._vm.watched():
            self._watch_list.addItem(folder)
        # Reflect the persisted pause flag without re-emitting a toggle.
        self._pause_check.blockSignals(True)
        self._pause_check.setChecked(self._vm.watch_paused())
        self._pause_check.blockSignals(False)

    # ---- transient highlight (added files) -------------------------------
    def highlight_added(self, paths) -> None:
        """Briefly highlight the manifest rows for ``paths`` (files a just-finished ingest added)."""
        paths = {str(p) for p in paths}
        if not paths:
            return
        self._highlight |= paths
        self._render_files()
        QTimer.singleShot(_HIGHLIGHT_MS, lambda: self._clear_highlight(paths))

    def _clear_highlight(self, paths) -> None:
        self._highlight -= set(paths)
        self._render_files()

    # ---- top-row browse (ingest) -----------------------------------------
    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add data files", self._last_dir)
        if paths:
            self._last_dir = str(Path(paths[0]).parent)
            self._controller.request_ingest(paths)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add data folder", self._last_dir)
        if path:
            self._last_dir = path
            self._controller.request_ingest([path])

    # ---- watched-folder controls -----------------------------------------
    def _add_watch(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Watch a folder for new data", self._last_dir)
        if path:
            self._last_dir = path
            self._vm.add_watch(path)

    def _remove_watch(self) -> None:
        for item in self._watch_list.selectedItems():
            self._vm.remove_watch(item.text())

    def _on_pause_toggled(self, checked: bool) -> None:
        self._vm.set_paused(checked)

    # ---- drag-drop -------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        md = event.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        event.acceptProposedAction()
        self._controller.request_ingest(paths)

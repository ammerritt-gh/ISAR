"""ExperimentStrip -- the top of the window's left column: which experiment is open.

With none open it reads "No experiment open" beside *New experiment…* (the one primary button),
*Open…* and *Recent ▾*. With one open it shows the experiment's name with *Rename* and its folder
path beneath. Purely presentational: every action is a signal, and the window decides what it does.

The buttons sit on two rows so the strip holds the left column's minimum width whatever the font.

This is the foundation's version: the strip with its signals and busy state. The one-time note and
the lifecycle behind New/Rename are filled by a later unit; its signatures do not change.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout,
                               QWidget)

from isar_gui.views.elided_label import ElidedLabel

_NO_EXPERIMENT = "No experiment open"


class ExperimentStrip(QWidget):
    new_requested = Signal()            # "New experiment…" clicked
    open_requested = Signal()           # "Open…" clicked
    recent_chosen = Signal(str)         # a path picked from "Recent ▾"
    rename_requested = Signal()         # "Rename" clicked (strip or one-time note)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("experiment_strip")
        self._root: Path | None = None
        self._busy_reason: str | None = None

        self._name = ElidedLabel(_NO_EXPERIMENT, self)
        self._name.setObjectName("experiment_name")
        font = self._name.font()
        font.setBold(True)
        self._name.setFont(font)
        self._rename = QPushButton("Rename", self)
        self._rename.setObjectName("experiment_rename")
        self._rename.clicked.connect(self.rename_requested)
        self._path = ElidedLabel("", self)
        self._path.setObjectName("experiment_path")
        self._path.setEnabled(False)   # greyed: secondary text

        self._new = QPushButton("New experiment…", self)
        self._new.setObjectName("experiment_new")
        self._new.setDefault(True)
        self._new.clicked.connect(self.new_requested)
        self._open = QPushButton("Open…", self)
        self._open.setObjectName("experiment_open")
        self._open.clicked.connect(self.open_requested)
        self._recent = QToolButton(self)
        self._recent.setObjectName("experiment_recent")
        self._recent.setText("Recent ▾")
        self._recent.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recent.setStyleSheet("QToolButton::menu-indicator { image: none; }")   # text has the arrow
        self._recent_menu = QMenu(self._recent)
        self._recent.setMenu(self._recent_menu)

        self._busy = QLabel("", self)
        self._busy.setObjectName("experiment_busy")
        self._busy.setWordWrap(True)
        self._busy.hide()

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self._name, 1)
        top.addWidget(self._rename, 0)
        primary = QHBoxLayout()
        primary.addWidget(self._new, 0)
        primary.addStretch(1)
        secondary = QHBoxLayout()
        secondary.setSpacing(6)
        secondary.addWidget(self._open, 0)
        secondary.addWidget(self._recent, 0)
        secondary.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 4)
        outer.setSpacing(3)
        outer.addLayout(top)
        outer.addWidget(self._path)
        outer.addLayout(primary)
        outer.addLayout(secondary)
        outer.addWidget(self._busy)
        # The initial look is "No experiment open" with no recents (set directly, not through the
        # public setters, which belong to the window's bind).
        self._path.hide()
        self._rename.hide()
        self._recent_menu.addAction("(no recent experiments)").setEnabled(False)

    def set_experiment(self, root: Path | None) -> None:
        """``None`` = "No experiment open"."""
        self._root = None if root is None else Path(root)
        if self._root is None:
            self._name.set_full_text(_NO_EXPERIMENT)
            self._path.set_full_text("")
        else:
            self._name.set_full_text(self._root.name)
            self._path.set_full_text(str(self._root))
        self._path.setVisible(self._root is not None)
        self._apply_enabled()

    def set_recent(self, paths: list[str]) -> None:
        """Most recent first; ``[]`` = none."""
        self._recent_menu.clear()
        if not paths:
            self._recent_menu.addAction("(no recent experiments)").setEnabled(False)
            return
        for path in paths:
            action = self._recent_menu.addAction(path)
            action.triggered.connect(lambda _checked=False, p=path: self.recent_chosen.emit(p))

    def set_busy(self, reason: str | None) -> None:
        """``None`` = idle; else New/Open/Recent/Rename are disabled and the reason is shown."""
        self._busy_reason = reason or None
        self._busy.setText(reason or "")
        self._busy.setVisible(bool(reason))
        self._apply_enabled()

    def _apply_enabled(self) -> None:
        idle = self._busy_reason is None
        for button in (self._new, self._open, self._recent):
            button.setEnabled(idle)
        self._rename.setVisible(self._root is not None)
        self._rename.setEnabled(idle)
        self._rename.setToolTip("" if idle else self._busy_reason)

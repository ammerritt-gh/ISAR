"""ExperimentStrip -- the top of the window's left column: which experiment is open.

With none open it reads "No experiment open" beside *New experiment…* (the one primary button),
*Open…* and *Recent ▾*. With one open it shows the experiment's name with *Rename* and its folder
path beneath. Purely presentational: every action is a signal, and the window decides what it does.

The buttons sit on two rows so the strip holds the left column's minimum width whatever the font.

Two lines appear only when the window asks for them: the one-time note after a drop created and
named the experiment (*Rename* and ✕), and a warning when an opened experiment's source data folder
is gone (in the warning colour, every missing folder in its tooltip).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedLabel

_NO_EXPERIMENT = "No experiment open"
# The one-time note's words by what named the experiment; "default" shows no note.
_CREATED_NOTES = {
    "folder": "Experiment created and named after the folder you dropped.",
    "data": "Experiment created and named after the files you dropped.",
    "data_folder": "Experiment created and named after the folder the dropped files are in.",
}


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

        # The one-time note: what named the experiment, with Rename and a dismiss cross.
        self._note = QFrame(self)
        self._note.setObjectName("experiment_note")
        self._note.setFrameShape(QFrame.Shape.StyledPanel)
        self._note_text = QLabel("", self._note)
        self._note_text.setObjectName("experiment_note_text")
        self._note_text.setWordWrap(True)
        note_rename = QPushButton("Rename", self._note)
        note_rename.setObjectName("experiment_note_rename")
        note_rename.clicked.connect(self.rename_requested)
        note_close = QToolButton(self._note)
        note_close.setObjectName("experiment_note_close")
        note_close.setText("✕")
        note_close.setAccessibleName("Dismiss this note")
        note_close.setToolTip("Dismiss this note")
        note_close.clicked.connect(self._note.hide)
        note_layout = QHBoxLayout(self._note)
        note_layout.setContentsMargins(6, 3, 3, 3)
        note_layout.setSpacing(4)
        note_layout.addWidget(self._note_text, 1)
        note_layout.addWidget(note_rename, 0)
        note_layout.addWidget(note_close, 0)
        self._note.hide()

        # A missing outside reference of the opened experiment (its source data folder).
        self._warning = QLabel("", self)
        self._warning.setObjectName("experiment_warning")
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(f"color: {TONE_COLOR[Tone.WARN]};")
        self._warning.hide()

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
        outer.addWidget(self._note)
        outer.addWidget(self._warning)
        outer.addLayout(primary)
        outer.addLayout(secondary)
        outer.addWidget(self._busy)
        # The initial look is "No experiment open" with no recents (set directly, not through the
        # public setters, which belong to the window's bind).
        self._path.hide()
        self._rename.hide()
        self._recent_menu.addAction("(no recent experiments)").setEnabled(False)

    def set_experiment(self, root: Path | None) -> None:
        """``None`` = "No experiment open". A different root hides the one-time note."""
        root = None if root is None else Path(root)
        if root != self._root:
            self._note.hide()
        self._root = root
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

    def show_created_note(self, named_from: str) -> None:
        """The one-time note after ISAR created and named the experiment; ``"default"`` shows none."""
        text = _CREATED_NOTES.get(named_from)
        if text is None:
            self._note.hide()
            return
        self._note_text.setText(text)
        self._note.show()

    def set_outside_warning(self, text: str | None, detail: str | None = None) -> None:
        """One warning line about a missing outside reference; ``None`` clears it. ``detail`` is its
        tooltip."""
        self._warning.setText(text or "")
        self._warning.setToolTip(detail or text or "")
        self._warning.setVisible(bool(text))

    def _apply_enabled(self) -> None:
        idle = self._busy_reason is None
        for button in (self._new, self._open, self._recent):
            button.setEnabled(idle)
        self._rename.setVisible(self._root is not None)
        self._rename.setEnabled(idle)
        self._rename.setToolTip("" if idle else self._busy_reason)

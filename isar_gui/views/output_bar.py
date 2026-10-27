"""OutputBar -- the bottom-right of the window: where results go, and the two explicit actions.

A status line ("Results are written to …\\leTacon_SP8_CsV3Sb5 as they arrive · last written 06:41:30")
with *Save copy as…* and *Export…*. When an action is unavailable the buttons are disabled and the
reason is shown beside them. Purely presentational: the buttons are signals and the window decides
what they do.

Below them sits the result of the last copy or export: one line naming its folder with one action
(*Open copy* or *Open folder*), or a failure in the error style, an export failure with its log path
as selectable text. Each message replaces the previous one; binding another experiment clears it.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedLabel

_NOTHING_WRITTEN = "No experiment, so nothing is being written."
_ERROR_STYLE = f"color: {TONE_COLOR[Tone.ERR]};"


class OutputBar(QWidget):
    save_copy_requested = Signal()      # "Save copy as…"
    export_requested = Signal()         # "Export…"
    open_copy_requested = Signal(object)  # Path of a finished copy ("Open copy")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("output_bar")
        self._root: Path | None = None
        self._last_written: datetime | None = None
        self._action_kind: str | None = None      # the result message's one action
        self._action_folder: Path | None = None

        self._status = ElidedLabel(_NOTHING_WRITTEN, self)
        self._status.setObjectName("output_status")
        self._save_copy = QPushButton("Save copy as…", self)
        self._save_copy.setObjectName("output_save_copy")
        self._save_copy.clicked.connect(self.save_copy_requested)
        self._export = QPushButton("Export…", self)
        self._export.setObjectName("output_export")
        self._export.clicked.connect(self.export_requested)
        self._reason = QLabel("", self)
        self._reason.setObjectName("output_reason")
        self._reason.setWordWrap(True)
        self._reason.hide()
        # The result of the last copy or export: a sentence naming the folder, with one action.
        self._message = ElidedLabel("", self)
        self._message.setObjectName("output_message")
        self._message.hide()
        self._message_action = QPushButton("", self)
        self._message_action.setObjectName("output_message_action")
        self._message_action.clicked.connect(self._on_message_action)
        self._message_action.hide()
        # A failed export's log path: selectable, so it can be copied into Explorer.
        self._log_path = QLabel("", self)
        self._log_path.setObjectName("output_log_path")
        self._log_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._log_path.setWordWrap(True)
        self._log_path.hide()

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self._save_copy, 0)
        buttons.addWidget(self._export, 0)
        buttons.addStretch(1)
        message_row = QHBoxLayout()
        message_row.setSpacing(6)
        message_row.addWidget(self._message, 1)
        message_row.addWidget(self._message_action, 0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(3)
        outer.addWidget(self._status)
        outer.addLayout(buttons)
        outer.addWidget(self._reason)
        outer.addLayout(message_row)
        outer.addWidget(self._log_path)
        outer.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.set_available(False, False, "need an experiment")

    # ---- where results go ----------------------------------------------------------------
    def set_experiment(self, root: Path | None) -> None:
        """Where results are written; ``None`` = no experiment open. Clears the last message."""
        self._root = None if root is None else Path(root)
        self._last_written = None
        self._clear_message()
        self._refresh_status()

    def note_written(self, when: datetime) -> None:
        """Results were just written: "last written HH:MM:SS" (with the date when not today)."""
        self._last_written = when
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self._root is None:
            self._status.set_full_text(_NOTHING_WRITTEN)
            return
        if self._last_written is None:
            tail = "nothing written yet"
        elif self._last_written.date() == datetime.now().date():
            tail = f"last written {self._last_written:%H:%M:%S}"
        else:
            tail = f"last written {self._last_written:%Y-%m-%d %H:%M:%S}"
        self._status.set_full_text(f"Results are written to …\\{self._root.name} as they arrive · {tail}")
        self._status.setToolTip(f"Results are written to {self._root}")   # the full path on hover

    # ---- the two actions -----------------------------------------------------------------
    def set_available(self, save_copy: bool, export: bool, reason: str) -> None:
        """Enable the actions; while either is off, ``reason`` is shown beside the buttons."""
        self._save_copy.setEnabled(save_copy)
        self._export.setEnabled(export)
        off = not (save_copy and export)
        self._reason.setText(reason if off else "")
        self._reason.setVisible(off and bool(reason))
        for button in (self._save_copy, self._export):
            button.setToolTip("" if button.isEnabled() else reason)

    def show_copy_done(self, folder: Path) -> None:
        """Name the finished copy and offer *Open copy*; new data keeps going to the original."""
        current = self._root.name if self._root is not None else "the open experiment"
        self._show_message(f"Copy saved to {folder} · new data keeps going to {current}",
                           "Open copy", folder)

    def show_copy_failed(self, reason: str) -> None:
        """A *Save copy as…* that did not happen: the reason, in the error style."""
        self._show_message(f"Copy not saved: {reason}", None, None, error=True)

    def show_export_progress(self, text: str) -> None:
        self._show_message(f"Exporting… {text}", None, None)

    def show_export_done(self, folder: Path) -> None:
        """Name the new export folder and offer *Open folder*."""
        self._show_message(f"Export written to {folder}", "Open folder", folder)

    def show_export_failed(self, reason: str, log_path: str) -> None:
        """The reason in the error style, and the engine's log path as selectable text."""
        self._show_message(f"Export failed: {reason}", None, None, error=True)
        if log_path:
            self._log_path.setText(f"Log: {log_path}")
            self._log_path.show()

    def _show_message(self, text: str, action: str | None, folder, *, error: bool = False) -> None:
        self._message.set_full_text(text)
        self._message.setStyleSheet(_ERROR_STYLE if error else "")
        self._message.show()
        self._message_action.setVisible(action is not None)
        self._message_action.setText(action or "")
        self._log_path.hide()
        self._log_path.setText("")
        self._action_kind = action
        self._action_folder = None if folder is None else Path(folder)

    def _clear_message(self) -> None:
        self._message.set_full_text("")
        self._message.setStyleSheet("")
        self._message.hide()
        self._message_action.hide()
        self._log_path.hide()
        self._log_path.setText("")
        self._action_kind = None
        self._action_folder = None

    def _on_message_action(self) -> None:
        if self._action_folder is None:
            return
        if self._action_kind == "Open copy":
            self.open_copy_requested.emit(self._action_folder)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._action_folder)))

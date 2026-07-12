"""CardDock — a raw-TOML editor for ``card.policy.toml`` (D2, v1 correction channel).

The card is the human-owned description of the experiment (detectors, known phonon energies, bad
scans, fitting guidelines). D8 forbids the *machine* from writing it, but a human editing it through
the GUI is still human authorship, so this dock reads and writes the file directly — it deliberately
does NOT go through the read-only :mod:`isar.workspace_api` facade.

v1 is a raw commented-TOML pane (a structured card editor is Phase-C-adjacent, since card edits are
the correction channel). The one guard: the text is parsed with :mod:`tomllib` before writing, so a
syntax error is reported inline and the on-disk card is never left invalid.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_CARD_NAME = "card.policy.toml"


class CardDock(QDockWidget):
    """Load / edit / save ``<workspace>/card.policy.toml`` with a pre-save TOML validity check."""

    def __init__(self, ws, parent=None):
        super().__init__("Card", parent)
        self.setObjectName("card_dock")  # stable name for saveState()/restoreState()
        self._path: Path | None = None

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(4, 4, 4, 4)

        self._editor = QPlainTextEdit()
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.setReadOnly(True)
        # A file dropped over the dock must reach MainWindow.dropEvent (open workspace / bootstrap-
        # ingest via the Files dock), not be
        # pasted as text into the editor — QPlainTextEdit accepts drops by default.
        self._editor.setAcceptDrops(False)
        outer.addWidget(self._editor)

        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        self._save_btn = QPushButton("Save")
        self._reload_btn = QPushButton("Reload")
        self._save_btn.clicked.connect(self.save)
        self._reload_btn.clicked.connect(self.reload)
        row_l.addWidget(self._save_btn)
        row_l.addWidget(self._reload_btn)
        row_l.addStretch(1)
        self._status = QLabel("")
        row_l.addWidget(self._status)
        outer.addWidget(row)

        self.setWidget(body)
        self.set_workspace(ws)

    # ---- binding ----------------------------------------------------------
    def set_workspace(self, ws) -> None:
        """Point the editor at ``ws``'s card (or disable it in the empty state)."""
        root = getattr(ws, "root", None)
        self._path = None if root is None else Path(root) / _CARD_NAME
        self.reload()

    # ---- load / save ------------------------------------------------------
    def reload(self) -> None:
        """(Re)load the card text from disk, discarding unsaved edits."""
        if self._path is None or not self._path.exists():
            self._editor.setPlainText("")
            self._set_enabled(False)
            self._status.setText("no workspace" if self._path is None else "card.policy.toml missing")
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._editor.setPlainText("")
            self._set_enabled(False)
            self._status.setText(f"could not read card: {exc}")
            return
        self._editor.setPlainText(text)
        self._set_enabled(True)
        self._status.setText(f"loaded {self._path.name}")

    def save(self) -> bool:
        """Validate the current text as TOML and, if valid, write it. Returns success.

        On a TOML syntax error the file is left untouched and the error is shown inline — the card on
        disk is never made invalid by a save.
        """
        if self._path is None:
            self._status.setText("no workspace to save to")
            return False
        text = self._editor.toPlainText()
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self._status.setText(f"NOT saved — TOML error: {exc}")
            return False
        try:
            self._path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._status.setText(f"could not write card: {exc}")
            return False
        self._status.setText(f"saved {self._path.name}")
        return True

    # ---- helpers ----------------------------------------------------------
    def _set_enabled(self, on: bool) -> None:
        self._editor.setReadOnly(not on)
        self._save_btn.setEnabled(on)
        self._reload_btn.setEnabled(on)

    @property
    def text(self) -> str:
        return self._editor.toPlainText()

    def set_text(self, text: str) -> None:
        """Test/helper hook: replace the editor contents without touching disk."""
        self._editor.setPlainText(text)

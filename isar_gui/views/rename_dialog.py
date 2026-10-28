"""RenameDialog -- name the open experiment (its name is its folder's name).

A name field prefilled with the current name; beneath it the folder the name gives, cleaned as the
manager will clean it ("Folder: <parent>\\<cleaned name>"), so the cleaning shows before it happens.
OK is disabled, with the reason inline, while the cleaned name is empty or that folder already
exists (the manager checks again). When the experiment has fits whose resolution curves sit inside
its folder, one line says adding them to the queue again will refit them: ISAR recognises a curve set by
its folder's path, so a renamed folder is a new curve set.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout, QWidget)

from isar.workspace_api.references import outside_references
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.workspace_manager import clean_experiment_name

log = logging.getLogger(__name__)

REFIT_LINE = ("Adding these spectra to the queue again will refit them: their resolution curves are inside "
              "this folder, and ISAR recognises curves by their folder's path.")


def refits_after_rename(ws) -> bool:
    """True when ``ws`` has stored fits and the resolution curves they used sit inside its folder."""
    if ws is None or not ws.results:
        return False
    return outside_references(ws).resolution_inside


class RenameDialog(QDialog):
    def __init__(self, root: Path, ws=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rename_dialog")
        self.setWindowTitle("Rename experiment")
        self._root = Path(root)

        self._field = QLineEdit(self._root.name, self)
        self._field.setObjectName("rename_name")
        self._folder = QLabel("", self)
        self._folder.setObjectName("rename_folder")
        self._folder.setWordWrap(True)
        self._reason = QLabel("", self)
        self._reason.setObjectName("rename_reason")
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color: {TONE_COLOR[Tone.ERR]};")
        self._refit = QLabel(REFIT_LINE, self)
        self._refit.setObjectName("rename_refit")
        self._refit.setWordWrap(True)
        self._refit.setStyleSheet(f"color: {TONE_COLOR[Tone.WARN]};")
        self._refit.setVisible(refits_after_rename(ws))
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name:", self))
        layout.addWidget(self._field)
        layout.addWidget(self._folder)
        layout.addWidget(self._reason)
        layout.addWidget(self._refit)
        layout.addWidget(self._buttons)
        self._field.textChanged.connect(self._update)
        self._update()

    def _update(self, *_args) -> None:
        clean = clean_experiment_name(self._field.text())
        target = self._root.parent / clean if clean else None
        self._folder.setText(f"Folder: {target}" if target is not None else "Folder: —")
        reason = ""
        if not clean:
            reason = "enter a name with letters or digits"
        elif str(target) != str(self._root) and target.exists():
            try:
                same = os.path.samefile(target, self._root)   # a case-only change is the same folder
            except OSError as exc:
                log.warning("rename dialog: cannot compare %s with %s: %s", target, self._root, exc)
                same = False
            if not same:
                reason = f"a folder named {clean} already exists in {self._root.parent}"
        self._reason.setText(reason)
        self._reason.setVisible(bool(reason))
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not reason)

    def name(self) -> str:
        return self._field.text()

    @classmethod
    def ask(cls, root: Path, ws=None, parent: QWidget | None = None) -> str | None:
        """Show the dialog; the typed name, or ``None`` when cancelled."""
        dialog = cls(root, ws, parent)
        return dialog.name() if dialog.exec() == QDialog.DialogCode.Accepted else None

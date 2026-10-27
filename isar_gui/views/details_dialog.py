"""DetailsDialog -- *Details…*: the experiment's name and folder, and its optional descriptive fields.

The name is shown with *Rename…* (the window's rename), the folder path is shown and selectable but not
editable, and six optional fields (title, proposal, sample, people, dates, notes) are kept with the
experiment in the intake backend's settings record. No fit reads them and none is required. The
resolution-curve line with its chooser acts at once, outside the fields' *Save details*.

*Save details* sends all six fields (an empty one as ``""``) plus every other key the record already
holds, unchanged, so a field another tool stored is never dropped. A refused save keeps the dialog open
with the reason under the fields.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.setup_section import LOCATE_LONG, LOCATE_TIP, locate_resolution_folder, resolution_text

# The descriptive fields: (key, label). The keys are this GUI's (the backend stores them as given).
FIELDS = (("title", "Title"), ("proposal", "Proposal"), ("sample", "Sample"), ("people", "People"),
          ("dates", "Dates"), ("notes", "Notes"))
_PLACEHOLDERS = {"people": "names, separated by commas", "dates": "e.g. 2026-11-02 to 2026-11-06"}
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"


class DetailsDialog(QDialog):
    rename_requested = Signal()

    def __init__(self, intake, name: str, folder: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("details_dialog")
        self.setWindowTitle("Experiment details")
        self._intake = intake

        self._name = ElidedLabel(name, self)
        self._name.setObjectName("details_name")
        rename = QPushButton("Rename…", self)
        rename.setObjectName("details_rename")
        rename.setToolTip("Rename the experiment and its folder together")
        rename.clicked.connect(self.rename_requested)
        name_row = QHBoxLayout()
        name_row.addWidget(self._name, 1)
        name_row.addWidget(rename, 0)
        self._folder = QLineEdit(str(folder), self)
        self._folder.setObjectName("details_folder")
        self._folder.setReadOnly(True)
        self._folder.setToolTip(str(folder))

        self._fields: dict[str, QLineEdit | QPlainTextEdit] = {}
        metadata = {} if intake.settings is None else intake.settings.metadata
        form = QFormLayout()
        form.addRow("Name", name_row)
        form.addRow("Folder", self._folder)
        optional = QLabel("Optional — kept with the experiment, never used by a fit:", self)
        optional.setWordWrap(True)
        form.addRow(optional)
        for key, label in FIELDS:
            if key == "notes":
                field = QPlainTextEdit(self)
                field.setPlainText(metadata.get(key, ""))
                field.setFixedHeight(64)
            else:
                field = QLineEdit(metadata.get(key, ""), self)
                field.setPlaceholderText(_PLACEHOLDERS.get(key, ""))
            field.setObjectName(f"details_{key}")
            field.setAccessibleName(label)
            self._fields[key] = field
            form.addRow(label, field)

        self._resolution = ElidedLabel("", self)
        self._resolution.setObjectName("details_resolution")
        locate = QPushButton(LOCATE_LONG, self)
        locate.setObjectName("details_locate")
        locate.setToolTip(LOCATE_TIP)
        locate.clicked.connect(self._locate)
        res_row = QHBoxLayout()
        res_row.addWidget(self._resolution, 1)
        res_row.addWidget(locate, 0)
        form.addRow("Resolution curves", res_row)

        self._message = QLabel("", self)
        self._message.setObjectName("details_message")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(_WARN)
        self._message.hide()
        buttons = QDialogButtonBox(self)
        self._save = buttons.addButton("Save details", QDialogButtonBox.ButtonRole.AcceptRole)
        self._save.setObjectName("details_save")
        self._save.setDefault(True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._save.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._message)
        layout.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

        intake.changed.connect(self._on_intake_changed)
        self._on_intake_changed()

    def fields(self) -> dict[str, str]:
        """The six fields as typed (an empty one as ``""``)."""
        return {key: (f.toPlainText() if isinstance(f, QPlainTextEdit) else f.text())
                for key, f in self._fields.items()}

    def save(self) -> None:
        """*Save details*: close on success, else show the reason and stay open."""
        settings = self._intake.settings
        kept = {} if settings is None else dict(settings.metadata)    # read now: a later key is never dropped
        outcome = self._intake.save_details({**kept, **self.fields()})
        self._show(outcome)
        if outcome.ok:
            self.accept()

    def _locate(self) -> None:
        self._show(locate_resolution_folder(self, self._intake))

    def _show(self, outcome) -> None:
        refused = outcome is not None and not outcome.ok
        self._message.setText(outcome.message if refused else "")
        self._message.setVisible(refused)

    def _on_intake_changed(self) -> None:
        # A rename re-binds the intake to the new folder while the dialog is open: follow it.
        root = self._intake.root
        if root is not None:
            self._name.set_full_text(Path(root).name)
            self._folder.setText(str(root))
            self._folder.setToolTip(str(root))
        text, _warn = resolution_text(self._intake.summary, self._intake.settings)
        self._resolution.set_full_text(text or "not chosen yet")

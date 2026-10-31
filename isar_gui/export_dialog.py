"""Export choices; global formats and experiment-specific destinations live in Session."""
from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from isar.workspace_api.export import ExportOptions, check_destination
from isar_gui import file_dialogs

log = logging.getLogger(__name__)


def experiment_key(root):
    return os.path.normcase(str(Path(root).resolve()))


class ExportDialog(QDialog):
    def __init__(self, root, view_state, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.request = None
        self.setWindowTitle("Export results")
        self.setMinimumWidth(560)
        self._generated_name = f"{self.root.name}-export-{datetime.now():%Y%m%d-%H%M%S}"
        self._name_edited = False
        preferences = view_state.get("export_options", {})
        try:
            options = ExportOptions.from_dict(preferences)
        except (ValueError, TypeError) as exc:
            log.warning("Invalid saved export choices; using defaults: %s", exc)
            options = ExportOptions()
        locations = view_state.get("export_destinations", {})
        saved = locations.get(experiment_key(root), {}) if isinstance(locations, dict) else {}
        self._custom_name = saved.get("name")
        layout = QVBoxLayout(self)
        intro = QLabel("Export all spectra in this experiment. Fits use the current authoritative model.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.choices = {}

        def checkbox(key, title, tip, box_layout):
            control = QCheckBox(title)
            control.setToolTip(tip)
            control.setChecked(getattr(options, key))
            box_layout.addWidget(control)
            self.choices[key] = control
            control.toggled.connect(self._update_enabled)
            return control

        content = QGroupBox("Include")
        content_layout = QVBoxLayout(content)
        checkbox("spectra", "Measured spectra — one file per scan/analyzer",
                 "Save every spectrum, including unfitted spectra. Excluded points retain their inclusion flag.",
                 content_layout)
        checkbox("curves", "Fit curves — one file per scan/analyzer",
                 "Save the authoritative total and every component on a separate fine energy grid. No refitting.",
                 content_layout)
        checkbox("parameters_separate", "Whole-model parameters — separate file per scan/analyzer",
                 "Readable values, units and available uncertainties for all model components.", content_layout)
        checkbox("parameters_combined", "Whole-model parameters — combined file for all scans",
                 "Put all authoritative model parameters into one table for comparison across scans.", content_layout)
        layout.addWidget(content)
        formats = QGroupBox("File formats — select either or both")
        formats_layout = QHBoxLayout(formats)
        checkbox("csv", "Neutral CSV", "Standard CSV with one header row and units in the column names.", formats_layout)
        checkbox("origin", "Origin", "CSV with column names, units, comments and X/Y/error designations; import instructions included.", formats_layout)
        layout.addWidget(formats)
        form = QFormLayout()
        self.scale = QComboBox()
        self.scale.addItem("Raw counts", "raw")
        self.scale.addItem("Monitor-normalized intensity", "normalised")
        self.scale.setCurrentIndex(self.scale.findData(options.scale))
        self.scale.setToolTip("One scale for this export. Normalized intensity is counts per monitor count; fit amplitudes use the same scale.")
        form.addRow("Intensity scale", self.scale)
        self.sides = QComboBox()
        self.sides.addItem("Combine each mode's Stokes and anti-Stokes", False)
        self.sides.addItem("Separate Stokes and anti-Stokes curves", True)
        self.sides.setCurrentIndex(int(options.split_sides))
        self.sides.setToolTip("Always export the total and every element. This choice only changes each mode's component columns.")
        form.addRow("Mode curves", self.sides)
        layout.addLayout(form)
        destination = QFormLayout()
        self.folder_name = QLineEdit(self._custom_name or self._generated_name)
        self.folder_name.setToolTip("Name of the export folder. An edited name is remembered for this experiment; automatic names get a new timestamp.")
        self.folder_name.textEdited.connect(self._edited_name)
        self.folder_path = QLineEdit(saved.get("parent") or str(self.root / "report" / "exports"))
        self.folder_path.setToolTip("Parent folder in which the export folder will be created. Remembered separately for each experiment.")
        destination.addRow("Export folder name", self.folder_name)
        destination.addRow("Save in folder", self.folder_path)
        layout.addLayout(destination)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_here = QPushButton("Save here")
        self.save_here.setToolTip("Export using the folder path and name shown above.")
        self.save_here.setDefault(True)
        self.save_here.clicked.connect(self._save)
        self.save_elsewhere = QPushButton("Save elsewhere")
        self.save_elsewhere.setToolTip("Choose a parent folder, then save this export using the folder name above.")
        self.save_elsewhere.clicked.connect(self._elsewhere)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Close without exporting or changing saved export choices.")
        cancel.clicked.connect(self.reject)
        for button in (self.save_here, self.save_elsewhere, cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._update_enabled()

    def _edited_name(self, _text):
        self._name_edited = True

    def _update_enabled(self):
        if hasattr(self, "sides"):
            self.sides.setEnabled(self.choices["curves"].isChecked())

    def options(self):
        return ExportOptions(**{key: box.isChecked() for key, box in self.choices.items()},
                             scale=self.scale.currentData(), split_sides=self.sides.currentData())

    def _elsewhere(self):
        parent = self.parentWidget() or self
        folder = file_dialogs.get_existing_directory(parent, "Choose export parent folder", self.folder_path.text())
        if folder:
            self.folder_path.setText(folder)
            self._save()

    def _save(self):
        try:
            options = self.options()
            name = self.folder_name.text().strip()
            if (not name or name in (".", "..") or any(c in name for c in '<>:"/\\|?*')
                    or name.endswith((".", " ")) or any(ord(c) < 32 for c in name)
                    or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL",
                                                       *[f"COM{i}" for i in range(1, 10)],
                                                       *[f"LPT{i}" for i in range(1, 10)]}):
                raise ValueError("Enter a valid export folder name, without path separators")
            parent = Path(self.folder_path.text().strip()).expanduser()
            if not parent.is_absolute():
                raise ValueError("Enter an absolute parent-folder path")
            target = check_destination(self.root, parent / name, overwrite=True)
            overwrite = target.exists()
            if overwrite and QMessageBox.question(
                    self, "Replace saved export?", f"Replace the existing export folder and its contents?\n\n{target}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.request = {"options": options.to_dict(), "destination": str(target), "overwrite": overwrite}
        except (ValueError, OSError) as exc:
            log.info("Export choices need correction: %s", exc)
            self.error.setText(str(exc))
            return
        self.accept()

    def save_preferences(self, view_state):
        if self.request is None:
            return
        target = Path(self.request["destination"])
        view_state["export_options"] = self.request["options"]
        custom = (target.name if self._name_edited or target.name != self._generated_name else None)
        view_state.setdefault("export_destinations", {})[experiment_key(self.root)] = {
            "parent": str(target.parent), "name": custom}

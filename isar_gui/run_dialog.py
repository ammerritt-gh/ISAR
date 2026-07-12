"""RunDialog — pick the data folder and the ``isar run`` options for the Run action.

A thin form over :class:`~isar_gui.commands.RunOptions`. It chooses *what to run on* and *how*; the
window turns the result into an ``isar run`` subprocess (the engine, never re-implemented). Sensible
defaults are pre-filled and the last-used values are remembered by the window across runs.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from isar_gui.commands import RunOptions

_ERRORS = ["full", "fast", "cheap", "bayes"]


class RunDialog(QDialog):
    """Data path + a few ``isar run`` knobs. Exposes :attr:`data_paths` and :attr:`options`."""

    def __init__(self, *, data: str = "", opts: RunOptions | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run analysis")
        self.setMinimumWidth(520)
        opts = opts or RunOptions()

        outer = QVBoxLayout(self)
        form = QFormLayout()

        self._data_edit = QLineEdit(data)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        data_row = QWidget()
        dl = QHBoxLayout(data_row)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self._data_edit)
        dl.addWidget(browse)
        form.addRow("Data folder or file:", data_row)

        self._errors = QComboBox()
        self._errors.addItems(_ERRORS)
        self._errors.setCurrentText(opts.errors if opts.errors in _ERRORS else "full")
        form.addRow("Uncertainty tier:", self._errors)

        self._M = QSpinBox()
        self._M.setRange(1, 100000)
        self._M.setValue(opts.M)
        form.addRow("Bootstrap draws (M):", self._M)

        self._detectors = QLineEdit(opts.detectors or "")
        self._detectors.setPlaceholderText("all (card default) — or e.g. ana5,ana7")
        form.addRow("Detectors:", self._detectors)

        self._llm = QCheckBox("LLM adjudication of QC flags (--llm)")
        self._llm.setChecked(opts.llm)
        form.addRow("", self._llm)
        self._force = QCheckBox("Refit every scan, ignore the ledger (--force)")
        self._force.setChecked(opts.force)
        form.addRow("", self._force)

        outer.addLayout(form)

        self._run_btn = QPushButton("Run")
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self._run_btn)
        btns.addWidget(cancel)
        outer.addLayout(btns)

    def _browse(self) -> None:
        start = self._data_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select data folder", start)
        if path:
            self._data_edit.setText(path)

    @property
    def data_paths(self) -> list[str]:
        text = self._data_edit.text().strip()
        return [text] if text else []

    @property
    def options(self) -> RunOptions:
        det = self._detectors.text().strip() or None
        return RunOptions(llm=self._llm.isChecked(), force=self._force.isChecked(),
                          M=self._M.value(), errors=self._errors.currentText(), detectors=det)

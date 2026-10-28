"""RunDialog — pick the data folder and the ``isar run`` options for the Run action.

A thin form over :class:`~isar_gui.commands.RunOptions`. It chooses *what to run on* and *how*; the
window turns the result into an ``isar run`` subprocess (the engine, never re-implemented). Sensible
defaults are pre-filled and the last-used values are remembered by the window across runs.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from isar_gui.commands import RunOptions

_ERRORS = ["full", "fast", "cheap", "bayes"]
_ERROR_TIPS = {
    "full": "Adds MINOS, bootstrap and profile intervals to the chosen model. Slower.",
    "fast": "The delta-method interval on the peak position and its trust flag, no bootstrap. The default for triage.",
    "cheap": "Covariance errors only.",
    "bayes": "Adds the nested-sampling interval. Slowest.",
}


NO_DATA_REASON = "Choose the folder your data files were read from."


class RunDialog(QDialog):
    """Data path + a few ``isar run`` knobs. Exposes :attr:`data_paths` and :attr:`options`.

    ``data`` holds one path, or several joined with the platform's path separator (``;`` on Windows). With no
    path Run is off and the dialog says why; it never closes on a run that has nothing to run on."""

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
        self._data_edit.setToolTip("The folder (or file) the analysis reads. Several are separated by "
                                   f"'{os.pathsep}'. Filled in from where this experiment's data were read from.")
        form.addRow("Data folder or file:", data_row)

        self._errors = QComboBox()
        self._errors.addItems(_ERRORS)
        self._errors.setCurrentText(opts.errors if opts.errors in _ERRORS else "fast")
        self._errors.setToolTip("What is attached to the fitted model after the peak-count search: pick fast for "
                                "triage. Hover an entry for what it adds.")
        for i, name in enumerate(_ERRORS):
            self._errors.setItemData(i, _ERROR_TIPS[name], Qt.ItemDataRole.ToolTipRole)
        form.addRow("Error estimate:", self._errors)

        self._M = QSpinBox()
        self._M.setRange(1, 100000)
        self._M.setValue(opts.M)
        self._M.setToolTip("At most this many bootstrap resamples for pooled masters and certification "
                           "(used by the full error estimate).")
        form.addRow("Bootstrap resamples (M):", self._M)

        self._detectors = QLineEdit(opts.detectors or "")
        self._detectors.setPlaceholderText("all (card default) — or e.g. a03,a05")
        self._detectors.setToolTip("Only the named analyzers are fitted this run; blank = all.")
        form.addRow("Only these analyzers:", self._detectors)

        self._llm = QCheckBox("Ask the LLM to review QC flags (optional)")
        self._llm.setToolTip("Let the LLM service review the QC flags. Optional; it needs a reachable LLM service.")
        self._llm.setChecked(opts.llm)
        form.addRow("", self._llm)
        self._force = QCheckBox("Refit every scan, even unchanged ones")
        self._force.setToolTip("Ignore the record of finished fits and fit every spectrum again.")
        self._force.setChecked(opts.force)
        form.addRow("", self._force)

        outer.addLayout(form)

        self._reason = QLabel(NO_DATA_REASON, self)           # why Run is off, as text
        self._reason.setObjectName("run_dialog_reason")
        self._reason.setStyleSheet("color: #b7791f;")
        outer.addWidget(self._reason)
        self._run_btn = QPushButton("Run")
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self.accept)
        self._data_edit.textChanged.connect(self._sync_run)
        self._sync_run()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self._run_btn)
        btns.addWidget(cancel)
        outer.addLayout(btns)

    def _sync_run(self) -> None:
        has_data = bool(self.data_paths)
        self._run_btn.setEnabled(has_data)
        self._reason.setVisible(not has_data)

    def _browse(self) -> None:
        start = next(iter(self.data_paths), str(Path.home()))
        path = QFileDialog.getExistingDirectory(self, "Select data folder", start)
        if path:
            self._data_edit.setText(path)

    @property
    def data_paths(self) -> list[str]:
        return [p.strip() for p in self._data_edit.text().split(os.pathsep) if p.strip()]

    @property
    def options(self) -> RunOptions:
        det = self._detectors.text().strip() or None
        return RunOptions(llm=self._llm.isChecked(), force=self._force.isChecked(),
                          M=self._M.value(), errors=self._errors.currentText(), detectors=det)

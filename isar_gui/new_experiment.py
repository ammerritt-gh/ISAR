"""NewExperimentDialog — the operator front door (D4).

*An experiment is your pile of data files, wrapped.* The operator drops a data folder (or a single
numor) onto the ISAR window, or picks File ▸ New Experiment…; this dialog previews the instrument
ISAR detects (by shelling ``isar detect`` — never importing the engine, G16), suggests a workspace
directory next to the data, and — on Create — shells ``isar init`` to scaffold the workspace. On
success it exposes :attr:`workspace_path` and accepts; the caller then opens it.

The dialog never requires the operator to already know what a "workspace" is (D2): the language is
plain, detection is automatic, and the only decision is *where to put it* (pre-filled).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui import commands
from isar_gui.commands import DetectResult
from isar_gui.process import ProcessRunner

_INTRO = ("An experiment is your pile of data files, wrapped. Pick the folder your data lands in — "
          "ISAR detects the instrument and sets up the rest.")


class NewExperimentDialog(QDialog):
    """Pick data → preview instrument → confirm workspace dir → scaffold via ``isar init``."""

    def __init__(self, prefill: str | None = None, *, python: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Experiment")
        self.setMinimumWidth(560)
        self._python = python or sys.executable
        self._workspace_path: str | None = None
        self._pending_out: str | None = None
        self._detect: DetectResult | None = None
        self._init_runner: ProcessRunner | None = None
        self._detect_runner: ProcessRunner | None = None
        self._detect_buf: list[str] = []
        self._init_lines: list[str] = []  # streamed init output, buffered to find the failure cause

        outer = QVBoxLayout(self)
        intro = QLabel(_INTRO)
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # --- data path row --------------------------------------------------
        outer.addWidget(QLabel("Data folder or file:"))
        self._data_edit = QLineEdit(prefill or "")
        data_browse = QPushButton("Browse…")
        data_browse.clicked.connect(self._browse_data)
        outer.addLayout(_row(self._data_edit, data_browse))
        # Re-detect when the operator finishes typing/pastes a path (not per keystroke: detection is
        # a subprocess that walks the directory).
        self._data_edit.editingFinished.connect(self._refresh_detection)

        # --- detection preview ---------------------------------------------
        self._detect_label = QLabel("")
        self._detect_label.setWordWrap(True)
        outer.addWidget(self._detect_label)

        # --- workspace dir row ---------------------------------------------
        outer.addWidget(QLabel("Create the experiment here:"))
        self._ws_edit = QLineEdit("")
        ws_browse = QPushButton("Browse…")
        ws_browse.clicked.connect(self._browse_ws)
        outer.addLayout(_row(self._ws_edit, ws_browse))

        # --- init log (shown once Create runs) ------------------------------
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setVisible(False)
        # This dialog has its own path fields; a dropped file must never be pasted into the log.
        self._log.setAcceptDrops(False)
        outer.addWidget(self._log)

        # --- buttons --------------------------------------------------------
        self._create_btn = QPushButton("Create")
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._on_create)
        self._create_btn.setEnabled(False)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        outer.addLayout(_row(_stretch(), self._create_btn, cancel))

        # Detect on a dropped/prefilled path once the event loop settles (never blocks construction;
        # tests that don't pump the loop won't spawn the subprocess).
        if prefill:
            QTimer.singleShot(0, self._refresh_detection)

    # ---- public surface ---------------------------------------------------
    @property
    def workspace_path(self) -> str | None:
        """The scaffolded workspace directory, set once ``isar init`` succeeds; else ``None``."""
        return self._workspace_path

    def planned_command(self) -> list[str] | None:
        """The ``isar init`` argv this dialog would run for the current inputs (None if incomplete)."""
        data = self._data_edit.text().strip()
        out = self._ws_edit.text().strip()
        if not data or not out:
            return None
        return commands.build_init_command(self._python, data, out)

    # ---- detection (async: shell `isar detect --json`) --------------------
    def _refresh_detection(self) -> None:
        data = self._data_edit.text().strip()
        if not data:
            self._apply_detect(None, blank=True)
            return
        self._detect_label.setText("detecting…")
        self._detect_buf = []
        self._detect_runner = ProcessRunner(self)
        self._detect_runner.line.connect(self._detect_buf.append)
        self._detect_runner.finished.connect(self._on_detect_finished)
        self._detect_runner.start(commands.build_detect_command(self._python, data))

    def _on_detect_finished(self, _code: int) -> None:
        self._apply_detect(commands.parse_detect_json("\n".join(self._detect_buf)))

    def _apply_detect(self, detect: DetectResult | None, *, blank: bool = False) -> None:
        """Synchronous core: reflect a detection result into the labels/defaults/enablement.

        Separated from the subprocess so it is unit-testable without launching ``isar detect``.
        """
        self._detect = detect
        if blank:
            self._detect_label.setText("")
        elif detect is None:
            self._detect_label.setText("⚠ Could not detect an instrument here — check the path "
                                       "points at raw data files.")
        elif not detect.supported:
            self._detect_label.setText(f"⚠ Detected {detect.facility}/{detect.instrument} "
                                       f"({detect.fmt}), but no card template ships for it yet.")
        else:
            probe = f" from {detect.probe}" if detect.probe else ""
            self._detect_label.setText(f"✓ Detected {detect.facility}/{detect.instrument} "
                                       f"(format {detect.fmt}){probe}.")
            if not self._ws_edit.text().strip():
                self._ws_edit.setText(commands.default_workspace_dir(
                    self._data_edit.text().strip(), detect.instrument))
        self._sync_create_enabled()

    def _sync_create_enabled(self) -> None:
        busy = self._init_runner is not None and self._init_runner.is_running()
        ok = bool(self._detect and self._detect.supported and self._ws_edit.text().strip())
        self._create_btn.setEnabled(ok and not busy)

    # ---- browse -----------------------------------------------------------
    def _browse_data(self) -> None:
        # Offer a directory picker (the common case); a single-file experiment can be typed/pasted.
        from pathlib import Path

        start = self._data_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select data folder", start)
        if path:
            self._data_edit.setText(path)
            self._refresh_detection()

    def _browse_ws(self) -> None:
        from pathlib import Path

        start = self._ws_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Where to create the experiment", start)
        if path:
            self._ws_edit.setText(path)
            self._sync_create_enabled()

    # ---- create (shell isar init) -----------------------------------------
    def _on_create(self) -> None:
        cmd = self.planned_command()
        if cmd is None:
            return
        self._pending_out = self._ws_edit.text().strip()
        self._init_lines = []
        self._apply_detect(self._detect)  # clear a stale failure message from a previous attempt
        self._log.setVisible(True)
        self._log.appendPlainText("$ " + " ".join(cmd))
        self._create_btn.setEnabled(False)
        self._init_runner = ProcessRunner(self)
        self._init_runner.line.connect(self._on_init_line)
        self._init_runner.finished.connect(self._on_init_finished)
        self._init_runner.start(cmd)

    def _on_init_line(self, line: str) -> None:
        self._init_lines.append(line)
        self._log.appendPlainText(line)

    def _on_init_finished(self, code: int) -> None:
        if code == 0:
            self._workspace_path = self._pending_out
            self.accept()
        else:
            cause = commands.last_error_line(self._init_lines)
            detail = cause if cause else "see log above"
            msg = f"isar init failed (exit {code}): {detail}"
            self._log.appendPlainText("\n" + msg)
            self._detect_label.setText(msg)
            self._sync_create_enabled()


# ---- tiny layout helpers ---------------------------------------------------
def _row(*widgets) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        if isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addItem(w)
    return lay


def _stretch():
    from PySide6.QtWidgets import QSizePolicy, QSpacerItem
    return QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

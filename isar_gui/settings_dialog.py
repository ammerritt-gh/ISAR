"""SettingsDialog — LLM backend profile + TAVI launcher path (D3).

LLM flexibility is a hard requirement: other machines will not run LLMM, so the endpoint is a
configurable OpenAI-compatible target with LLMM as the *default profile*, never an assumption. The
settings live in the GUI session (the window persists them) and are handed to ``isar run`` as
``ISAR_LLM_*`` env overrides; this dialog only edits them and offers a reachability test.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.health import LLM_DEFAULT_ENDPOINT, ping_http

_PROFILES = ["llmm", "openai"]


class SettingsDialog(QDialog):
    """Edit the LLM profile/endpoint/model/key and the TAVI launcher path. Exposes :attr:`settings`."""

    def __init__(self, settings: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backend Settings")
        self.setMinimumWidth(560)
        s = dict(settings or {})

        outer = QVBoxLayout(self)
        note = QLabel("LLM adjudication is optional. LLMM is the default profile; point 'openai' at "
                      "any OpenAI-compatible server. Leave the model blank to use the card default.")
        note.setWordWrap(True)
        outer.addWidget(note)

        form = QFormLayout()
        self._profile = QComboBox()
        self._profile.addItems(_PROFILES)
        self._profile.setCurrentText(s.get("profile", "llmm") if s.get("profile") in _PROFILES
                                     else "llmm")
        form.addRow("LLM profile:", self._profile)

        self._endpoint = QLineEdit(s.get("llm_endpoint", LLM_DEFAULT_ENDPOINT))
        form.addRow("LLM endpoint:", self._endpoint)
        self._model = QLineEdit(s.get("llm_model", ""))
        self._model.setPlaceholderText("card default — or e.g. gpt-oss-120b")
        form.addRow("LLM model:", self._model)
        self._api_key = QLineEdit(s.get("llm_api_key", ""))
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("only for openai-compatible endpoints that require a key")
        form.addRow("API key:", self._api_key)

        tavi_row = QWidget()
        tl = QHBoxLayout(tavi_row)
        tl.setContentsMargins(0, 0, 0, 0)
        self._tavi_bat = QLineEdit(s.get("tavi_bat", ""))
        self._tavi_bat.setPlaceholderText("path to TAVI's run-tavi-dev.bat (auto-detected if blank)")
        tavi_browse = QPushButton("Browse…")
        tavi_browse.clicked.connect(self._browse_tavi)
        tl.addWidget(self._tavi_bat)
        tl.addWidget(tavi_browse)
        form.addRow("TAVI launcher:", tavi_row)
        outer.addLayout(form)

        test_row = QHBoxLayout()
        test_btn = QPushButton("Test connection")
        test_btn.clicked.connect(self._test)
        self._test_label = QLabel("")
        test_row.addWidget(test_btn)
        test_row.addWidget(self._test_label)
        test_row.addStretch(1)
        outer.addLayout(test_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton("Save")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        outer.addLayout(buttons)

    def _browse_tavi(self) -> None:
        start = self._tavi_bat.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Select run-tavi-dev.bat", start,
                                              "Batch files (*.bat)")
        if path:
            self._tavi_bat.setText(path)

    def _test(self) -> None:
        endpoint = self._endpoint.text().strip() or LLM_DEFAULT_ENDPOINT
        self._test_label.setText("testing…")
        h = ping_http(endpoint)
        if h.ok:
            self._test_label.setText(f"✓ {endpoint} — {h.detail}")
            self._test_label.setStyleSheet("color: #2e7d32;")
        else:
            self._test_label.setText(f"✗ {endpoint} — {h.detail}")
            self._test_label.setStyleSheet("color: #c62828;")

    @property
    def settings(self) -> dict:
        return {
            "profile": self._profile.currentText(),
            "llm_endpoint": self._endpoint.text().strip(),
            "llm_model": self._model.text().strip(),
            "llm_api_key": self._api_key.text(),
            "tavi_bat": self._tavi_bat.text().strip(),
        }

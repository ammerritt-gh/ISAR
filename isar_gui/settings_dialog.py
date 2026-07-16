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
    QCheckBox,
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
from isar.workspace_api import DEFAULT_CLOUD_FALLBACK_MODEL, validate_endpoint

_PROFILES = ["llmm", "openai"]


class SettingsDialog(QDialog):
    """Edit non-secret backend settings and request a credential-store key change.

    ``settings`` never contains an API key.  A blank password field means no credential change;
    the parent owns the native credential operation after the dialog validates its endpoint.
    """

    def __init__(self, settings: dict | None = None, *, api_key_present: bool = False,
                 credential_storage_available: bool = True, parent=None):
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
        self._model.setPlaceholderText("card default — or e.g. deepseek/deepseek-v4-flash")
        form.addRow("LLM model:", self._model)
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("enter a new key to replace the stored key")
        self._clear_api_key = False
        self._clear_key = QPushButton("Clear stored key")
        self._clear_key.clicked.connect(self._request_key_clear)
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self._api_key)
        key_layout.addWidget(self._clear_key)
        key_state = "stored securely" if api_key_present else "not stored"
        if not credential_storage_available:
            key_state = "not persisted (Windows Credential Manager is unavailable)"
            self._api_key.setEnabled(False)
            self._clear_key.setEnabled(False)
        self._key_status = QLabel(key_state)
        form.addRow("API key:", key_row)
        form.addRow("Key status:", self._key_status)

        self._cloud_fallback = QCheckBox(
            f"Allow cloud fallback: {DEFAULT_CLOUD_FALLBACK_MODEL}"
        )
        self._cloud_fallback.setChecked(bool(s.get("cloud_fallback_enabled", False)))
        self._cloud_fallback.setToolTip(
            "When enabled, adjudication data may leave this computer for the named cloud service."
        )
        form.addRow("Cloud fallback:", self._cloud_fallback)
        cloud_note = QLabel(
            "Off by default. Enabling this sends adjudication context to "
            f"{DEFAULT_CLOUD_FALLBACK_MODEL}."
        )
        cloud_note.setWordWrap(True)
        form.addRow("", cloud_note)

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
        try:
            endpoint = validate_endpoint(self._endpoint.text().strip() or LLM_DEFAULT_ENDPOINT)
        except ValueError as exc:
            self._test_label.setText(f"✗ {exc}")
            self._test_label.setStyleSheet("color: #c62828;")
            return
        self._test_label.setText("testing…")
        h = ping_http(endpoint)
        if h.ok:
            self._test_label.setText(f"✓ {endpoint} — {h.detail}")
            self._test_label.setStyleSheet("color: #2e7d32;")
        elif h.reachable:
            self._test_label.setText(f"! {endpoint} — {h.detail}")
            self._test_label.setStyleSheet("color: #b26a00;")
        else:
            self._test_label.setText(f"✗ {endpoint} — {h.detail}")
            self._test_label.setStyleSheet("color: #c62828;")

    def _request_key_clear(self) -> None:
        self._api_key.clear()
        self._clear_api_key = True
        self._key_status.setText("will be cleared when settings are saved")

    def accept(self) -> None:
        try:
            endpoint = validate_endpoint(self._endpoint.text().strip() or LLM_DEFAULT_ENDPOINT)
        except ValueError as exc:
            self._test_label.setText(f"✗ {exc}")
            self._test_label.setStyleSheet("color: #c62828;")
            return
        self._endpoint.setText(endpoint)
        super().accept()

    @property
    def api_key_change(self) -> str | None:
        """A replacement key, ``""`` for Clear, or ``None`` for a blank/no-op field."""
        replacement = self._api_key.text()
        if replacement:
            return replacement
        return "" if self._clear_api_key else None

    @property
    def settings(self) -> dict:
        return {
            "profile": self._profile.currentText(),
            "llm_endpoint": self._endpoint.text().strip(),
            "llm_model": self._model.text().strip(),
            "cloud_fallback_enabled": self._cloud_fallback.isChecked(),
            "cloud_fallback_model": DEFAULT_CLOUD_FALLBACK_MODEL,
            "tavi_bat": self._tavi_bat.text().strip(),
        }

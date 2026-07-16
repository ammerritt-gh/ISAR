"""SettingsDialog: round-trips settings, Test connection reflects reachability."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar_gui import settings_dialog
from isar_gui.settings_dialog import SettingsDialog


def test_defaults_to_llmm(qtbot):
    dlg = SettingsDialog({})
    qtbot.addWidget(dlg)
    s = dlg.settings
    assert s["profile"] == "llmm"
    assert s["llm_endpoint"].startswith("http://127.0.0.1:8766")


def test_round_trips_provided_non_secret_settings(qtbot):
    given = {"profile": "openai", "llm_endpoint": "https://example.test/v1", "llm_model": "gpt-oss",
             "cloud_fallback_enabled": True, "tavi_bat": r"C:\tavi\run-tavi-dev.bat"}
    dlg = SettingsDialog(given, api_key_present=True)
    qtbot.addWidget(dlg)
    from isar.workspace_api import DEFAULT_CLOUD_FALLBACK_MODEL

    assert {key: dlg.settings[key] for key in given} == given
    assert dlg.settings["cloud_fallback_model"] == DEFAULT_CLOUD_FALLBACK_MODEL
    assert dlg.api_key_change is None
    assert "stored securely" in dlg._key_status.text()


def test_key_field_never_round_trips_a_secret(qtbot):
    dlg = SettingsDialog({"llm_api_key": "old-secret"}, api_key_present=True)
    qtbot.addWidget(dlg)
    assert dlg._api_key.text() == ""
    dlg._api_key.setText("new-secret")
    assert dlg.api_key_change == "new-secret"
    assert "llm_api_key" not in dlg.settings


def test_clear_key_is_an_explicit_request(qtbot):
    dlg = SettingsDialog({}, api_key_present=True)
    qtbot.addWidget(dlg)
    dlg._request_key_clear()
    assert dlg.api_key_change == ""


def test_test_connection_reports_reachable(qtbot, monkeypatch):
    from isar_gui.health import Health
    monkeypatch.setattr(settings_dialog, "ping_http", lambda ep, **k: Health(True, "reachable"))
    dlg = SettingsDialog({"llm_endpoint": "http://127.0.0.1:8765"})
    qtbot.addWidget(dlg)
    dlg._test()
    assert "✓" in dlg._test_label.text()
    assert "2e7d32" in dlg._test_label.styleSheet()


def test_test_connection_reports_unreachable(qtbot, monkeypatch):
    from isar_gui.health import Health
    monkeypatch.setattr(settings_dialog, "ping_http", lambda ep, **k: Health(False, "unreachable"))
    dlg = SettingsDialog({"llm_endpoint": "http://127.0.0.1:8765"})
    qtbot.addWidget(dlg)
    dlg._test()
    assert "✗" in dlg._test_label.text()
    assert "c62828" in dlg._test_label.styleSheet()

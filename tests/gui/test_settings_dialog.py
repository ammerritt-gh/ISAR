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


def test_round_trips_provided_settings(qtbot):
    given = {"profile": "openai", "llm_endpoint": "http://h/v1", "llm_model": "gpt-oss",
             "llm_api_key": "sk-1", "tavi_bat": r"C:\tavi\run-tavi-dev.bat"}
    dlg = SettingsDialog(given)
    qtbot.addWidget(dlg)
    assert dlg.settings == given


def test_test_connection_reports_reachable(qtbot, monkeypatch):
    from isar_gui.health import Health
    monkeypatch.setattr(settings_dialog, "ping_http", lambda ep, **k: Health(True, "reachable"))
    dlg = SettingsDialog({"llm_endpoint": "http://ok"})
    qtbot.addWidget(dlg)
    dlg._test()
    assert "✓" in dlg._test_label.text()
    assert "2e7d32" in dlg._test_label.styleSheet()


def test_test_connection_reports_unreachable(qtbot, monkeypatch):
    from isar_gui.health import Health
    monkeypatch.setattr(settings_dialog, "ping_http", lambda ep, **k: Health(False, "unreachable"))
    dlg = SettingsDialog({"llm_endpoint": "http://down"})
    qtbot.addWidget(dlg)
    dlg._test()
    assert "✗" in dlg._test_label.text()
    assert "c62828" in dlg._test_label.styleSheet()

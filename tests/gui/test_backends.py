"""MainWindow backend wiring: settings persistence, LLM env injection, Launch TAVI, health apply."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QProcess

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


def _win(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    return w


def test_default_settings_are_llmm(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    s = w._settings()
    assert s["profile"] == "llmm"
    assert s["llm_endpoint"].startswith("http://127.0.0.1:8766")


def test_llm_env_only_exports_set_fields(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    # Default: model + key blank -> not exported; profile + endpoint always present.
    env = w._llm_env()
    assert env["ISAR_LLM_PROFILE"] == "llmm"
    assert "ISAR_LLM_ENDPOINT" in env
    assert "ISAR_LLM_MODEL" not in env and "ISAR_LLM_API_KEY" not in env


def test_llm_env_openai_profile(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    w._session.view_state["settings"] = {"profile": "openai", "llm_endpoint": "http://h/v1",
                                         "llm_model": "gpt-oss", "llm_api_key": "sk-1"}
    env = w._llm_env()
    assert env == {"ISAR_LLM_PROFILE": "openai", "ISAR_LLM_ENDPOINT": "http://h/v1",
                   "ISAR_LLM_MODEL": "gpt-oss", "ISAR_LLM_API_KEY": "sk-1"}


def test_launch_tavi_missing_sets_status(qtbot, make_ws, monkeypatch):
    w = _win(qtbot, make_ws)
    monkeypatch.setattr("isar_gui.main_window.find_tavi_bat", lambda configured: None)
    assert w._launch_tavi() is False
    assert "TAVI not found" in w.statusBar().currentMessage()


def test_launch_tavi_starts_detached(qtbot, make_ws, monkeypatch, tmp_path):
    w = _win(qtbot, make_ws)
    bat = str(tmp_path / "run-tavi-dev.bat")
    monkeypatch.setattr("isar_gui.main_window.find_tavi_bat", lambda configured: bat)
    calls = []
    monkeypatch.setattr(QProcess, "startDetached",
                        staticmethod(lambda prog, args: calls.append((prog, args)) or True))
    assert w._launch_tavi() is True
    assert calls and calls[0][0] == "cmd.exe" and bat in calls[0][1]


def test_apply_health_colours_and_tooltips(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    w._apply_health({"llm": (True, "reachable", "http://llm"),
                     "tavi": (False, "unreachable (URLError)", "http://tavi")})
    assert "2e7d32" in w._llm_health.styleSheet()      # green
    assert "c62828" in w._tavi_health.styleSheet()     # red
    assert "http://llm" in w._llm_health.toolTip()
    assert "unreachable" in w._tavi_health.toolTip()


def test_apply_health_ignores_error(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    before = w._llm_health.styleSheet()
    w._apply_health({"error": "boom"})
    assert w._llm_health.styleSheet() == before        # unchanged on a probe error


def test_refresh_health_threaded_updates_labels(qtbot, make_ws, monkeypatch):
    """The real threaded probe: worker thread -> queued signal -> label update on the Qt thread."""
    from isar_gui import main_window as mw
    from isar_gui.health import Health

    monkeypatch.setattr(mw, "ping_http", lambda url, **k: Health(True, "reachable"))
    w = _win(qtbot, make_ws)
    w._refresh_health()
    qtbot.waitUntil(lambda: "2e7d32" in w._llm_health.styleSheet(), timeout=4000)
    assert "2e7d32" in w._tavi_health.styleSheet()

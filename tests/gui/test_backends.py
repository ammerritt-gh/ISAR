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
    assert env["ISAR_LLM_FALLBACK_MODEL"] == ""


def test_llm_env_reads_credential_just_in_time(qtbot, make_ws, monkeypatch):
    import isar_gui.main_window as mw

    w = _win(qtbot, make_ws)
    w._session.view_state["settings"] = {"profile": "openai", "llm_endpoint": "https://example.test/v1",
                                         "llm_model": "gpt-oss", "cloud_fallback_enabled": True}
    monkeypatch.setattr(mw, "credential_storage_supported", lambda: True)
    monkeypatch.setattr(mw, "load_api_key", lambda endpoint: "sk-1")
    env = w._llm_env()
    from isar.workspace_api import DEFAULT_CLOUD_FALLBACK_MODEL

    assert env == {"ISAR_LLM_PROFILE": "openai", "ISAR_LLM_ENDPOINT": "https://example.test/v1",
                   "ISAR_LLM_MODEL": "gpt-oss", "ISAR_LLM_API_KEY": "sk-1",
                   "ISAR_LLM_FALLBACK_MODEL": DEFAULT_CLOUD_FALLBACK_MODEL}


def test_settings_migrate_the_retired_cloud_fallback_pin(qtbot, make_ws):
    # A settings dict saved before 2026-07-16 carries the retired gpt-oss-120b pin; left alone it
    # would silently override the repaired default forever. An explicit different pin survives.
    from isar.workspace_api import DEFAULT_CLOUD_FALLBACK_MODEL

    w = _win(qtbot, make_ws)
    w._session.view_state["settings"] = {"cloud_fallback_enabled": True,
                                         "cloud_fallback_model": "nanogpt/openai/gpt-oss-120b"}
    assert w._settings()["cloud_fallback_model"] == DEFAULT_CLOUD_FALLBACK_MODEL
    assert w._llm_env()["ISAR_LLM_FALLBACK_MODEL"] == DEFAULT_CLOUD_FALLBACK_MODEL

    w._session.view_state["settings"] = {"cloud_fallback_enabled": True,
                                         "cloud_fallback_model": "nanogpt/minimax/minimax-m3"}
    assert w._settings()["cloud_fallback_model"] == "nanogpt/minimax/minimax-m3"


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


def test_apply_health_marks_reachable_http_error_amber(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    w._apply_health({"llm": (False, True, "reachable (HTTP 401)", "http://llm"),
                     "tavi": (False, False, "unreachable (URLError)", "http://tavi")})
    assert "b26a00" in w._llm_health.styleSheet()
    assert "c62828" in w._tavi_health.styleSheet()
    assert "TAVI backend unreachable" in w.statusBar().currentMessage()


def test_apply_health_ignores_error(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    before = w._llm_health.styleSheet()
    w._apply_health({"error": "boom"})
    assert w._llm_health.styleSheet() == before        # unchanged on a probe error


def test_refresh_health_threaded_updates_labels(qtbot, make_ws, monkeypatch):
    """The worker result is queue-polled and updates labels only on the Qt thread."""
    from isar_gui import main_window as mw
    from isar_gui.health import Health

    monkeypatch.setattr(mw, "ping_http", lambda url, **k: Health(True, "reachable"))
    w = _win(qtbot, make_ws)
    w._refresh_health()
    qtbot.waitUntil(lambda: "2e7d32" in w._llm_health.styleSheet(), timeout=4000)
    assert "2e7d32" in w._tavi_health.styleSheet()


def test_refresh_health_probes_the_liveness_endpoints(qtbot, make_ws, monkeypatch):
    # Both backends 404 on their bare base by design — probing it rendered a RUNNING backend
    # amber (reachable_error) and, for TAVI, blocked campaign launch. Probe <base>/health.
    w = _win(qtbot, make_ws)
    captured = {}
    monkeypatch.setattr(w._health_probe, "request", lambda urls: captured.update(urls) or 1)
    w._refresh_health()
    assert captured["tavi"].endswith("/api/v1/health")
    assert captured["llm"].endswith("/health") and "//health" not in captured["llm"]


def test_health_poll_ignores_stale_result(qtbot, make_ws):
    from isar_gui.health import Health, HealthProbeResult

    class StaleProbe:
        in_flight = False

        @staticmethod
        def request(urls):
            return 2

        @staticmethod
        def shutdown():
            pass

        @staticmethod
        def poll():
            return [HealthProbeResult(1, {"llm": "http://old", "tavi": "http://tavi"},
                                      health={"llm": Health(True, "reachable"),
                                              "tavi": Health(True, "reachable")})]

    w = _win(qtbot, make_ws)
    w._health_probe = StaleProbe()
    w._health_generation = 2
    before = w._llm_health.styleSheet()
    w._poll_health_results()
    assert w._llm_health.styleSheet() == before


def test_blocked_health_worker_is_inert_after_window_close(qtbot, make_ws, monkeypatch):
    """Closing must not leave a worker able to emit through a deleted Qt signal."""
    from threading import Event
    from isar_gui import main_window as mw
    from isar_gui.health import Health

    started = Event()
    release = Event()

    def blocked(url, **kwargs):
        started.set()
        assert release.wait(2)
        return Health(True, "reachable")

    monkeypatch.setattr(mw, "ping_http", blocked)
    w = _win(qtbot, make_ws)
    qtbot.waitUntil(started.is_set, timeout=2000)
    w.close()
    assert w._health_closed
    assert not w._health_poll_timer.isActive()
    release.set()
    qtbot.wait(100)

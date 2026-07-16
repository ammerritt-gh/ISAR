"""Qt-free backend health helpers: ping reachability semantics + TAVI bat resolution."""
from __future__ import annotations

import urllib.error
from threading import Event

from isar_gui import health


def test_ping_reachable_when_opener_succeeds():
    h = health.ping_http("http://x", opener=lambda url, timeout: object())
    assert h.ok and "reachable" in h.detail


def test_ping_reachable_on_http_error_status():
    def opener(url, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    h = health.ping_http("http://x", opener=opener)
    assert not h.ok and h.reachable and h.status == "reachable_error" and "401" in h.detail


def test_ping_down_on_connection_failure():
    def opener(url, timeout):
        raise OSError("connection refused")
    h = health.ping_http("http://x", opener=opener)
    assert not h.ok and not h.reachable and h.status == "down" and "unreachable" in h.detail


def test_health_probe_coalesces_without_overlapping_workers():
    started = Event()
    release = Event()
    calls: list[str] = []

    def prober(url: str) -> health.Health:
        calls.append(url)
        if url == "first":
            started.set()
            assert release.wait(2)
        return health.Health(True, "reachable")

    probe = health.HealthProbe(prober)
    first = probe.request({"llm": "first"})
    assert started.wait(2)
    probe.request({"llm": "second"})
    latest = probe.request({"llm": "latest"})
    assert calls == ["first"]
    release.set()

    def completed_generation():
        results = probe.poll()
        return results[0].generation if results else None

    import time
    deadline = time.monotonic() + 2
    while completed_generation() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    deadline = time.monotonic() + 2
    while calls != ["first", "latest"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["first", "latest"]
    deadline = time.monotonic() + 2
    results = []
    while not results and time.monotonic() < deadline:
        results = probe.poll()
        time.sleep(0.01)
    assert first < latest
    assert results and results[0].generation == latest


def test_health_probe_shutdown_does_not_schedule_pending_work():
    started = Event()
    release = Event()

    def prober(url: str) -> health.Health:
        started.set()
        assert release.wait(2)
        return health.Health(True, "reachable")

    probe = health.HealthProbe(prober)
    probe.request({"llm": "first"})
    assert started.wait(2)
    probe.request({"llm": "pending"})
    probe.shutdown()
    release.set()

    import time
    deadline = time.monotonic() + 2
    while probe.in_flight and time.monotonic() < deadline:
        probe.poll()
        time.sleep(0.01)
    assert not probe.in_flight


def test_tavi_health_url_targets_the_liveness_endpoint():
    # TAVI's bare API base 404s by design; only <base>/health answers 200. Probing the base
    # rendered a running TAVI amber and blocked campaign launch.
    assert health.tavi_health_url("http://127.0.0.1:8642/api/v1") == \
        "http://127.0.0.1:8642/api/v1/health"
    assert health.tavi_health_url("http://host:9000/api/v1/") == "http://host:9000/api/v1/health"


def test_find_tavi_bat_prefers_configured(tmp_path):
    bat = tmp_path / "run-tavi-dev.bat"
    bat.write_text("@echo off\n", encoding="utf-8")
    assert health.find_tavi_bat(str(bat)) == str(bat)


def test_find_tavi_bat_ignores_missing_configured(tmp_path):
    # A missing configured path is skipped; the result is then None or a real conventional bat
    # (which may exist on a dev machine). Never the non-existent configured path.
    result = health.find_tavi_bat(str(tmp_path / "nope.bat"))
    if result is not None:
        from pathlib import Path
        assert Path(result).is_file()
        assert result != str(tmp_path / "nope.bat")

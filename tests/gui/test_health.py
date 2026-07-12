"""Qt-free backend health helpers: ping reachability semantics + TAVI bat resolution."""
from __future__ import annotations

import urllib.error

from isar_gui import health


def test_ping_reachable_when_opener_succeeds():
    h = health.ping_http("http://x", opener=lambda url, timeout: object())
    assert h.ok and "reachable" in h.detail


def test_ping_reachable_on_http_error_status():
    def opener(url, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    h = health.ping_http("http://x", opener=opener)
    assert h.ok and "401" in h.detail          # a server that answers 401 is up


def test_ping_down_on_connection_failure():
    def opener(url, timeout):
        raise OSError("connection refused")
    h = health.ping_http("http://x", opener=opener)
    assert not h.ok and "unreachable" in h.detail


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

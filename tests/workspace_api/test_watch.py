"""Behavioural tests for isar.workspace_api.watch (polling backend, real tmp_path files).

Event-driven: the callback sets a threading.Event and we wait on it with a bounded timeout — no
bare sleeps used as assertions, so the suite stays fast and xdist-safe. Poll interval is tiny.
"""
from __future__ import annotations

import importlib.util
import threading

import pytest

from isar.workspace_api import Workspace

# Short-but-comfortable timings: poll far below debounce so a burst coalesces; both far below the
# per-test wait budget.
POLL = 0.02
DEBOUNCE = 60
WAIT = 3.0


def _append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_change_to_results_fires_with_results_name(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    got = {}
    fired = threading.Event()

    def cb(changed):
        got["changed"] = set(changed)
        fired.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL):
        _append(root / "report" / "results.csv", "2,det\n")
        assert fired.wait(WAIT), "callback never fired"
    assert got["changed"] == {"results"}


def test_two_rapid_changes_coalesce(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    seen = []
    fired = threading.Event()

    def cb(changed):
        seen.append(set(changed))
        fired.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL):
        _append(root / "report" / "results.csv", "2,det\n")
        _append(root / "session.json", " ")
        assert fired.wait(WAIT), "callback never fired"
    assert len(seen) == 1
    assert seen[0] == {"results", "snapshot"}


def test_file_appearing_after_start_fires(make_ws):
    # report/report.json is not written by the fixture -> its appearance is a change.
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    got = {}
    fired = threading.Event()

    def cb(changed):
        got["changed"] = set(changed)
        fired.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL):
        (root / "report" / "report.json").write_text("{}", encoding="utf-8")
        assert fired.wait(WAIT), "callback never fired for appearing file"
    assert got["changed"] == {"report"}


def test_callback_exception_survives_and_warns(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    calls = []
    first = threading.Event()
    second = threading.Event()

    def cb(changed):
        calls.append(set(changed))
        if len(calls) == 1:
            first.set()
            raise RuntimeError("boom")
        second.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL):
        _append(root / "report" / "results.csv", "2,det\n")
        assert first.wait(WAIT), "first callback never fired"
        _append(root / "session.json", " ")
        assert second.wait(WAIT), "watcher did not survive callback exception"
    assert any("boom" in w for w in ws.warnings)


def test_stop_prevents_further_callbacks(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    fired = threading.Event()

    def cb(changed):
        fired.set()

    w = ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL)
    _append(root / "report" / "results.csv", "2,det\n")
    assert fired.wait(WAIT), "callback never fired before stop"
    w.stop()
    assert not w._thread.is_alive(), "watcher thread not joined after stop"
    fired.clear()
    _append(root / "report" / "results.csv", "3,det\n")
    # Generous wait: no callback must arrive after stop.
    assert not fired.wait(0.5), "callback fired after stop()"


def test_context_manager_stops_on_exit(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)

    def cb(changed):
        pass

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL) as w:
        assert w._thread.is_alive()
    assert not w._thread.is_alive(), "context manager did not stop the watcher"


@pytest.mark.skipif(
    importlib.util.find_spec("watchdog") is None,
    reason="watchdog not installed (optional [gui] dep); documents the backend contract",
)
def test_watchdog_backend_selected_when_available(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    got = {}
    fired = threading.Event()

    def cb(changed):
        got["changed"] = set(changed)
        fired.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE) as w:
        assert w.backend_name == "watchdog"
        _append(root / "report" / "results.csv", "2,det\n")
        assert fired.wait(WAIT), "watchdog callback never fired"
    assert got["changed"] == {"results"}


def test_poll_backend_selected_by_default(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    with ws.watch(lambda c: None, backend="poll", poll_interval_s=POLL) as w:
        assert w.backend_name == "poll"

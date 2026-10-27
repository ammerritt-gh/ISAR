"""Behavioural tests for isar.workspace_api.watch (polling backend, real tmp_path files).

Event-driven: the callback sets a threading.Event and we wait on it with a bounded timeout — no
bare sleeps used as assertions, so the suite stays fast and xdist-safe. Poll interval is tiny.
"""
from __future__ import annotations

import importlib.util
import threading
import time

import pytest

from isar.workspace_api import Workspace
from isar.workspace_api.watch import _Debouncer

# Short-but-comfortable timings: poll far below debounce so a burst coalesces; both far below the
# per-test wait budget.
POLL = 0.02
DEBOUNCE = 150
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


def test_appending_a_fit_inputs_line_fires_with_the_fit_inputs_name(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    got = {}
    fired = threading.Event()

    def cb(changed):
        got["changed"] = set(changed)
        fired.set()

    with ws.watch(cb, debounce_ms=DEBOUNCE, poll_interval_s=POLL):
        _append(root / "report" / "fit_inputs.jsonl", '{"kind": "run", "event": "start", "run": "r1"}\n')
        assert fired.wait(WAIT), "callback never fired"
    assert got["changed"] == {"fit_inputs"}


def test_two_rapid_changes_coalesce(make_ws):
    # Wider than DEBOUNCE: under parallel load the poll thread can see the second write after a
    # 150 ms window has closed, which splits the burst into two callbacks.
    coalesce_ms = 400
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    seen = []
    fired = threading.Event()

    def cb(changed):
        seen.append(set(changed))
        fired.set()

    with ws.watch(cb, debounce_ms=coalesce_ms, poll_interval_s=POLL):
        _append(root / "report" / "results.csv", "2,det\n")
        # Sleep > poll_interval but < debounce so the backend delivers the two changes as TWO
        # separate add() calls that the debouncer must merge (exercises the timer re-arm path),
        # rather than one pre-merged poll tick.
        time.sleep(POLL * 3)
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


# ---------------------------------------------------------------------------
# _Debouncer quiesce: an in-flight callback re-armed during emit is still joined by cancel()
# ---------------------------------------------------------------------------
def test_debouncer_cancel_joins_inflight_and_reports_timeout():
    entered = threading.Event()
    release = threading.Event()
    emits = []

    def emit(names):
        emits.append(set(names))
        entered.set()
        release.wait(WAIT)          # block inside the callback until the test lets go

    d = _Debouncer(debounce_ms=20, emit=emit)
    d.add({"a"})
    assert entered.wait(WAIT), "first emit never started"
    # Re-arm a second timer while the first callback is still blocked mid-emit (this replaces the
    # _timer slot; a debouncer that tracked only _timer would lose the in-flight flush).
    d.add({"b"})
    # cancel() while the first callback is still running -> cannot quiesce within the short budget.
    assert d.cancel(timeout=0.2) is False
    release.set()                   # let the first callback finish; its flush thread then joins
    time.sleep(0.1)                 # settle
    assert emits == [{"a"}]         # the re-armed timer was cancelled -> it never emitted


def test_debouncer_cancel_returns_true_when_idle():
    d = _Debouncer(debounce_ms=20, emit=lambda names: None)
    d.add({"a"})
    time.sleep(0.1)                 # let the single flush complete
    assert d.cancel(timeout=WAIT) is True

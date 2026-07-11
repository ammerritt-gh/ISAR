"""WatchBridge: a workspace file change arrives as a Qt signal on the Qt thread, post-refresh."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.watch_bridge import WatchBridge


def _append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_change_emits_on_qt_thread_after_refresh(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)

    seen = {}
    main_thread = threading.get_ident()

    def on_changed(changed):
        seen["changed"] = set(changed)
        seen["thread"] = threading.get_ident()

    bridge = WatchBridge(ws, debounce_ms=100, poll_interval_s=0.02)
    bridge.changed.connect(on_changed)

    with bridge:
        with qtbot.waitSignal(bridge.changed, timeout=4000):
            _append(root / "report" / "results.csv", "2,det\n")

    assert seen["changed"] == {"results"}
    # The slot ran on the GUI (main) thread, not the watcher's daemon thread.
    assert seen["thread"] == main_thread


def test_bridge_refreshes_before_emit(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)

    # Prime a cached view, then confirm the facade was refreshed by the time the signal lands.
    _ = ws.results
    refreshed = {}

    def on_changed(changed):
        # After refresh the cache is cleared; re-reading results now reflects the new row.
        refreshed["n"] = len(ws.results)

    bridge = WatchBridge(ws, debounce_ms=100, poll_interval_s=0.02)
    bridge.changed.connect(on_changed)
    with bridge:
        with qtbot.waitSignal(bridge.changed, timeout=4000):
            _append(root / "report" / "results.csv", "2,det\n")
    assert refreshed["n"] == 2


def test_stop_is_idempotent(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)
    bridge = WatchBridge(ws, poll_interval_s=0.02)
    bridge.start()
    bridge.stop()
    bridge.stop()  # no error on double-stop
    assert bridge._watcher is None


def test_no_emit_after_stop(qtbot, make_ws):
    """A file change made after stop() must never surface as a ``changed`` emission — including a
    tick already queued (on the watcher's daemon thread) before stop() ran on the Qt thread."""
    root = make_ws(rows=[{"scan": 1, "detector": "det"}])
    ws = Workspace.open(root)

    seen = {"count": 0}
    bridge = WatchBridge(ws, debounce_ms=50, poll_interval_s=0.02)
    bridge.changed.connect(lambda changed: seen.__setitem__("count", seen["count"] + 1))
    bridge.start()
    bridge.stop()

    _append(root / "report" / "results.csv", "2,det\n")
    # Event-driven wait: give the (now-stopped) watcher's poll loop several intervals to have
    # noticed and posted a tick, if it still could. No signal is expected.
    qtbot.wait(400)
    assert seen["count"] == 0

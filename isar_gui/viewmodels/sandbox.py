"""SandboxViewModel — runs the G7 sandbox preview fit OFF the UI thread and marshals the result back.

The compute (:mod:`isar.sandbox_api`) is Qt-free and can take ~0.1–1 s. One daemon worker runs at a
time; rapid requests coalesce to the newest pending request. Results return through a queue polled
on the GUI thread, so the UI never blocks and dragging E₀ cannot create unbounded fit threads.
"""
from __future__ import annotations

import logging
import queue
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from isar import sandbox_api
from isar_gui import collector_guard

log = logging.getLogger(__name__)


def _work(results: queue.SimpleQueue, root: str, key: str, rid: int, center_override=None) -> None:
    """The worker thread (NO Qt access, the collector off for its whole life). It gets the queue, never the
    view-model: a last reference dropped on the worker would destroy that QObject off the GUI thread."""
    try:
        res = sandbox_api.preview_fit(root, key, center_override=center_override)
    except Exception as exc:  # noqa: BLE001 — a crashed preview is a shown state, not a hang
        log.warning("sandbox worker crashed for %s: %s", key, exc)
        res = sandbox_api.SandboxResult(
            unit_key=key, status="error",
            reason=f"sandbox crashed: {type(exc).__name__}: {exc}")
    results.put((rid, res))


class SandboxViewModel(QObject):
    """Owns the sandbox worker. ``request_fit(unit_key)`` starts a preview; ``result_ready`` carries
    a :class:`~isar.sandbox_api.SandboxResult` when it finishes; ``busy_changed`` toggles the UI."""

    result_ready = Signal(object)   # SandboxResult
    busy_changed = Signal(bool)

    def __init__(self, workspace_root=None, parent=None):
        super().__init__(parent)
        self._root = workspace_root
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._poll)
        self._next_id = 0       # monotonic request token
        self._latest_id = 0     # newest request — only its result may be shown
        self._active = False    # exactly one worker may be active
        self._pending = None    # newest coalesced (root, key, request id, center)
        self._thread: threading.Thread | None = None

    def set_workspace_root(self, root) -> None:
        """Rebind the preview source and invalidate every request from the previous workspace.

        An active worker cannot be cancelled safely, so it drains silently. Any pending old-root
        request is discarded; a subsequent request for the new root waits behind the active one.
        """
        if root == self._root:
            return
        self._root = root
        self._next_id += 1
        self._latest_id = self._next_id
        self._pending = None

    def request_fit(self, unit_key: str, center_override=None) -> None:
        """Preview-fit ``unit_key``. ``center_override`` (raw-frame energy) is the user-placed E₀
        line — when given it overrides the run-level calibration zero for this preview."""
        key = str(unit_key)
        if not self._root:
            self.result_ready.emit(sandbox_api.SandboxResult(
                unit_key=key, status="error", reason="no workspace loaded"))
            return
        self._next_id += 1
        rid = self._next_id
        self._latest_id = rid
        root = str(self._root)
        co = None if center_override is None else float(center_override)
        self._pending = (root, key, rid, co)  # overwrite any intermediate pending request
        if not self._active:
            self.busy_changed.emit(True)
            self._start_pending()
        self._timer.start()

    def _start_pending(self) -> None:
        """Start the newest pending request. Called only on the GUI thread."""
        if self._active or self._pending is None:
            return
        args = self._pending
        self._pending = None
        self._active = True
        self._thread = threading.Thread(target=_work, args=(self._queue, *args), daemon=True,
                                        name="isar-gui-sandbox")
        collector_guard.hold(self._thread)
        self._thread.start()

    # ---- GUI thread ----
    def _poll(self) -> None:
        latest_res = None
        answered = False
        try:
            while True:
                rid, res = self._queue.get_nowait()
                self._active = False
                answered = True
                if rid == self._latest_id:      # keep only the newest request's result; drop stale
                    latest_res = res
        except queue.Empty:
            pass
        if answered:
            collector_guard.finish(self._thread)   # its answer is queued: it is only returning
        if latest_res is not None:
            self.result_ready.emit(latest_res)
        self._start_pending()
        if not self._active and self._pending is None:
            self._timer.stop()
            self.busy_changed.emit(False)

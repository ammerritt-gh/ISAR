"""File-change notifications for a :class:`~isar.workspace_api.Workspace` (P0-5).

``ws.watch(callback)`` returns a running :class:`Watcher` that fires ``callback(changed: set[str])``
when any of a fixed set of workspace artifacts change on disk, so a live view can re-read only what
moved instead of re-parsing everything on a timer.

Backends
--------
* **polling** (default, dependency-free): a background daemon thread stats each watched file
  (``os.stat`` mtime-ns + size) every ``poll_interval_s`` and diffs against the previous snapshot.
* **watchdog** (optional): used automatically when the ``watchdog`` package is importable. It is
  NOT a hard dependency of isar core — the import is guarded and it rides the later ``[gui]`` extra.

Both backends feed a shared debouncer: changes seen within ``debounce_ms`` coalesce into ONE
callback carrying the union of changed logical names.

Thread model
------------
The callback runs on a background daemon thread owned by the watcher (the polling thread, or a
short-lived debounce timer thread for the watchdog backend) — **never** the calling thread and never
a GUI/Qt thread. A GUI consumer must marshal the callback onto its own event loop. The watcher does
**not** touch the :class:`Workspace` caches; the intended pattern is to call ``ws.refresh()`` from
inside the callback before re-reading::

    def on_change(changed):
        ws.refresh()
        redraw(ws.results)

    with ws.watch(on_change) as w:
        ...

An exception raised by the callback is caught, appended to ``ws.warnings``, and does not stop the
watcher. The watcher never writes to the workspace.
"""
from __future__ import annotations

import importlib.util
import os
import threading

# Logical name -> path parts relative to the workspace root. The fixed watched set (P0-5).
_WATCHED: dict[str, tuple[str, ...]] = {
    "results": ("report", "results.csv"),
    "events": ("report", "session_log.jsonl"),
    "report": ("report", "report.json"),
    "card_state": ("card.state.json",),
    "snapshot": ("session.json",),
}


def _stat_key(path):
    """A change-detection key for one file, or ``None`` if it does not exist yet.

    Tolerant of a not-yet-written file (``FileNotFoundError``) and transient stat errors on Windows
    — both read as "absent", so the file's later appearance registers as a change.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class _Debouncer:
    """Coalesce changed-name notifications inside a ``debounce_ms`` window into one ``emit`` call.

    Thread-safe: backends may call :meth:`add` from any thread. Each :meth:`add` (re)arms a single
    timer; the timer fires ``emit(names)`` once the window elapses with no further additions.
    :meth:`cancel` disarms and drops any pending names (used by :meth:`Watcher.stop`).
    """

    def __init__(self, debounce_ms: float, emit):
        self._delay = max(debounce_ms, 0) / 1000.0
        self._emit = emit
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None
        self._cancelled = False

    def add(self, names) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._pending |= set(names)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            names = self._pending
            self._pending = set()
            self._timer = None
        if names:
            self._emit(names)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = set()


class _PollBackend:
    """Stat-mtime/size polling backend. Callbacks originate on this thread via the debouncer."""

    name = "poll"

    def __init__(self, paths: dict[str, object], poll_interval_s: float, on_change):
        self._paths = paths
        self._interval = poll_interval_s
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="isar-ws-watch", daemon=True)
        # Snapshot at construction (before the thread starts) so pre-existing files are not
        # reported and a change racing the start is still caught on the first poll.
        self._state = {name: _stat_key(p) for name, p in self._paths.items()}

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            changed = set()
            for name, p in self._paths.items():
                key = _stat_key(p)
                if key != self._state[name]:
                    changed.add(name)
                    self._state[name] = key
            if changed:
                self._on_change(changed)

    def stop(self, timeout: float) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)


class _WatchdogBackend:
    """watchdog-observer backend (used when ``watchdog`` is importable). Debounce-fed like polling.

    Watches the parent directories of the watched files and maps each event path back to its logical
    name. Callbacks originate on a debounce timer thread (still a background daemon, never the caller).
    """

    name = "watchdog"

    def __init__(self, paths: dict[str, object], on_change):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self._on_change = on_change
        # Reverse map: absolute file path -> logical name.
        self._by_path = {os.path.normcase(os.path.abspath(str(p))): name
                         for name, p in paths.items()}
        self._observer = Observer()
        parent = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                for pth in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
                    if not pth:
                        continue
                    name = parent._by_path.get(os.path.normcase(os.path.abspath(pth)))
                    if name is not None:
                        parent._on_change({name})

        handler = _Handler()
        # Schedule one watch per distinct existing parent directory.
        dirs = {os.path.dirname(os.path.abspath(str(p))) for p in paths.values()}
        for d in dirs:
            if os.path.isdir(d):
                self._observer.schedule(handler, d, recursive=False)

    @property
    def thread(self) -> threading.Thread:
        return self._observer

    def start(self) -> None:
        self._observer.start()

    def stop(self, timeout: float) -> None:
        self._observer.stop()
        self._observer.join(timeout)


def _select_backend(name: str | None) -> str:
    if name in ("poll", "watchdog"):
        return name
    if name is not None:
        raise ValueError(f"unknown watch backend {name!r} (expected 'poll', 'watchdog', or None)")
    if importlib.util.find_spec("watchdog") is not None:
        return "watchdog"
    return "poll"


class Watcher:
    """A running file-change watcher over a fixed set of workspace artifacts.

    Created and started by :meth:`isar.workspace_api.Workspace.watch`; not instantiated directly by
    consumers. Supports the context-manager protocol (``with ws.watch(cb) as w:``); leaving the
    block calls :meth:`stop`. See the module docstring for the thread model.
    """

    def __init__(self, ws, callback, *, debounce_ms: float = 200,
                 poll_interval_s: float = 1.0, backend: str | None = None):
        self._ws = ws
        self._callback = callback
        self._paths = {name: ws.root.joinpath(*parts) for name, parts in _WATCHED.items()}
        self._debouncer = _Debouncer(debounce_ms, self._emit)
        self.backend_name = _select_backend(backend)
        if self.backend_name == "watchdog":
            self._backend = _WatchdogBackend(self._paths, self._debouncer.add)
        else:
            self._backend = _PollBackend(self._paths, poll_interval_s, self._debouncer.add)
        self._started = False

    # ---- the shared debounce sink -----------------------------------------
    def _emit(self, changed) -> None:
        try:
            self._callback(set(changed))
        except Exception as exc:  # noqa: BLE001 — a bad callback must not kill the watcher
            self._ws.warnings.append(f"watch callback error: {exc}")

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> "Watcher":
        if not self._started:
            self._backend.start()
            self._started = True
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the backend (join its thread, bounded by ``timeout``) and drop pending callbacks.

        Ordered so no callback can fire after return: stop+join the backend first (no further
        ``add``), then cancel the debouncer (disarm any armed timer).
        """
        self._backend.stop(timeout)
        self._debouncer.cancel()

    # ---- introspection (also used by tests) -------------------------------
    @property
    def _thread(self) -> threading.Thread:
        return self._backend.thread

    # ---- context manager --------------------------------------------------
    def __enter__(self) -> "Watcher":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False

"""Keep Python's cyclic collector off while a GUI background thread runs.

A collection started on a worker thread can finalize unreachable PySide6 wrappers there, and a Qt object
destroyed off the GUI thread deadlocked the GUI (seen in the intake worker, and once at close with the
``isar-gui-health`` thread). So the GUI thread calls :func:`hold` before it starts a background thread and
:func:`release` once the thread has ended; the collector comes back only when no held thread of any owner
is still alive, so overlapping workers (intake, health probe, sandbox) compose. Both calls run on the GUI
thread only, which also means the backlog is collected there.
"""
from __future__ import annotations

import gc
import threading

_held: list[threading.Thread] = []   # threads started under a hold and not yet seen ended (GUI thread only)
_gc_was_enabled = False


def hold(thread: threading.Thread) -> None:
    """Disable the collector before ``thread`` starts (GUI thread)."""
    global _gc_was_enabled
    release()                         # drop threads that ended since, so the saved state below is true
    if not _held:
        _gc_was_enabled = gc.isenabled()
        gc.disable()
    _held.append(thread)


def release() -> bool:
    """Forget held threads that have ended and re-enable the collector once none is left (GUI thread).
    Returns True while a held thread is still running.

    A thread still alive at window close keeps its hold until a later call finds it ended; the next
    ``hold`` or ``release`` (any owner) does that, so the collector is never left off for good."""
    global _gc_was_enabled
    _held[:] = [t for t in _held if t.is_alive()]
    if not _held and _gc_was_enabled:
        _gc_was_enabled = False
        gc.enable()
    return bool(_held)


def finish(thread: threading.Thread, timeout: float = 1.0) -> None:
    """For a thread whose result is already queued (so it is only returning): wait briefly for it to end,
    then :func:`release`, so the collector is back at once rather than at the next poll."""
    thread.join(timeout)
    release()

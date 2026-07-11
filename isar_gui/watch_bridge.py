"""WatchBridge — marshal :meth:`Workspace.watch` callbacks onto the Qt event loop.

``ws.watch(cb)`` fires ``cb(changed)`` on a background daemon thread (never the Qt thread — see
isar.workspace_api.watch). A Qt consumer must not touch widgets from that thread. This bridge wraps
the watcher and re-emits each change as a Qt :class:`Signal` delivered by a **queued** connection,
so every connected slot runs on the thread that owns the bridge (the GUI thread).

The bridge also calls ``ws.refresh()`` on the Qt thread *before* emitting, so consumers can simply
re-read the facade's views in their slot without worrying about stale caches or thread-safety of the
refresh itself.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot


class WatchBridge(QObject):
    """Bridges a workspace file-watch onto the Qt thread.

    Emits :attr:`changed` (carrying the set of changed logical names) on the GUI thread after
    refreshing the workspace. ``start()`` begins watching; ``stop()`` tears the watcher down.
    """

    #: Emitted on the Qt thread after ``ws.refresh()``; payload is the set of changed logical names.
    changed = Signal(set)

    # Internal hop: the watcher's background thread emits this; a queued connection to the private
    # slot moves execution onto the Qt thread before we refresh + re-emit.
    _tick = Signal(set)

    def __init__(self, ws, parent=None, **watch_kwargs):
        super().__init__(parent)
        self._ws = ws
        self._watch_kwargs = watch_kwargs
        self._watcher = None
        # Queued so the slot runs on THIS object's (GUI) thread, not the watcher's daemon thread.
        self._tick.connect(self._on_tick, Qt.QueuedConnection)

    def start(self) -> "WatchBridge":
        if self._watcher is None:
            # _tick.emit is thread-safe; it just posts an event to the GUI loop.
            self._watcher = self._ws.watch(self._tick.emit, **self._watch_kwargs)
        return self

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @Slot(set)
    def _on_tick(self, changed) -> None:
        # Runs on the Qt thread: refresh first so slots just re-read views, then fan out.
        self._ws.refresh()
        self.changed.emit(set(changed))

    # Context-manager sugar mirrors the underlying Watcher.
    def __enter__(self) -> "WatchBridge":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

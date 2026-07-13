"""FilesViewModel — view-ready state for the Files dock (Phase D).

Two responsibilities, both thin:
  * turn ``ws.files`` (the ingest manifest, :class:`~isar.workspace_api.FileView` rows) into display
    dicts the dock renders — one row per classified data file with a human ``status_text``;
  * mediate the watched-folder configuration, which is GUI-only state persisted in the per-workspace
    :class:`~isar_gui.gui_store.GuiStore` (never engine-owned). The dock calls the passthroughs
    below; each mutation emits :attr:`watch_config_changed` so the window can persist the store and
    re-seed the :class:`~isar_gui.folder_watcher.FolderWatcher`.

Holds no cache of its own beyond the facade's; ``refresh`` (or the base's watch-driven
``on_workspace_changed``) after the bridge re-read the facade is enough to pick up a new manifest.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal

from isar_gui.viewmodels.base import ViewModel


def _status_text(classification, n_scans, error=None) -> str:
    """Human one-liner for a file row from its classification, scan count, and parse error."""
    if error:
        return "parse failed"
    cls = (classification or "").lower()
    if cls == "measurement":
        n = n_scans or 0
        return f"parsed {n} scan{'s' if n != 1 else ''}"
    if cls == "companion":
        return "supporting file (not a measurement)"
    if cls == "foreign":
        return "belongs to a different experiment"
    if cls == "unrecognized":
        return "not recognised as measurement data"
    if not cls:
        return "pending"
    return cls


class FilesViewModel(ViewModel):
    """File-manifest rows + watched-folder config passthrough to the :class:`GuiStore`."""

    #: Emitted after any watched-folder mutation (add/remove/pause) so the window persists + re-seeds.
    watch_config_changed = Signal()

    def __init__(self, ws, store=None, parent=None):
        super().__init__(ws, parent)
        self._store = store  # GuiStore | None (None in the empty/pending state — no workspace file)

    def set_store(self, store) -> None:
        """Bind the per-workspace :class:`GuiStore` (called on the ready bind).

        Emits ``refreshed`` (so the dock re-renders the watched list) but NOT
        ``watch_config_changed`` — binding a store is not a user mutation, so it must not trigger a
        persist. The window seeds the folder watcher from :meth:`watched` directly on the bind.
        """
        self._store = store
        self.refreshed.emit()

    # ---- file rows -------------------------------------------------------
    @property
    def rows(self) -> list:
        """One display dict per manifest file:
        ``{path, name, classification, n_scans, status_text, error}``."""
        out = []
        for fv in self.workspace.files:
            path = str(getattr(fv, "path", "") or "")
            classification = getattr(fv, "classification", None)
            n_scans = getattr(fv, "n_scans", None)
            error = getattr(fv, "error", None)
            out.append({
                "path": path,
                "name": Path(path).name or path,
                "classification": classification,
                "n_scans": n_scans,
                "error": error,
                "status_text": _status_text(classification, n_scans, error),
            })
        return out

    # ---- watched-folder config (delegates to the GuiStore) ---------------
    def watched(self) -> list[str]:
        return self._store.watched_folders() if self._store is not None else []

    def watch_paused(self) -> bool:
        return self._store.watch_paused() if self._store is not None else False

    def add_watch(self, path: str) -> None:
        if self._store is None:
            return
        if self._store.add_watched(path):
            self.watch_config_changed.emit()

    def remove_watch(self, path: str) -> None:
        if self._store is None:
            return
        if self._store.remove_watched(path):
            self.watch_config_changed.emit()

    def set_paused(self, flag: bool) -> None:
        if self._store is None:
            return
        self._store.set_watch_paused(flag)
        self.watch_config_changed.emit()

    # ---- refresh ---------------------------------------------------------
    def refresh(self) -> None:
        self.refreshed.emit()

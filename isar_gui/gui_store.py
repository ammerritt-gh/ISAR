"""GuiStore — per-workspace GUI-only state (Qt-free), first slice: watched folders.

This is the operator's *view* preferences pinned to one workspace: which folders the GUI watches for
new data, and whether watching is paused. It lives in ``<workspace_root>/gui.state.json`` — NOT in
the engine-owned ``card.policy.toml`` / ``card.state.json`` / ``session.json`` (Hard Rule 1: code
must never write policy, and machine bookkeeping must never mix with view prefs). The engine already
skips ``gui.state.json`` during discovery (Phase B), so nothing here is ever ingested.

Qt-free by design (mirrors :mod:`isar_gui.session`): the load/save/query API needs no QApplication,
so it is unit-tested directly. The Qt-side debounce lives in :class:`~isar_gui.session.Autosaver`,
which the window reuses to flush this store 500 ms after a change.

Saving is atomic (write ``.tmp`` then :func:`os.replace`) and utf-8; loading is corruption-tolerant
(missing/garbage → defaults + a log line, never a raise). Unknown top-level keys are preserved on
round-trip so a Phase-E ``groups`` section written by a newer build survives an older one.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

STORE_NAME = "gui.state.json"


class GuiStore:
    """GUI-only state for one workspace. Phase D lands only the watched-folders section; Phase E adds
    ``groups`` on the same file (unknown keys are preserved so the two coexist)."""

    VERSION = 1

    def __init__(self, root, data: dict | None = None):
        self._root = Path(root)
        # Keep the full raw dict so unknown keys (future sections) round-trip untouched; the typed
        # accessors below read/write only the keys this slice owns.
        self._data: dict = dict(data) if isinstance(data, dict) else {}
        self._data.setdefault("version", self.VERSION)
        if not isinstance(self._data.get("watched_folders"), list):
            self._data["watched_folders"] = []
        if not isinstance(self._data.get("watch_paused"), bool):
            self._data["watch_paused"] = False

    # ---- path ------------------------------------------------------------
    @classmethod
    def path_for(cls, workspace_root) -> Path:
        return Path(workspace_root) / STORE_NAME

    # ---- load / save -----------------------------------------------------
    @classmethod
    def load(cls, workspace_root) -> "GuiStore":
        """Load ``<root>/gui.state.json``; a missing or corrupt file yields defaults (+ a log)."""
        path = cls.path_for(workspace_root)
        if not path.exists():
            return cls(workspace_root)
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning("gui.state.json unreadable at %s (%s) — using defaults", path, exc)
            return cls(workspace_root)
        if not isinstance(data, dict):
            log.warning("gui.state.json at %s is not an object — using defaults", path)
            return cls(workspace_root)
        return cls(workspace_root, data)

    def save(self) -> None:
        """Write atomically (``.tmp`` + :func:`os.replace`), utf-8. Failures log, never raise."""
        path = self.path_for(self._root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save gui.state.json at %s: %s", path, exc)

    # ---- watched folders -------------------------------------------------
    def watched_folders(self) -> list[str]:
        return [str(p) for p in self._data.get("watched_folders", [])]

    def add_watched(self, path: str) -> bool:
        """Add ``path`` to the watched set (deduped, order-preserving). Returns whether it changed."""
        p = str(Path(path))
        current = self.watched_folders()
        if p in current:
            return False
        self._data["watched_folders"] = current + [p]
        return True

    def remove_watched(self, path: str) -> bool:
        """Remove ``path`` from the watched set. Returns whether it changed."""
        p = str(Path(path))
        current = self.watched_folders()
        if p not in current:
            return False
        self._data["watched_folders"] = [q for q in current if q != p]
        return True

    def watch_paused(self) -> bool:
        return bool(self._data.get("watch_paused", False))

    def set_watch_paused(self, flag: bool) -> None:
        self._data["watch_paused"] = bool(flag)

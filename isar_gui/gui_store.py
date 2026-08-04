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
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

STORE_NAME = "gui.state.json"


@dataclass
class GroupRec:
    """One manual scan group: a stable id, a human name, and an ordered/deduped member list.

    ``members`` are scan-unit *keys*. New-form memberships are the atomic ``"exp:scan:det"`` unit key
    (one analyzer of a source scan); legacy v1 stores hold scan-level ``"exp:scan"`` keys, which
    :func:`isar_gui.scan_keys.member_matches` still resolves to every analyzer unit of that scan (no
    key is rewritten on load — nothing is deleted).
    """
    id: str
    name: str
    members: list = field(default_factory=list)


class GuiStore:
    """GUI-only state for one workspace. Phase D lands only the watched-folders section; Phase E adds
    ``groups`` on the same file (unknown keys are preserved so the two coexist)."""

    VERSION = 1

    def __init__(self, root, data: dict | None = None):
        self._root = Path(root)
        # Keep the full raw dict verbatim so EVERY key round-trips untouched — including a value under
        # a key this build owns but cannot read (e.g. a newer schema wrote a dict where this build
        # expects a list). Normalization is READ-ONLY: the accessors below treat a wrong-typed owned
        # value as empty (with a one-time log) but never rewrite ``_data`` on load, so nothing is
        # silently clobbered on open. Only an explicit user mutation of a section replaces its value.
        self._data: dict = dict(data) if isinstance(data, dict) else {}
        self._data.setdefault("version", self.VERSION)
        self._warned: set[str] = set()   # owned keys already flagged as wrong-typed (log once each)

    def _list_or_empty(self, key: str) -> list:
        """Value stored under ``key`` when it is a list, else ``[]`` (logged once, ``_data`` untouched)."""
        val = self._data.get(key)
        if isinstance(val, list):
            return val
        if val is not None and key not in self._warned:
            self._warned.add(key)
            log.warning("gui.state.json key %r is %s, not a list — treating as empty (value kept on "
                        "disk for round-trip)", key, type(val).__name__)
        return []

    # ---- path ------------------------------------------------------------
    @property
    def root(self) -> Path:
        """The workspace directory this store lives in (``<root>/gui.state.json``)."""
        return self._root

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
        return [str(p) for p in self._list_or_empty("watched_folders")]

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
        val = self._data.get("watch_paused")
        return val if isinstance(val, bool) else False

    def set_watch_paused(self, flag: bool) -> None:
        self._data["watch_paused"] = bool(flag)

    # ---- campaign setup form (GUI Pass 3) ----------------------------------
    def campaign_form(self) -> dict:
        """The remembered campaign setup form (goal/spec/tier/budget/seed), or ``{}``."""
        val = self._data.get("campaign")
        return dict(val) if isinstance(val, dict) else {}

    def set_campaign_form(self, form: dict) -> None:
        self._data["campaign"] = dict(form)

    # ---- manual groups (Phase E) -----------------------------------------
    # Storage stays JSON (single writer, tiny data, Save-As copytree safety). The API below is
    # storage-agnostic — nothing outside this module touches ``self._data["groups"]`` — so swapping
    # the backing store for sqlite later stays a one-module change.
    def _raw_groups(self) -> list:
        return self._list_or_empty("groups")

    def _owned_list_for_write(self, key: str) -> list:
        """Return the list stored under ``key``, coercing a wrong-typed value to a fresh list IN
        PLACE. Only mutators call this — replacing an unreadable value is deliberate at the moment the
        user edits that section, unlike a passive load (which must preserve it)."""
        val = self._data.get(key)
        if not isinstance(val, list):
            val = []
            self._data[key] = val
        return val

    def _find(self, gid: str) -> dict | None:
        for g in self._raw_groups():
            if isinstance(g, dict) and g.get("id") == gid:
                return g
        return None

    @staticmethod
    def _dedup(seq) -> list:
        """Order-preserving de-duplication of a member sequence."""
        out: list = []
        for x in seq or []:
            s = str(x)
            if s not in out:
                out.append(s)
        return out

    def create_group(self, name: str) -> str:
        """Create a new empty manual group and return its ``uuid4`` hex id (also appended to order)."""
        gid = uuid.uuid4().hex
        self._owned_list_for_write("groups").append(
            {"id": gid, "name": str(name), "members": []})
        self._owned_list_for_write("group_order").append(gid)
        return gid

    def rename_group(self, gid: str, name: str) -> None:
        g = self._find(gid)
        if g is not None:
            g["name"] = str(name)

    def delete_group(self, gid: str) -> None:
        """Remove the group and every trace of it (its members go with it, plus order/collapsed)."""
        self._data["groups"] = [g for g in self._raw_groups()
                                if not (isinstance(g, dict) and g.get("id") == gid)]
        self._data["group_order"] = [x for x in self.group_order() if x != gid]
        self._data["collapsed"] = [x for x in self._collapsed_raw() if x != gid]

    def add_member(self, gid: str, scan_key: str) -> None:
        g = self._find(gid)
        if g is None:
            return
        g["members"] = self._dedup(list(g.get("members") or []) + [scan_key])

    def remove_member(self, gid: str, scan_key: str) -> None:
        g = self._find(gid)
        if g is None:
            return
        g["members"] = [m for m in self._dedup(g.get("members")) if m != str(scan_key)]

    def groups(self) -> list[GroupRec]:
        """All manual groups as :class:`GroupRec` (members ordered + deduped). Malformed rows skipped."""
        out: list[GroupRec] = []
        for g in self._raw_groups():
            if not isinstance(g, dict) or not g.get("id"):
                continue
            out.append(GroupRec(id=str(g["id"]), name=str(g.get("name") or ""),
                                members=self._dedup(g.get("members"))))
        return out

    def memberships_of(self, scan_key: str) -> list[str]:
        """Ids of the manual groups that contain ``scan_key`` (order follows :meth:`groups`).

        Matching is via :func:`isar_gui.scan_keys.member_matches`, so a legacy scan-level membership
        resolves to every analyzer unit of that scan, while a new-form unit membership matches only
        that unit."""
        from isar_gui.scan_keys import member_matches
        return [g.id for g in self.groups()
                if any(member_matches(m, scan_key) for m in g.members)]

    def group_order(self) -> list[str]:
        return [str(x) for x in self._list_or_empty("group_order")]

    def set_group_order(self, order) -> None:
        self._data["group_order"] = [str(x) for x in (order or [])]

    # ---- ui scraps: collapsed set (auto-group ids are allowed here too) ---
    def _collapsed_raw(self) -> list:
        return self._list_or_empty("collapsed")

    def collapsed(self) -> set[str]:
        return {str(x) for x in self._collapsed_raw()}

    def set_collapsed(self, gid: str, flag: bool) -> None:
        current = self.collapsed()
        if flag:
            current.add(str(gid))
        else:
            current.discard(str(gid))
        self._data["collapsed"] = sorted(current)

    # ---- ui scraps: active scans-dock dimension tab ----------------------
    def scans_tab(self) -> str | None:
        """The last-active scans-dock dimension tab name (``"All"`` / ``"T"`` / ...), or ``None``."""
        val = self._data.get("scans_tab")
        return val if isinstance(val, str) and val else None

    def set_scans_tab(self, name: str) -> None:
        self._data["scans_tab"] = str(name)

    # ---- orthogonal scan filter (Pass 2c) --------------------------------
    # The filter is a view pref, not machine bookkeeping — it lives here, never in card.state.json.
    # Storage is the serialised ScanFilter.to_dict(); this store stays schema-agnostic about its keys.
    def scan_filter(self) -> dict:
        """The persisted filter dict (``ScanFilter.to_dict()`` shape), or ``{}`` when absent/garbage."""
        val = self._data.get("scan_filter")
        if isinstance(val, dict):
            return val
        if val is not None and "scan_filter" not in self._warned:
            self._warned.add("scan_filter")
            log.warning("gui.state.json key 'scan_filter' is %s, not an object — treating as empty "
                        "(value kept on disk for round-trip)", type(val).__name__)
        return {}

    def set_scan_filter(self, d) -> None:
        """Replace the persisted filter with ``d`` (a ``ScanFilter.to_dict()`` result, JSON-safe)."""
        self._data["scan_filter"] = dict(d) if isinstance(d, dict) else {}

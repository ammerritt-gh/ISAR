"""Experiment settings for the BL43 intake: the resolution-curve folder, the gpi folder and descriptive metadata.

Each save appends one entry to ``intake/settings.jsonl`` through ``store``. Nothing here starts work
or checks a curve: a folder that is missing or empty is reported by the curve check, not refused here.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import store

log = logging.getLogger(__name__)

_FOLDER_KINDS = ("resolution_folder", "gpi_folder")
_KINDS = (*_FOLDER_KINDS, "experiment_metadata")
_FOLDER_NAME = {"resolution_folder": "resolution folder", "gpi_folder": "gpi folder"}
_SOURCES = ("user", "discovered")
_BUSY = "the intake store is busy (another ISAR process is saving); try again"


@dataclass(frozen=True)
class SaveResult:
    ok: bool
    reason: str | None          # exact reason when not ok; None when ok
    value: object | None        # canonical accepted value; None when not ok
    targets: tuple[str, ...]    # as given: scan keys, unit keys, or ("experiment",)
    entry_id: str | None        # the saved entry's id; None when not ok


@dataclass(frozen=True)
class ResolutionFolder:
    path: str
    source: str                 # "user" | "discovered"
    entry_id: str
    at: str                     # the entry's UTC time
    found_beside: str | None    # the data folder discovery found it beside; None for "user"


@dataclass(frozen=True)
class ExperimentSettings:
    resolution_folder: ResolutionFolder | None   # None: nothing saved
    metadata: dict[str, str]                     # the latest metadata save, whole; {} when none
    damaged: tuple[store.Damaged, ...] = ()      # settings lines skipped (store- or schema-level), by line
    gpi_folder: ResolutionFolder | None = None   # the gpi_files folder; None: nothing saved


def _problem(e: dict) -> str | None:
    """Why a store entry is not a settings entry this module can use; None when it is."""
    kind = e.get("kind")
    if kind not in _KINDS:
        return f"unknown kind {kind!r}"
    if kind in _FOLDER_KINDS:
        for k in ("path", "source", "at"):
            if not isinstance(e.get(k), str):
                return f"{kind} entry has no {k!r}"
        if e["source"] not in _SOURCES:
            return f"unknown folder source {e['source']!r}"
        if not (e.get("found_beside") is None or isinstance(e["found_beside"], str)):
            return "found_beside is not text"
    else:
        fields = e.get("fields")
        if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
            return "fields must be a mapping of text"
    return None


def _folder(e: dict) -> ResolutionFolder:
    return ResolutionFolder(path=e["path"], source=e["source"], entry_id=e["id"], at=e["at"],
                            found_beside=e.get("found_beside"))


def current_settings(root) -> ExperimentSettings:
    """The highest-``seq`` entry of each kind; a damaged line is skipped (logged) and listed in ``damaged``."""
    read = store.read_all(root, store.SETTINGS)
    damaged = list(read.damaged)
    latest: dict[str, dict] = {}
    for e, line in zip(read.entries, read.lines):
        why = _problem(e)
        if why is not None:
            log.warning("%s line %d skipped: %s", store.SETTINGS, line, why)
            damaged.append(store.Damaged(store.SETTINGS, line, why))
            continue
        if e["kind"] not in latest or e["seq"] > latest[e["kind"]]["seq"]:
            latest[e["kind"]] = e
    folder = latest.get("resolution_folder")
    gpi = latest.get("gpi_folder")
    meta = latest.get("experiment_metadata")
    return ExperimentSettings(
        resolution_folder=_folder(folder) if folder is not None else None,
        metadata=dict(meta["fields"]) if meta is not None else {},
        damaged=tuple(sorted(damaged, key=lambda d: d.line)),
        gpi_folder=_folder(gpi) if gpi is not None else None,
    )


def _refuse(reason: str, targets: tuple[str, ...]) -> SaveResult:
    return SaveResult(False, reason, None, targets, None)


def _write(root, entry: dict, targets: tuple[str, ...], value: object, check=None) -> SaveResult:
    try:
        stored = store.append(root, store.SETTINGS, entry, check=check)
    except store.Refused as r:
        reason = r.reason
    except store.StoreBusy:
        reason = _BUSY
    except OSError as e:
        reason = f"the intake store could not be written: {e}"
    else:
        return SaveResult(True, None, value, targets, stored["id"])
    log.warning("settings save (%s) not written: %s", entry["kind"], reason)
    return _refuse(reason, targets)


def _no_folder_yet(kind: str):
    def check(entries: list[dict]) -> str | None:
        for e in entries:
            if e.get("kind") == kind and _problem(e) is None:   # a damaged folder line never blocks
                f = _folder(e)
                return f"a {_FOLDER_NAME[kind]} is already saved ({f.path}, {f.source}); discovery never replaces it"
        return None
    return check


def save_resolution_folder(root, folder, *, source: str = "user",
                           found_beside: str | None = None) -> SaveResult:
    return _save_folder("resolution_folder", root, folder, source, found_beside)


def save_gpi_folder(root, folder, *, source: str = "user", found_beside: str | None = None) -> SaveResult:
    """The experiment's gpi folder (``gpi_files/``), with ``save_resolution_folder``'s rules."""
    return _save_folder("gpi_folder", root, folder, source, found_beside)


def _save_folder(kind: str, root, folder, source: str, found_beside: str | None) -> SaveResult:
    if source not in _SOURCES:
        raise ValueError(f"source must be 'user' or 'discovered', got {source!r}")
    if source == "discovered" and found_beside is None:
        raise ValueError("a discovered folder needs found_beside")
    if source == "user" and found_beside is not None:
        raise ValueError("found_beside is only recorded for a discovered folder")
    target = ("experiment",)
    if folder is None or not str(folder).strip():
        return _refuse("no folder given", target)
    p = Path(str(folder)).expanduser()
    name = _FOLDER_NAME[kind]
    if not p.is_absolute():
        return _refuse(f"the {name} must be an absolute path: {folder}", target)
    if not p.exists():
        return _refuse(f"{name} {p} does not exist", target)
    if not p.is_dir():
        return _refuse(f"{p} is not a folder", target)
    canonical = str(p.resolve())
    entry = {"kind": kind, "path": canonical, "source": source}
    check = None
    if source == "discovered":
        entry["found_beside"] = found_beside
        check = _no_folder_yet(kind)
    return _write(root, entry, target, canonical, check)


def save_experiment_metadata(root, fields: Mapping[str, str]) -> SaveResult:
    target = ("experiment",)
    if not isinstance(fields, Mapping):
        return _refuse(f"experiment metadata must be a mapping of text fields (got {type(fields).__name__})", target)
    for k, v in fields.items():
        if not isinstance(k, str) or not k.strip():
            return _refuse(f"metadata field name {k!r} must be non-blank text", target)
        if not isinstance(v, str):
            return _refuse(f"metadata field {k!r} must be text (got {type(v).__name__})", target)
    value = dict(fields)
    return _write(root, {"kind": "experiment_metadata", "fields": value}, target, value)

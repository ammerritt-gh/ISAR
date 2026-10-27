"""Append-only, lock-guarded JSONL store for the BL43 intake records.

Entries are only ever added, never rewritten or removed. ``append`` holds one OS lock around its
read-check-write and fsyncs before it returns, so a return means the entry is on disk. ``read``
takes no lock: a concurrent writer at worst leaves a half line that the next read skips.

A damaged line (a torn write, or debris that is not a JSON object or lacks ``id``, ``seq`` or ``at``) is
skipped with a logged warning and listed by ``read_all``; no read or append raises for one, and
nothing rewrites or removes it.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

log = logging.getLogger(__name__)

ENTRIES = "intake/entries.jsonl"     # user values and conditions
SETTINGS = "intake/settings.jsonl"   # experiment settings
LOCK = "intake/.lock"
LOCK_TIMEOUT_S = 2.0                 # bounds a GUI-thread wait; a timeout is StoreBusy, not a hang


class StoreBusy(TimeoutError):
    """The lock was not obtained within LOCK_TIMEOUT_S; nothing was written."""


class Refused(Exception):
    """A ``check`` refused the append; nothing was written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _dumps(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _lock_once(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, 0)  # msvcrt locks from the current position
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout_s: float):
    """An exclusive OS lock on ``path`` (created if missing), released by the OS if the holder dies.
    Not reentrant: a second take in the same process waits like any other. ``StoreBusy`` on timeout."""
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _lock_once(fd)
                break
            except OSError:  # held by another handle; the OS also releases it if the holder dies
                if time.monotonic() >= deadline:
                    raise StoreBusy(f"lock {lock} not obtained within {timeout_s} s") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _locked(root: Path):
    return file_lock(root / LOCK, timeout_s=LOCK_TIMEOUT_S)


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


@dataclass(frozen=True)
class Damaged:
    file: str          # the store file relative to the workspace, "intake/entries.jsonl"
    line: int          # 1-based line number
    why: str           # "does not parse as JSON" | "not a JSON object" | "no str 'id', int 'seq' and str 'at'" | a schema reason


@dataclass(frozen=True)
class StoreRead:
    entries: list[dict]                 # the valid entries, in file order
    damaged: tuple[Damaged, ...]        # in file order
    lines: tuple[int, ...] = ()         # each entry's 1-based line number, so a schema check can name it


def _skip(found: list, name: str, lineno: int, why: str) -> None:
    log.warning("%s line %d skipped: %s", name, lineno, why)
    found.append(Damaged(name, lineno, why))


def _objects(data: bytes, name: str, damaged: list) -> list[tuple[int, dict]]:
    out = []
    for lineno, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:  # bad JSON or UTF-8: a write in progress, or debris from a dead writer
            _skip(damaged, name, lineno, "does not parse as JSON")
            continue
        if not isinstance(obj, dict):
            _skip(damaged, name, lineno, "not a JSON object")
            continue
        out.append((lineno, obj))
    return out


def _read(data: bytes, name: str) -> StoreRead:
    damaged: list[Damaged] = []
    entries, lines = [], []
    for lineno, obj in _objects(data, name, damaged):
        seq = obj.get("seq")
        if isinstance(obj.get("id"), str) and isinstance(seq, int) and not isinstance(seq, bool) \
                and isinstance(obj.get("at"), str):
            entries.append(obj)
            lines.append(lineno)
        else:
            _skip(damaged, name, lineno, "no str 'id', int 'seq' and str 'at'")
    return StoreRead(entries, tuple(sorted(damaged, key=lambda d: d.line)), tuple(lines))


def read_jsonl(path) -> list[dict]:
    """Tolerant line reading with no id/seq check; the fit-inputs log reads through it."""
    path = Path(path)
    return [obj for _, obj in _objects(_bytes(path), str(path), [])]


def read_all(root, name: str) -> StoreRead:
    """The valid entries and every damaged line, both in file order; takes no lock."""
    return _read(_bytes(Path(root) / name), name)


def read(root, name: str) -> list[dict]:
    return read_all(root, name).entries


def append(root, name: str, entry: dict, *, check: Callable[[list[dict]], str | None] | None = None) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"entry must be a dict, got {type(entry).__name__}")
    clash = sorted({"id", "seq", "at"} & entry.keys())
    if clash:
        raise ValueError(f"entry must not set {clash}; the store assigns them")
    try:
        _dumps(entry)
    except (TypeError, ValueError) as e:
        raise ValueError(f"entry does not serialise as JSON: {e}") from e
    root = Path(root)
    path = root / name
    with _locked(root):
        data = _bytes(path)
        # shortcut: O(lines) read per save, fine to tens of thousands of entries; keep a sidecar max-seq past that
        present = _read(data, name).entries          # damaged lines left out, never raised
        if check is not None:
            reason = check(present)
            if reason is not None:
                raise Refused(reason)
        stored = {
            **entry,
            "id": "e" + uuid.uuid4().hex[:12],
            "seq": 1 + max((e["seq"] for e in present), default=0),
            "at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        line = _dumps(stored) + b"\n"
        if data and not data.endswith(b"\n"):
            log.warning("%s: ended an unterminated last line (a torn write) with a newline", path)
            line = b"\n" + line
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    return stored

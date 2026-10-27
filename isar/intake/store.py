"""Append-only, lock-guarded JSONL store for the BL43 intake records.

Entries are only ever added, never rewritten or removed. ``append`` holds one OS lock around its
read-check-write and fsyncs before it returns, so a return means the entry is on disk. ``read``
takes no lock: a concurrent writer at worst leaves a half line that the next read skips.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
def _locked(root: Path):
    lock = root / LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                _lock_once(fd)
                break
            except OSError:  # held by another handle; the OS also releases it if the holder dies
                if time.monotonic() >= deadline:
                    raise StoreBusy(f"intake lock {lock} not obtained within {LOCK_TIMEOUT_S} s") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _objects(data: bytes, path: Path) -> Iterator[tuple[int, dict]]:
    for lineno, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:  # bad JSON or UTF-8: a write in progress, or debris from a dead writer
            log.warning("%s line %d does not parse as JSON; skipped", path, lineno)
            continue
        if not isinstance(obj, dict):
            raise ValueError(f"{path} line {lineno}: expected a JSON object, got {type(obj).__name__}")
        yield lineno, obj


def _entries(data: bytes, path: Path) -> list[dict]:
    out = []
    for lineno, obj in _objects(data, path):
        if not (isinstance(obj.get("id"), str) and isinstance(obj.get("seq"), int)):
            raise ValueError(f"{path} line {lineno}: not an intake entry (needs a str 'id' and an int 'seq')")
        out.append(obj)
    return out


def read_jsonl(path) -> list[dict]:
    """Tolerant line reading with no id/seq check; the fit-inputs log reads through it."""
    path = Path(path)
    return [obj for _, obj in _objects(_bytes(path), path)]


def read(root, name: str) -> list[dict]:
    path = Path(root) / name
    return _entries(_bytes(path), path)


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
        present = _entries(data, path)
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

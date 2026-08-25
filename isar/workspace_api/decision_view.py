"""Reading the LATEST discovery decision back out of a workspace (SCENE-5 5.2, D7 operator alert).

Mirrors ``scene_view``/``agenda_view``'s error posture: absence is graceful (no decisions file, or
an empty one, reads as ``None``); corruption is loud (an unparseable last line raises ``ValueError``
rather than degrading into a warning — a torn write mid-append would otherwise look identical to "no
decision yet", which is a different operator fact). ``isar/drive/steering/planner.py`` is the one
writer (``_append_decision``): one ``isar.discovery-decision/1`` JSON object per line, fsynced,
never rewritten — so the last line is always the newest decision once it exists at all.

Tail-read only: a live campaign's ``decisions.jsonl`` grows for the whole campaign, and this is
called on every GUI poll, so reading the whole file per call is the mistake this module exists to
avoid. Newline (``0x0A``) never appears as a continuation byte inside a multi-byte UTF-8 sequence,
so seeking to a discovered newline and decoding only the bytes after it is always character-safe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DECISIONS_NAME = "decisions.jsonl"
_CHUNK_SIZE = 8192


def _tail_line(path: Path) -> str | None:
    """The last non-empty line of ``path``, decoded as UTF-8 — or ``None`` for an empty file."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        if pos == 0:
            return None
        buf = b""
        while pos > 0:
            read_size = min(_CHUNK_SIZE, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
            stripped = buf.rstrip(b"\n")
            newline_idx = stripped.rfind(b"\n")
            if newline_idx != -1:
                return stripped[newline_idx + 1:].decode("utf-8")
            if pos == 0:
                return stripped.decode("utf-8") if stripped else None
    return None


def latest_decision(ws) -> dict | None:
    """The most recently persisted discovery decision record, or ``None``.

    ``None`` covers both "no ``decisions.jsonl`` yet" and "the file exists but is empty" — both are
    the honest "no decision yet" state, never a corruption signal. Raises ``ValueError`` when the
    last line exists but is not parseable JSON: a torn/corrupt append is loud, not silently absent.
    """
    path = ws._report_dir / _DECISIONS_NAME
    if not path.is_file():
        return None
    line = _tail_line(path)
    if line is None:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"decisions.jsonl {path}: last line is not valid JSON: {exc}") from exc

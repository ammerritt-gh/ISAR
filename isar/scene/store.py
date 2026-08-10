"""Content-addressed persistence for scene revisions (SCENE-3 PR 3c).

Layout under ``<workspace>/report/scene/``::

    <digest>.json    the canonical bytes of one revision — exactly what the digest covers
    current.json     a pointer at the revision the workspace is currently showing

**Lineage and time live in the pointer, never in the blob.** A revision has to record what it
succeeded, and an unchanged workspace has to rebuild to the *same* digest with the pointer
unmoved — that is what makes a scene replayable. Those two requirements are only compatible if
``parent`` and ``written_at`` sit outside the digested payload: put either inside and every
rebuild mints a new digest, the store grows a blob per run, and "did anything actually change?"
stops being answerable by comparing hashes.

**Blob first, pointer second.** A crash between the two leaves a valid old pointer plus an
orphaned new blob, which is safe and self-healing on the next write. The pointer is convenience,
never authority: the blobs are the record, they are addressable by digest forever, and nothing
here ever deletes one.

The atomic-write idiom (temp file in the target directory → flush → ``fsync`` → ``os.replace``)
mirrors ``isar/session/master_delivery.py:_persist_content_addressed``; it is reimplemented
rather than imported because ``isar/scene`` is import-banned from ``isar.session``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .build import SceneRevision
from .model import canonical_bytes, digest_bytes

log = logging.getLogger(__name__)

POINTER_SCHEMA = "isar.scene-pointer/1"


@dataclass(frozen=True, slots=True)
class StoreResult:
    """What one ``write_revision`` did. ``unchanged`` means the pointer already named ``digest``
    and nothing on disk moved."""

    digest: str
    path: Path
    unchanged: bool
    parent: str | None


def _scene_dir(report_dir) -> Path:
    return Path(report_dir) / "scene"


def _write_atomic(target: Path, payload: bytes) -> None:
    """Replace ``target`` with ``payload``, atomically, durably, or not at all."""
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError as cleanup_error:
            log.warning("could not remove incomplete scene artifact %s: %s",
                        temporary, cleanup_error)
        raise


def _read_pointer(root: Path) -> dict | None:
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"unreadable scene pointer {pointer_path}: {error}") from error
    if not isinstance(pointer, dict) or not isinstance(pointer.get("digest"), str):
        raise ValueError(f"scene pointer {pointer_path} names no digest: {pointer!r}")
    return pointer


def write_revision(report_dir, revision: SceneRevision) -> StoreResult:
    """Publish ``revision`` and point ``current.json`` at it.

    Idempotent in both halves: a pointer already naming this digest is left completely alone
    (``unchanged``), and an existing blob is never rewritten — content-addressed bytes cannot
    differ from themselves.
    """
    root = _scene_dir(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    previous = _read_pointer(root)
    target = root / f"{revision.digest}.json"

    if previous is not None and previous["digest"] == revision.digest:
        return StoreResult(digest=revision.digest, path=target, unchanged=True,
                           parent=previous.get("parent"))

    if not target.exists():
        _write_atomic(target, revision.canonical)

    parent = None if previous is None else previous["digest"]
    pointer = {"schema": POINTER_SCHEMA, "digest": revision.digest, "parent": parent,
               "written_at": datetime.now(timezone.utc).isoformat()}
    _write_atomic(root / "current.json", canonical_bytes(pointer))
    return StoreResult(digest=revision.digest, path=target, unchanged=False, parent=parent)


def read_current(report_dir) -> tuple[dict, dict] | None:
    """``(pointer, payload)`` for the current revision, or ``None`` if there is not one yet.

    Absence is normal — a workspace with no scene, or a crash between blob and pointer, both
    read as the last consistent state. Corruption never is: a pointer naming a missing blob,
    unparseable JSON, or a blob whose bytes do not hash to its own filename raises ``ValueError``
    naming the path, rather than handing back a payload nobody can vouch for.
    """
    root = _scene_dir(report_dir)
    pointer = _read_pointer(root)
    if pointer is None:
        return None

    blob_path = root / f"{pointer['digest']}.json"
    if not blob_path.is_file():
        raise ValueError(f"scene pointer names missing revision {blob_path}")
    try:
        payload = json.loads(blob_path.read_bytes().decode("utf-8"))  # strict: no Infinity/NaN
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"unreadable scene revision {blob_path}: {error}") from error

    # Recomputed from the parsed payload, not from the raw bytes: this checks that the file both
    # hashes to its name AND is in canonical form, so a hand-edit that preserved the byte count
    # or a rewrite by a non-canonical encoder is caught rather than replayed as authentic.
    actual = digest_bytes(canonical_bytes(payload))
    if actual != pointer["digest"]:
        raise ValueError(f"scene revision {blob_path} does not match its digest "
                         f"(recomputed {actual})")
    return pointer, payload

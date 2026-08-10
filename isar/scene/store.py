"""Content-addressed persistence for scene revisions (SCENE-3 PR 3c).

Layout under ``<workspace>/report/scene/``::

    <digest>.json    the canonical bytes of one revision — exactly what the digest covers
    current.json     a pointer at the revision the workspace is currently showing
    history.jsonl    one append-only line per pointer flip: the durable lineage
    last_attempt.json  what the last build-and-store attempt did — including that it failed

**Lineage and time live outside the blob.** A revision has to record what it succeeded, and an
unchanged workspace has to rebuild to the *same* digest with the pointer unmoved — that is what
makes a scene replayable. Those two requirements are only compatible if ``parent`` and
``written_at`` sit outside the digested payload: put either inside and every rebuild mints a new
digest, the store grows a blob per run, and "did anything actually change?" stops being answerable
by comparing hashes.

The DURABLE lineage is ``history.jsonl``, not the pointer. ``current.json`` carries only the latest
link, so A→B→C leaves nothing on disk that remembers B→A; the append-only log keeps every link,
in keeping with the house posture that nothing is ever deleted. Each line records ``digest``,
``parent``, ``written_at`` and the ``source`` that flipped the pointer. A rebuild that changed
nothing appends nothing — no flip happened.

**Blob first, pointer second.** A crash between the two leaves a valid old pointer plus an
orphaned new blob, which is safe and self-healing on the next write. The pointer is convenience,
never authority: the blobs are the record, they are addressable by digest forever, and nothing
here ever deletes one.

**Existence is not integrity.** A blob already at the target name is compared byte for byte with
the revision before anything relies on it, and rewritten when it differs — a corrupt current blob
therefore heals on a rebuild of the same scene, and a corrupt orphan is repaired before the pointer
can ever flip onto it.

**Two writers, no lock.** ``isar report --scene`` can run against a live campaign, so single-writer
is false. Concurrency is optimistic: a writer that captured the pointer digest when it snapshotted
its inputs passes it as ``expected_parent`` and the flip refuses with
:class:`ConcurrentSceneWrite` if the pointer moved meanwhile. A lock file is deliberately not used —
a stale lock left by a crash would block every subsequent campaign, which is worse than the race it
prevents. The blob write itself is content-addressed and safe under any interleaving.

The atomic-write idiom (temp file in the target directory → flush → ``fsync`` → ``os.replace``)
mirrors ``isar/session/master_delivery.py:_persist_content_addressed``; it is reimplemented
rather than imported because ``isar/scene`` is import-banned from ``isar.session``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .build import SceneRevision
from .model import canonical_bytes, digest_bytes

log = logging.getLogger(__name__)

POINTER_SCHEMA = "isar.scene-pointer/1"
HISTORY_NAME = "history.jsonl"
ATTEMPT_NAME = "last_attempt.json"

# A digest is a sha256 hexdigest and nothing else. Validated on every read because the pointer's
# digest becomes a FILE NAME: a value carrying a separator or a traversal segment would otherwise
# make the store read whatever path it spelled.
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")

# "No expectation declared", which is NOT the same as expecting ``None`` (an empty store).
UNSET = object()


class ConcurrentSceneWrite(ValueError):
    """Another writer moved the pointer between this writer's snapshot and its flip.

    A ``ValueError`` subclass on purpose: every existing caller already handles ``ValueError``
    from this module (the driver hook logs and skips, ``isar report --scene`` exits 3), so a
    losing writer degrades to the same visible skip rather than an unhandled traceback.
    """


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
    """Replace ``target`` with ``payload`` in one step, or leave it as it was.

    A reader never sees a half-written file: the content is fully written and fsynced under a
    temporary name first, and ``os.replace`` swaps it in atomically. Deliberately NOT a
    crash-durability guarantee — the containing directory is not fsynced (there is no portable
    way to on Windows), so a power loss can still lose the newly published name. That is the
    case the layout already handles: a lost pointer reads as absence, a lost blob under a
    surviving pointer raises loudly rather than replaying something wrong, and either is
    republished by the next build of the same workspace.
    """
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
    """The validated pointer, or ``None`` when the workspace has no scene yet.

    Every field is checked BEFORE any of them is used, on both the read and the write path. A
    pointer is machine-written state that decides which file is read as the current scene, so a
    foreign or future ``schema`` refuses by name rather than being interpreted as a ``/1`` (H1:
    no compatibility reader, no guessing), and ``digest``/``parent`` must be sha256 hexdigests —
    which is also what keeps a pointer from naming a path instead of a blob.
    """
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"unreadable scene pointer {pointer_path}: {error}") from error
    if not isinstance(pointer, dict):
        raise ValueError(f"scene pointer {pointer_path} is not an object: {pointer!r}")
    schema = pointer.get("schema")
    if schema != POINTER_SCHEMA:
        raise ValueError(f"scene pointer {pointer_path} declares schema {schema!r}, expected "
                         f"{POINTER_SCHEMA!r} — this reader will not interpret it")
    if not _is_digest(pointer.get("digest")):
        raise ValueError(f"scene pointer {pointer_path} names no digest: {pointer!r}")
    parent = pointer.get("parent")
    if parent is not None and not _is_digest(parent):
        raise ValueError(f"scene pointer {pointer_path} names a malformed parent {parent!r}")
    if not isinstance(pointer.get("written_at"), str):
        raise ValueError(f"scene pointer {pointer_path} carries no written_at string: {pointer!r}")
    return pointer


def _is_digest(value) -> bool:
    return isinstance(value, str) and _DIGEST.match(value) is not None


def pointer_digest(report_dir: Path) -> str | None:
    """The digest ``current.json`` names right now, or ``None`` — a writer's SNAPSHOT token.

    Read it when the inputs are snapshotted (before building), pass it back as
    ``write_revision(..., expected_parent=...)``, and the flip refuses if another writer published
    in between. Raises the same ``ValueError`` a damaged pointer raises everywhere else.
    """
    return (_read_pointer(_scene_dir(report_dir)) or {}).get("digest")


def _ensure_blob(target: Path, revision: SceneRevision) -> None:
    """Leave exactly ``revision.canonical`` at ``target``, whatever was there before.

    Existence is not integrity: a blob at the right name with the wrong bytes is the one state a
    content-addressed store cannot detect by looking at names, and it reads as corruption forever.
    The comparison is a read of a file that is about to be relied on, so it is done ALWAYS — for
    the current revision (whose repair is what makes ``unchanged=True`` honest) and for an orphan
    at a new digest (repaired before the pointer can flip onto it).
    """
    if target.exists():
        if target.read_bytes() == revision.canonical:
            return
        log.warning("scene revision %s did not match its own digest — rewriting it", target)
    _write_atomic(target, revision.canonical)


def _append_history(root: Path, record: dict) -> None:
    """Append one lineage line. Durable per line: written whole, flushed and fsynced.

    Append-only because the pointer forgets: ``current.json`` carries the latest link only, so
    A→B→C leaves no trace of B→A anywhere else.
    """
    with open(root / HISTORY_NAME, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def record_attempt(report_dir: Path, record: dict) -> None:
    """Record the outcome of one build-and-store attempt in ``last_attempt.json``. NEVER raises.

    The scene hook cannot fail a delivery, so without this a scene that stopped building looks
    exactly like a scene that had nothing to rebuild — the pointer simply stays where it was. The
    sidecar says which: ``{"status": "ok", "digest", "written_at"}`` or ``{"status": "failed",
    "reason", "written_at"}``. It is a diagnostic, never authority; the pointer and the blobs are
    unaffected by it, which is why its own failure is logged and dropped rather than raised into a
    caller that is already reporting something else.
    """
    try:
        root = _scene_dir(report_dir)
        root.mkdir(parents=True, exist_ok=True)
        payload = {**record, "written_at": datetime.now(timezone.utc).isoformat()}
        _write_atomic(root / ATTEMPT_NAME, canonical_bytes(payload))
    except (OSError, ValueError) as error:      # never swallowed silently (repo code hygiene)
        log.warning("could not record the scene attempt under %s: %s", report_dir, error)


def read_last_attempt(report_dir: Path) -> dict | None:
    """The last recorded attempt, or ``None`` when there is none or it cannot be read.

    Unlike the pointer, an unreadable sidecar is not loud: it decides nothing, and refusing to
    show a stored revision because a diagnostic file is damaged would trade a real answer for a
    cosmetic one. The damage is logged.
    """
    path = _scene_dir(report_dir) / ATTEMPT_NAME
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        log.warning("unreadable scene attempt record %s: %s", path, error)
        return None
    return record if isinstance(record, dict) else None


def write_revision(report_dir: Path, revision: SceneRevision, *,
                   expected_parent=UNSET, source: str = "driver") -> StoreResult:
    """Publish ``revision`` and point ``current.json`` at it.

    Idempotent: a pointer already naming this digest is left completely alone — ``unchanged``,
    down to its ``written_at`` — and nothing is appended to the history, because no flip happened.

    The blob is ensured — written, or verified byte for byte and repaired — BEFORE the unchanged
    test and before any pointer flip. Otherwise a workspace whose current blob had been lost or
    corrupted would short-circuit on the pointer and report a cheerful ``unchanged`` while
    ``read_current`` went on raising forever, and a corrupt orphan at a new digest would have the
    pointer flipped onto it. Ensuring first makes ``unchanged`` honest: the content on disk really
    is this revision.

    ``expected_parent`` is optimistic concurrency. Supply the digest that ``pointer_digest``
    returned when the caller SNAPSHOTTED its inputs and the flip happens only if the pointer still
    names it; otherwise :class:`ConcurrentSceneWrite` names both digests and nothing moves. The
    unchanged path is deliberately not guarded by it: if the pointer already names this exact
    revision then the state on disk is the state this writer wanted, whoever put it there. Omit
    the argument (single-writer callers, tests) and no check is made.

    ``source`` is recorded in the history line so lineage says WHICH writer flipped the pointer.

    A lost *pointer* with surviving blobs restarts the pointer's own lineage (``parent`` is
    ``None`` again). The history log is unaffected and still holds every earlier link, and every
    superseded revision stays addressable by its digest.
    """
    root = _scene_dir(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    previous = _read_pointer(root)
    target = root / f"{revision.digest}.json"
    _ensure_blob(target, revision)

    if previous is not None and previous["digest"] == revision.digest:
        return StoreResult(digest=revision.digest, path=target, unchanged=True,
                           parent=previous.get("parent"))

    parent = None if previous is None else previous["digest"]
    if expected_parent is not UNSET and parent != expected_parent:
        raise ConcurrentSceneWrite(
            f"scene pointer moved to {parent} since this writer snapshotted at "
            f"{expected_parent}; refusing to publish {revision.digest} over it. The blob is "
            f"written and addressable at {target}.")
    written_at = datetime.now(timezone.utc).isoformat()
    pointer = {"schema": POINTER_SCHEMA, "digest": revision.digest, "parent": parent,
               "written_at": written_at}
    _write_atomic(root / "current.json", canonical_bytes(pointer))
    _append_history(root, {"digest": revision.digest, "parent": parent,
                           "written_at": written_at, "source": str(source)})
    return StoreResult(digest=revision.digest, path=target, unchanged=False, parent=parent)


def read_current(report_dir: Path) -> tuple[dict, dict] | None:
    """``(pointer, payload)`` for the current revision, or ``None`` if there is not one yet.

    Absence is normal — a workspace with no scene, or a crash between blob and pointer, both
    read as the last consistent state. Corruption never is: a pointer that fails validation (see
    :func:`_read_pointer`), a pointer naming a missing blob, unparseable JSON, or a blob whose
    bytes do not hash to its own filename raises ``ValueError`` naming the path, rather than
    handing back a payload nobody can vouch for.
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

    # Recomputed from the PARSED payload, not from the raw bytes: the question a replay asks is
    # "is this the content the digest names?", so an edit to any value is caught while a
    # re-indented or differently-ordered file that still means the same thing is accepted. What
    # this therefore does NOT assert is that the bytes on disk are themselves canonical.
    actual = digest_bytes(canonical_bytes(payload))
    if actual != pointer["digest"]:
        raise ValueError(f"scene revision {blob_path} does not match its digest "
                         f"(recomputed {actual})")
    return pointer, payload

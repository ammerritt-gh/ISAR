"""Content-addressed persistence for scene revisions (SCENE-3 PR 3c; generic core SCENE-4 PR 4.1).

Layout under ``<workspace>/report/<dir_name>/`` (``scene/`` for the scene store)::

    <digest>.json    the canonical bytes of one revision — exactly what the digest covers
    current.json     a pointer at the revision the workspace is currently showing
    history.jsonl    one append-only line per pointer flip: the durable lineage
    last_attempt.json  what the last build-and-store attempt did — including that it failed

The store core is :class:`RevisionStore`, parameterized by directory name, pointer schema, and
the artifact label used in diagnostics — the agenda store (SCENE-4 PR 4.1, ``isar/scene/agenda.py``)
is a second binding of the same core, not a second implementation. It persists any revision
object exposing ``.canonical`` (the digested bytes) and ``.digest`` (their sha256 hexdigest);
``SceneRevision`` and ``AgendaRevision`` both do. The module-level functions are the scene
binding's original public API, delegating to :data:`SCENE_STORE` unchanged in signature and
semantics. The store's optimistic-concurrency limitations are unchanged by the generalization.

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
nothing appends nothing — no flip happened. The guarantee is COMPLETED flips: a crash between the
pointer flip and the append leaves the newest link unlogged, so the pointer stays authoritative for
the latest state and the log is authoritative for the chain behind it.

**Blob first, pointer second.** A crash between the two leaves a valid old pointer plus an
orphaned new blob, which is safe and self-healing on the next write. The pointer is convenience,
never authority: the blobs are the record, they are addressable by digest forever, and nothing
here ever deletes one.

**Existence is not integrity.** A blob already at the target name is compared byte for byte with
the revision before anything relies on it, and rewritten when it differs — a corrupt current blob
therefore heals on a rebuild of the same scene, and a corrupt orphan is repaired before the pointer
can ever flip onto it.

**Two writers, ONE interprocess lock around the pointer flip** (PR #33 review B-2: the previous
optimistic-only design let two processes both pass the ``expected_parent`` check off one stale
read and both "win"). ``isar report --scene`` can run against a live campaign, so single-writer
is false: every read-pointer → compare-``expected_parent`` → write-pointer/append-history step
in :meth:`RevisionStore.write_revision` now runs inside a per-store ``current.lock`` critical
section (:func:`_locked`) — ``os.open(..., O_CREAT | O_EXCL)``, the one cross-platform atomic
"only one caller creates this file" primitive, bounded retry (~5s total) before raising
:class:`StoreLockTimeout` naming the lock path. The blob write itself stays OUTSIDE the lock: it
is content-addressed and idempotent — two writers publishing identical bytes at the same digest
converge safely under any interleaving — so serializing it would only add contention with no
correctness gain. A lock file is not "deliberately avoided" as it was before: a crash leaving one
behind would wedge every later writer forever, which is exactly what the STALE-LOCK AGE TAKEOVER
answers — a lock file older than the age threshold is presumed abandoned (a live writer holds it
only for the duration of one pointer flip, which is short) and is removed, with a logged warning,
before the next attempt.

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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


class ConcurrentRevisionWrite(ValueError):
    """Another writer moved the pointer between this writer's snapshot and its flip.

    A ``ValueError`` subclass on purpose: every existing caller already handles ``ValueError``
    from this module (the driver hook logs and skips, ``isar report --scene`` exits 3), so a
    losing writer degrades to the same visible skip rather than an unhandled traceback.
    """


class StaleSceneAtPublication(ValueError):
    """A caller's own publication-time re-read found the SCENE pointer had moved since it
    snapshotted the scene at the start of a derivation (PR #33 A-P0-4).

    Not raised by this module -- ``RevisionStore`` has no notion of "the scene store in
    particular" or of a caller's earlier snapshot taken from a DIFFERENT store than the one being
    written. It lives here anyway, beside :class:`ConcurrentRevisionWrite`, for the same layering
    reason: ``isar.scene`` sits below ``isar.drive`` on the import graph, and an exception type a
    consumer above it needs to catch has to live at or below the boundary, never above it (the
    one raiser today is ``isar.drive.steering.planner``). A ``ValueError`` subclass for the same
    reason ``ConcurrentRevisionWrite`` is one: an existing ``except ValueError`` degrades this the
    same way it already degrades a concurrent pointer write.

    **This narrows the A-P0-4 window, it does not close it.** The re-read runs OUTSIDE any
    store's :func:`_locked` critical section -- it is an application-level sanity check against
    the SCENE store, a different store than the one about to be written (the AGENDA store), so it
    cannot itself be atomic with that write. The agenda write immediately after it still goes
    through the agenda store's own ``expected_parent`` CAS and interprocess lock, which is what
    actually refuses a lineage fork; this check only shrinks the gap between "read the scene" and
    "publish something built from it". The residual sliver -- a scene write landing in the
    instant between this re-read and the agenda store's own lock acquisition -- is that lock's
    territory, not this check's.
    """


class StoreLockTimeout(ValueError):
    """Another writer held the store lock past the wait budget (PR #33 review B-2).

    Also a ``ValueError`` subclass, same reasoning as :class:`ConcurrentRevisionWrite`: an
    existing ``except ValueError`` at a call site degrades a lock timeout the same way it
    already degrades a concurrent-write refusal, rather than needing a second except clause.
    """


# Lock tuning (PR #33 review B-2). No config surface yet — a fixed, generous budget for a
# critical section that holds for the duration of one pointer flip (microseconds to low
# milliseconds), never a whole build.
_LOCK_NAME = "current.lock"
_LOCK_STALE_AGE_S = 30.0
_LOCK_WAIT_TOTAL_S = 5.0
_LOCK_RETRY_INTERVAL_S = 0.05


@contextmanager
def _locked(root: Path, artifact_label: str):
    """Interprocess mutual exclusion around one store's pointer flip.

    ``os.open(path, O_CREAT | O_EXCL)`` is the one primitive both POSIX and Windows implement as
    an atomic "create this file iff it does not already exist" — Python's ``os`` module maps it
    onto ``CreateFileW(..., CREATE_NEW, ...)`` on Windows, so this needs no platform branch.

    Bounded retry (~:data:`_LOCK_WAIT_TOTAL_S` total) before raising :class:`StoreLockTimeout`
    naming the lock path — a caller that cannot get the lock in a few seconds is contending with
    something stuck, not a normal flip. A lock file older than :data:`_LOCK_STALE_AGE_S` is
    presumed abandoned by a crashed writer (a live holder's critical section is short) and is
    removed with a logged warning, so a crash can never wedge a workspace forever the way the
    module docstring's retired "no lock" reasoning worried about.
    """
    lock_path = root / _LOCK_NAME
    deadline = time.monotonic() + _LOCK_WAIT_TOTAL_S
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                continue  # vanished between the failed open and the stat -- retry NOW
            if age > _LOCK_STALE_AGE_S and not _lock_owner_alive(lock_path):
                # Age alone is not proof of abandonment (a debugger pause or a slow
                # filesystem can hold a LIVE writer past any threshold, and deleting a live
                # writer's lock reintroduces the exact two-writer race this lock exists to
                # prevent — review W2-1). Takeover requires BOTH: stale by age AND the
                # recorded owner process is verifiably gone.
                log.warning(
                    "%s lock %s is %.1fs old (> %.0fs) and its owner %s is gone -- taking "
                    "it over as abandoned",
                    artifact_label, lock_path, age, _LOCK_STALE_AGE_S,
                    _lock_owner_of(lock_path))
                try:
                    lock_path.unlink()
                except OSError:
                    pass  # another writer already took it over; loop and retry the open
                continue
            if time.monotonic() >= deadline:
                raise StoreLockTimeout(
                    f"{artifact_label} store lock {lock_path} (held by "
                    f"{_lock_owner_of(lock_path)}) was still held after "
                    f"{_LOCK_WAIT_TOTAL_S:.0f}s -- another writer appears to be stuck; remove "
                    "the lock file by hand if it is not")
            time.sleep(_LOCK_RETRY_INTERVAL_S)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        removed = False
        for _ in range(3):
            try:
                lock_path.unlink()
                removed = True
                break
            except FileNotFoundError:
                removed = True  # a stale-takeover raced us; nothing left to release
                break
            except OSError:
                time.sleep(0.05)  # transient handle (antivirus etc.) -- brief retry
        if not removed:
            # A lock we cannot release stalls every later writer for the full stale window;
            # that is operator-visible breakage, not a shrug (review W2-2).
            log.error("could not remove %s lock %s after retries; later writers will stall "
                      "until the stale-lock takeover clears it", artifact_label, lock_path)


def _lock_owner_of(lock_path: Path) -> str:
    """The owner PID recorded inside a lock file, for diagnostics -- 'unknown' if unreadable."""
    try:
        text = lock_path.read_text(encoding="ascii").strip()
        return f"pid {int(text)}"
    except (OSError, ValueError):
        return "unknown owner"


def _lock_owner_alive(lock_path: Path) -> bool:
    """Whether the process that wrote the lock file still exists.

    Unreadable or unparsable owner records count as ALIVE: takeover must be provably safe,
    and "we could not tell" is not proof of death -- the timeout path (which only stalls,
    never races) handles the truly unknowable case.
    """
    try:
        pid = int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return True
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # no such process
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            STILL_ACTIVE = 259
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else


# The scene binding's original exception name; existing callers catch it by this spelling.
ConcurrentSceneWrite = ConcurrentRevisionWrite


@dataclass(frozen=True, slots=True)
class StoreResult:
    """What one ``write_revision`` did. ``unchanged`` means the pointer already named ``digest``
    and nothing on disk moved."""

    digest: str
    path: Path
    unchanged: bool
    parent: str | None


def _write_atomic(target: Path, payload: bytes, label: str) -> None:
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
            log.warning("could not remove incomplete %s artifact %s: %s",
                        label, temporary, cleanup_error)
        raise


def _is_digest(value) -> bool:
    return isinstance(value, str) and _DIGEST.match(value) is not None


@dataclass(frozen=True, slots=True)
class RevisionStore:
    """One binding of the content-addressed store core.

    ``dir_name`` is the directory under ``<workspace>/report/``, ``pointer_schema`` the exact
    schema string ``current.json`` must declare, and ``artifact_label`` the word diagnostics use
    for the artifact family — an agenda error must not wear scene wording. The revision objects
    it persists are duck-typed: anything with ``.canonical`` bytes and their ``.digest``.
    """

    dir_name: str
    pointer_schema: str
    artifact_label: str

    def _root(self, report_dir) -> Path:
        return Path(report_dir) / self.dir_name

    def _read_pointer(self, root: Path) -> dict | None:
        """The validated pointer, or ``None`` when the workspace has none of this artifact yet.

        Every field is checked BEFORE any of them is used, on both the read and the write path. A
        pointer is machine-written state that decides which file is read as the current revision,
        so a foreign or future ``schema`` refuses by name rather than being interpreted as this
        one (H1: no compatibility reader, no guessing), and ``digest``/``parent`` must be sha256
        hexdigests — which is also what keeps a pointer from naming a path instead of a blob.
        """
        pointer_path = root / "current.json"
        if not pointer_path.is_file():
            return None
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(
                f"unreadable {self.artifact_label} pointer {pointer_path}: {error}") from error
        if not isinstance(pointer, dict):
            raise ValueError(
                f"{self.artifact_label} pointer {pointer_path} is not an object: {pointer!r}")
        schema = pointer.get("schema")
        if schema != self.pointer_schema:
            raise ValueError(
                f"{self.artifact_label} pointer {pointer_path} declares schema {schema!r}, "
                f"expected {self.pointer_schema!r} — this reader will not interpret it")
        if not _is_digest(pointer.get("digest")):
            raise ValueError(
                f"{self.artifact_label} pointer {pointer_path} names no digest: {pointer!r}")
        parent = pointer.get("parent")
        if parent is not None and not _is_digest(parent):
            raise ValueError(
                f"{self.artifact_label} pointer {pointer_path} names a malformed parent "
                f"{parent!r}")
        if not isinstance(pointer.get("written_at"), str):
            raise ValueError(
                f"{self.artifact_label} pointer {pointer_path} carries no written_at string: "
                f"{pointer!r}")
        return pointer

    def pointer_digest(self, report_dir: Path) -> str | None:
        """The digest ``current.json`` names right now, or ``None`` — a writer's SNAPSHOT token.

        Read it when the inputs are snapshotted (before building), pass it back as
        ``write_revision(..., expected_parent=...)``, and the flip refuses if another writer
        published in between. Raises the same ``ValueError`` a damaged pointer raises everywhere
        else.
        """
        return (self._read_pointer(self._root(report_dir)) or {}).get("digest")

    def _ensure_blob(self, target: Path, revision) -> None:
        """Leave exactly ``revision.canonical`` at ``target``, whatever was there before.

        Existence is not integrity: a blob at the right name with the wrong bytes is the one
        state a content-addressed store cannot detect by looking at names, and it reads as
        corruption forever. The comparison is a read of a file that is about to be relied on, so
        it is done ALWAYS — for the current revision (whose repair is what makes
        ``unchanged=True`` honest) and for an orphan at a new digest (repaired before the pointer
        can flip onto it).
        """
        if target.exists():
            if target.read_bytes() == revision.canonical:
                return
            log.warning("%s revision %s did not match its own digest — rewriting it",
                        self.artifact_label, target)
        _write_atomic(target, revision.canonical, self.artifact_label)

    def _append_history(self, root: Path, record: dict) -> None:
        """Append one lineage line. Durable per line: written whole, flushed and fsynced.

        Append-only because the pointer forgets: ``current.json`` carries the latest link only,
        so A→B→C leaves no trace of B→A anywhere else.
        """
        with open(root / HISTORY_NAME, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_attempt(self, report_dir: Path, record: dict) -> None:
        """Record the outcome of one build-and-store attempt in ``last_attempt.json``.
        NEVER raises.

        The scene hook cannot fail a delivery, so without this a scene that stopped building
        looks exactly like a scene that had nothing to rebuild — the pointer simply stays where
        it was. The sidecar says which: ``{"status": "ok", "digest", "written_at"}`` or
        ``{"status": "failed", "reason", "written_at"}``. It is a diagnostic, never authority;
        the pointer and the blobs are unaffected by it, which is why its own failure is logged
        and dropped rather than raised into a caller that is already reporting something else.
        """
        try:
            root = self._root(report_dir)
            root.mkdir(parents=True, exist_ok=True)
            payload = {**record, "written_at": datetime.now(timezone.utc).isoformat()}
            _write_atomic(root / ATTEMPT_NAME, canonical_bytes(payload), self.artifact_label)
        except (OSError, ValueError) as error:  # never swallowed silently (repo code hygiene)
            log.warning("could not record the %s attempt under %s: %s",
                        self.artifact_label, report_dir, error)

    def read_last_attempt(self, report_dir: Path) -> dict | None:
        """The last recorded attempt, or ``None`` when there is none or it cannot be read.

        Unlike the pointer, an unreadable sidecar is not loud: it decides nothing, and refusing
        to show a stored revision because a diagnostic file is damaged would trade a real answer
        for a cosmetic one. The damage is logged.
        """
        path = self._root(report_dir) / ATTEMPT_NAME
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            log.warning("unreadable %s attempt record %s: %s", self.artifact_label, path, error)
            return None
        return record if isinstance(record, dict) else None

    def write_revision(self, report_dir: Path, revision, *,
                       expected_parent=UNSET, source: str = "driver") -> StoreResult:
        """Publish ``revision`` and point ``current.json`` at it.

        Idempotent: a pointer already naming this digest is left completely alone —
        ``unchanged``, down to its ``written_at`` — and nothing is appended to the history,
        because no flip happened.

        The blob is ensured — written, or verified byte for byte and repaired — BEFORE the
        unchanged test and before any pointer flip. Otherwise a workspace whose current blob had
        been lost or corrupted would short-circuit on the pointer and report a cheerful
        ``unchanged`` while ``read_current`` went on raising forever, and a corrupt orphan at a
        new digest would have the pointer flipped onto it. Ensuring first makes ``unchanged``
        honest: the content on disk really is this revision.

        ``expected_parent`` is optimistic concurrency. Supply the digest that ``pointer_digest``
        returned when the caller SNAPSHOTTED its inputs and the flip happens only if the pointer
        still names it; otherwise :class:`ConcurrentRevisionWrite` names both digests and nothing
        moves. Omit the argument (single-writer callers, tests) and no check is made.

        ``source`` is recorded in the history line so lineage says WHICH writer flipped the
        pointer.

        A lost *pointer* with surviving blobs restarts the pointer's own lineage (``parent`` is
        ``None`` again). The history log is unaffected and still holds every earlier link, and
        every superseded revision stays addressable by its digest.

        **Locking (PR #33 review B-2).** The pointer read, the ``expected_parent`` comparison
        (including the "already unchanged" case), the pointer write, and the history append all
        run inside :func:`_locked` — a single interprocess critical section per store, so two
        concurrent writers can no longer both read the same stale pointer and both pass their
        own ``expected_parent`` check. The unchanged check is INSIDE the lock, not exempted from
        it: it reads the pointer, and a read taken outside the lock could be the exact stale read
        that let two writers both believe nothing needed to change. The blob write
        (:meth:`_ensure_blob`, just above the lock) stays outside deliberately — it is
        content-addressed and idempotent, so any interleaving of two writers publishing the
        identical bytes converges on the same correct file, and serializing it would only add
        contention with no correctness gain.
        """
        root = self._root(report_dir)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{revision.digest}.json"
        self._ensure_blob(target, revision)

        with _locked(root, self.artifact_label):
            previous = self._read_pointer(root)
            if previous is not None and previous["digest"] == revision.digest:
                return StoreResult(digest=revision.digest, path=target, unchanged=True,
                                   parent=previous.get("parent"))

            parent = None if previous is None else previous["digest"]
            if expected_parent is not UNSET and parent != expected_parent:
                raise ConcurrentRevisionWrite(
                    f"{self.artifact_label} pointer moved to {parent} since this writer "
                    f"snapshotted at {expected_parent}; refusing to publish {revision.digest} "
                    f"over it. The blob is written and addressable at {target}.")
            written_at = datetime.now(timezone.utc).isoformat()
            pointer = {"schema": self.pointer_schema, "digest": revision.digest,
                      "parent": parent, "written_at": written_at}
            _write_atomic(root / "current.json", canonical_bytes(pointer), self.artifact_label)
            self._append_history(root, {"digest": revision.digest, "parent": parent,
                                        "written_at": written_at, "source": str(source)})
            return StoreResult(digest=revision.digest, path=target, unchanged=False,
                               parent=parent)

    def read_current(self, report_dir: Path) -> tuple[dict, dict] | None:
        """``(pointer, payload)`` for the current revision, or ``None`` if there is not one yet.

        Absence is normal — a workspace with no revision, or a crash between blob and pointer,
        both read as the last consistent state. Corruption never is: a pointer that fails
        validation (see ``_read_pointer``), a pointer naming a missing blob, unparseable JSON, or
        a blob whose bytes do not hash to its own filename raises ``ValueError`` naming the path,
        rather than handing back a payload nobody can vouch for.
        """
        root = self._root(report_dir)
        pointer = self._read_pointer(root)
        if pointer is None:
            return None

        blob_path = root / f"{pointer['digest']}.json"
        if not blob_path.is_file():
            raise ValueError(
                f"{self.artifact_label} pointer names missing revision {blob_path}")
        try:
            payload = json.loads(blob_path.read_bytes().decode("utf-8"))  # strict: no Inf/NaN
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(
                f"unreadable {self.artifact_label} revision {blob_path}: {error}") from error

        # Recomputed from the PARSED payload, not from the raw bytes: the question a replay asks
        # is "is this the content the digest names?", so an edit to any value is caught while a
        # re-indented or differently-ordered file that still means the same thing is accepted.
        # What this therefore does NOT assert is that the bytes on disk are themselves canonical.
        actual = digest_bytes(canonical_bytes(payload))
        if actual != pointer["digest"]:
            raise ValueError(f"{self.artifact_label} revision {blob_path} does not match its "
                             f"digest (recomputed {actual})")
        return pointer, payload


SCENE_STORE = RevisionStore("scene", POINTER_SCHEMA, "scene")


# ---------------------------------------------------------------------------
# The scene binding's original module-level API — signatures and semantics unchanged.
# ---------------------------------------------------------------------------
def pointer_digest(report_dir: Path) -> str | None:
    """See :meth:`RevisionStore.pointer_digest`."""
    return SCENE_STORE.pointer_digest(report_dir)


def record_attempt(report_dir: Path, record: dict) -> None:
    """See :meth:`RevisionStore.record_attempt`."""
    SCENE_STORE.record_attempt(report_dir, record)


def read_last_attempt(report_dir: Path) -> dict | None:
    """See :meth:`RevisionStore.read_last_attempt`."""
    return SCENE_STORE.read_last_attempt(report_dir)


def write_revision(report_dir: Path, revision, *,
                   expected_parent=UNSET, source: str = "driver") -> StoreResult:
    """See :meth:`RevisionStore.write_revision`."""
    return SCENE_STORE.write_revision(report_dir, revision,
                                      expected_parent=expected_parent, source=source)


def read_current(report_dir: Path) -> tuple[dict, dict] | None:
    """See :meth:`RevisionStore.read_current`."""
    return SCENE_STORE.read_current(report_dir)

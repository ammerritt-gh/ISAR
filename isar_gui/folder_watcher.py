"""FolderWatcher — detect *settled* new data files in watched folders (Phase D).

A beamtime folder fills as the instrument writes numors. We want to auto-ingest a file only once it
has stopped growing — ingesting a half-written numor would parse garbage. So this watcher combines
two signals (belt and braces, because ``QFileSystemWatcher`` is unreliable on some Windows network
shares):

  * a :class:`QFileSystemWatcher` on the watched folders (``directoryChanged`` → poll now), and
  * a :class:`QTimer` that polls the candidate files every ``poll_ms``.

Stability algorithm (per candidate file): track its ``(size, mtime)``. Each poll that observes the
SAME ``(size, mtime)`` as the previous poll increments a run-length counter; any change resets it to
1 and re-records the pair. After ``stable_polls`` consecutive equal observations the file is declared
stable and emitted **once** via :attr:`files_stable`. It is then marked done and only re-armed if it
changes again later (a growing-file resend). With the defaults (``poll_ms=1000``, ``stable_polls=3``)
that is roughly three seconds of quiescence. Both are injectable so tests run at ms scale.

Skips (never emitted): files the workspace already knows with a matching ``(size, mtime)`` (an
injected ``is_known`` cheap no-op guard — engine idempotency is the real guarantee), and hidden or
transient-extension files the engine would skip anyway (``.tmp``/``.part``/…). Everything else is
handed to the controller, which lets the engine classify it.

Pause: while paused the watcher keeps observing (so a file that settles during a pause is remembered)
but emits nothing; on unpause the accumulated stable set is emitted in one batch.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

log = logging.getLogger(__name__)

# Extensions never worth queuing. Two sources, unioned:
#   * the engine's own ingest skip set — mirror of isar.session.ingest.SKIP_SUFFIXES (kept as a
#     GUI-side copy, not imported: G16 forbids isar_gui reaching into engine internals). Queuing one
#     of these only makes the engine skip it again a second later.
#   * partial-write / editor-scratch extensions the engine set does not carry, so a half-written
#     download or swap file never even reaches the stability counter.
# If the engine set changes, update this copy to match (isar/session/ingest.py).
_ENGINE_SKIP_SUFFIXES = {".nxs", ".log", ".md", ".png", ".csv", ".tmp", ".unused"}
_PARTIAL_WRITE_SUFFIXES = {".part", ".swp", ".swx", ".crdownload", ".partial", ".lock"}
_SKIP_SUFFIXES = _ENGINE_SKIP_SUFFIXES | _PARTIAL_WRITE_SUFFIXES


def _should_skip_name(name: str) -> bool:
    """True for hidden dotfiles and known transient/partial-write extensions."""
    if name.startswith(".") or name.startswith("~"):
        return True
    return Path(name).suffix.lower() in _SKIP_SUFFIXES


class FolderWatcher(QObject):
    """Emit :attr:`files_stable` once a newly-appeared file in a watched folder stops changing."""

    #: Emitted with a list of absolute file paths that have settled (never while paused).
    files_stable = Signal(list)

    def __init__(self, poll_ms: int = 1000, stable_polls: int = 3, is_known=None, parent=None):
        super().__init__(parent)
        self._stable_polls = max(1, int(stable_polls))
        self._is_known = is_known or (lambda _p, _s, _m: False)
        self._folders: list[str] = []
        self._paused = False
        # path -> {"size", "mtime", "count", "emitted"}
        self._tracked: dict[str, dict] = {}
        self._deferred: list[str] = []   # stable files accumulated while paused, emitted on unpause

        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(lambda _p: self.poll())

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(poll_ms)))
        self._timer.timeout.connect(self.poll)

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> "FolderWatcher":
        self._timer.start()
        return self

    def stop(self) -> None:
        self._timer.stop()

    # ---- configuration ---------------------------------------------------
    def set_folders(self, folders) -> None:
        """Reconcile the watched-folder set with ``folders`` (absolute paths). Drops tracking for
        files no longer under any watched folder, and (re)subscribes the QFileSystemWatcher."""
        new = [str(Path(f)) for f in folders if str(f)]
        old_paths = self._fs_watcher.directories()
        if old_paths:
            self._fs_watcher.removePaths(old_paths)
        existing = [f for f in new if Path(f).is_dir()]
        if existing:
            self._fs_watcher.addPaths(existing)
        self._folders = new
        # Forget tracked files that fell out of the watched set.
        self._tracked = {p: st for p, st in self._tracked.items()
                         if any(_is_within(p, f) for f in new)}
        # Drop any files that settled-while-paused under the PREVIOUS folder set: a workspace switch
        # re-seeds the watcher through here, and those deferred files belong to the old workspace —
        # flushing them on the next unpause would ingest them into the newly-bound one.
        if self._deferred:
            log.info("watched-folder set changed — dropping %d deferred file(s) from the previous set",
                     len(self._deferred))
            self._deferred = []
        self.poll()

    def set_paused(self, flag: bool) -> None:
        """Pause/resume emission. Observation continues while paused; on unpause the accumulated
        stable set is emitted in one batch."""
        was_paused = self._paused
        self._paused = bool(flag)
        if was_paused and not self._paused and self._deferred:
            batch, self._deferred = self._deferred, []
            self.files_stable.emit(batch)

    def is_paused(self) -> bool:
        return self._paused

    # ---- the poll --------------------------------------------------------
    def poll(self) -> None:
        """Scan the watched folders once; advance each candidate's stability counter; emit the set
        that just crossed the stability threshold (deferred while paused)."""
        newly_stable: list[str] = []
        for path, size, mtime in self._candidates():
            if self._is_known(path, size, mtime):
                # Already in the ledger with matching size+mtime — nothing to do.
                self._tracked.pop(path, None)
                continue
            st = self._tracked.get(path)
            if st is None:
                self._tracked[path] = {"size": size, "mtime": mtime, "count": 1, "emitted": False}
                continue
            changed = st["size"] != size or st["mtime"] != mtime
            if st["emitted"]:
                if changed:  # a resend/append after we already emitted — re-arm from scratch
                    st.update(size=size, mtime=mtime, count=1, emitted=False)
                continue
            if changed:
                st.update(size=size, mtime=mtime, count=1)
            else:
                st["count"] += 1
                if st["count"] >= self._stable_polls:
                    st["emitted"] = True
                    newly_stable.append(path)
        if not newly_stable:
            return
        if self._paused:
            self._deferred.extend(newly_stable)
        else:
            self.files_stable.emit(newly_stable)

    def _candidates(self):
        """Yield ``(abs_path, size, mtime)`` for non-hidden, non-transient files directly inside the
        watched folders. Stat failures and un-listable folders are skipped (logged), never raised."""
        for folder in self._folders:
            d = Path(folder)
            if not d.is_dir():
                continue
            try:
                entries = list(d.iterdir())
            except OSError as exc:
                log.warning("could not list watched folder %s: %s", folder, exc)
                continue
            for child in entries:
                if not child.is_file() or _should_skip_name(child.name):
                    continue
                try:
                    stt = child.stat()
                except OSError as exc:
                    log.warning("could not stat watched file %s: %s", child, exc)
                    continue
                yield str(child), stt.st_size, stt.st_mtime


def _is_within(path: str, base: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError) as exc:
        log.debug("path %s not within %s: %s", path, base, exc)
        return False

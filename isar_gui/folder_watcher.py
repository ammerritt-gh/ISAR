"""Watch top-level data files and only hand settled, retryable files to ingest.

The watcher deliberately keeps its own bookkeeping small and visible.  It does not decide whether a
file is scientific data; it waits for a file to stop changing, then lets the engine classify it.  A
parse failure is not treated as success: the unchanged file is retried after 5, 15, and 30 seconds
and then remains visibly marked for an operator to inspect.

Polling is BOUNDED per tick (the watcher runs on the Qt thread; a huge beamtime directory or a slow
network share must never freeze the operator interface): directory entries are consumed
incrementally via ``os.scandir`` under a per-tick entry/time budget, so one observation "sweep" over
the watched folders may span several ticks.  A sweep accumulates everything it sees sweep-locally
and its results are applied — stability counting, absence pruning, folder-listed authority — only
when the sweep COMPLETES under the generation it started with.  Any mid-sweep invalidation (a
directory-change notification, a configure call, a pause flip) discards the partial sweep, which
therefore can never prune or promote on stale data.  Small folders (the common case) complete a
whole sweep within a single tick, so behavior there is identical to the historical one-poll-per-
sweep model.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
from pathlib import Path
import stat as stat_module
import time

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

log = logging.getLogger(__name__)

_ENGINE_SKIP_SUFFIXES = {".nxs", ".log", ".md", ".png", ".csv", ".tmp", ".unused"}
_PARTIAL_WRITE_SUFFIXES = {".part", ".swp", ".swx", ".crdownload", ".partial", ".lock"}
_SKIP_SUFFIXES = _ENGINE_SKIP_SUFFIXES | _PARTIAL_WRITE_SUFFIXES
_RETRY_DELAYS = (5.0, 15.0, 30.0)

# Per-tick sweep budget: at most this many directory entries are consumed per poll (on Windows,
# DirEntry.stat() costs no extra syscall, so entries are the single unit of work), with a real
# wall-clock guard as the second bound. Both exist to keep the Qt thread responsive.
DEFAULT_ENTRY_BUDGET = 500
_TICK_WALL_BUDGET_S = 0.1


def _should_skip_name(name: str) -> bool:
    """True for hidden dotfiles and known transient/partial-write extensions."""
    return name.startswith((".", "~")) or Path(name).suffix.lower() in _SKIP_SUFFIXES


@dataclass
class _Sweep:
    """One in-flight observation pass over the watched folders (may span several poll ticks).

    Everything here is sweep-local: nothing leaks into the watcher's global bookkeeping until the
    sweep completes under the generation it was started with.  ``folders`` is the remaining work
    queue (rotated per sweep for fairness); ``scandir`` is the open iterator into ``folders[0]``.
    """
    generation: int
    folders: list
    scandir: object = None
    candidates: list = field(default_factory=list)     # (path, size, mtime) in observation order
    observed: set = field(default_factory=set)         # candidate paths seen so far
    listed_paths: set = field(default_factory=set)     # every visible entry, even if stat failed
    listed_folders: set = field(default_factory=set)   # folders fully enumerated (absence authority)
    unavailable: set = field(default_factory=set)

    def close(self) -> None:
        if self.scandir is not None:
            try:
                self.scandir.close()
            except Exception as exc:   # closing a network-share handle can itself fail
                log.debug("closing sweep scandir handle failed: %s", exc)
            self.scandir = None


@dataclass(frozen=True)
class WatchStatus:
    """Operator-facing watcher state.  All lists contain absolute paths/folders."""

    paused: bool = False
    deferred: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    retrying: tuple[str, ...] = ()
    exhausted: tuple[str, ...] = ()
    # The last completed sweep that handed files to reading, and how many (wall clock, for the dialog).
    found_at: datetime | None = None
    found: int = 0

    @property
    def needs_attention(self) -> bool:
        return bool(self.unavailable or self.retrying or self.exhausted)

    def messages(self) -> list[str]:
        """The attention lines the Files dock and the Watch dialog both show, in one order."""
        out: list[str] = []
        if self.paused and self.deferred:
            out.append(f"Watching is paused; {len(self.deferred)} settled file(s) waiting")
        if self.unavailable:
            out.append("Watched folder unavailable: " + ", ".join(self.unavailable))
        if self.retrying:
            out.append(f"Retrying {len(self.retrying)} file(s) that could not be parsed")
        if self.exhausted:
            out.append(f"{len(self.exhausted)} file(s) still could not be parsed after automatic retries; "
                       "inspect them")
        return out


class FolderWatcher(QObject):
    """Emit settled top-level files once; retain failures for bounded automatic retry."""

    files_stable = Signal(list)
    status_changed = Signal(object)

    def __init__(self, poll_ms: int = 1000, stable_polls: int = 3, is_known=None,
                 clock=None, entry_budget: int = DEFAULT_ENTRY_BUDGET, parent=None):
        super().__init__(parent)
        self._stable_polls = max(1, int(stable_polls))
        self._is_known = is_known or (lambda _p, _s, _m: False)
        self._clock = clock or time.monotonic
        self._entry_budget = max(1, int(entry_budget))
        self._folders: list[str] = []
        self._paused = False
        # path -> size, mtime, count, emitted, awaiting_ingest, retry_attempt, retry_due, exhausted
        self._tracked: dict[str, dict] = {}
        self._deferred: OrderedDict[str, None] = OrderedDict()
        self._unavailable: set[str] = set()
        self._last_status: WatchStatus | None = None
        self._found_at: datetime | None = None
        self._found = 0
        # Sweep state machine: the in-flight sweep, its validity generation, the presence set from
        # the LAST COMPLETED sweep (what retries are checked against), and the re-entrancy guard.
        self._sweep: _Sweep | None = None
        self._generation = 0
        self._present: set[str] = set()
        self._polling = False
        self._rr_offset = 0

        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_directory_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(poll_ms)))
        self._timer.timeout.connect(self.poll)

    def start(self) -> "FolderWatcher":
        self._timer.start()
        return self

    def stop(self) -> None:
        self._timer.stop()

    # ---- configuration ---------------------------------------------------
    def configure(self, folders, paused: bool, *, reset_binding: bool = False) -> None:
        """Apply folders and pause state as one atomic observation boundary.

        A normal edit keeps entries still covered by the new folders.  ``reset_binding`` is only
        for a workspace rebind and discards every old entry, preventing a same-folder rebind from
        delivering data into the new workspace.
        """
        new = _normalise_folders(folders)
        # Any configuration edit (folder set or pause flip) invalidates an in-flight sweep: its
        # partial view predates the new observation boundary and must never become authoritative.
        self._invalidate_sweep()
        self._paused = bool(paused)
        self._folders = new
        if reset_binding:
            self._tracked.clear()
            self._deferred.clear()
            self._unavailable.clear()
            self._found_at, self._found = None, 0
        else:
            self._tracked = {p: st for p, st in self._tracked.items()
                             if any(_is_within(p, f) for f in new)}
            self._deferred = OrderedDict((p, None) for p in self._deferred
                                         if any(_is_within(p, f) for f in new))
            self._unavailable.intersection_update(new)
        self._sync_filesystem_watcher()
        # Poll only after *both* fields have been committed.  This fixes the old set_folders /
        # set_paused ordering hole where a file could be emitted during a pause configuration edit.
        # Flush the older paused batch first, so arrival order remains meaningful if the same poll
        # also finds a newly settled file.
        self._flush_deferred_if_running()
        self.poll()
        self._emit_status()

    def set_folders(self, folders) -> None:
        """Compatibility wrapper: legacy callers meant a fresh workspace binding."""
        self.configure(folders, self._paused, reset_binding=True)

    def set_paused(self, flag: bool) -> None:
        """Compatibility wrapper for the atomic :meth:`configure` API."""
        self.configure(self._folders, flag)

    def is_paused(self) -> bool:
        return self._paused

    def status(self) -> WatchStatus:
        """Return the latest operator-facing state without requiring a signal turn."""
        if self._last_status is None:
            self._emit_status()
        assert self._last_status is not None
        return self._last_status

    # ---- ingest outcomes -------------------------------------------------
    def record_ingest_report(self, report) -> None:
        """Record per-file parse outcomes from a completed GUI ingest report.

        Only report rows carrying ``error`` are retried.  A clean row clears any former retry state;
        files classified as supporting/not-recognised data are successful observations, not errors.
        """
        now = self._clock()
        for item in getattr(report, "files", ()) or ():
            if not isinstance(item, dict):
                log.warning("ignoring malformed ingest report file row: %r", item)
                continue
            raw_path = item.get("path")
            if not raw_path:
                log.warning("ignoring ingest report file row without a path: %r", item)
                continue
            path = str(Path(raw_path))
            if not self._is_current_top_level_path(path):
                log.debug("ignoring ingest result for file outside the current watcher binding: %s", path)
                continue
            error = item.get("error")
            state = self._tracked.get(path)
            # A report can also come from Add Files/Add Folder.  Only an emission this watcher made
            # may advance its retry state; otherwise a manual failure would create a retry that can
            # never be attributed to the current workspace binding.
            if state is None or not state.get("awaiting_ingest"):
                log.debug("ignoring ingest result not emitted by this watcher: %s", path)
                continue
            if not error:
                self._tracked.pop(path, None)
                self._deferred.pop(path, None)
                continue
            attempt = int(state.get("retry_attempt", 0))
            if attempt >= len(_RETRY_DELAYS):
                state.update(exhausted=True, retry_due=None, emitted=True, awaiting_ingest=False)
                log.warning("watched file exhausted automatic parse retries: %s (%s)", path, error)
            else:
                delay = _RETRY_DELAYS[attempt]
                state.update(retry_attempt=attempt + 1, retry_due=now + delay,
                             exhausted=False, emitted=True, awaiting_ingest=False)
                log.warning("watched file parse failed; retry %d of %d in %.0f s: %s (%s)",
                            attempt + 1, len(_RETRY_DELAYS), delay, path, error)
        self._emit_status()

    # ---- poll ------------------------------------------------------------
    def poll(self) -> None:
        """Advance the bounded sweep; apply its results only when it completes uninvalidated.

        Each tick consumes at most the entry/wall budget.  Due retries fire on EVERY tick (checked
        against the last completed sweep's presence set plus anything the current partial sweep has
        already seen), so a retry deadline is honored even while a large sweep spans many ticks.
        """
        if self._polling:            # re-entrancy guard: a signal firing mid-poll must not recurse
            return
        self._polling = True
        try:
            sweep = self._sweep
            if sweep is None or sweep.generation != self._generation:
                sweep = self._sweep = self._new_sweep()
            done = self._advance_sweep(sweep)
            if done and sweep.generation == self._generation:
                self._apply_completed_sweep(sweep)
                self._sweep = None
            due = self._due_retries(self._present | sweep.observed)
            for path in due:
                self._queue_or_emit(path)
            self._flush_deferred_if_running()
            self._emit_status()
        finally:
            self._polling = False

    def _on_directory_changed(self, _path: str) -> None:
        # A change notification mid-sweep means the partial view is stale: discard and restart.
        self._invalidate_sweep()
        self.poll()

    def _invalidate_sweep(self) -> None:
        self._generation += 1
        if self._sweep is not None:
            self._sweep.close()
            self._sweep = None

    def _new_sweep(self) -> _Sweep:
        # Rotate the starting folder per sweep so a folder larger than several tick budgets cannot
        # permanently starve its siblings of first-slot progress.
        folders = list(self._folders)
        if folders:
            self._rr_offset %= len(folders)
            folders = folders[self._rr_offset:] + folders[:self._rr_offset]
            self._rr_offset += 1
        return _Sweep(generation=self._generation, folders=folders)

    def _advance_sweep(self, sweep: _Sweep) -> bool:
        """Consume up to the per-tick budget of directory entries; True when the sweep is done."""
        budget = self._entry_budget
        deadline = time.monotonic() + _TICK_WALL_BUDGET_S
        while sweep.folders:
            folder = sweep.folders[0]
            if sweep.scandir is None:
                if not Path(folder).is_dir():
                    sweep.unavailable.add(folder)
                    sweep.folders.pop(0)
                    continue
                try:
                    sweep.scandir = os.scandir(folder)
                except OSError as exc:
                    log.warning("could not list watched folder %s: %s", folder, exc)
                    sweep.unavailable.add(folder)
                    sweep.folders.pop(0)
                    continue
            while budget > 0 and time.monotonic() < deadline:
                try:
                    entry = next(sweep.scandir)
                except StopIteration:
                    sweep.close()
                    sweep.listed_folders.add(folder)   # fully enumerated: authoritative for absence
                    sweep.folders.pop(0)
                    break
                except OSError as exc:
                    # A listing that dies midway is NOT authoritative for absence.
                    log.warning("could not list watched folder %s: %s", folder, exc)
                    sweep.close()
                    sweep.unavailable.add(folder)
                    sweep.folders.pop(0)
                    break
                budget -= 1
                if _should_skip_name(entry.name):
                    continue
                # Remember every visible entry before stat.  If a network share denies a transient
                # stat, the file is still present and must not lose retry/deferred state.
                path = str(Path(folder) / entry.name)
                sweep.listed_paths.add(path)
                try:
                    st = entry.stat()
                except OSError as exc:
                    log.warning("could not stat watched file %s: %s", path, exc)
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                sweep.candidates.append((path, st.st_size, st.st_mtime))
                sweep.observed.add(path)
            else:
                return False           # budget exhausted mid-folder; resume here next tick
        return True

    def _apply_completed_sweep(self, sweep: _Sweep) -> None:
        """Commit a completed, uninvalidated sweep: the ONLY place global observation state moves."""
        newly_stable: list[str] = []
        for path, size, mtime in sweep.candidates:
            if self._is_known(path, size, mtime):
                self._tracked.pop(path, None)
                self._deferred.pop(path, None)
                continue
            state = self._tracked.get(path)
            if state is None:
                self._tracked[path] = _state(size, mtime)
                continue
            changed = state["size"] != size or state["mtime"] != mtime
            if changed:
                # A changed file is new evidence: a previously exhausted parser failure may now be
                # complete/fixed, so re-arm all retry bookkeeping rather than silently ignoring it.
                state.update(_state(size, mtime))
                self._deferred.pop(path, None)
                continue
            if state.get("exhausted") or state.get("retry_due") is not None:
                continue
            if state["emitted"]:
                continue
            state["count"] += 1
            if state["count"] >= self._stable_polls:
                state["emitted"] = True
                newly_stable.append(path)

        # A successful directory listing is authoritative for absence; an individual stat failure
        # is not.  Preserve prior retry/deferred state while a transient access problem clears.
        self._present = sweep.observed | sweep.listed_paths
        self._prune_confirmed_absent(self._present, sweep.listed_folders)
        if sweep.unavailable != self._unavailable:
            newly_unavailable = sweep.unavailable - self._unavailable
            if newly_unavailable:
                log.warning("watched folder(s) unavailable: %s", ", ".join(sorted(newly_unavailable)))
            returned = self._unavailable - sweep.unavailable
            if returned:
                log.info("watched folder(s) available again: %s", ", ".join(sorted(returned)))
                self._sync_filesystem_watcher()
            self._unavailable = set(sweep.unavailable)
        if newly_stable:
            self._found_at, self._found = datetime.now(), len(newly_stable)
        for path in newly_stable:
            self._queue_or_emit(path)

    def _due_retries(self, observed: set[str]) -> list[str]:
        now = self._clock()
        due: list[str] = []
        for path, state in self._tracked.items():
            retry_due = state.get("retry_due")
            if retry_due is not None and now >= retry_due and path in observed:
                state["retry_due"] = None
                state["emitted"] = True
                due.append(path)
        return due

    def _prune_confirmed_absent(self, observed: set[str], listed_folders: set[str]) -> None:
        """Remove paths only if their own top-level folder was listed successfully this poll."""
        stale = [path for path in self._tracked
                 if path not in observed and any(_is_direct_child(path, folder) for folder in listed_folders)]
        for path in stale:
            self._tracked.pop(path, None)
            self._deferred.pop(path, None)

    def _queue_or_emit(self, path: str) -> None:
        if self._paused:
            self._deferred[path] = None
        else:
            state = self._tracked.get(path)
            if state is not None:
                state["awaiting_ingest"] = True
            self.files_stable.emit([path])

    def _flush_deferred_if_running(self) -> None:
        if self._paused or not self._deferred:
            return
        batch = list(self._deferred)
        self._deferred.clear()
        for path in batch:
            state = self._tracked.get(path)
            if state is not None:
                state["awaiting_ingest"] = True
        self.files_stable.emit(batch)

    def _sync_filesystem_watcher(self) -> None:
        old_paths = self._fs_watcher.directories()
        if old_paths:
            self._fs_watcher.removePaths(old_paths)
        existing = [folder for folder in self._folders if Path(folder).is_dir()]
        if existing:
            failed = self._fs_watcher.addPaths(existing)
            if failed:
                log.warning("could not attach folder-change watches: %s", ", ".join(failed))

    def _is_current_top_level_path(self, path: str) -> bool:
        return any(_is_direct_child(path, folder) for folder in self._folders)

    def _emit_status(self) -> None:
        retrying = tuple(sorted(path for path, state in self._tracked.items()
                               if state.get("retry_due") is not None))
        exhausted = tuple(sorted(path for path, state in self._tracked.items()
                               if state.get("exhausted")))
        status = WatchStatus(
            paused=self._paused,
            deferred=tuple(self._deferred),
            unavailable=tuple(sorted(self._unavailable)),
            retrying=retrying,
            exhausted=exhausted,
            found_at=self._found_at,
            found=self._found,
        )
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(status)


def _state(size, mtime, *, emitted: bool = False) -> dict:
    return {"size": size, "mtime": mtime, "count": 1, "emitted": emitted,
            "awaiting_ingest": False,
            "retry_attempt": 0, "retry_due": None, "exhausted": False}


def _normalise_folders(folders) -> list[str]:
    out: list[str] = []
    for folder in folders:
        if not str(folder):
            continue
        text = str(Path(folder))
        if text not in out:
            out.append(text)
    return out


def _is_direct_child(path: str, folder: str) -> bool:
    try:
        return Path(path).resolve().parent == Path(folder).resolve()
    except OSError as exc:
        log.debug("could not compare watched path %s to %s: %s", path, folder, exc)
        return False


def _is_within(path: str, base: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError) as exc:
        log.debug("path %s not within %s: %s", path, base, exc)
        return False

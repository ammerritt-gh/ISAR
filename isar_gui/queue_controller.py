"""QueueController -- the window's one owner of the analysis queue (C-controller).

It runs the engine's analysis service (``isar.session.analysis_service``, one long-lived process per open
experiment) on a :class:`~isar_gui.process.ProcessRunner`, keeps the two queues in the engine's pure
scheduler (:class:`isar.session.analysis_queue.AnalysisQueue`), applies the duplicate rule with the intake
worker's ``current`` map, adds arrivals while the switch is on, pauses, holds the writer for a copy, and shuts
down in order. Nothing else in the window keeps queue state: the panel, the rail and the scan list read
:meth:`view`, :meth:`selection`, :meth:`marks` and :meth:`fast_all` on ``changed`` and call the action methods.

The GUI computes nothing about a fit. It sends the user's actions down (``start``, ``stop``, ``hold_writes``,
``shutdown``) and shows what the service reports. "Done" means saved: an item enters the counts and the
scan list's marks only once the writer's ``saved`` event arrives; until then it reads *saving*.

The service is not a coordinator job: it runs beside ``WorkspaceOperationCoordinator`` on its own runner, so
an ingest or an export is never queued behind the analysis. Times are ``time.monotonic()`` seconds taken
here when an event arrives, never the service's clock. Returned views are frozen; callers never mutate them.

A batch (:meth:`start_inter_batch`) is one ``inter`` item: it appears in :attr:`QueueView.batches`, never in
the per-file marks.

shortcut: ``view()``, ``marks()`` and every event rebuild their answer from the scheduler's snapshot, O(items);
fine for the hundreds of items of a beamtime, cache per change if a queue ever holds many thousands.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QObject, QTimer, Signal

from isar.session.analysis_queue import AnalysisQueue
from isar.session.analysis_queue import ItemView as _SchedItem
from isar.workspace_api.inter_spectra import BatchRequest, batch_preview
from isar.workspace_api.scan_keys import scan_prefix, split_key
from isar.workspace_api.scan_list import PooledRow
from isar_gui.process import ProcessRunner
from isar_gui.views.readiness_line import READY, classify

log = logging.getLogger(__name__)

QueueStatus = Literal["waiting", "running", "saving", "done", "done_warning", "held", "failed", "stopped"]
Mark = Literal["queued", "running", "saving"]
ServiceState = Literal["off", "starting", "ready", "failed", "stopping"]

SERVICE_MODULE = "isar.session.analysis_service"      # a module path, never imported here
_SHUTDOWN_SILENCE_S = 120.0     # an algorithm setting: shutdown waits for the service this long since its last line
_PASSES = ("fast", "deep")
_LOOK = ("held", "failed", "done_warning")             # what "needs a look" counts; stopped is the user's own act
_COMPLETE = ("done", "done_warning", "already_current")  # settled with a result in the folder (or one already there)
_PROGRESS_KEYS = ("pass", "phase", "step", "of", "stage", "stages", "kind", "index", "round", "done", "failed",
                 "not_reached", "fixed", "total")
_CRASH = "the analysis process ended unexpectedly; see {log}"
_UNCONFIRMED = "not confirmed saved; re-add it"


# ---- what the views read ------------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemView:
    item_id: str
    unit_key: str                 # "experiment:scan:detector"
    set_key: str                  # scan_prefix(unit_key)
    set_label: str                # the scan list's label for set_key; set_key when unknown
    analyzer: str                 # split_key(unit_key)[2]
    pass_kind: Literal["fast", "deep", "inter"]
    status: QueueStatus
    reason: str | None            # plain words; None for waiting/running/saving/done
    automatic: bool
    runnable: bool                # False: a deep item waiting for its file's fast item
    cores: int | None             # running only
    started_at: float | None      # time.monotonic() at the "started" event
    step_started_at: float | None  # time.monotonic() at the latest "progress" event
    progress: Mapping | None      # the latest C-progress dict, verbatim
    finished_at: float | None


@dataclass(frozen=True)
class QueueGroup:                 # one scan set within one queue; reading only (design step 5)
    set_key: str
    set_label: str
    automatic: bool               # every item in it was added automatically
    n_done: int                   # saved items of this set and pass added since the bind
    n_total: int                  # every item of this set and pass added since the bind (not cleared)
    items: tuple[ItemView, ...]   # running first, then waiting, each in add order


@dataclass(frozen=True)
class FinishedGroup:              # one line per scan set and pass
    set_key: str
    set_label: str
    pass_kind: Literal["fast", "deep"]
    counts: Mapping[str, int]     # finished status -> count, saved items only
    look: tuple[ItemView, ...]    # held, failed, stopped, done_warning, and every finished deep item


@dataclass(frozen=True)
class BatchView:                  # one inter-spectra batch: the dock's row (C-queue)
    item_id: str
    batch_id: str                 # "b" + 12 hex, minted by start_inter_batch
    label: str                    # "Inter-spectra batch, started 14:32": its kind and start time
    kind: Literal["anchored", "simultaneous"]
    direction: str | None
    status: QueueStatus
    reason: str | None            # plain words: waiting for, failed, stopped, a warning
    n_total: int                  # the scope's members
    cores: int | None             # running only
    progress: Mapping | None      # the latest progress keys the service sent
    not_reached_reasons: tuple[tuple[str, int], ...]   # always () for now: the protocol sends counts, not reasons
    started_at: float | None
    step_started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class QueueView:
    bound: bool
    fast: tuple[QueueGroup, ...]
    deep: tuple[QueueGroup, ...]
    finished: tuple[FinishedGroup, ...]
    n_running: Mapping[str, int]  # per pass_kind; a deep item in its fast stage counts as deep
    n_waiting: Mapping[str, int]
    n_saving: int
    n_look: int                   # held + failed + done_warning in `finished` (stopped excluded)
    paused: bool
    blocked: str | None           # "a campaign is running in this experiment", else None
    cores: int                    # the setting
    cores_in_use: int
    service: ServiceState
    service_error: str | None     # plain words with the log path, when service == "failed"
    batches: tuple[BatchView, ...] = ()     # newest first; finished ones stay until clear_finished
    inter: tuple[ItemView, ...] = ()        # the active batch items, by seq


@dataclass(frozen=True)
class Selection:
    keys: tuple[str, ...]         # analyzer-file keys only (pooled masters left out)
    text: str                     # "2 analyzers selected (97 K: a06, a07)"; "" when none
    n_pooled: int                 # pooled masters in the selection (named, not queued)


@dataclass(frozen=True)
class LeaveSummary:
    n_running: int
    n_running_deep: int
    longest_deep_s: float | None  # the longest-running deep item's elapsed seconds
    n_waiting: int
    n_saving: int


# ---- the controller's own records ----------------------------------------------------------------------
class _Result:
    """One result the service reported for one pass of an item, and what became of its save."""

    __slots__ = ("saving", "saved", "error")

    def __init__(self, saving: bool) -> None:
        self.saving = saving
        self.saved: bool | None = None      # None: not yet answered (only meaningful when ``saving``)
        self.error: str | None = None

    @property
    def pending(self) -> bool:
        return self.saving and self.saved is None


class _Rec:
    """What the controller knows about an item that the scheduler does not keep."""

    __slots__ = ("item_id", "unit_key", "pass_kind", "automatic", "set_key", "analyzer", "label", "spec", "results",
                 "saves", "final", "finished_at", "override")

    def __init__(self, view: _SchedItem, spec: dict | None = None, label: str = "") -> None:
        self.item_id = view.item_id
        self.unit_key = view.unit_key
        self.pass_kind = view.pass_kind
        self.automatic = view.automatic
        batch = view.pass_kind == "inter"
        self.set_key = "" if batch else scan_prefix(view.unit_key)
        self.analyzer = "" if batch else str(split_key(view.unit_key)[2])
        self.label = label                          # a batch's name ("Inter-spectra batch, started 14:32"); "" for a file
        self.spec = spec                            # a batch's start object, as the service gets it; None for a file
        self.results: dict[str, _Result] = {}       # fast and deep: the one result of each pass
        self.saves: list[_Result] = []              # a batch: every ring or sweep, in arrival order
        self.final: str | None = None       # the scheduler's settled state, once seen
        self.finished_at: float | None = None
        self.override: str | None = None    # a reason the scheduler cannot know (the process died)

    def pending(self, pass_kind: str) -> bool:
        if pass_kind == "inter":
            return any(r.pending for r in self.saves)
        res = self.results.get(pass_kind)
        return res is not None and res.pending

    def any_pending(self) -> bool:
        return any(r.pending for r in (*self.results.values(), *self.saves))

    def answer(self, pass_kind: str, saved: bool, error: str | None = None) -> None:
        """The writer's answer; a batch answers in arrival order, so it is the oldest save still pending."""
        if pass_kind == "inter":
            res = next((r for r in self.saves if r.pending), None)
        else:
            res = self.results.get(pass_kind)
        if res is not None:
            res.saved, res.error = saved, error


def _service_argv(python_exe: str, root: Path, log_path: Path) -> list[str]:
    """The service command line (a test replaces this to run a scripted stand-in)."""
    return [python_exe, "-m", SERVICE_MODULE, "--workspace", str(root), "--log", str(log_path)]


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


class QueueController(QObject):
    changed = Signal()            # anything in view(), selection(), marks() or fast_all() changed
    log_line = Signal(str)        # one Log line (design step 22); the window prints it
    results_saved = Signal(tuple)  # tuple[str, ...]: unit keys confirmed saved by one batch of `saved` events
    auto_fast_changed = Signal(bool)   # the switch moved; the window saves it in the GuiStore

    def __init__(self, python_exe: str, intake, scan_list_vm, *, cores: int,
                 blocked: Callable[[], str | None] = lambda: None, parent=None) -> None:
        super().__init__(parent)
        if cores < 1:
            raise ValueError(f"cores must be at least 1, got {cores}")
        self._python = python_exe
        self._intake = intake
        self._list = scan_list_vm
        self._cores = int(cores)
        self._blocked = blocked
        self._clock: Callable[[], float] = time.monotonic       # tests replace it
        self._root: Path | None = None
        self._queue: AnalysisQueue | None = None
        self._auto_fast = True
        self._recs: dict[str, _Rec] = {}
        self._bind_no = 0
        self._seen_finished = 0                # how many of the scheduler's finished items were noted
        self._stages: list[tuple[_Rec, str, str | None, float]] = []   # saved fast stages of deep items
        self._held_adds: list[tuple[tuple[str, ...], str, bool]] = []
        self._saved_batch: list[str] = []
        self._labels: tuple[object, dict[str, str]] = (None, {})
        # the service
        self._runner: ProcessRunner | None = None
        self._service: ServiceState = "off"
        self._service_error: str | None = None
        self._service_log: Path | None = None
        # the writer hold
        self._holds = 0
        self._hold_sent = False
        self._hold_confirmed = False
        self._hold_waiters: list[Callable[[], None]] = []
        self._shutdown_waiters: list[Callable[[], None]] = []
        self._silence_logged = False
        self._keep_on_shutdown = False      # the shutdown under way keeps the waiting items (a rename)
        self._silence = QTimer(self)
        self._silence.setSingleShot(True)
        self._silence.timeout.connect(self._on_silence)
        self._list.selection_changed.connect(lambda _keys: self.changed.emit())
        self._list.snapshot_changed.connect(self.changed)
        self._intake.worker_changed.connect(self._on_worker_changed)

    # ---- binding ---------------------------------------------------------------------------------------
    def bind(self, root: Path | None, *, auto_fast: bool, keep_waiting: bool = False) -> None:
        """Serve the experiment at ``root`` (``None``: none); the same root again changes only the switch. A
        different root forgets everything: the leave flows shut the service down first, so a non-empty queue
        here is a defect and is logged. With ``keep_waiting`` (rename only) the waiting items and the finished
        list stay and the service restarts lazily at the new root."""
        root = None if root is None else Path(root)
        self._auto_fast = bool(auto_fast)
        if root is not None and root == self._root and self._queue is not None:
            pass                        # the same experiment bound again: its queue and service stand
        elif keep_waiting and self._queue is not None and root is not None:
            self._drop_service()
            gone = self._queue.abandon_active("the experiment folder was renamed")
            if gone:
                log.warning("renamed with %d running or saving item(s); they are failed", len(gone))
            self._root = root
            self._sync()
        else:
            if self._queue is not None and self._has_work():
                log.warning("bind to %s with work still queued for %s: the queue is forgotten", root, self._root)
            self._drop_service()
            self._forget()
            self._root = root
            if root is not None:
                self._queue = AnalysisQueue(self._cores, id_prefix=f"q{self._bind_no}-")
        self.changed.emit()
        self._pump()

    def _forget(self) -> None:
        self._queue = None
        self._recs.clear()
        self._held_adds.clear()
        self._saved_batch.clear()
        self._stages.clear()
        self._seen_finished = 0
        self._bind_no += 1
        self._holds = 0
        self._hold_sent = self._hold_confirmed = False

    def _has_work(self) -> bool:
        snap = self._queue.snapshot()
        return bool(snap.fast or snap.deep or snap.inter)

    def _drop_service(self) -> None:
        """Kill the service tree now and forget it (an idle one, or a defect path); waiters are released."""
        runner, self._runner = self._runner, None
        self._silence.stop()
        if runner is not None:
            runner.kill_tree()
        self._service = "off"
        self._service_error = None
        self._hold_sent = self._hold_confirmed = False
        self._release_waiters()

    def _release_waiters(self) -> None:
        waiters, self._hold_waiters = self._hold_waiters, []
        waiters += self._shutdown_waiters
        self._shutdown_waiters = []
        for then in waiters:
            then()

    # ---- reads -----------------------------------------------------------------------------------------
    def auto_fast(self) -> bool:
        return self._auto_fast

    def _label(self, set_key: str) -> str:
        snap = self._list.snapshot
        if self._labels[0] is not snap:
            self._labels = (snap, {s.key: s.label for s in snap.sets})
        return self._labels[1].get(set_key, set_key)

    def _own_pending(self, rec: _Rec) -> bool:
        return rec.pending(rec.pass_kind)

    def _status(self, v: _SchedItem, rec: _Rec) -> QueueStatus:
        if v.state == "waiting":
            return "waiting"
        if v.state == "saving" or (v.state == "running" and self._own_pending(rec)):
            return "saving"
        if v.state == "running":
            return "running"
        return "done" if v.state == "already_current" else v.state      # type: ignore[return-value]

    def _reason(self, v: _SchedItem, rec: _Rec, status: str) -> str | None:
        if status in ("waiting", "running", "saving"):
            return None
        own = rec.results.get(rec.pass_kind)
        if rec.override is not None:
            return rec.override
        if status == "failed" and own is not None and own.saving and own.saved is False:
            return f"the result could not be saved: {own.error}"
        return v.reason

    def _item(self, v: _SchedItem) -> ItemView:
        rec = self._recs[v.item_id]
        status = self._status(v, rec)
        return ItemView(
            item_id=v.item_id, unit_key=v.unit_key, set_key=rec.set_key, set_label=self._label(rec.set_key),
            analyzer=rec.analyzer, pass_kind=v.pass_kind, status=status, reason=self._reason(v, rec, status),
            automatic=v.automatic, runnable=v.runnable, cores=v.cores or None, started_at=v.started_at,
            step_started_at=v.progress_at, progress=v.progress, finished_at=rec.finished_at)

    def _name(self, rec: _Rec) -> str:
        """A file's pass in words (``97 K a06 (fast pass)``), or a batch's name."""
        if rec.pass_kind == "inter":
            return rec.label
        return f"{self._label(rec.set_key)} {rec.analyzer} ({rec.pass_kind} pass)"

    def _batch(self, v: _SchedItem) -> BatchView:
        rec = self._recs[v.item_id]
        status = self._status(v, rec)
        if status == "waiting":
            blocker = None if v.waiting_on is None else self._recs.get(v.waiting_on)
            reason = None if blocker is None else f"waiting for {self._name(blocker)} to finish"
        else:
            reason = self._reason(v, rec, status)
        spec = rec.spec
        return BatchView(
            item_id=v.item_id, batch_id=spec["batch_id"], label=rec.label, kind=spec["kind"],
            direction=spec["direction"], status=status, reason=reason, n_total=len(spec["members"]),
            cores=v.cores or None, progress=v.progress, not_reached_reasons=(),
            started_at=v.started_at, step_started_at=v.progress_at, finished_at=rec.finished_at)

    def _stage_item(self, rec: _Rec, status: str, reason: str | None, at: float) -> ItemView:
        """The fast pass a deep item did first, once saved: a finished fast result in its own right."""
        return ItemView(
            item_id=f"{rec.item_id}:fast", unit_key=rec.unit_key, set_key=rec.set_key,
            set_label=self._label(rec.set_key), analyzer=rec.analyzer, pass_kind="fast", status=status,  # type: ignore[arg-type]
            reason=reason if status == "done_warning" else None, automatic=rec.automatic, runnable=False,
            cores=None, started_at=None, step_started_at=None, progress=None, finished_at=at)

    def view(self) -> QueueView:
        q = self._queue
        if q is None:
            return QueueView(False, (), (), (), {"fast": 0, "deep": 0, "inter": 0}, {"fast": 0, "deep": 0, "inter": 0},
                             0, 0, False, self._blocked(), self._cores, 0, self._service, self._service_error)
        snap = q.snapshot()
        totals: dict[tuple[str, str], list[int]] = {}
        for rec in self._recs.values():
            if rec.pass_kind == "inter":
                continue
            t = totals.setdefault((rec.set_key, rec.pass_kind), [0, 0])
            t[1] += 1
            t[0] += rec.final in _COMPLETE
        n_running = {"fast": 0, "deep": 0, "inter": 0}
        n_waiting = {"fast": 0, "deep": 0, "inter": 0}
        n_saving = 0
        groups: dict[str, list[QueueGroup]] = {}
        inter: tuple[ItemView, ...] = ()
        for kind, items in (("fast", snap.fast), ("deep", snap.deep), ("inter", snap.inter)):
            views = [self._item(v) for v in items]
            for iv in views:
                if iv.status == "waiting":
                    n_waiting[kind] += 1
                elif iv.status == "running":
                    n_running[kind] += 1
                else:
                    n_saving += 1
            if kind == "inter":
                inter = tuple(views)
                continue
            by_set: dict[str, list[ItemView]] = {}
            for iv in views:
                by_set.setdefault(iv.set_key, []).append(iv)
            groups[kind] = []
            for set_key, members in by_set.items():
                members = [m for m in members if m.status != "waiting"] + [m for m in members if m.status == "waiting"]
                done, total = totals.get((set_key, kind), (0, 0))
                groups[kind].append(QueueGroup(set_key, members[0].set_label, all(m.automatic for m in members),
                                               done, total, tuple(members)))
        settled = [self._item(v) for v in snap.finished] + [self._stage_item(*s) for s in self._stages]
        n_look = sum(iv.status in _LOOK for iv in settled)       # a failed batch needs a look as much as a file
        fin: dict[tuple[str, str], list[ItemView]] = {}
        for iv in sorted((i for i in settled if i.pass_kind != "inter"), key=lambda i: i.finished_at or 0.0):
            fin.setdefault((iv.set_key, iv.pass_kind), []).append(iv)
        finished = []
        for (set_key, kind), members in fin.items():
            counts: dict[str, int] = {}
            for m in members:
                counts[m.status] = counts.get(m.status, 0) + 1
            look = tuple(m for m in members if m.status in ("held", "failed", "stopped", "done_warning") or kind == "deep")
            finished.append(FinishedGroup(set_key, members[0].set_label, kind, counts, look))   # type: ignore[arg-type]
        batches = tuple(self._batch(v) for v in sorted(
            (*snap.inter, *(v for v in snap.finished if v.pass_kind == "inter")), key=lambda v: v.seq, reverse=True))
        return QueueView(True, tuple(groups["fast"]), tuple(groups["deep"]), tuple(finished), n_running, n_waiting,
                         n_saving, n_look, snap.paused, self._blocked(), snap.cores_setting, snap.cores_in_use,
                         self._service, self._service_error, batches=batches, inter=inter)

    def selection(self) -> Selection:
        snap = self._list.snapshot
        keys, n_pooled = [], 0
        for key in self._list.selected_keys():
            row = snap.spectrum(key)
            if isinstance(row, PooledRow):
                n_pooled += 1
            elif row is not None:
                keys.append(key)
        if not keys:
            return Selection((), "", n_pooled)
        n = len(keys)
        return Selection(tuple(keys), f"{n} {_plural(n, 'analyzer', 'analyzers')} selected ({self._grouped(keys, ': ')})",
                         n_pooled)

    def _grouped(self, unit_keys: Sequence[str], sep: str = " ") -> str:
        """``"97 K a06, a07; 120 K a06"`` (``sep`` ' ' or ': ' between a set's label and its analyzers)."""
        by_set: dict[str, list[str]] = {}
        for key in unit_keys:
            by_set.setdefault(scan_prefix(key), []).append(str(split_key(key)[2]))
        return "; ".join(f"{self._label(s)}{sep}{', '.join(names)}" for s, names in by_set.items())

    def marks(self) -> Mapping[str, Mapping[str, Mark]]:
        """``unit_key -> {pass_kind: mark}`` for the active items. A deep item's own fast stage shows on its
        file's fast mark (running while its latest progress is a fast one, saving between its fast result and
        that save) with the deep mark queued; afterwards the deep mark follows the item."""
        out: dict[str, dict[str, Mark]] = {}
        if self._queue is None:
            return out
        snap = self._queue.snapshot()
        for v in (*snap.fast, *snap.deep):
            rec = self._recs[v.item_id]
            mine = out.setdefault(v.unit_key, {})
            if v.state == "waiting":
                mine[v.pass_kind] = "queued"
            elif v.pass_kind == "fast":
                mine["fast"] = "saving" if v.state == "saving" or self._own_pending(rec) else "running"
            else:
                fast_res = rec.results.get("fast")
                fast_pending = rec.pending("fast")
                own_pending = self._own_pending(rec)
                if fast_pending:
                    mine["fast"] = "saving"
                if v.state == "saving":
                    if own_pending:
                        mine["deep"] = "saving"
                elif own_pending:
                    mine["deep"] = "saving"
                elif fast_res is None and v.progress is not None and v.progress.get("pass") == "fast":
                    mine["fast"] = "running"
                    mine["deep"] = "queued"
                elif fast_pending:
                    mine["deep"] = "queued"
                else:
                    mine["deep"] = "running"
        return out

    def _ready(self) -> tuple[tuple[str, ...] | None, str | None]:
        """The analyzer files the readiness line counts as ready, or (None, why not now)."""
        if self._queue is None:
            return None, "no experiment is open"
        att = self._intake.attention
        if att is None:
            if self._intake.error and not self._intake.current_pending:
                return None, f"the readiness check failed: {self._intake.error}"
            return None, "checking which analyzers are ready"
        if not att.evaluated:      # not a BL43LXU experiment: every file, and the engine holds what it cannot fit
            return tuple(sp.key for ss in self._list.snapshot.sets for sp in ss.spectra), None
        summary = self._intake.summary
        missing = frozenset() if summary is None else frozenset(
            (s.scan, a) for s in summary.incomplete_sets for a in s.missing_analyzers)
        return tuple(f"{r.scan}:{r.analyzer}" for r in classify(att, missing)[READY]), None

    def fast_all(self) -> tuple[int | None, str | None]:
        """(how many ready analyzer files a fast pass would add now, None) or (None, why not now)."""
        keys, why = self._ready()
        if keys is None:
            return None, why
        return len(self._queue.addable(keys, "fast", self._intake.current or {})), None

    def leave_summary(self) -> LeaveSummary | None:
        """What leaving now would stop or forget; None when nothing is running, saving or waiting."""
        if self._queue is None:
            return None
        snap = self._queue.snapshot()
        now = self._clock()
        running = saving = waiting = deep = 0
        longest: float | None = None
        for v in (*snap.fast, *snap.deep, *snap.inter):
            status = self._status(v, self._recs[v.item_id])
            if status == "waiting":
                waiting += 1
            elif status == "saving":
                saving += 1
            else:
                running += 1
                if v.pass_kind == "deep":
                    deep += 1
                    if v.started_at is not None:
                        longest = max(longest or 0.0, now - v.started_at)
        if not (running or saving or waiting):
            return None
        return LeaveSummary(running, deep, longest, waiting, saving)

    def writes_busy(self) -> str | None:
        """Why the folder is being written right now, or None."""
        if self._queue is not None:
            snap = self._queue.snapshot()
            if any(v.state != "waiting" for v in (*snap.fast, *snap.deep, *snap.inter)):
                return "analysis is running: press Pause and wait for the running analyzers to finish"
        if self._holds:
            return "a copy or an export is reading the experiment"
        return None

    # ---- user actions ----------------------------------------------------------------------------------
    @staticmethod
    def _check_pass(pass_kind: str) -> None:
        if pass_kind not in _PASSES:
            raise ValueError(f"unknown pass {pass_kind!r}")

    def add_selection(self, pass_kind: str) -> None:
        self._check_pass(pass_kind)
        keys = self.selection().keys
        if keys:
            self._request_add(keys, pass_kind, automatic=False)

    def start_inter_batch(self, req: BatchRequest) -> tuple[bool, str]:
        """Queue one batch from the dock's request. The preview is read again here, so a scope that changed since
        it was shown is judged as it is now; the id is minted here, since the GUI may not import the engine."""
        if self._queue is None:
            return False, "no experiment is open"
        preview = batch_preview(self._root, req)
        if not preview.can_start:
            return False, preview.reason or "the batch cannot start"
        label = f"Inter-spectra batch, started {time.strftime('%H:%M')}"   # local time: the start the operator pressed
        spec = {"batch_id": "b" + secrets.token_hex(6), "kind": req.kind, "direction": req.direction,
                "anchors": list(req.anchors) if req.kind == "anchored" else [], "members": list(preview.members)}
        view = self._queue.add_inter(spec["batch_id"], preview.members)
        self._recs[view.item_id] = _Rec(view, spec=spec, label=label)
        message = f"Queued {label} ({req.kind}): {preview.line}"
        self.log_line.emit(message)
        self.changed.emit()
        self._pump()
        return True, message

    def fast_pass_all_ready(self) -> None:
        keys, _why = self._ready()
        if keys:
            self._request_add(keys, "fast", automatic=False)

    def on_arrivals(self, scan_ids: Sequence[str]) -> None:
        """New or updated scan sets were read in (``GuiIngestReport.added + .updated``): with the switch on, every
        analyzer file of them joins the fast queue as an automatic item. Turning the switch on sweeps nothing."""
        if not self._auto_fast or self._queue is None:
            return
        wanted = set(scan_ids)
        keys = tuple(sp.key for ss in self._list.snapshot.sets if ss.key in wanted for sp in ss.spectra)
        if keys:
            self._request_add(keys, "fast", automatic=True)

    def remove(self, item_id: str) -> None:
        if self._queue is not None and self._queue.remove(item_id):
            del self._recs[item_id]
            self.changed.emit()

    def stop(self, item_id: str) -> None:
        """Ask the service to end one running item (the confirmation is the caller's); the queue changes on the
        service's ``finished`` event."""
        snap = self._queue.snapshot() if self._queue is not None else None
        running = snap is not None and any(v.item_id == item_id and v.state == "running"
                                           for v in (*snap.fast, *snap.deep, *snap.inter))
        if not (running and self._send({"op": "stop", "item_id": item_id})):
            log.warning("stop %s: it is not running", item_id)

    def pause(self) -> None:
        if self._queue is not None:
            self._queue.pause()
        self.changed.emit()

    def resume(self) -> None:
        if self._queue is not None:
            self._queue.resume()
        self.changed.emit()
        self._pump()

    def clear_finished(self) -> None:
        if self._queue is not None:
            self._sync()
            self._queue.clear_finished()
            self._stages.clear()
            self._seen_finished = 0
        self.changed.emit()

    def show(self, unit_key: str) -> None:
        """The one action that moves the selection: select the analyzer file in the scan list (and so the display)."""
        self._list.select_keys([unit_key])

    def set_auto_fast(self, on: bool) -> None:
        on = bool(on)
        if on != self._auto_fast:
            self._auto_fast = on
            self.auto_fast_changed.emit(on)
            self.changed.emit()

    def set_cores(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"cores must be at least 1, got {n}")
        self._cores = int(n)
        if self._queue is not None:
            self._queue.set_cores(self._cores)
        self.changed.emit()
        self._pump()

    def poke(self) -> None:
        """The window's ``blocked`` answer may have changed: start what can start."""
        self.changed.emit()
        self._pump()

    # ---- adding ----------------------------------------------------------------------------------------
    def _request_add(self, keys: Sequence[str], pass_kind: str, *, automatic: bool) -> None:
        if self._queue is None or not keys:
            return
        if self._intake.current_pending:       # never judge "already current" on a map older than the last change
            self._held_adds.append((tuple(keys), pass_kind, automatic))
            return
        self._apply_add(tuple(keys), pass_kind, automatic)
        self.changed.emit()
        self._pump()

    def _on_worker_changed(self) -> None:
        if self._held_adds and self._queue is not None and not self._intake.current_pending:
            held, self._held_adds = self._held_adds, []
            for keys, pass_kind, automatic in held:
                self._apply_add(keys, pass_kind, automatic)
            self._pump()
        self.changed.emit()

    def _apply_add(self, keys: tuple[str, ...], pass_kind: str, automatic: bool) -> None:
        current = self._intake.current
        report = self._queue.add(keys, pass_kind, automatic=automatic,        # type: ignore[arg-type]
                                 current={} if current is None else current)   # None: a failed read; the start rechecks
        for v in report.added:
            self._recs[v.item_id] = _Rec(v)
        if report.added:
            self.log_line.emit(f"Queued for a {pass_kind} pass: " + self._grouped([v.unit_key for v in report.added])
                               + (" (added automatically)" if automatic else ""))
        if automatic:
            return              # a re-read file that is already done or queued is not news
        why = {"current": ("already has a current {p} pass", "already have a current {p} pass"),
               "waiting": ("is already queued for a {p} pass", "are already queued for a {p} pass"),
               "running": ("is already running a {p} pass", "are already running a {p} pass")}
        for reason, (one, many) in why.items():
            left = [key for key, r in report.skipped if r == reason]
            if left:
                phrase = (one if len(left) == 1 else many).format(p=pass_kind)
                self.log_line.emit(f"Not added: {self._grouped(left)} {phrase}")

    # ---- the service -----------------------------------------------------------------------------------
    def _send(self, command: dict) -> bool:
        return self._runner is not None and self._runner.write_line(json.dumps(command, ensure_ascii=False))

    def _has_waiting(self) -> bool:
        snap = self._queue.snapshot()
        return any(v.state == "waiting" for v in (*snap.fast, *snap.deep, *snap.inter))

    def _pump(self) -> None:
        """Start the service on the first item that can start, then send each start the scheduler hands out."""
        q = self._queue
        if q is None or self._root is None or self._service == "stopping" or q.snapshot().paused:
            return
        if self._blocked() is not None:
            return
        if self._service in ("off", "failed"):
            if self._has_waiting():
                self._start_service()
            return
        if self._service != "ready":
            return
        starts = q.next_starts()
        for s in starts:
            cmd = {"op": "start", "item_id": s.item_id, "unit_key": s.unit_key, "pass": s.pass_kind,
                   "cores": s.cores}
            if s.pass_kind == "inter":
                cmd["batch"] = self._recs[s.item_id].spec
            if not self._send(cmd):
                log.warning("could not send start for %s: the analysis process is not accepting commands", s.item_id)
        if starts:
            self.changed.emit()

    def _start_service(self) -> None:
        runs = self._root / "report" / "gui-runs"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base, n = f"queue-{stamp}", 1
        try:
            runs.mkdir(parents=True, exist_ok=True)
            while (runs / f"{base}.log").exists() or (runs / f"{base}.service.log").exists():
                n += 1
                base = f"queue-{stamp}-{n}"
        except OSError as exc:
            log.exception("cannot prepare %s for the analysis process", runs)
            self._service = "failed"
            self._service_error = f"the analysis process could not start: {exc}"
            self.log_line.emit(f"The analysis process could not start: {exc}")
            self.changed.emit()
            return
        self._service_log = runs / f"{base}.service.log"
        runner = ProcessRunner(self)
        runner.line.connect(lambda text, r=runner: self._on_line(r, text))
        runner.finished.connect(lambda code, r=runner: self._on_exit(r, code))
        runner.finished.connect(runner.deleteLater)
        self._runner = runner
        self._service = "starting"
        self._service_error = None
        self.changed.emit()
        runner.start(_service_argv(self._python, self._root, self._service_log), log_path=str(runs / f"{base}.log"))

    def _on_line(self, runner: ProcessRunner, text: str) -> None:
        if runner is not self._runner:
            return                      # a service of an experiment already left
        if self._service == "stopping":
            self._arm_silence()
        try:
            ev = json.loads(text)
        except ValueError:
            return                      # not protocol: the runner's retained log has it
        if isinstance(ev, dict) and isinstance(ev.get("ev"), str):
            self._on_event(ev)

    def _on_event(self, ev: dict) -> None:
        handlers = {"ready": self._ev_ready, "started": self._ev_started, "progress": self._ev_progress,
                    "cores": self._ev_cores, "result": self._ev_result, "saved": self._ev_saved,
                    "save_failed": self._ev_save_failed, "finished": self._ev_finished,
                    "writes_held": self._ev_writes_held, "writes_released": self._ev_writes_released,
                    "log": self._ev_log, "error": self._ev_error}
        handler = handlers.get(ev["ev"])
        if handler is None:
            return                      # pong, bye: nothing to do
        try:
            handler(ev)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("analysis event %r not applied (%s: %s)", ev, type(exc).__name__, exc)
        self._sync()
        self.changed.emit()

    def _ev_ready(self, ev: dict) -> None:
        if self._service == "stopping":
            return                      # shutdown was asked while starting: the exit completes it
        self._service = "ready"
        if self._holds and not self._hold_sent:
            self._hold_sent = self._send({"op": "hold_writes"})
        self._pump()

    def _ev_started(self, ev: dict) -> None:
        if not self._queue.on_started(ev["item_id"], at=self._clock()):
            log.warning("started for %s, which is not running", ev["item_id"])

    def _ev_progress(self, ev: dict) -> None:
        self._queue.on_progress(ev["item_id"], {k: ev[k] for k in _PROGRESS_KEYS if k in ev}, at=self._clock())

    def _ev_cores(self, ev: dict) -> None:
        self._queue.on_cores(ev["item_id"], int(ev["cores"]))
        self._pump()

    def _ev_result(self, ev: dict) -> None:
        pass_kind, saving = ev["pass"], bool(ev.get("saving"))
        end_record = ev.get("event") == "end"
        if self._queue.on_result(ev["item_id"], pass_kind, ev["status"], ev.get("reason"), saving=saving,
                                 end_record=end_record):
            rec = self._recs[ev["item_id"]]
            if pass_kind == "inter":
                rec.saves.append(_Result(saving))
            else:
                rec.results[pass_kind] = _Result(saving)

    def _ev_saved(self, ev: dict) -> None:
        iid, pass_kind = ev["item_id"], ev["pass"]
        if self._queue.on_saved(iid, pass_kind):
            rec = self._recs[iid]
            rec.answer(pass_kind, True)
            if pass_kind == "inter":
                self._saved_batch.extend(ev["keys"])        # a batch reports its spectra, not its own key
            else:
                self._saved_batch.append(rec.unit_key)
            if rec.pass_kind == "deep" and pass_kind == "fast" and ev.get("status") in ("done", "done_warning"):
                self._stages.append((rec, ev["status"], ev.get("reason"), self._clock()))
            QTimer.singleShot(0, self._flush_saved)
        self._pump()                    # the answer may free the file for a waiting item

    def _ev_save_failed(self, ev: dict) -> None:
        iid, pass_kind = ev["item_id"], ev["pass"]
        error = str(ev.get("error"))
        if self._queue.on_save_failed(iid, pass_kind, error):
            self._recs[iid].answer(pass_kind, False, error)
        self._pump()

    def _ev_finished(self, ev: dict) -> None:
        self._queue.on_finished(ev["item_id"], ev["end"], ev.get("reason"))
        self._pump()

    def _ev_writes_held(self, ev: dict) -> None:
        self._hold_confirmed = True
        if self._service == "stopping":  # the service may still save what it was handed: the waiters run at its exit
            return
        waiters, self._hold_waiters = self._hold_waiters, []
        for then in waiters:
            then()

    def _ev_writes_released(self, ev: dict) -> None:
        self._hold_confirmed = False

    def _ev_log(self, ev: dict) -> None:
        text = str(ev.get("text"))
        level = {"error": logging.ERROR, "warning": logging.WARNING}.get(ev.get("level"), logging.INFO)
        log.log(level, "analysis process: %s", text)
        if level >= logging.WARNING:
            self.log_line.emit(f"Analysis process: {text}")

    def _ev_error(self, ev: dict) -> None:
        log.error("analysis process error: %s", ev.get("text"))
        self.log_line.emit(f"Analysis process error: {ev.get('text')}")

    def _flush_saved(self) -> None:
        keys, self._saved_batch = tuple(dict.fromkeys(self._saved_batch)), []
        if keys:
            self.results_saved.emit(keys)

    def _sync(self) -> None:
        """Note items that settled since the last look: their times, and the Log line step 22 wants."""
        snap = self._queue.snapshot()
        now = self._clock()
        for v in snap.finished[self._seen_finished:]:
            rec = self._recs.get(v.item_id)
            if rec is None or rec.final is not None:
                continue
            rec.final, rec.finished_at = v.state, now
            self._log_settled(v, rec)
        self._seen_finished = len(snap.finished)

    def _batch_words(self, v: _SchedItem, rec: _Rec, status: str, reason: str | None) -> str:
        kind = rec.spec["kind"]
        name = f"{rec.label} ({kind})"
        if status == "stopped":
            kept = sum(1 for r in rec.saves if r.saved)
            return f"Stopped: {name}: kept {kept} " + (_plural(kept, "ring", "rings") if kind == "anchored"
                                                       else _plural(kept, "sweep", "sweeps"))
        if status in ("held", "failed"):
            return f"{status.capitalize()}: {name}: {reason}"
        if status == "done_warning":
            return f"Finished with a warning: {name}: {reason}"
        p = v.progress or {}
        return (f"Batch finished: {name}: {p.get('done', 0)} refitted, {p.get('failed', 0)} failed, "
                f"{p.get('not_reached', 0)} not reached")

    def _log_settled(self, v: _SchedItem, rec: _Rec) -> None:
        status = self._status(v, rec)
        reason = self._reason(v, rec, status)
        if rec.pass_kind == "inter":
            self.log_line.emit(self._batch_words(v, rec, status, reason))
            return
        what = self._name(rec)
        if status in ("held", "failed"):
            self.log_line.emit(f"{status.capitalize()}: {what}: {reason}")
        elif status == "done_warning":
            self.log_line.emit(f"Finished with a warning: {what}: {reason}")
        elif v.state == "already_current":
            self.log_line.emit(f"Already current, not run again: {what}")
        elif status == "done" and rec.pass_kind == "deep":
            self.log_line.emit(f"Deep pass finished: {self._label(rec.set_key)} {rec.analyzer}")

    def _on_exit(self, runner: ProcessRunner, code: int) -> None:
        if runner is not self._runner:
            return
        self._runner = None
        self._silence.stop()
        self._hold_sent = self._hold_confirmed = False
        if self._service == "stopping":
            if code != 0 and not self._silence_logged:
                self._log_unconfirmed("the analysis process ended while stopping")
            self._silence_logged = False
            self._queue.abandon_active("the analysis process was stopped")
            self._sync()
            self._service = "off"
            self._release_hold_waiters()    # a copy asked for during the leave reads the folder before the reopen
            self._finish_shutdown()
            return
        reason = _CRASH.format(log=self._service_log)
        saving = {iid for iid, rec in self._recs.items() if self._own_pending(rec)}
        for iid in self._queue.abandon_active(reason):
            self._recs[iid].override = _UNCONFIRMED if iid in saving else reason
        self._service = "failed"
        self._service_error = f"the analysis process ended unexpectedly (exit code {code}); see {self._service_log}"
        log.error("analysis process exited with %s", code)
        self.log_line.emit(f"The analysis process ended unexpectedly; see {self._service_log}")
        self._sync()
        self._release_hold_waiters()
        self.changed.emit()

    def _release_hold_waiters(self) -> None:
        waiters, self._hold_waiters = self._hold_waiters, []
        for then in waiters:
            then()

    # ---- the folder ------------------------------------------------------------------------------------
    def hold_writes(self, then: Callable[[], None]) -> None:
        """Keep the writer from saving until :meth:`release_writes`; ``then()`` once no save can start. Holds nest."""
        self._holds += 1
        if self._service == "stopping":  # the service still saves what it was handed: wait for its exit
            self._hold_waiters.append(then)
        elif self._service != "ready":   # no service (or not yet ready): nothing saves; the hold is sent after ready
            then()
        elif self._hold_confirmed:
            then()
        else:
            self._hold_waiters.append(then)
            if not self._hold_sent:
                self._hold_sent = self._send({"op": "hold_writes"})
        self.changed.emit()

    def release_writes(self) -> None:
        if self._holds == 0:
            log.warning("release_writes without a hold")
            return
        self._holds -= 1
        if self._holds == 0:
            if self._hold_sent:
                self._send({"op": "release_writes"})
            self._hold_sent = self._hold_confirmed = False
            self._hold_waiters.clear()      # released before the hold came: the copy must not start
        self.changed.emit()

    def shutdown(self, then: Callable[[], None], *, keep_waiting: bool = False) -> None:
        """Orderly end for leaving the experiment: running items are stopped, handed-over results are saved,
        waiting items are forgotten; ``then()`` once the process is gone. With ``keep_waiting`` (rename only) the
        waiting items and the finished list stay for the :meth:`bind` that follows."""
        self._keep_on_shutdown = keep_waiting
        if self._runner is None:
            self._finish_shutdown(then)
            return
        self._shutdown_waiters.append(then)
        if self._service == "stopping":
            return
        self._service = "stopping"
        self._arm_silence()
        self._send({"op": "shutdown"})
        self.changed.emit()

    def _arm_silence(self) -> None:
        self._silence.start(int(_SHUTDOWN_SILENCE_S * 1000))

    def _log_unconfirmed(self, why: str) -> None:
        pending = [rec for rec in self._recs.values() if rec.any_pending()]
        files = self._grouped([rec.unit_key for rec in pending if rec.pass_kind != "inter"])
        batches = ", ".join(rec.label for rec in pending if rec.pass_kind == "inter")
        named = "; ".join(part for part in (files, batches) if part)
        if named:
            self.log_line.emit(f"Not confirmed saved when {why}: {named}")

    def _on_silence(self) -> None:
        log.error("analysis process silent for %.0f s while stopping; killing it", _SHUTDOWN_SILENCE_S)
        self._log_unconfirmed("ISAR stopped waiting for the analysis process")
        self._silence_logged = True         # its kill exits non-zero: the exit must not name them again
        if self._runner is not None:
            self._runner.kill_tree()

    def _finish_shutdown(self, then: Callable[[], None] | None = None) -> None:
        """The queue is forgotten with the process: the next experiment starts empty (but not after a rename)."""
        if then is not None:
            self._shutdown_waiters.append(then)
        keep, self._keep_on_shutdown = self._keep_on_shutdown, False
        if not keep and self._root is not None and self._queue is not None:
            root = self._root
            self._forget()
            self._queue = AnalysisQueue(self._cores, id_prefix=f"q{self._bind_no}-")
            self._root = root
        self._service = "off"
        self.changed.emit()
        waiters, self._shutdown_waiters = self._shutdown_waiters, []
        for cb in waiters:
            cb()

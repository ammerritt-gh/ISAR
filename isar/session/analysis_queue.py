"""The analysis queue scheduler (C-schedule): two queues, fast first, within a cores setting.

Plain bookkeeping for the GUI's analysis queue. Qt-free, stdlib only, no I/O, no clock, no threads;
one owner thread calls it, and the caller passes every time in (``at=``). It imports nothing from
``isar``: what is current arrives as the injected ``current`` mapping, and what a deep item can
use arrives from the engine as ``on_cores``.

Rules (design steps 4, 6, 7, 8, 10, 13, 16):

* Every fast or deep item is one analyzer file (``unit_key``) with one pass. An inter item is one
  batch over its ``members`` files: it occupies all of them, waits for waiting or running fast items
  on them, and blocks fast and deep items on them while it runs. Fast items start before deep and
  inter items; within a pass the oldest *runnable* item starts first.
* A file is occupied from the claim until its item settles, saving included. A deep or inter item
  also waits while a fast item for its file waits.
* A fast item takes one core. A deep or inter item starts only while no fast item runs, claims every
  idle core, and gives back what it cannot use through ``on_cores``; the next ``next_starts`` hands
  those on, fast first.
* A lowered cores setting changes no running item; nothing starts while cores_in_use >= setting.

shortcut: every call scans all items ever added (settled ones stay so late events return False);
fine for hundreds of items, index active items by unit_key if a queue ever holds thousands.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Literal, Mapping

PassKind = Literal["fast", "deep", "inter"]
ItemState = Literal["waiting", "running", "saving",  # active
                    "done", "done_warning", "already_current", "held", "failed", "stopped"]  # settled
SkipReason = Literal["current", "waiting", "running"]
End = Literal["completed", "stopped", "failed"]

_PASSES = ("fast", "deep")                    # the passes ``add`` takes; inter items come from add_inter
_RESULT_PASSES = ("fast", "deep", "inter")
_ENDS = ("completed", "stopped", "failed")
_RESULT_STATUSES = ("done", "done_warning", "already_current", "held", "failed")
_ACTIVE = ("waiting", "running", "saving")


@dataclass(frozen=True)
class ItemView:
    item_id: str
    unit_key: str
    pass_kind: PassKind
    automatic: bool
    seq: int                                 # add order, 1-based, unique within this queue
    state: ItemState
    cores: int                               # cores held; > 0 only while "running"
    reason: str | None                       # plain words (held / failed / stopped / done_warning / already_current)
    waiting_on: str | None                   # "waiting" only: item_id of the same-file item it waits for
    runnable: bool                           # "waiting" only: runnable(x); False otherwise
    saved_passes: frozenset[str]             # passes of this item the writer confirmed saved
    started_at: float | None                 # the caller's clock, from on_started
    progress: Mapping[str, object] | None    # last C-progress dict, verbatim
    progress_at: float | None                # the caller's clock at that progress
    members: tuple[str, ...] = ()            # the files occupied: (unit_key,) for fast and deep


@dataclass(frozen=True)
class Start:
    item_id: str
    unit_key: str
    pass_kind: PassKind
    cores: int                               # >= 1


@dataclass(frozen=True)
class AddReport:
    added: tuple[ItemView, ...]
    skipped: tuple[tuple[str, SkipReason], ...]   # (unit_key, why), in input order


@dataclass(frozen=True)
class QueueSnapshot:
    paused: bool
    cores_setting: int
    cores_in_use: int                        # may exceed cores_setting after a lowered setting
    fast: tuple[ItemView, ...]               # active fast items, by seq
    deep: tuple[ItemView, ...]               # active deep items, by seq
    finished: tuple[ItemView, ...]           # settled items, in settle order, until clear_finished()
    inter: tuple[ItemView, ...] = ()         # active batch items, by seq


class _Result:
    """One result the item's process sent for one pass, and what became of its save."""

    __slots__ = ("status", "reason", "saving", "saved", "error")

    def __init__(self, status: str, reason: str | None, saving: bool) -> None:
        self.status = status
        self.reason = reason
        self.saving = saving
        self.saved: bool | None = None   # None until the writer answers (only meaningful if saving)
        self.error: str | None = None

    @property
    def pending(self) -> bool:
        return self.saving and self.saved is None


class _Item:
    def __init__(self, item_id: str, unit_key: str, pass_kind: str, automatic: bool, seq: int,
                 members: tuple[str, ...]) -> None:
        self.item_id = item_id
        self.unit_key = unit_key
        self.pass_kind = pass_kind
        self.automatic = automatic
        self.seq = seq
        self.members = members
        self.state: str = "waiting"
        self.cores = 0
        self.reason: str | None = None
        self.end: str | None = None
        self.end_reason: str | None = None
        self.results: dict[str, _Result] = {}  # fast and deep: the one result of each pass
        self.saves: list[_Result] = []         # inter: every ring or sweep, in arrival order
        self.started_at: float | None = None
        self.progress: Mapping[str, object] | None = None
        self.progress_at: float | None = None

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE

    @property
    def occupies_file(self) -> bool:
        return self.state in ("running", "saving")

    @property
    def pending(self) -> bool:
        return any(r.pending for r in (*self.results.values(), *self.saves))

    @property
    def saved_passes(self) -> frozenset[str]:
        got = {p for p, r in self.results.items() if r.saving and r.saved}
        if any(r.saving and r.saved for r in self.saves):
            got.add("inter")
        return frozenset(got)


class AnalysisQueue:
    def __init__(self, cores: int, *, id_prefix: str = "q") -> None:
        if cores < 1:
            raise ValueError(f"cores must be at least 1, got {cores}")
        self._cores = cores
        self._prefix = id_prefix
        self._items: dict[str, _Item] = {}     # insertion order is seq order
        self._finished: list[str] = []         # settled ids, in settle order, until clear_finished
        self._paused = False
        self._seq = 0

    # -- adding and removing -------------------------------------------------------------------

    def _classify(self, unit_keys: Iterable[str], pass_kind: str,
                  current: Mapping[str, frozenset[str]]) -> tuple[list[str], list[tuple[str, SkipReason]]]:
        """Split keys into those to add and those to skip, in input order (no side effects)."""
        if pass_kind not in _PASSES:
            raise ValueError(f"unknown pass {pass_kind!r}")
        active: dict[str, str] = {}            # unit_key -> "waiting" | "running" for this pass
        for it in self._items.values():
            if it.pass_kind == pass_kind and it.active:
                active[it.unit_key] = "waiting" if it.state == "waiting" else "running"
        add: list[str] = []
        skipped: list[tuple[str, SkipReason]] = []
        for key in unit_keys:
            if key in active:
                skipped.append((key, active[key]))      # type: ignore[arg-type]
            elif pass_kind in current.get(key, frozenset()):
                skipped.append((key, "current"))
            else:
                add.append(key)
                active[key] = "waiting"                 # a repeat in this call is a duplicate
        return add, skipped

    def add(self, unit_keys: Iterable[str], pass_kind: PassKind, *, automatic: bool,
            current: Mapping[str, frozenset[str]]) -> AddReport:
        add, skipped = self._classify(unit_keys, pass_kind, current)
        views = []
        for key in add:
            self._seq += 1
            it = _Item(f"{self._prefix}{self._seq}", key, pass_kind, automatic, self._seq, (key,))
            self._items[it.item_id] = it
            views.append(self._view(it))
        return AddReport(added=tuple(views), skipped=tuple(skipped))

    def add_inter(self, batch_id: str, members: Iterable[str], *, automatic: bool = False) -> ItemView:
        """One batch over its member files; always added, since a batch may run as often as wanted."""
        self._seq += 1
        it = _Item(f"{self._prefix}{self._seq}", f"inter:{batch_id}:batch", "inter", automatic, self._seq,
                   tuple(members))
        self._items[it.item_id] = it
        return self._view(it)

    def addable(self, unit_keys: Iterable[str], pass_kind: PassKind,
                current: Mapping[str, frozenset[str]]) -> tuple[str, ...]:
        return tuple(self._classify(unit_keys, pass_kind, current)[0])

    def remove(self, item_id: str) -> bool:
        it = self._items.get(item_id)
        if it is None or it.state != "waiting":
            return False
        del self._items[item_id]
        return True

    # -- starting ------------------------------------------------------------------------------

    def _blocker(self, x: _Item) -> _Item | None:
        """The item x waits for: an active one on its files, else (deep and inter) a waiting fast one."""
        files = set(x.members)
        for y in self._items.values():
            if y is not x and y.occupies_file and files & set(y.members):
                return y
        if x.pass_kind != "fast":
            for y in self._items.values():
                if y.pass_kind == "fast" and y.state == "waiting" and files & set(y.members):
                    return y
        return None

    def _oldest_runnable(self, pass_kinds: tuple[str, ...]) -> _Item | None:
        for it in self._items.values():
            if it.state == "waiting" and it.pass_kind in pass_kinds and self._blocker(it) is None:
                return it
        return None

    def _fast_running(self) -> bool:
        return any(it.state == "running" and it.pass_kind == "fast" for it in self._items.values())

    def _deep_may_start(self) -> bool:
        # Option b (operator, 2026-10-27): a deep item starts only while no fast item runs.
        return not self._fast_running()

    def _deep_share(self, free: int) -> int:
        # Amendment 1: claim every idle core; on_cores takes back what the engine says it cannot use.
        return free

    @property
    def _in_use(self) -> int:
        return sum(it.cores for it in self._items.values())

    def next_starts(self) -> list[Start]:
        if self._paused:
            return []
        starts: list[Start] = []
        free = self._cores - self._in_use
        while free >= 1:
            it = self._oldest_runnable(("fast",))
            if it is not None:
                cores = 1
            else:
                it = self._oldest_runnable(("deep", "inter")) if self._deep_may_start() else None
                if it is None:
                    break
                cores = self._deep_share(free)
            it.state = "running"
            it.cores = cores
            free -= cores
            starts.append(Start(it.item_id, it.unit_key, it.pass_kind, cores))   # type: ignore[arg-type]
        return starts

    # -- events from the analysis process and the writer ---------------------------------------

    def _item(self, item_id: str) -> _Item:
        try:
            return self._items[item_id]
        except KeyError:
            raise KeyError(f"unknown item {item_id!r}") from None

    def on_started(self, item_id: str, *, at: float) -> bool:
        it = self._item(item_id)
        if it.state != "running":
            return False
        it.started_at = at
        return True

    def on_progress(self, item_id: str, progress: Mapping[str, object], *, at: float) -> bool:
        it = self._item(item_id)
        if it.state != "running":
            return False
        it.progress = MappingProxyType(dict(progress))
        it.progress_at = at
        return True

    def on_cores(self, item_id: str, cores: int) -> bool:
        it = self._item(item_id)
        if it.state != "running":
            return False
        if cores == it.cores:
            return False
        if cores < 1 or cores > it.cores:
            raise ValueError(f"item {item_id} holds {it.cores} cores; cannot change to {cores}")
        it.cores = cores
        return True

    def on_result(self, item_id: str, pass_kind: PassKind, status: str, reason: str | None,
                  *, saving: bool) -> bool:
        if pass_kind not in _RESULT_PASSES:
            raise ValueError(f"unknown pass {pass_kind!r}")
        if status not in _RESULT_STATUSES:
            raise ValueError(f"unknown result status {status!r}")
        it = self._item(item_id)
        if it.state != "running":
            return False
        if pass_kind == "inter":
            it.saves.append(_Result(status, reason, saving))
        else:
            it.results[pass_kind] = _Result(status, reason, saving)
        return True

    def _save_answer(self, item_id: str, pass_kind: str, saved: bool, error: str | None) -> bool:
        it = self._item(item_id)
        if it.state not in ("running", "saving"):
            return False
        if pass_kind == "inter":
            res = next((r for r in it.saves if r.pending), None)   # the writer answers in arrival order
        else:
            res = it.results.get(pass_kind)
        if res is None or not res.pending:
            return False
        res.saved = saved
        res.error = error
        if it.state == "saving" and not it.pending:
            self._settle(it)
        return True

    def on_saved(self, item_id: str, pass_kind: PassKind) -> bool:
        return self._save_answer(item_id, pass_kind, True, None)

    def on_save_failed(self, item_id: str, pass_kind: PassKind, error: str) -> bool:
        return self._save_answer(item_id, pass_kind, False, error)

    def on_finished(self, item_id: str, end: End, reason: str | None) -> bool:
        if end not in _ENDS:
            raise ValueError(f"unknown end {end!r}")
        it = self._item(item_id)
        if it.state != "running":
            return False
        it.end = end
        it.end_reason = reason
        it.cores = 0
        if it.pending:
            it.state = "saving"
        else:
            self._settle(it)
        return True

    def abandon_active(self, reason: str) -> tuple[str, ...]:
        """The analysis process died: every running or saving item settles as failed."""
        gone = []
        for it in list(self._items.values()):
            if it.occupies_file:
                it.cores = 0
                self._set_settled(it, "failed", reason)
                gone.append(it.item_id)
        return tuple(gone)

    # -- settling ------------------------------------------------------------------------------

    def _set_settled(self, it: _Item, state: str, reason: str | None) -> None:
        it.state = state
        it.reason = reason
        it.cores = 0
        self._finished.append(it.item_id)

    def _settle(self, it: _Item) -> None:
        """The settled-state table: an item ends once its process ended and every save answered."""
        if it.pass_kind == "inter":
            self._settle_batch(it)
            return
        own = it.results.get(it.pass_kind)
        fast = it.results.get("fast")
        if own is not None:
            if own.saving and own.saved is False:
                self._set_settled(it, "failed", f"not saved: {own.error}")
            else:
                self._set_settled(it, own.status, own.reason)
        elif it.end == "stopped":
            self._set_settled(it, "stopped", "stopped by the user")
        elif it.pass_kind == "deep" and fast is not None and fast.status in ("held", "failed"):
            self._set_settled(it, fast.status, fast.reason)
        elif it.end == "failed":
            self._set_settled(it, "failed", it.end_reason or "the fitting process failed")
        else:
            self._set_settled(it, "failed", "the fitting process ended without a result")

    def _settle_batch(self, it: _Item) -> None:
        """A batch settles on a failed save first, then a user stop, then a process failure, else on its
        last ring's verdict."""
        failed = next((r for r in it.saves if r.saving and r.saved is False), None)
        last = it.saves[-1] if it.saves else None
        if failed is not None:
            self._set_settled(it, "failed", f"not saved: {failed.error}")
        elif it.end == "stopped":
            self._set_settled(it, "stopped", "stopped by the user; completed rings/sweeps are kept")
        elif it.end == "failed":
            self._set_settled(it, "failed", it.end_reason or "the fitting process failed")
        elif last is not None:
            self._set_settled(it, last.status, last.reason)
        else:
            self._set_settled(it, "failed", "the fitting process ended without a result")

    # -- settings and views --------------------------------------------------------------------

    def set_cores(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"cores must be at least 1, got {n}")
        self._cores = n

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def clear_finished(self) -> None:
        self._finished.clear()

    def _view(self, it: _Item) -> ItemView:
        waiting = it.state == "waiting"
        blocker = self._blocker(it) if waiting else None
        return ItemView(
            item_id=it.item_id, unit_key=it.unit_key, pass_kind=it.pass_kind,   # type: ignore[arg-type]
            automatic=it.automatic, seq=it.seq, state=it.state,                 # type: ignore[arg-type]
            cores=it.cores, reason=it.reason,
            waiting_on=blocker.item_id if blocker else None,
            runnable=waiting and blocker is None,
            saved_passes=it.saved_passes,
            started_at=it.started_at, progress=it.progress, progress_at=it.progress_at,
            members=it.members,
        )

    def snapshot(self) -> QueueSnapshot:
        active = [it for it in self._items.values() if it.active]
        return QueueSnapshot(
            paused=self._paused,
            cores_setting=self._cores,
            cores_in_use=self._in_use,
            fast=tuple(self._view(it) for it in active if it.pass_kind == "fast"),
            deep=tuple(self._view(it) for it in active if it.pass_kind == "deep"),
            finished=tuple(self._view(self._items[i]) for i in self._finished),
            inter=tuple(self._view(it) for it in active if it.pass_kind == "inter"),
        )

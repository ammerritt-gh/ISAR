"""The inter-spectra batch runner (C-queue). Qt-free; top-level imports stdlib only, like analysis_service.

``run_batch`` runs one batch inside the item process the analysis service starts. It reads the experiment once
(the freeze: settings, locks, the ledger, each member's prepared context and input fingerprint), plans the batch
on the graph, and fits it with its own refit pool inside the item's cores:

- anchored: ring by ring from the anchors; every refit of a ring conditions on the state frozen when the ring
  starts (earlier rings, anchors, valid locks), so ring-mates never see each other; seams are measured when the
  ring completes;
- simultaneous: rounds of stage 1 (counts and branch assignments against one frozen snapshot) then stage 2
  (continuous Gauss-Seidel sweeps over conflict-free groups) until a round changes nothing, or the sweep cap ends
  it as "did not settle".

Stage 1 is search, price, resume: one core ``Search`` per spectrum of the ring or round, all driven together through
one pool stream. Each search waits only for its own jobs: when the last job of its pass returns, it is fed that pass's
prices in its own job order and sends its next jobs at once, so no spectrum waits for another. A pool exception
becomes that job's failed price and is never cached; a search that raises fails only its spectrum. With more than one
worker and a core that has ``run_task``, a job's inner work (its fits, basins and split nodes) runs as tasks on the
same pool and workers left idle price the searches' likely next promotions into the cache (``_Sched``).

Every ring or sweep is handed over through ``emit`` and the runner waits on ``ack`` for the writer's verdict
before the next stage: acceptance decides what the next stage conditions on, and a refused refit's held value
goes back. Nothing here writes a user value (hard rule 1) or touches the fast solo fit. The 30-minute figure is
a target recorded in the timing block and read by no branch: nothing stops, fails or is cut by it.

The runner sees only the ``emit``, ``progress`` and ``ack`` callables; the service owns the pipes. ``core`` (the
numerical core, a module; default :mod:`isar.analyze.coupled.refit`) is injectable for tests and is always
called with ``workers=1``: the runner's pool is the only one.
"""
from __future__ import annotations

import dataclasses
import heapq
import importlib
import itertools
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Callable, Literal

log = logging.getLogger(__name__)

CORE = "isar.analyze.coupled.refit"
CORE_VERSION = "isar.coupled-refit/2"
TARGET_S = 1800.0          # the design's 30-minute target: recorded and reported, read by no branch
NOT_CURRENT = "its fast pass is not current; run the fast pass first"
CUT_OFF = "cut off by a failed neighbour"
SCOPE_CHANGED = "the scope changed since the preview"
# what a refused spectrum is told to do when the cause is its inputs (the writer cannot say which way they differ)
INPUTS_REMEDY = ("; start the batch again, and if it fails the same way, add the data again (the same files or "
                 "their folder) so ISAR reads them afresh")
STALE_LOCK = "its locked fit is out of date, so it is neither refitted nor used"
NOTHING_LEFT = "no spectrum was left to refit before a round settled; its results are kept"
# the members event (C-members-event): the whole map per event, at most one per MEMBERS_GAP_S from pool transitions
# shortcut: full map per event, upgrade to deltas past ~5000 members
MEMBERS_GAP_S = 0.25
_THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")    # as driver._THREAD_ENV
_ELIGIBLE = ("accepted", "unchanged")
_KINDS = ("anchored", "simultaneous")
_DIRECTIONS = ("q", "T", "qT")


@dataclass(frozen=True)
class BatchSpec:                       # JSON-able; the "batch" object of the start command
    batch_id: str                      # "b" + 12 hex, minted by the GUI controller; unique per experiment
    kind: Literal["anchored", "simultaneous"]
    direction: Literal["q", "T", "qT"] | None   # anchored: the user's choice; simultaneous: None
    anchors: tuple[str, ...]           # spectrum keys; () for simultaneous
    members: tuple[str, ...]           # = BatchPreview.members: the scope, anchors included

    def to_json(self) -> dict:
        return {"batch_id": self.batch_id, "kind": self.kind, "direction": self.direction,
                "anchors": list(self.anchors), "members": list(self.members)}

    @staticmethod
    def from_json(d: dict) -> "BatchSpec":                 # raises ValueError naming the bad field
        if not isinstance(d, dict):
            raise ValueError(f"batch: not an object ({type(d).__name__})")
        bid = d.get("batch_id")
        if not (isinstance(bid, str) and len(bid) == 13 and bid[0] == "b"
                and all(c in "0123456789abcdef" for c in bid[1:])):
            raise ValueError(f"batch_id: {bid!r} is not b + 12 hex")
        kind = d.get("kind")
        if kind not in _KINDS:
            raise ValueError(f"kind: {kind!r} is not anchored or simultaneous")
        direction = d.get("direction")
        if (kind == "anchored" and direction not in _DIRECTIONS) or (kind == "simultaneous" and direction is not None):
            raise ValueError(f"direction: {direction!r} does not fit a {kind} batch")
        keys = {}
        for name in ("anchors", "members"):
            v = d.get(name)
            if not isinstance(v, (list, tuple)) or not all(isinstance(k, str) for k in v):
                raise ValueError(f"{name}: not a list of spectrum keys")
            keys[name] = tuple(v)
        if kind == "anchored" and not keys["anchors"]:
            raise ValueError("anchors: an anchored batch needs at least one")
        return BatchSpec(bid, kind, direction, keys["anchors"], keys["members"])


@dataclass(frozen=True)
class BatchItem:                       # what the item process receives (like file_fit.FileItem)
    item_id: str
    unit_key: str                      # "inter:<batch_id>:batch"
    pass_kind: str                     # "inter"
    cores: int                         # >= 1
    spec: dict                         # BatchSpec.to_json()


@dataclass(frozen=True)
class InterResult:                     # dataclasses.asdict() of this goes down the item pipe as ("result", ...)
    item_id: str
    unit_key: str
    pass_kind: str                     # "inter" | "cores"   (same field names as file_fit.FileResult)
    status: str                        # "done" | "done_warning" | "failed" | "cores"
    reason: str | None
    payload: dict | None               # isar.inter-result/1; None for "cores"
    cores: int | None = None
    ack: int | None = None             # token the runner will wait on; None = no wait


@dataclass
class _Tally:
    """One spectrum's stage-1 search in one ring or round: C-timing-report's ``spectra`` record, via ``as_dict``."""
    stage: str
    unit: str
    iterations: int = 0          # pool iterations it took part in (a pass served wholly from the cache is not one)
    candidates: int = 0          # fresh prices: jobs that ran
    cached: int = 0              # prices served from the cache
    starts: int = 0
    fits: int = 0                # optimizer fits (Priced.starts) of the fresh prices
    failures: int = 0
    reasons: Counter = field(default_factory=Counter)
    evidence_s: float = 0.0
    refine_s: float = 0.0
    stopped: object = None
    branches_seen: int = 0
    dormant_seen: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {"stage": self.stage, "unit": self.unit, "iterations": self.iterations, "candidates": self.candidates,
                "cached": self.cached, "starts": self.starts,
                "starts_per_candidate": self.starts / self.candidates if self.candidates else None,
                "fits": self.fits, "failures": self.failures,
                "failure_reasons": [[r, n] for r, n in self.reasons.most_common()],
                "evidence_s": self.evidence_s, "refine_s": self.refine_s, "stopped": self.stopped,
                "branches_seen": self.branches_seen, "dormant_seen": self.dormant_seen, "error": self.error}


def _stopped(a):
    return (a.coverage or {}).get("stopped")


def _seen(s) -> tuple[int, int]:
    """(branches, dormant branches) the neighbours of snapshot ``s`` carry, each once under its canonical name."""
    canon = lambda b: (s.aliases or {}).get(b, b)     # noqa: E731
    held = [h for ti in s.terms for h in ti.held]
    return (len({canon(m.branch) for h in held for m in h.modes}),
            len({canon(d.branch) for h in held for d in h.dormant}))


class _Ended(Exception):
    """The batch ends here with ``state`` and ``reason`` (an analysis outcome, not a crash)."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state, self.reason = state, reason


def run_batch(workspace_root: Path, item: BatchItem, *, progress: Callable[[dict], None],
              emit: Callable[[InterResult], None], ack: Callable[[int], dict], core=None,
              target_s: float = TARGET_S, members: Callable[[dict], None] | None = None) -> None:
    # never raises for an analysis failure: it is logged and emitted as event "end" outcome "failed".
    # ack(token) blocks until the service has saved that result and returns the verdict.
    # members(ev), when given, gets the full map of member states (waiting, fitting, done, refused, failed,
    # not_reached, held), from this thread only; it is never part of a progress dict.
    run = _Run(Path(workspace_root), item, progress, emit, ack, core, target_s, members)
    try:
        try:
            run.go()
        except _Ended as ended:
            log.info("batch %s ends %s: %s", item.unit_key, ended.state, ended.reason)
            run.end(ended.state, ended.reason)
    except (EOFError, BrokenPipeError, ConnectionError) as exc:      # the service is gone: nobody to report to
        log.warning("batch %s: the service went away (%s); the runner ends", item.unit_key, exc)
    except Exception as exc:  # noqa: BLE001 — a batch's failure is its end line, never the service's
        log.exception("batch %s failed", item.unit_key)
        try:
            run.end("failed", f"analysis error ({type(exc).__name__}: {exc})")
        except Exception:  # noqa: BLE001 — the end could not be handed over; the service writes one
            log.exception("batch %s: the failed end could not be handed over", item.unit_key)
    finally:
        run.pool.close()


# ---- the pool ------------------------------------------------------------------------------------------
def _work(core_name: str, fn: str, blob: bytes):
    """A pool job: one core call, its arguments pickled by the runner (so the shipping cost is measured)."""
    args, kwargs = pickle.loads(blob)
    return getattr(importlib.import_module(core_name), fn)(*args, **kwargs)


class _Pool:
    """The runner's one refit pool, opened on the first call wide enough; one job or one worker runs inline.
    At most ``workers`` jobs are in flight, so "submitted and not collected" means a worker has it. ``watch(key, on)``
    is told of each job's start and collection; ``tick(force)`` lets the run publish its members map."""

    def __init__(self, core, workers: int, watch=None, tick=None) -> None:
        self.core, self.workers, self.ex, self.ship_s = core, workers, None, 0.0
        self.watch, self.tick = watch, tick
        self.busy_s = 0.0                                # seconds ``run_task`` took in workers, on the task path
        self.orphans: set = set()                        # in-flight futures whose job is gone: no one waits for them
        self.ports: set = set()                          # live job ports of a running stream
        self.broken: BrokenProcessPool | None = None     # set when a worker died; the next map raises it, as its submit did

    def _note(self, key, on: bool) -> None:
        if self.watch is not None:
            self.watch(key, on)

    def _tick(self, force: bool) -> None:
        if self.tick is not None:
            self.tick(force)

    def _pickle(self, args, kwargs) -> bytes:
        # shortcut: ctx pickled per job, upgrade to an initializer-shipped dict when ship_s exceeds 10% of wall
        t0 = time.perf_counter()
        blob = pickle.dumps((args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        self.ship_s += time.perf_counter() - t0
        return blob

    def map(self, fn: str, calls: list) -> dict:
        """``{key: result or the exception it raised}`` of ``core.<fn>(*args, **kwargs)`` per ``(key, args, kwargs)``."""
        out: dict = {}

        def keep(key, result):
            out[key] = result
            return ()
        self._run(fn, calls, keep, inline=self.workers <= 1 or len(calls) <= 1)
        return {key: out[key] for key, _a, _k in calls}      # the order of calls, as before

    def _cap(self) -> int:
        """Tasks this pool may have in flight: the workers, less the orphans still running (never below one, so a
        stream always progresses; the extra task then queues behind them in the executor)."""
        return max(1, self.workers - len(self.orphans))

    def uses_tasks(self, fn: str) -> bool:
        """Whether ``stream(fn, ...)`` takes the task scheduler: more than one worker, a price, a task-capable core."""
        return self.workers > 1 and fn == "price" and hasattr(self.core, "run_task")

    def stream(self, fn: str, calls: list, more, *, lookahead=None, lookahead_done=None) -> None:
        """Run ``core.<fn>(*args, **kwargs)`` for each ``(key, args, kwargs)`` of ``calls``, at most ``workers`` in
        flight; as each finishes, call ``more(key, result or the exception it raised)`` in this process, which returns
        the further calls to queue (FIFO, after those queued). Returns when nothing is queued or in flight. A dead
        pool: before anything was sent it raises; mid-stream every unsent call is reported to ``more`` as the
        ``BrokenProcessPool``, the flying ones as they come back, and a call queued after that raises it here.
        On the task path (``uses_tasks``) each call is a job thread whose inner tasks share the pool, ``more`` still
        hears of each requested call once, and ``lookahead()`` (offers ``((unit, job key), args)``) and
        ``lookahead_done(key, price)`` let idle workers price jobs ahead; the other paths ignore both."""
        if self.uses_tasks(fn):
            _Sched(self, calls, more, lookahead, lookahead_done).run()
            return
        self._run(fn, calls, more, inline=self.workers <= 1)

    def _executor(self):
        if self.broken is not None:
            raise self.broken
        if self.ex is None:
            for name in _THREAD_ENV:          # one BLAS thread per process, as _deep_stage pins it
                os.environ.setdefault(name, "1")
            self.ex = ProcessPoolExecutor(max_workers=self.workers)
        return self.ex

    def _run(self, fn: str, calls: list, more, inline: bool) -> None:
        queue = deque(calls)
        if inline:
            while queue:
                key, args, kwargs = queue.popleft()
                self._note(key, True)
                self._tick(True)                  # this job may run for minutes: show it now
                try:
                    result = getattr(self.core, fn)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — one spectrum's failure is its result
                    log.exception("%s of %s failed", fn, key)
                    result = exc
                finally:
                    self._note(key, False)
                queue.extend(more(key, result))
                self._tick(not queue)             # the last job is back: publish it before the save waits on the ack
            return
        self._executor()
        flying, blob, sent = {}, None, 0      # blob: the pickle of queue[0], made while workers run
        while queue or flying:
            if self.broken is not None and not flying:
                raise self.broken             # calls queued after the pool died: they reach it as the next map would
            while queue and len(flying) < self._cap() and self.broken is None:
                key, args, kwargs = queue.popleft()
                blob = blob or self._pickle(args, kwargs)
                try:
                    flying[self.ex.submit(_work, self.core.__name__, fn, blob)] = key
                except BrokenProcessPool as exc:
                    if not sent:                          # the pool was dead before this stage sent anything: raise, as before
                        raise
                    log.error("%s: the worker pool broke (%s); %d jobs not sent", fn, exc, len(queue) + 1)
                    self.broken = exc                     # a dead worker mid-stage: the unsent jobs fail as the flying ones do
                    lost, blob = [key, *(q[0] for q in queue)], None
                    queue.clear()
                    for k in lost:
                        queue.extend(more(k, exc))
                    break
                blob = None
                sent += 1
                self._note(key, True)
                if queue:
                    blob = self._pickle(queue[0][1], queue[0][2])
            if not flying:
                continue
            done, _ = wait(flying, timeout=MEMBERS_GAP_S, return_when=FIRST_COMPLETED)
            for fut in done:
                key = flying.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001 — one spectrum's failure is its result
                    log.exception("%s of %s failed in the pool", fn, key)
                    result = exc
                self._note(key, False)
                queue.extend(more(key, result))
            self._tick(False)                 # flushes a throttled transition
        self._tick(True)                      # and the last one, which a throttle could have held back

    def close(self) -> None:
        for port in list(self.ports):         # a stream cut off from outside: its job threads end, no one waits on them
            port.abandon()
        if self.ex is not None:
            self.ex.shutdown()
            self.ex = None


_ABANDONED = object()      # what a lookahead job's thread reports when the runner abandoned it
JOIN_S = 2.0               # shortcut: leaving a stream waits this long in all for abandoned job threads, upgrade if a replay glue step ever outlasts it


def _work_task(core_name: str, blob: bytes):
    """A pool task of the scheduler: one core ``run_task`` (the payload pickled by the runner). Returns the value and
    the seconds ``run_task`` took in the worker, which is what ``busy_s`` adds."""
    payload = pickle.loads(blob)
    t0 = time.perf_counter()
    value = importlib.import_module(core_name).run_task(*payload)
    return value, time.perf_counter() - t0


class _Task:
    """One task of one job (the handle of C-port): queued, sent, then back with a value or an exception."""
    __slots__ = ("port", "payload", "prepare", "rank", "state", "value", "exc", "done", "dropped", "entry")

    def __init__(self, port, payload, prepare, rank) -> None:
        self.port, self.payload, self.prepare, self.rank = port, payload, prepare, rank
        self.state, self.value, self.exc, self.entry, self.dropped = "queued", None, None, None, False
        self.done = threading.Event()


class _JobPort:
    """C-port for one pricing job: its thread submits and waits, the scheduler (the main thread) sends. ``cls`` is
    "requested" or "lookahead"."""

    def __init__(self, sched, key, job, cls) -> None:
        self.sched, self.key, self.job, self.cls = sched, key, job, cls
        self.tasks: list[_Task] = []
        self.abandoned = self.submitted = self.ended = False
        self.thread: threading.Thread | None = None

    def submit(self, payload, prepare=None, rank=0):
        from isar.analyze.coupled.tasks import Abandoned
        t = _Task(self, payload, prepare, rank)
        self.tasks.append(t)                       # before the flag check, so abandon() cannot miss it
        if self.abandoned:
            raise Abandoned
        self.submitted = True
        self.sched.events.put(("task", t))
        return t

    def wait(self, handle):
        from isar.analyze.coupled.tasks import Abandoned
        if self.abandoned:
            raise Abandoned
        if handle.dropped:
            raise RuntimeError("waiting on a dropped task")
        if handle.state != "back":
            if handle.rank:                         # the job needs it now
                self.sched.events.put(("raise", handle))
            handle.done.wait()
            if self.abandoned:
                raise Abandoned
        if handle.exc is not None:
            raise handle.exc
        return handle.value

    def peek(self, handle):
        if handle.state != "back":
            return False, None
        return True, handle.exc if handle.exc is not None else handle.value

    def drop(self, handles) -> None:
        for h in handles:
            h.dropped = True

    def abandon(self) -> None:
        self.abandoned = True
        for t in list(self.tasks):
            t.done.set()


class _Sched:
    """One stream of "price" calls on the task path (main thread): requested calls run as job threads whose inner
    tasks fill the pool, and spare workers price ``lookahead`` offers. Only this thread touches the heap, the
    futures and ``more``; job threads talk to it through ``events`` and wait only on their own tasks. At most
    ``pool._cap()`` tasks are in flight. Priority is at send time: a requested task goes before any lookahead
    task, a lookahead task only to a free worker with nothing requested waiting, and a running one finishes."""

    def __init__(self, pool, calls, more, lookahead, lookahead_done) -> None:
        self.pool, self.more, self.lookahead, self.lookahead_done = pool, more, lookahead, lookahead_done
        self.queue = deque(calls)                  # requested calls not yet started
        self.events: SimpleQueue = SimpleQueue()   # ("task", t) ("raise", t) ("done", t, fut) ("job", port, result)
        self.heap: list = []                       # [class, rank, seq, task]: requested 0, lookahead 1
        self.seq = itertools.count()
        self.live: set[_JobPort] = set()
        self.look: dict = {}                       # key -> live lookahead port
        self.finished: dict = {}                   # key -> (job, price) of a finished lookahead job
        self.tried: set = set()                    # lookahead keys started once, never started again
        self.inflight: dict = {}                   # task -> future
        self.sent = 0
        self.ex = None

    # -- state
    def _free(self) -> int:
        return self.pool._cap() - len(self.inflight)

    def _starting(self) -> int:
        return sum(1 for p in self.live if not p.submitted and not p.ended)

    def _requested_live(self) -> bool:
        return any(p.cls == "requested" for p in self.live)

    def _push(self, t: _Task) -> None:
        if t.entry is not None:
            t.entry[3] = None                      # the old entry is skipped when it surfaces
        t.entry = [0 if t.port.cls == "requested" else 1, t.rank, next(self.seq), t]
        heapq.heappush(self.heap, t.entry)

    def _top(self):
        while self.heap:
            e = self.heap[0]
            t = e[3]
            if t is None or t.entry is not e or t.state != "queued" or t.dropped or t.port.abandoned:
                heapq.heappop(self.heap)
                continue
            return e
        return None

    def _finish(self, t: _Task, value=None, exc=None) -> None:
        t.value, t.exc, t.state, t.payload = value, exc, "back", None
        t.done.set()

    # -- sending
    def _send(self, e) -> None:
        heapq.heappop(self.heap)
        t = e[3]
        t.entry = None
        pool = self.pool
        try:
            payload = t.prepare(t.payload) if t.prepare is not None else t.payload
            # shortcut: ctx bytes ride with every task, upgrade to a per-worker handshake if ship_s grows
            t0 = time.perf_counter()
            blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
            pool.ship_s += time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001 — the job that submitted it gets the exception
            log.exception("a pool task could not be prepared")
            self._finish(t, exc=exc)
            return
        t.state, t.payload = "sent", None
        try:
            fut = self.ex.submit(_work_task, pool.core.__name__, blob)
        except BrokenProcessPool as exc:
            if not self.sent:                      # the pool was dead before this stream sent anything: raise, as before
                raise
            log.error("price: the worker pool broke (%s)", exc)
            self._broke(exc)
            return
        self.sent += 1
        self.inflight[t] = fut
        fut.add_done_callback(lambda f, t=t: self.events.put(("done", t, f)))

    def _broke(self, exc: BrokenProcessPool) -> None:
        """A dead pool: every task not back resolves to the exception (so every job ends with it), requested calls
        not yet started are reported to ``more`` as it, and what ``more`` returns raises it from the stream."""
        self.pool.broken = exc
        for port in self.live:
            for t in port.tasks:
                if t.state != "back":
                    self._finish(t, exc=exc)
        lost, self.queue = list(self.queue), deque()
        for key, _a, _k in lost:
            self.queue.extend(self.more(key, exc))

    # -- starting jobs
    def _start_job(self, key, args, kwargs, cls) -> None:
        port = _JobPort(self, key, args[1], cls)
        port.thread = threading.Thread(target=self._job_main, args=(port, args, kwargs), daemon=True,
                                       name=f"price-{cls}")
        self.live.add(port)
        self.pool.ports.add(port)
        if cls == "requested":
            self.pool._note(key, True)
        else:
            self.look[key] = port
        port.thread.start()

    def _job_main(self, port, args, kwargs) -> None:
        from isar.analyze.coupled.tasks import Abandoned
        try:
            result = self.pool.core.price(*args, **kwargs, tasks=port)
        except Abandoned:
            result = _ABANDONED
        except BaseException as exc:  # noqa: BLE001 — the scheduler hears of every end
            result = exc if isinstance(exc, Exception) else RuntimeError(f"price ended with {exc!r}")
        self.events.put(("job", port, result))

    def _start_call(self, call) -> None:
        key, args, kwargs = call
        fin = self.finished.get(key)
        if fin is not None and fin[0] == args[1]:  # a late hit: the lookahead job finished after the search chose this call
            self.queue.extend(self.more(key, fin[1]))
            return
        lp = self.look.get(key)
        if lp is not None and lp.job == args[1]:   # promote the live lookahead job: its tasks now rank as requested
            del self.look[key]
            lp.cls = "requested"
            self.pool._note(key, True)
            for t in lp.tasks:
                if t.state == "queued" and not t.dropped:
                    self._push(t)
            return
        self._start_job(key, args, kwargs, "requested")

    def _offer(self) -> bool:
        """Start one lookahead job when workers are spare; False when no offer is new."""
        if self.lookahead is None or len(self.look) >= self.pool.workers:
            return False
        for key, args in self.lookahead():
            if key in self.look or key in self.finished or key in self.tried:
                continue
            self.tried.add(key)
            self._start_job(key, args, {}, "lookahead")
            return True
        return False

    def _fill(self) -> None:
        while self.pool.broken is None and self._free() > 0:
            top, idle = self._top(), not self.queue and not self._starting()
            if top is not None and top[0] == 0 and top[1] == 0:
                self._send(top)
            elif self.queue and self._free() > self._starting():
                self._start_call(self.queue.popleft())
            elif top is not None and (top[0] == 0 or idle):
                self._send(top)
            elif idle and self._free() > self._starting() and self._offer():
                pass
            else:
                return

    # -- events
    def _handle(self, ev) -> None:
        kind = ev[0]
        if kind == "task":
            t = ev[1]
            if t.dropped or t.port.abandoned:
                return
            if self.pool.broken is not None:
                self._finish(t, exc=self.pool.broken)
                return
            t.entry = None
            self._push(t)
        elif kind == "raise":
            t = ev[1]
            if t.state == "queued" and t.rank and not t.dropped:
                t.rank = 0
                self._push(t)
        elif kind == "done":
            _, t, fut = ev
            self.inflight.pop(t, None)
            exc = fut.exception()
            if exc is None:
                value, seconds = fut.result()
                self.pool.busy_s += seconds
                if t.state != "back":
                    self._finish(t, value=value)
            elif isinstance(exc, BrokenProcessPool):
                if self.pool.broken is None:
                    log.error("price: the worker pool broke (%s)", exc)
                    self._broke(exc)
            elif t.state != "back":
                self._finish(t, exc=exc)
        else:
            self._ended(ev[1], ev[2])

    def _ended(self, port, result) -> None:
        self.live.discard(port)
        self.pool.ports.discard(port)
        port.ended = True
        if port.cls == "requested":
            self.pool._note(port.key, False)
            if isinstance(result, Exception):
                log.error("price of %s failed in the pool", port.key, exc_info=result)
            self.queue.extend(self.more(port.key, result))
            return
        self.look.pop(port.key, None)
        if result is _ABANDONED or isinstance(result, Exception):
            if isinstance(result, Exception):
                log.warning("lookahead price of %s failed; it is not cached", port.key, exc_info=result)
            return
        self.finished[port.key] = (port.job, result)
        if self.lookahead_done is not None:
            self.lookahead_done(port.key, result)

    # -- the stream
    def run(self) -> None:
        pool = self.pool
        if pool.broken is not None:
            raise pool.broken
        self.ex = pool._executor()
        try:
            while True:
                try:
                    while True:
                        self._handle(self.events.get_nowait())
                except Empty:
                    pass
                if pool.broken is not None and not self._requested_live() and self.queue:
                    raise pool.broken              # calls queued after the pool died reach it as the next map would
                self._fill()
                if not self.queue and not self._requested_live():
                    break
                try:
                    self._handle(self.events.get(timeout=MEMBERS_GAP_S))
                except Empty:
                    pass
                pool._tick(False)
        finally:
            self._leave()
        pool._tick(True)

    def _leave(self) -> None:
        """Abandon every live job (lookahead ones; the requested ones too when the stream ends by an exception), and
        hand the tasks still in flight to the pool's orphans, which no stream waits for."""
        ports = list(self.live)
        for port in ports:
            port.abandon()
        for fut in self.inflight.values():
            if not fut.done():
                self.pool.orphans.add(fut)
                fut.add_done_callback(self.pool.orphans.discard)
        self.inflight.clear()
        end = time.perf_counter() + JOIN_S
        for port in ports:
            port.thread.join(max(0.0, end - time.perf_counter()))
            if port.thread.is_alive():
                log.warning("price job %s was still unwinding when its stream ended", port.key)
        self.pool.ports.difference_update(ports)
        self.live.clear()


def pool_size(cores: int, width: int) -> int:
    """E14: ``min(item cores, widest stage, ISAR_WORKERS when set and > 0)``, at least 1."""
    w = min(int(cores), max(1, int(width)))
    env = os.environ.get("ISAR_WORKERS", "").strip()
    if env.isdigit() and int(env) > 0:
        w = min(w, int(env))
    return max(1, w)


# ---- JSON of the core's objects ----------------------------------------------------------------------------
def _plain(o, skip=()):
    """A JSON-safe copy: dataclasses as dicts (``skip`` names fields left out), tuple keys joined by "|"."""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _plain(getattr(o, f.name)) for f in dataclasses.fields(o) if f.name not in skip}
    if isinstance(o, Mapping):
        return {("|".join(map(str, k)) if isinstance(k, tuple) else str(k)): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [_plain(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, (str, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "tolist"):                          # numpy arrays and scalars
        return _plain(o.tolist())
    return repr(o)


def _came_in(snap) -> dict:
    # the context is the spectrum's own prepared data (rebuilt from the workspace), the settings are the batch's
    return _plain(snap, skip=("context", "own_record", "settings"))


def _term(link):
    from isar.session.inter_spectra.graph import Term
    if link.kind == "q" and link.metric not in ("hkl", "lattice"):
        raise ValueError(f"q link {link.a}|{link.b} has no metric, so no tolerated slope applies")
    return Term("T-change" if link.kind == "T" else "q-change", (link.a, link.b), (float(link.step),), None, None,
                link.metric)


def _other(link, k: str) -> str:
    return link.b if link.a == k else link.a


def _renamed(h, rename: Mapping[str, str]):
    """``h`` with its branch ids, observed and dormant, under the reconciled names (E18); ``h`` itself when none
    changes. A dormant id that a rename merges into one the fit observes (or into another dormant id) is not a
    returning branch: it is dropped, and one record is kept per canonical branch."""
    if not rename or not (any(m.branch in rename for m in h.modes) or any(d.branch in rename for d in h.dormant)):
        return h
    modes = tuple(replace(m, branch=rename.get(m.branch, m.branch)) for m in h.modes)
    seen, dormant = {m.branch for m in modes}, {}
    for d in h.dormant:
        b = rename.get(d.branch, d.branch)
        if b not in seen and b not in dormant:
            dormant[b] = replace(d, branch=b)
    return replace(h, modes=modes, dormant=tuple(dormant.values()))


def _renamed_snapshot(s, rename: Mapping[str, str]):
    """A snapshot with its own and held fits under the stage's reconciled branch ids, the rename in its aliases."""
    if not rename:
        return s
    from isar.analyze.coupled.identity import merged_aliases
    merged = merged_aliases(s.aliases, rename)     # a stored dormant id may name a branch merged in an earlier stage
    return replace(s, own=_renamed(s.own, merged), aliases=merged,
                   terms=tuple(replace(t, held=tuple(_renamed(h, merged) for h in t.held)) for t in s.terms))


def rebuilt_factors(core, A: Mapping, snaps: Mapping, rename: Mapping[str, str]) -> dict:
    """Each kept factor's definition built once more from its spectrum's stage snapshot under the reconciled ids
    (``core.stage_factors``): before the ids met, an endpoint did not carry the other's branch and left its own
    guard out of the scale, so the two endpoints of one link held two definitions. A factor the rebuilt snapshot
    does not form keeps the one it has, and a factor another member holds, whose term includes this one, is given
    to it (every block of a shared term minimises the same definition, PR #70 ad145c659301b-1)."""
    out = {}
    for k, a in A.items():
        defs = core.stage_factors(_renamed_snapshot(snaps[k], rename)) if a.factors else {}
        out[k] = replace(a, factors=tuple(defs.get(f.factor_id, f) for f in a.factors))
    owners: dict[str, set] = {}
    for a in out.values():
        for f in a.factors:
            owners.setdefault(f.factor_id, set()).add(f)
    for k, a in out.items():
        have = {f.factor_id for f in a.factors}
        extra = tuple(next(iter(fs)) for fid, fs in owners.items()
                      if fid not in have and len(fs) == 1 and k in next(iter(fs)).members)
        if extra:
            out[k] = replace(a, factors=a.factors + extra)
    return out


def canonical_factors(A: Mapping, ids, refs: Mapping, outside: Mapping) -> tuple[dict, dict]:
    """E18: one factor set for the stage. Each assignment's slot ids are mapped to the reconciled ids; a factor
    is kept only when every member of its term carries its (reconciled) branch, stage members by the reconciled
    ids of their matched slots (an unmatched slot has no neighbour density, so its block could not hold the factor
    its partner would: the batch would not minimise one objective) and fixed neighbours (``outside``: unit -> Held)
    by their modes. Returns the assignments with their incident kept factors, and the rename (alias -> name) the
    stage's held values follow."""
    rename = {o: name for name, others in (ids.alternatives.get("aliases") or {}).items() for o in others}
    carries = {k: {ids.ids.get(r) for r, s in zip(refs[k], a.slots) if s is not None} - {None} for k, a in A.items()}
    for u, h in outside.items():
        if u not in carries:
            carries[u] = {rename.get(m.branch, m.branch) for m in h.modes}
    out = {}
    for k, a in A.items():
        kept = []
        for f in a.factors:
            b = rename.get(f.branch_id, f.branch_id)
            if all(b in carries.get(m, ()) for m in f.members):
                kept.append(f if b == f.branch_id else replace(
                    f, branch_id=b, factor_id=f.factor_id[:len(f.factor_id) - len(f.branch_id)] + b))
        out[k] = replace(a, slots=tuple(None if s is None else rename.get(s, s) for s in a.slots),
                         factors=tuple(kept))
    return out, rename


# ---- one batch --------------------------------------------------------------------------------------------
class _Run:
    def __init__(self, root: Path, item: BatchItem, progress, emit, ack, core, target_s: float,
                 members=None) -> None:
        self.root, self.item, self._progress, self._emit, self._ack = root, item, progress, emit, ack
        self.core, self.target_s = core, target_s
        self.pool = _Pool(None, 1)
        self.token = 0
        self.started = False
        self.batch = str((item.spec or {}).get("batch_id"))
        self.kind = (item.spec or {}).get("kind")
        self.t0 = time.perf_counter()
        self.timing = {"wall_s": 0.0, "prepare_s": 0.0, "ship_s": 0.0, "save_s": 0.0, "assign_s": 0.0,
                       "likelihood_s": 0.0, "continuous_s": 0.0, "uncertainty_s": 0.0, "release_s": 0.0,
                       "starts": 0, "stages": [], "target_s": target_s, "over_target": False, "search_s": 0.0,
                       "pool": {"workers": 1, "iterations": 0, "map_wall_s": 0.0, "busy_s": 0.0}}
        self.tallies: dict[tuple[str, str], _Tally] = {}      # (stage label, unit) -> its stage-1 search record
        self.members: tuple[str, ...] = ()
        self.fixed: dict[str, str] = {}           # unit -> "anchor" | "lock" (never refitted)
        self.latest: dict[str, str] = {}          # unit -> "done" | "failed": its latest attempt (counts are current)
        self.failed: dict[str, str] = {}          # unit -> reason of its latest failed attempt
        self.not_reached: dict[str, str] = {}     # unit -> reason it was never attempted
        self.stale_locks: set[str] = set()        # locked on an out-of-date fit: neither refitted nor used (fixed)
        self.records: dict[str, dict] = {}        # fit id -> record of a refit accepted in this batch
        self.dormant: dict[str, tuple] = {}       # fit id -> its Dormant records (branches not observed in it)
        self.aliases: dict[str, str] = {}         # every id merged in this batch so far: alias -> canonical name
        self.stored_merges: dict[str, str] | None = None   # ids merged in earlier batches, read once (stored_aliases)
        self.lines: dict = {}                     # fit id -> RefitLine of the stored log, read when a fit is missing
        self.ctx: dict = {}
        self.stage_name, self.index, self.of, self.round = "prepare", 0, None, None
        self.rings_done = self.sweeps = self.rounds = None
        self.drift = None
        self._members_cb = members
        self.fitting: dict[str, int] = {}         # unit -> pool jobs in flight (pricing jobs of one unit may overlap)
        self.refused: set[str] = set()            # units whose latest failed attempt was a writer refusal
        self.final = False                        # end() has started: a member never attempted is "not reached"
        self._members_last = None                 # the last event sent
        self._members_at = -math.inf              # perf_counter of the last send

    # -- the members event
    def fly(self, key, on: bool) -> None:
        """_Pool.watch: a job of a unit starts or is collected (bookkeeping only; price keys are (unit, job key))."""
        u = key[0] if isinstance(key, tuple) else key
        n = self.fitting.get(u, 0) + (1 if on else -1)
        if n > 0:
            self.fitting[u] = n
        else:
            self.fitting.pop(u, None)

    def member_states(self) -> tuple[dict, dict]:
        held = self.held()
        states, reasons = {}, {}
        for u in self.members:
            if u in held:
                states[u] = "held"
                if u in self.stale_locks:
                    reasons[u] = STALE_LOCK
            elif u in self.fitting:
                states[u] = "fitting"
            elif self.latest.get(u) == "done":
                states[u] = "done"                # only judge() sets it: the writer accepted the save
            elif self.latest.get(u) == "failed":
                states[u] = "refused" if u in self.refused else "failed"
                reasons[u] = self.failed.get(u) or "failed"
            elif u in self.not_reached or self.final:
                states[u] = "not_reached"
                reasons[u] = self.not_reached.get(u, "not reached")      # the words unreached_list() uses
            else:
                states[u] = "waiting"             # queued for this ring or sweep, or returned and not yet judged
        return states, reasons

    def publish(self, force: bool = False) -> None:
        if self._members_cb is None or not self.members:         # before the freeze there is no map
            return
        now = time.perf_counter()
        if not force and now - self._members_at < MEMBERS_GAP_S:
            return                                # nothing is lost: the next call recomputes from live state
        states, reasons = self.member_states()
        ev = {"batch": self.batch, "kind": self.kind, "stage": self.stage_name, "index": self.index, "of": self.of,
              "round": self.round, "counts": self.counts(), "states": states, "reasons": reasons}
        if ev == self._members_last:
            return
        self._members_last, self._members_at = ev, now
        self._members_cb(ev)                      # a dead pipe raises: run_batch's outer except handles it

    # -- the service side
    def send(self, payload: dict, status: str = "done", reason: str | None = None) -> dict:
        self.token += 1
        self._emit(InterResult(self.item.item_id, self.item.unit_key, "inter", status, reason, payload, ack=self.token))
        t0 = time.perf_counter()
        verdict = self._ack(self.token)
        self.timing["save_s"] += time.perf_counter() - t0
        return verdict

    def payload(self, event: str, body: dict) -> dict:
        return {"schema": "isar.inter-result/1", "unit": f"inter:{self.batch}:batch", "pass": "inter",
                "batch": self.batch, "event": event, event: body}

    def held(self) -> set:
        """The members that stay as they are: anchors, locks, and stale locks (never refitted, so never to do)."""
        return set(self.fixed) | (self.stale_locks & set(self.members))

    def counts(self) -> dict:
        held = self.held()
        done = sum(1 for u, s in self.latest.items() if s == "done" and u not in held)
        failed = sum(1 for u, s in self.latest.items() if s == "failed" and u not in held)
        n = len(self.members)
        return {"done": done, "failed": failed, "not_reached": n - done - failed - len(held),
                "fixed": len(held), "total": n}

    @staticmethod
    def _grouped(reasons) -> list[list]:
        """[[reason, n], ...], most frequent first, ties in first-seen order."""
        tally: dict[str, int] = {}
        for why in reasons:
            tally[why] = tally.get(why, 0) + 1
        return [[why, n] for why, n in sorted(tally.items(), key=lambda kv: -kv[1])]

    def failed_reasons(self) -> list[list]:
        held = self.held()
        return self._grouped(why for u, why in self.failed.items()
                             if u in self.members and u not in held and self.latest.get(u) == "failed")

    def not_reached_reasons(self) -> list[list]:
        return self._grouped(d["reason"] for d in self.unreached_list())

    def progress(self, phase: str) -> None:
        self._progress({"pass": "inter", "kind": self.kind, "stage": self.stage_name, "index": self.index,
                        "of": self.of, "round": self.round, "phase": phase, **self.counts(),
                        "failed_reasons": self.failed_reasons(), "not_reached_reasons": self.not_reached_reasons()})
        self.publish(True)            # every phase boundary, the judge result and the final map are never throttled

    def unreached_list(self) -> list[dict]:
        held = self.held()
        return [{"unit": u, "reason": self.not_reached.get(u, "not reached")} for u in self.members
                if u not in held and u not in self.latest]

    def end(self, state: str, reason: str | None = None) -> None:
        if not self.started:          # nothing saved yet: the writer would refuse the end of an unknown batch
            self._emit(InterResult(self.item.item_id, self.item.unit_key, "inter", "failed", reason, None))
            return
        self.final = True
        self.progress("end")          # the last event a finished batch leaves in the GUI carries its final accounting
        self.timing["wall_s"] = time.perf_counter() - self.t0
        self.timing["ship_s"] = self.pool.ship_s
        self.timing["over_target"] = self.timing["wall_s"] > self.target_s     # information only
        spectra = [t.as_dict() for t in self.tallies.values()]
        slow = max(spectra, key=lambda r: r["evidence_s"] + r["refine_s"], default=None)
        wide = max(self.timing["stages"], key=lambda s: s["wall_s"], default=None)
        self.timing["spectra"] = spectra
        self.timing["slowest"] = {
            "spectrum": None if slow is None else {"stage": slow["stage"], "unit": slow["unit"],
                                                   "seconds": slow["evidence_s"] + slow["refine_s"]},
            "stage": None if wide is None else {"kind": wide["kind"], "index": wide["index"],
                                                "wall_s": wide["wall_s"]}}
        self.timing["pool"]["workers"] = self.pool.workers
        c, missed = self.counts(), self.unreached_list()
        if state == "failed":
            status = "failed"
        elif c["failed"] or missed or state in ("did_not_settle", "nothing_left"):
            status = "done_warning"
            if reason is None:
                reason = "; ".join(x for x in (f"{len(missed)} spectra not reached" if missed else "",
                                               f"{c['failed']} failed" if c["failed"] else "") if x)
        else:
            status = "done"
        body = {"state": state, "reason": reason, "done": c["done"], "failed": c["failed"], "not_reached": missed,
                "failed_units": [{"unit": u, "reason": why} for u, why in self.failed.items()
                                 if u in self.members and self.latest.get(u) == "failed" and u not in self.held()],
                "stale_locks": [{"unit": u, "reason": STALE_LOCK} for u in sorted(self.stale_locks & set(self.members))],
                "rings_done": self.rings_done, "sweeps": self.sweeps, "rounds": self.rounds,
                "timing": _plain(self.timing), "drift": self.drift}
        self.send(self.payload("end", body), status, reason)

    # -- the freeze
    def go(self) -> None:
        from isar.intake import gpi, inputs, scans as intake_scans, store
        from isar.intake.inter_spectra import read_inter_settings, read_user
        from isar.session import currentness, file_fit
        from isar.session.inter_spectra import authority, q_provider, records
        from isar.session.inter_spectra.graph import anchored_scope, build_graph
        from isar.session.inter_spectra.layout import grid_for
        from isar.session.inter_spectra.nodes import read_nodes

        self.records_mod, self.read_user = records, read_user
        if self.core is None:
            self.core = importlib.import_module(CORE)
        spec = BatchSpec.from_json(self.item.spec)
        self.spec, self.batch, self.kind = spec, spec.batch_id, spec.kind
        self.progress("prepare")
        t0 = time.perf_counter()
        self.settings = read_inter_settings(self.root)
        user = read_user(self.root)
        self.intake_seq = user.max_seq
        self.frozen = frozen = file_fit.freeze(self.root)
        ns = read_nodes(self.root)
        self.nodes = ns.nodes
        if spec.kind == "anchored":
            scope, kinds = anchored_scope(spec.anchors, spec.direction, ns)
            self.members = tuple(sorted(set(spec.members)))
            if set(scope) != set(spec.members):
                raise _Ended("failed", SCOPE_CHANGED)
        else:
            scope, kinds = frozenset(k for k in spec.members if k in ns.nodes), frozenset({"q", "T"})
            self.members = tuple(sorted(set(spec.members)))
            for k in set(spec.members) - scope:
                self.not_reached[k] = "unknown to the import record"
        qc = q_provider.q_coordinates(self.root, scope)        # a module attribute: a timing tool may supply its own
        self.epochs = {**{scan: label for scan, (_cell, label) in qc.lattices.items()},
                       **{k.rsplit(":", 1)[0]: why for k, why in qc.reasons.items()
                          if why not in (q_provider.NO_HKL, "unknown spectrum")}}
        self.graph = build_graph(ns.nodes, ns, scope=scope, kinds=kinds, grid=grid_for(frozen.card.policy.plugin),
                                 coords=qc.points if "q" in kinds else {},
                                 collinearity_tol=self.settings.collinearity_tol, q_reasons=qc.reasons)
        if "q" in kinds and spec.kind == "anchored":       # E17: a spectrum without a point says why
            for k, why in qc.reasons.items():
                if k in scope and k not in spec.anchors and not self.graph.links_of(k):
                    self.not_reached.setdefault(k, why)

        # which fit of each member is in use now (C-authority), and whether it may condition
        rows = intake_scans.read_scans(self.root)
        read = store.read_all(self.root, store.ENTRIES)
        index = inputs.index_entries(read.entries, lines=read.lines)
        log0 = records.read_log(self.root)
        by_unit: dict[str, list] = {}
        for r in log0.refits:
            by_unit.setdefault(r.unit, []).append(r)
        needed = sorted({*scope, *(c.unit for u in scope for r in by_unit.get(u, ()) for c in r.conditioned_on)})
        self.now = now = records.inputs_now_from(rows, index, frozen.card.state.tabulated_calibration, needed,
                                                 gpi=gpi.entries_in_force(self.root))
        self.lines = {r.fit: r for r in log0.refits}
        self.res = {u:authority.resolve(u, refits=by_unit.get(u, ()), pick=user.picks.get(u), lock=user.locks.get(u),
                                         has_fast=authority.has_fast_solo(frozen.session.get(u), u), inputs_now=now)
                    for u in scope}
        locked = {u for u in scope if user.locks.get(u) is not None and user.locks[u].locked}
        self.stale_locks = {u for u in locked if not self.res[u].conditions}
        self.locks = locked - self.stale_locks
        for u in self.stale_locks:
            self.not_reached[u] = STALE_LOCK

        # E6: preparation, one parse per scan, one prepare per member.
        # shortcut: serial prepare; parallelise through the pool when prepare_s exceeds 10% of wall.
        request = currentness.queue_deep_request()
        scans: dict[str, object] = {}
        self.inputs, self.current = {}, set()
        for u in sorted(scope):
            if not ns.nodes[u].eligible:
                continue
            exp, scan, det = u.rsplit(":", 2)
            entry = frozen.session.get(u)
            if f"{exp}:{scan}" not in scans:
                scans[f"{exp}:{scan}"] = file_fit.prepare_scan(frozen, exp, int(scan), entry)
            sp = scans[f"{exp}:{scan}"]
            if isinstance(sp, str):
                self.ctx[u] = ("failed", sp)
                continue
            prep = file_fit.prepare_unit(frozen, sp, u, ("fast",))
            if prep.reason is not None:
                self.ctx[u] = ("failed", prep.reason)
                continue
            self.ctx[u] = file_fit.fit_context(prep)
            self.inputs[u] = prep.now.inputs_hash
            if "fast" in currentness.judge(entry, det, prep.now, request):
                self.current.add(u)
        self.timing["prepare_s"] = time.perf_counter() - t0
        self.incoming = {u: self.held_of(u, self.res[u].fit) for u in self.usable(scope)
                         if self.res[u].fit and u not in self.stale_locks}
        self.fast = {u: self.held_of(u, "fast") for u in self.incoming}
        if spec.kind == "anchored":
            self.anchored()
        else:
            self.simultaneous()

    def usable(self, units) -> list[str]:
        """Members that may be refitted and may condition: prepared, with a current fast pass (E10)."""
        return [u for u in sorted(units) if u in self.current and not isinstance(self.ctx.get(u), tuple)]

    def why_unusable(self, u: str) -> tuple[str, str]:
        """``("failed" | "not_reached", reason)`` of a member ``usable`` leaves out."""
        c = self.ctx.get(u)
        if isinstance(c, tuple):
            return "failed", c[1]
        if u not in self.ctx:
            n = self.nodes.get(u)
            return "not_reached", (n.reason if n is not None and n.reason else "not eligible")
        return "not_reached", NOT_CURRENT

    def mark_unusable(self, u: str) -> None:
        kind, why = self.why_unusable(u)
        if kind == "failed":
            self.latest[u], self.failed[u] = "failed", why
        else:
            self.not_reached.setdefault(u, why)

    def record_of(self, u: str, fit: str) -> dict:
        if fit == "fast":
            return self.frozen.session.get(u).detectors[u.rsplit(":", 1)[1]]["fast_pass"]
        if fit in self.records:
            return self.records[fit]
        line = self.line_of(fit)
        payload = None if line is None else self.records_mod.read_payload(self.root, line)
        if not payload or not (payload.get("overlay") or {}).get("fast_pass"):
            raise ValueError(f"{u}: the stored fit {fit} cannot be read")
        return payload["overlay"]["fast_pass"]

    def line_of(self, fit: str):
        """The stored log line of a refit not made in this batch (the log is re-read once for a fit not seen yet)."""
        if fit not in self.lines:
            self.lines = {r.fit: r for r in self.records_mod.read_log(self.root).refits}
        return self.lines.get(fit)

    def held_of(self, u: str, fit: str):
        # a stored refit carries the inputs it was made from (C-save-inter), never today's hash, and the branches
        # not observed in it, so a branch that fades and returns keeps its id across rings, sweeps and batches
        line = None if fit == "fast" or fit in self.records else self.line_of(fit)
        inputs = line.inputs if line is not None and line.inputs is not None else self.inputs.get(u)
        if fit not in self.dormant and line is not None:
            from isar.analyze.coupled.types import Dormant
            stored = ((self.records_mod.read_payload(self.root, line) or {}).get("came_out") or {}).get("dormant") or ()
            self.dormant[fit] = tuple(Dormant(d["branch"], d["last_seen_fit"], d["last_seen_unit"]) for d in stored)
        held = self.core.held_from_fit(self.record_of(u, fit), unit=u, fit=fit, inputs=inputs,
                                       context=self.ctx[u], T=self.nodes[u].T, dormant=self.dormant.get(fit, ()))
        # a stored refit's ids were canonical when it was saved; a batch that merged them since renames them here
        return _renamed(held, self.stored_aliases()) if line is not None else held

    def stored_aliases(self) -> dict[str, str]:
        """The ids merged in earlier batches of this workspace (alias -> canonical name), read once per batch. A
        batch's aliases are cumulative within it (``self.aliases``), so its last saved refit's snapshot carries them."""
        if self.stored_merges is None:
            from isar.analyze.coupled.identity import merged_aliases
            last = {}
            for line in self.records_mod.read_log(self.root).refits:
                last[line.batch] = line
            merged: dict[str, str] = {}
            for line in last.values():
                came_in = (self.records_mod.read_payload(self.root, line) or {}).get("came_in") or {}
                merged = merged_aliases(merged, came_in.get("aliases") or {})
            self.stored_merges = merged
        return self.stored_merges

    def merge_aliases(self, rename: Mapping[str, str]) -> None:
        from isar.analyze.coupled.identity import merged_aliases
        self.aliases = merged_aliases(self.aliases, rename)

    def note_dormant(self, ids, k: str, fit: str, observed=()) -> tuple:
        """The identity's absent records of ``k`` (C-identity) as its new fit's Dormant branches, under the batch's
        merged ids: one record per canonical branch, none for a branch the fit observes (``observed``: its ids)."""
        from isar.analyze.coupled.types import Dormant
        canon = lambda b: self.aliases.get(b, b)     # noqa: E731
        seen, out = {canon(b) for b in observed}, {}
        for r in (ids.absent or {}).get(k, ()):
            b = canon(r["branch"])
            if b not in seen and b not in out:
                out[b] = Dormant(b, r["last_seen_fit"], r["last_seen_unit"])
        self.dormant[fit] = tuple(out.values())
        return self.dormant[fit]

    def snapshot(self, u: str, own, sid: str, terms, stage_id: str | None = None):
        from isar.analyze.coupled.types import Snapshot
        return Snapshot(snapshot_id=sid, stage_id=stage_id or sid, unit=u, fit_id=self.records_mod.new_fit_id(),
                        context=self.ctx[u], own=own, own_record=self.record_of(u, own.fit), terms=tuple(terms),
                        settings=self.settings, batch_kind=self.kind, fast=self.fast.get(u),
                        aliases=dict(self.aliases))

    def start(self, *, anchors, rings, seams, preview, terms) -> None:
        # the slope each scan's q-change penalty used; an anchored batch only (a simultaneous one uses no q slope)
        slopes = {s: {"metric": m, "slope": self.settings.q_slope(m, s),
                      "unit": "meV per r.l.u." if m == "hkl" else "meV per inverse angstrom"}
                  for s, m in self.graph.metric.items()} if self.kind == "anchored" else {}
        body = {"batch_kind": self.kind, "scope": {"direction": self.spec.direction, "members": list(self.members)},
                "anchors": list(anchors), "rings": dict(rings), "seams": [list(s) for s in seams],
                "settings": {"version": self.settings.version, "values": _plain(self.settings.to_values())},
                "preview": preview, "fixed": dict(self.fixed),
                "unreached": {u: self.not_reached[u] for u in self.members if u in self.not_reached},
                "terms": [_plain(t) for t in terms], "metric": dict(self.graph.metric), "q_slope": slopes,
                "core": CORE_VERSION,
                "inputs_at": int(self.frozen.inputs_at), "intake_seq": int(self.intake_seq),
                "epochs": dict(self.epochs), "inputs": {u: self.inputs.get(u) for u in self.members}}
        verdict = self.send(self.payload("start", body))
        if not verdict.get("saved"):
            raise _Ended("failed", f"could not save the batch start: {verdict.get('error')}")
        self.started = True

    def stage_label(self) -> str:
        """The stage of the stage-1 searches and of the refits that follow them: ``ring<r>`` or ``round<n>``."""
        return f"ring{self.index}" if self.kind == "anchored" else f"round{self.round}"

    def tally(self, label: str, k: str) -> _Tally:
        return self.tallies.setdefault((label, k), _Tally(label, k))

    def assign(self, snaps: dict, cache: dict) -> tuple[dict, dict]:
        """Stage 1 of every snapshot: one core ``Search`` per spectrum, all driven together through one pool stream.
        Each search waits only for its own jobs: when the last job of its pass is back it is fed that pass's prices
        in job order (cache hits included), and its next pass's uncached jobs join the stream at once, while other
        spectra's jobs still run. A pass the cache answers alone is fed inline. ``cache``: unit -> {job key:
        Priced}, whose keys hash every value a price depends on (fit ids included), so a changed snapshot never
        reuses one. Returns ``(A, errors)``: the assignments, and the exception that failed each spectrum whose search raised (a
        pool exception is only that job's failed price; a dead pool raises out of here and ends the batch)."""
        from isar.analyze.coupled.types import Priced
        t0 = time.perf_counter()
        label = self.stage_label()
        tallies = {}
        for k, s in snaps.items():
            tallies[k] = self.tallies[label, k] = _Tally(label, k)
            tallies[k].branches_seen, tallies[k].dormant_seen = _seen(s)
        live, A, errors = {}, {}, {}
        asked = {k: set() for k in snaps}

        def timed(fn, *args):
            t = time.perf_counter()
            try:
                return fn(*args)
            finally:
                self.timing["search_s"] += time.perf_counter() - t

        def fail(k, exc):
            log.error("the search of %s failed: %s", k, exc, exc_info=exc)
            errors[k] = exc
            tallies[k].error = f"{type(exc).__name__}: {exc}"
            live.pop(k, None)

        def next_fresh(k):
            """The next pass of ``k``'s search, after feeding any passes the cache answers alone; () when done."""
            have = cache.setdefault(k, {})
            while True:
                jobs = timed(live[k].next_jobs)
                if not jobs:
                    if not live[k].done:
                        raise RuntimeError("the search returned no jobs but is not done")
                    return ()
                keys = [j.key for j in jobs]
                if len(set(keys)) != len(keys) or asked[k] & set(keys):
                    raise RuntimeError("the search repeated a job")
                asked[k].update(keys)
                if any(key not in have for key in keys):
                    return jobs
                tallies[k].cached += len(keys)
                timed(live[k].feed, [have[key] for key in keys])

        tasks = getattr(self.pool, "uses_tasks", lambda fn: False)("price")   # a stand-in pool has today's stream()

        def lookahead() -> list:
            """Offers for idle workers: each live search's likely next promotions, round-robin across spectra."""
            rows = []
            for k, search in live.items():
                try:
                    named = search.likely_next(self.pool.workers) if hasattr(search, "likely_next") else ()
                except Exception:  # noqa: BLE001 — lookahead is only speculation; the search's own calls report real failures
                    log.warning("likely_next of %s failed; no lookahead from it", k, exc_info=True)
                    continue
                rows.append([((k, j.key), (snaps[k], j)) for j in named
                             if j.key not in cache.get(k, ()) and j.key not in asked[k]])
            return [o for row in itertools.zip_longest(*rows) for o in row if o is not None]

        def lookahead_done(key, p) -> None:
            k, job_key = key
            if k in live and isinstance(p, Priced):
                cache.setdefault(k, {})[job_key] = p

        passes: dict = {}            # k -> (its pass's jobs, the keys of that pass still in the pool)
        got: dict = {}               # (k, job key) -> what the pool returned for it

        def advance(k) -> list:
            """``k``'s next pass as pool calls, or [] once its search is done or failed."""
            try:
                jobs = next_fresh(k)
                if not jobs:
                    a = timed(live[k].result)
                    tallies[k].stopped = _stopped(a)
                    A[k] = a
                    del live[k]
                    return []
            except Exception as exc:  # noqa: BLE001
                fail(k, exc)
                return []
            calls = [((k, j.key), (snaps[k], j), {}) for j in jobs if j.key not in cache[k]]
            passes[k] = (jobs, {c[0] for c in calls})
            self.timing["pool"]["iterations"] += 1
            return calls

        def back(key, result) -> list:
            """``_Pool.stream``'s ``more``: once the last job of ``k``'s pass is back, feed it in job order."""
            k = key[0]
            got[key] = result
            jobs, waiting = passes[k]
            waiting.discard(key)
            if waiting:
                return []
            del passes[k]
            out, tl, hits = [], tallies[k], 0
            for j in jobs:
                if (k, j.key) not in got:
                    out.append(cache[k][j.key])
                    hits += 1
                    continue
                p = got.pop((k, j.key))
                if isinstance(p, Exception):              # not a price: never cached, a later stage prices it afresh
                    p = Priced(key=j.key, count=j.count, slots=tuple(j.slots), ln_z=None, status="no_evidence",
                               mode=None, reason=f"pricing failed ({type(p).__name__}: {p})", starts=0,
                               seconds=0.0, depth=j.depth)
                else:
                    cache[k][j.key] = p
                tl.candidates += 1
                tl.starts += len(j.starts)
                tl.fits += p.starts
                tl.evidence_s += p.seconds
                if p.ln_z is None:
                    tl.failures += 1
                    tl.reasons[p.reason or p.status] += 1
                self.timing["likelihood_s"] += p.seconds
                if not tasks:                              # on the task path the pool's own busy_s counts it
                    self.timing["pool"]["busy_s"] += p.seconds
                out.append(p)
            tl.iterations += 1
            tl.cached += hits
            log.info("batch %s %s %s pass %d: %d jobs sent, %d cached, %d searches live, %.1fs", self.batch, label, k,
                     tl.iterations, len(jobs) - hits, hits, len(live), time.perf_counter() - t0)
            try:
                timed(live[k].feed, out)
            except Exception as exc:  # noqa: BLE001
                fail(k, exc)
                return []
            return advance(k)

        for k, s in snaps.items():
            try:
                live[k] = timed(self.core.open_search, s)
            except Exception as exc:  # noqa: BLE001 — one spectrum's failure is its result
                fail(k, exc)
        calls = [c for k in list(live) for c in advance(k)]          # every spectrum's first pass, in snaps order
        if calls:
            t, busy0 = time.perf_counter(), getattr(self.pool, "busy_s", 0.0)
            if tasks:
                self.pool.stream("price", calls, back, lookahead=lookahead, lookahead_done=lookahead_done)
                self.timing["pool"]["busy_s"] += self.pool.busy_s - busy0
            else:
                self.pool.stream("price", calls, back)               # a dead pool raises here, uncaught
            self.timing["pool"]["map_wall_s"] += time.perf_counter() - t
        self.timing["assign_s"] += time.perf_counter() - t0
        return {k: A[k] for k in snaps if k in A}, errors

    def identity(self, A: dict, snaps: dict, previous: dict, sid: str, energies: dict | None = None):
        """``propagate_ids`` over one stage (argument shapes: its docstring, amendment 6). Without ``energies``
        each slot takes the incoming energies padded with the window centre (only unmatched slots read them)."""
        from isar.analyze.coupled import identity
        refs = {k: [(k, snaps[k].fit_id, m) for m in range(A[k].count)] for k in A}
        energies = dict(energies or {})
        for k, a in A.items():
            if energies.get(k) is None:
                own = [m.omega0 for m in snaps[k].own.modes]
                energies[k] = (own + [0.5 * sum(snaps[k].own.window)] * a.count)[:a.count]
        ids = identity.propagate_ids(
            sorted(A), self.graph.links, refs, self.settings, batch_mode=self.kind,
            rings=self.ring_of if self.kind == "anchored" else None, snapshot_id=sid, assignments=A,
            previous=previous, energies=energies)
        return ids, refs

    def refits(self, snaps: dict, A: dict) -> dict:
        calls = [(k, (snaps[k],), {"stage": "continuous", "assignment": A[k], "workers": 1}) for k in snaps]
        R = self.pool.map("refit", calls)
        label = self.stage_label()
        for k, r in R.items():
            if not isinstance(r, Exception):
                for name in ("continuous_s", "uncertainty_s", "release_s"):
                    self.timing[name] += float((r.timing or {}).get(name, 0.0))
                    self.tally(label, k).refine_s += float((r.timing or {}).get(name, 0.0))
                self.timing["starts"] += int(r.starts or 0)
        return R

    def entry(self, k: str, snap, r, extra_flags=(), stage1: dict | None = None) -> dict:
        """The stage payload's refit line of one refit (or of the exception the refit raised)."""
        cond = list({h.unit: {"unit": h.unit, "fit": h.fit, "inputs": h.inputs}
                     for ti in snap.terms for h in ti.held}.values())
        base = {"fit": snap.fit_id, "unit": k, "seed": snap.own.fit, "inputs": self.inputs.get(k),
                "conditioned_on": cond}
        if isinstance(r, Exception):
            return {**base, "status": "failed", "reason": f"analysis error ({type(r).__name__}: {r})", "flag": None,
                    "why_changed": None, "payload": {"came_in": _came_in(snap), "came_out": None, "overlay": None}}
        flag = _plain(r.flag) if r.flag else None
        if flag is not None and extra_flags:
            flag["reasons"] = [*flag.get("reasons", []), *extra_flags]
            flag["flagged"] = True
        came_out = _plain(r, skip=("fit", "overlay"))         # both live in the payload's overlay
        if stage1 is not None:
            came_out["stage1"] = stage1                         # E12: the round's stage 1 rides on its first sweep
        came_out["dormant"] = _plain(self.dormant.get(snap.fit_id, ()))     # branches not observed in this fit
        overlay = _plain(r.overlay) if r.status in _ELIGIBLE and r.overlay is not None else None
        if overlay is not None:                                 # the context it is drawn in, now and later
            solo = self.frozen.session.get(k).detectors[k.rsplit(":", 1)[1]]
            overlay.update({name: _plain(solo.get(name)) for name in ("calib", "T")})
        status, reason = r.status, r.reason
        if status in _ELIGIBLE and overlay is None:             # a fit nobody can draw is not offered
            status, reason = "failed", r.overlay_unavailable or "no drawable fit"
        return {**base, "status": status, "reason": reason, "flag": flag, "why_changed": r.why_changed,
                "payload": {"came_in": _came_in(snap), "came_out": came_out, "overlay": overlay}}

    def save_stage(self, stage: str, index: int, entries: list, seams: list) -> dict:
        body = {"stage": stage, "index": index, "round": self.round, "refits": entries, "seams": seams,
                "progress": self.counts(), "not_reached": dict(self.not_reached)}
        verdict = self.send(self.payload("stage", body))
        if not verdict.get("saved"):
            raise _Ended("failed", f"could not save {stage} {index}: {verdict.get('error')}")
        return verdict

    def judge(self, k: str, r, verdict: dict, fit_id: str) -> bool:
        """Apply the writer's verdict to one member's attempt; True when its refit is now authoritative."""
        if not isinstance(r, Exception) and r.status in _ELIGIBLE and verdict["accepted"].get(k) == fit_id:
            self.latest[k] = "done"
            self.failed.pop(k, None)
            self.refused.discard(k)
            return True
        refused = verdict["refused"].get(k)
        if refused is not None:       # fitted, but the writer kept it out of use; an inputs refusal says what to do
            refused = (f"fitted but not saved ({refused})"
                       + (INPUTS_REMEDY if refused.endswith(self.records_mod.INPUTS_TAIL) else ""))
        why = (refused or (f"analysis error ({type(r).__name__}: {r})" if isinstance(r, Exception)
                                             else r.reason or r.status))
        self.latest[k], self.failed[k] = "failed", why
        (self.refused.add if verdict["refused"].get(k) is not None else self.refused.discard)(k)
        return False

    def identity_flags(self, ids, k: str) -> list[dict]:
        """The identity reasons of one spectrum (ambiguous, loop, unresolved return) as flag reasons, each with
        the values recorded for that spectrum only."""
        return [{"code": code, "severity": f.get("severity", "warn"),
                 "text": "; ".join(str(v) for v in list((f.get("by_unit") or {}).get(k) or ())[:3])}
                for code, f in (ids.flags or {}).items() if k in f.get("units", ()) and code != "seam_disagreement"]

    def stage_timing(self, kind: str, index: int, t0: float, width: int, n: int) -> None:
        self.timing["stages"].append({"kind": kind, "index": index, "wall_s": time.perf_counter() - t0,
                                      "width": width, "n_refits": n})

    def take_new_locks(self, held: dict) -> set:
        """E11: locks are re-read at every ring and sweep; a member locked since becomes a fixed neighbour, read
        as the fit its lock pins. A lock on a stale (or unrecorded) fit is judged as at the freeze: it is neither
        refitted nor used, so its held value goes. Returns the units that leave the refits."""
        from isar.session.inter_spectra import authority
        user = self.read_user(self.root)
        new = set()
        for u, ls in user.locks.items():
            if not (ls.locked and u in self.members and u not in self.fixed and u not in self.stale_locks
                    and u in self.current and not isinstance(self.ctx.get(u), tuple)):
                continue
            fit = ls.fit or "fast"
            line = None if fit == "fast" or fit in self.records else self.line_of(fit)
            new.add(u)
            if fit not in ("fast", *self.records) and (line is None or authority.stale_of(line, self.now) is not None):
                self.stale_locks.add(u)
                held.pop(u, None)
                self.not_reached.setdefault(u, STALE_LOCK)
                continue
            self.fixed[u] = "lock"
            held[u] = self.held_of(u, fit)
            self.not_reached.pop(u, None)
        return new

    def open_pool(self, width: int) -> None:
        # width is the item's cores, not the stage's width: one search pass holds several jobs per spectrum
        w = pool_size(self.item.cores, width)
        self.pool = _Pool(self.core, w, watch=self.fly, tick=self.publish)
        if w < self.item.cores:              # the cores this batch cannot use go back to the queue
            self._emit(InterResult(self.item.item_id, self.item.unit_key, "cores", "cores", None, None, w))

    # -- anchored (E16)
    def anchored(self) -> None:
        from isar.analyze.coupled.types import TermInput
        from isar.session.inter_spectra.preview import preview_anchored
        from isar.session.inter_spectra.rings import plan_anchored
        g, spec = self.graph, self.spec
        plan = plan_anchored(g, spec.anchors, self.locks | self.stale_locks, self.stale_locks)
        self.ring_of = plan.ring_of
        for a in plan.anchors:
            if a not in self.incoming:                  # E10: an anchor that is not current ends the batch here
                raise _Ended("failed", f"anchor {a}: {self.why_unusable(a)[1]}")
        self.fixed = {**{a: "anchor" for a in plan.anchors}, **{u: "lock" for u in sorted(self.locks)}}
        held = {u: self.incoming[u] for u in self.fixed if u in self.incoming}
        for u in self.members:
            if u in self.fixed or u in self.stale_locks:
                continue
            if u in plan.not_reached:
                self.not_reached.setdefault(u, plan.not_reached[u])
            elif u not in self.incoming:
                self.mark_unusable(u)
        self.open_pool(self.item.cores)
        self.of = len(plan.rings)
        self.start(anchors=plan.anchors, rings=plan.ring_of, seams=[(s.link.a, s.link.b) for s in plan.seams],
                   preview=preview_anchored(g, plan, spec.direction).text,
                   terms=sorted({_term(l) for r in plan.rings for ls in r.conditioners.values() for l in ls},
                                key=lambda t: t.members))
        self.stage_name, self.rings_done = "plan", 0
        self.progress("plan")
        for r, ring in enumerate(plan.rings, 1):
            t_ring = time.perf_counter()
            self.stage_name, self.index = "ring", r
            self.take_new_locks(held)                       # a lock set mid-batch is fixed from this ring on
            held_frozen = dict(held)                        # the state every refit of this ring conditions on
            sid = f"{self.batch}:ring{r}"
            todo = {}
            for k in ring.members:
                if k in self.fixed or k in self.stale_locks or k not in self.incoming:
                    continue
                links = [l for l in ring.conditioners.get(k, ()) if _other(l, k) in held_frozen]
                if not links:
                    self.not_reached[k] = CUT_OFF
                    continue
                todo[k] = [TermInput(_term(l), (held_frozen[_other(l, k)],)) for l in links]
            if not todo:
                self.rings_done = r
                self.stage_timing("ring", r, t_ring, 0, 0)
                self.publish(True)                      # this ring's members were just cut off or are held
                continue
            snaps = {k: self.snapshot(k, self.incoming[k], sid, terms) for k, terms in todo.items()}
            self.progress("assign")
            A, errors = self.assign(snaps, {})
            previous = {**held_frozen, **{k: s.own for k, s in snaps.items() if k in A}}    # amendment 11: own Held too
            ids, refs = self.identity(A, snaps, previous, sid)
            A, rename = canonical_factors(A, ids, refs, held_frozen)
            self.merge_aliases(rename)
            snaps = {k: _renamed_snapshot(s, rename) for k, s in snaps.items()}
            self.progress("refine")
            R = self.refits({k: s for k, s in snaps.items() if k in A}, A)
            R.update(errors)                                # a spectrum whose search failed is a failed refit line
            # seams, judged on each spectrum's latest refit energies (after the ring's continuous refits)
            self.progress("seams")
            ok = {k for k, x in R.items() if not isinstance(x, Exception) and x.status in _ELIGIBLE}
            energies = {k: [m.omega0 for m in R[k].modes] for k in ok if len(R[k].modes) == A[k].count}
            ids_after, _ = self.identity(A, snaps, previous, sid, energies)
            seams = self.ring_seams(plan, r, R, ok, ids_after)
            for k, s in snaps.items():
                self.note_dormant(ids_after, k, s.fit_id,
                                  () if isinstance(R[k], Exception) else [m.branch for m in R[k].modes])
            entries = [self.entry(k, snaps[k], R[k], self.identity_flags(ids, k)) for k in snaps]
            verdict = self.save_stage("ring", r, entries, seams)
            for k, s in snaps.items():
                if self.judge(k, R[k], verdict, s.fit_id):
                    self.records[s.fit_id] = R[k].fit
                    held[k] = self.core.held_from_fit(R[k].fit, unit=k, fit=s.fit_id, inputs=self.inputs.get(k),
                                                      context=self.ctx[k], T=self.nodes[k].T,
                                                      dormant=self.dormant[s.fit_id])
            self.rings_done = r
            self.stage_timing("ring", r, t_ring, len(snaps), len(snaps))
            self.progress("save")
        self.drift = self.drift_of(held)
        self.end("completed")

    def ring_seams(self, plan, r: int, R: dict, ok: set, ids_after) -> list[dict]:
        """The seams of ring ``r`` that disagree: the core's measured mismatch over the threshold, and the
        identity's seam conflicts. Nothing is added to either fit (a later simultaneous batch reconciles)."""
        out = []
        for s in plan.seams:
            a, b = s.link.a, s.link.b
            if s.ring != r or a not in ok or b not in ok:
                continue
            rep = self.core.seam_report(R[a], R[b], s.link, self.settings)
            if rep.get("over"):
                out.append({"a": a, "b": b, "link": s.link.kind, "mismatch": rep.get("max_U"),
                            "flag": {"code": "seam_disagreement", "severity": "warn",
                                     "text": f"the two fronts disagree across this seam (mismatch "
                                             f"{rep.get('max_U', 0.0):.3g}, threshold {rep.get('threshold')})"},
                            "report": _plain(rep)})
        kinds = {tuple(sorted((l.a, l.b))): l.kind for l in self.graph.links}
        for s in ids_after.seams:
            a, b = s["link"]
            out.append({"a": a, "b": b, "link": kinds.get(tuple(sorted((a, b))), "q"), "mismatch": None,
                        "flag": {"code": "seam_disagreement", "severity": "warn",
                                 "text": f"identity across this seam: {s['issue'].replace('_', ' ')}"},
                        "report": _plain(s)})
        return out

    # -- simultaneous (E15)
    def simultaneous(self) -> None:
        from isar.analyze.coupled.types import TermInput
        from isar.session.inter_spectra.preview import preview_simultaneous
        from isar.session.inter_spectra.schedule import check_groups, plan_simultaneous
        g, s = self.graph, self.settings
        plan = plan_simultaneous(g, self.locks | self.stale_locks)
        check_groups(plan)
        self.ring_of = None
        self.fixed = {u: "lock" for u in plan.held if u in self.locks}
        held = {u: self.incoming[u] for u in self.fixed if u in self.incoming}
        active = set()
        for u in self.members:
            if u in self.fixed or u in self.stale_locks:
                continue
            if u in plan.not_coupled:
                self.not_reached.setdefault(u, plan.not_coupled[u])
            elif u in self.incoming:
                active.add(u)
                held[u] = self.incoming[u]
            else:
                self.mark_unusable(u)
        self.open_pool(self.item.cores)
        self.of = s.sweep_cap
        self.start(anchors=(), rings={}, seams=[], preview=preview_simultaneous(g, plan).text, terms=plan.terms)
        self.stage_name, self.sweeps, self.rounds = "plan", 0, 0
        self.progress("plan")

        cut: set = set()                                 # members the writer will no longer accept a condition on

        def terms_of(k, state):
            """Every term of ``k`` whose other members all have a held value (an unusable one conditions none)."""
            out = []
            for i in plan.terms_of.get(k, ()):
                t = plan.terms[i]
                others = [m for m in t.members if m != k]
                if all(m in state and m not in cut for m in others):
                    out.append(TermInput(t, tuple(state[m] for m in others)))
            return out

        def leave(new):
            for u in new:
                active.discard(u)

        cache: dict = {}                                 # unit -> {job key: Priced}, kept across rounds
        data, unary = {}, {}
        prev_total, outcome = None, "settled"
        while True:                                      # one round
            self.rounds += 1
            self.round = self.rounds
            sid = f"{self.batch}:round{self.rounds}"
            t_round = time.perf_counter()
            leave(self.take_new_locks(held))
            snaps = {}
            for k in sorted(active):
                terms = terms_of(k, held)
                if terms:
                    snaps[k] = self.snapshot(k, held[k], sid, terms)
                else:
                    active.discard(k)
                    self.not_reached.setdefault(k, CUT_OFF)
            if not snaps:                                # no round can settle: nothing is left to refit
                outcome = "nothing_left"
                break
            self.stage_name, self.index = "sweep", self.sweeps
            self.progress("assign")
            A, errors = self.assign(snaps, cache)        # one frozen state for the whole round
            for k, exc in errors.items():                # its search failed: it would fail every round, its held
                self.latest[k] = "failed"                # value stays and conditions its neighbours as a fixed one
                self.failed[k] = f"analysis error ({type(exc).__name__}: {exc})"
                active.discard(k)
            ids, refs = self.identity(A, snaps, dict(held), sid)
            A, rename = canonical_factors(A, ids, refs, {u: h for u, h in held.items() if u not in A})
            self.merge_aliases(rename)
            A = rebuilt_factors(self.core, A, snaps, rename)     # one definition per link, both guards in it
            held = {u: _renamed(h, rename) for u, h in held.items()}
            self.stage_timing("round", self.rounds, t_round, len(snaps), 0)
            if self.rounds > 1 and not any(a.changed for a in A.values()):
                break                                    # the previous round ended quiet on this very state
            stage1 = {k: {"round": self.rounds, "assignment": _plain(A[k]),
                          "identity": {str(m): {"id": ids.ids.get(ref), "status": ids.status.get(ref)}
                                       for m, ref in enumerate(refs[k])}} for k in A}
            quiet = 0
            while True:
                if self.sweeps >= s.sweep_cap:
                    outcome = "did_not_settle"
                    break
                t_sweep = time.perf_counter()
                leave(self.take_new_locks(held))         # E11: a new lock leaves the sweeps as a fixed neighbour
                before, before_cost = dict(held), (dict(data), dict(unary))
                n = self.sweeps + 1
                self.stage_name, self.index = "sweep", n
                self.progress("refine")
                move, width, snaps2, R_all = 0.0, 0, {}, {}
                for group in plan.groups:                # E8: Gauss-Seidel, each group sees the earlier groups
                    gs = {}
                    for k in group:
                        if k not in active or k not in A:
                            continue
                        terms = terms_of(k, held)
                        if terms:
                            gs[k] = self.snapshot(k, held[k], f"{self.batch}:sweep{n}", terms, stage_id=sid)
                        else:                    # its neighbours left during the round (a stale lock went)
                            active.discard(k)
                            self.not_reached.setdefault(k, CUT_OFF)
                    ks = list(gs)
                    if not ks:
                        continue
                    width = max(width, len(ks))
                    R = self.refits(gs, A)
                    for k in ks:
                        r = R[k]
                        snaps2[k], R_all[k] = gs[k], r
                        if not isinstance(r, Exception) and r.status in _ELIGIBLE and r.fit is not None:
                            old = {m.branch: m.omega0 for m in held[k].modes}
                            move = max([move, *(abs(e - old[b]) for b, e in r.energies.items() if b in old)])
                            held[k] = self.core.held_from_fit(r.fit, unit=k, fit=gs[k].fit_id,
                                                              inputs=self.inputs.get(k), context=self.ctx[k],
                                                              T=self.nodes[k].T,
                                                              dormant=self.note_dormant(ids, k, gs[k].fit_id, r.energies))
                            data[k], unary[k] = r.D, r.unary
                        elif isinstance(r, Exception) or r.status == "failed":
                            move = math.inf              # rules.settled: a failed block is never quiet
                if not snaps2:                           # every member left during the round: nothing to save
                    break
                self.sweeps = n
                entries = [self.entry(k, snaps2[k], R_all[k], self.identity_flags(ids, k), stage1.pop(k, None))
                           for k in snaps2]
                verdict = self.save_stage("sweep", n, entries, [])
                for k, snap in snaps2.items():
                    if self.judge(k, R_all[k], verdict, snap.fit_id):
                        self.records[snap.fit_id] = R_all[k].fit
                    elif k in verdict["refused"]:        # E9: every refused member goes back, and the sweep is not quiet
                        held[k] = before[k]
                        for now, was in zip((data, unary), before_cost):
                            if k in was:
                                now[k] = was[k]
                            else:
                                now.pop(k, None)
                        move = math.inf
                        if verdict["refused"][k] in (self.records_mod.REFUSED["locked"],
                                                     self.records_mod.REFUSED["picked"],
                                                     self.records_mod.REFUSED["inputs_changed"]):
                            active.discard(k)        # the writer refuses every later refit of it too
                        if verdict["refused"][k] == self.records_mod.REFUSED["picked"]:
                            self.hold_pick(k, held)
                cut |= self.refused_neighbours(verdict, snaps2)
                total = self.total(A, held, data, unary)     # the stopping cost of the state the writer accepted
                self.stage_timing("sweep", n, t_sweep, width, len(snaps2))
                self.progress("save")
                quiet =quiet + 1 if move <= s.sweep_tol_meV else 0
                improving = (prev_total is not None and total is not None
                             and (prev_total - total) > s.cost_rel_tol * max(1.0, abs(prev_total)))
                prev_total = total
                if quiet >= 2 and not improving:         # converged; reopen counts and assignments next round
                    break
            if outcome == "did_not_settle":
                break
        self.drift = self.drift_of(held)
        if outcome == "did_not_settle":
            self.end("did_not_settle", f"did not settle within {s.sweep_cap} sweeps; its results are kept")
        elif outcome == "nothing_left":
            self.end(outcome, NOTHING_LEFT)
        else:
            self.end(outcome)

    def hold_pick(self, k: str, held: dict) -> None:
        """A member refused as ``picked`` is fixed at the fit the user picked, read as a lock's fit is, so its
        neighbours condition on what is in use; a pick that cannot be read drops its terms instead."""
        pick = self.read_user(self.root).picks.get(k)
        try:
            if pick is None:
                raise ValueError("no pick")
            if pick.fit != "fast" and pick.fit not in self.records:
                line = self.line_of(pick.fit)
                from isar.session.inter_spectra import authority
                if line is None or authority.stale_of(line, self.now) is not None:
                    raise ValueError(f"the pick {pick.fit} is missing or stale")
            held[k] = self.held_of(k, pick.fit)
        except Exception as e:
            log.warning("inter-spectra: %s: the picked fit cannot condition its neighbours (%s)", k, e)
            held.pop(k, None)

    def refused_neighbours(self, verdict: dict, snaps: dict) -> set:
        """E9/E11: the neighbours a sweep's refusals name that no later sweep can condition on: one whose inputs
        changed, or one whose fit the user picked since the start (the writer refuses every other fit of it for
        the rest of the batch). A neighbour whose own refit went back, or a new lock (a fixed neighbour from the
        next sweep), stays."""
        plain = (self.records_mod.REFUSED["locked"], self.records_mod.REFUSED["picked"],
                 self.records_mod.REFUSED["inputs_changed"])
        fits = {s.fit_id for k, s in snaps.items() if k in verdict["refused"] and verdict["refused"][k] not in plain}
        if not fits:
            return set()
        locks = self.read_user(self.root).locks
        out = set()
        for line in self.records_mod.read_log(self.root).refits:
            a = line.accept
            if line.fit not in fits or a is None or a.detail is None:
                continue
            n = a.detail
            if a.reason == "neighbour_inputs_changed" or (
                    a.reason == "neighbour_fit_changed" and n not in verdict["refused"]
                    and not (n in locks and locks[n].locked)):
                out.add(n)
        return out

    def total(self, A: dict, held: dict, data: dict, unary: dict) -> float | None:
        """The one shared objective over the stage's factor set at the held energies (spec 4). A factor whose
        member no longer carries its branch (its refit went back) is left out of this diagnostic sum."""
        factors = [f for a in A.values() for f in a.factors]    # every copy: two definitions under one id raise
        energies, kept = {}, []
        for f in factors:
            fid = f.factor_id
            vals = {}
            for m in f.members:
                md = next((x for x in held[m].modes if x.branch == f.branch_id), None) if m in held else None
                if md is None:
                    break
                vals[(m, fid)] = md.omega0
            else:
                energies.update(vals)
                kept.append(f)
        try:
            total, _ = self.core.total_objective(kept, energies, {k: v for k, v in data.items() if v is not None},
                                                 unary)
        except ValueError:
            log.exception("batch %s: the shared objective could not be summed", self.batch)
            return None
        return float(total)

    def drift_of(self, held: dict) -> dict | None:
        """Cumulative T drift per (series, analyzer, branch) over the final held fits (an endpoint diagnostic)."""
        from isar.analyze.coupled import identity
        segs: dict = {}
        for u, h in held.items():
            n = self.nodes.get(u)
            if n is None or n.T is None or n.series is None:
                continue
            for m in h.modes:
                segs.setdefault((n.series, n.detector, m.branch), []).append(
                    {"unit": u, "T": n.T, "omega0": m.omega0, "guard": m.guard, "status": "matched"})
        segments = [{"analyzer": f"{series}|{det}", "branch": b, "observations": obs}
                    for (series, det, b), obs in sorted(segs.items()) if len(obs) > 1]
        return _plain(identity.drift_report(segments, self.settings)) if segments else None

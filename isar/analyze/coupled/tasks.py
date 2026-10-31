"""The task port (C-port): how one candidate's pricing sends its inner work to the runner's one pool.

``refit.price(snapshot, job, tasks=port)`` submits payloads ``(ctx, kind, args)``; a pool worker runs
``refit.run_task(*payload)`` to completion and never waits for another task, so only the job's own thread waits.
The production port (the runner's scheduler) lives with the runner; this module holds the protocol, ``Abandoned``,
the job context a task carries and the worker side's engine cache. Qt-free; imports the standard library only.
"""
from __future__ import annotations

import pickle
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from typing import Protocol

# One engine per candidate a worker may serve in turn (an algorithm setting, not a tuning knob).
ENGINE_CACHE = 8


class Abandoned(BaseException):
    """Raised by Port.wait in a job thread whose job the runner abandoned. BaseException on purpose: no
    `except Exception` in the core may swallow it; refit.price lets it propagate and the thread ends."""


class Port(Protocol):
    def submit(self, payload: tuple, prepare: Callable[[tuple], tuple] | None = None, rank: int = 0) -> Hashable: ...
    def wait(self, handle) -> object: ...
    def peek(self, handle) -> tuple[bool, object]: ...
    def drop(self, handles: Iterable) -> None: ...


def pack_context(snapshot, job, gradients: bool) -> bytes:
    """The job context every task of one price carries: one pickle, made once per price."""
    return pickle.dumps((snapshot, job, gradients), protocol=pickle.HIGHEST_PROTOCOL)


_local = threading.local()          # per thread, so a thread-backed test pool is safe


def worker_engine(ctx: bytes, build: Callable[..., object]):
    """The worker's engine for a job context, from this thread's LRU (keyed by the context bytes themselves), or
    ``build(snapshot, job, gradients)`` on a miss. The caller resets its ledger before each task."""
    cache = getattr(_local, "engines", None)
    if cache is None:
        cache = _local.engines = OrderedDict()
    if ctx in cache:
        cache.move_to_end(ctx)
        return cache[ctx]
    got = cache[ctx] = build(*pickle.loads(ctx))
    while len(cache) > ENGINE_CACHE:
        cache.popitem(last=False)
    return got

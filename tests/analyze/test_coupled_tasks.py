"""One candidate's price through the task port (plan S1U1, checks 1, 4 and 5) on synthetic spectra generated at test
time (no measured value).

``ThreadPort`` is a thread-backed test port (ThreadPoolExecutor(4)) honouring C-port: ``prepare`` runs when a task
starts, ``peek`` reads ``Future.done()``, rank 0 before rank 1, a dropped task is never waited for. P2's tests reuse it.
"""
import itertools
import pickle
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze.coupled import refit as R
from isar.analyze.coupled.tasks import Abandoned
from test_coupled_refit import _entry_snapshot, _job


class ThreadPort:
    """C-port over a ThreadPoolExecutor: each submit queues one task and one executor slot; a slot, when it starts,
    takes the queued task of lowest (rank, submission order), runs its ``prepare`` then ``refit.run_task``. Payloads
    and results cross a pickle, as they would to a pool process, so nothing is shared by reference."""

    def __init__(self, workers=4):
        self.pool = ThreadPoolExecutor(workers)
        self.lock = threading.Lock()
        self.ids = itertools.count()
        self.queued = {}                    # handle -> [rank, payload, prepare]
        self.futures = {}
        self.dropped = set()
        self.kinds = []                     # every submitted task's kind, in order

    def submit(self, payload, prepare=None, rank=0):
        h = next(self.ids)
        with self.lock:
            self.futures[h] = Future()
            self.queued[h] = [rank, payload, prepare]
            self.kinds.append(payload[1])
        self.pool.submit(self._slot)
        return h

    def _slot(self):
        with self.lock:
            if not self.queued:
                return
            h = min(self.queued, key=lambda x: (self.queued[x][0], x))
            _rank, payload, prepare = self.queued.pop(h)
        fut = self.futures[h]
        fut.set_running_or_notify_cancel()
        try:
            payload = pickle.loads(pickle.dumps(prepare(payload) if prepare else payload))
            fut.set_result(pickle.loads(pickle.dumps(R.run_task(*payload))))
        except BaseException as exc:        # the port hands run_task's exception to wait, whatever it is
            fut.set_exception(exc)

    def wait(self, handle):
        with self.lock:
            if handle in self.dropped:
                raise RuntimeError(f"waiting on dropped task {handle}")
            if handle in self.queued:
                self.queued[handle][0] = 0  # the job needs it now
        return self.futures[handle].result()

    def peek(self, handle):
        fut = self.futures[handle]
        if not fut.done():
            return False, None
        return True, fut.exception() or fut.result()

    def drop(self, handles):
        with self.lock:
            for h in handles:
                if h in self.futures and not self.futures[h].done():
                    self.dropped.add(h)
                    self.queued.pop(h, None)

    def close(self):
        self.pool.shutdown(wait=True)


@pytest.fixture
def port():
    p = ThreadPort(4)
    yield p
    p.close()


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


# 4 purity ----------------------------------------------------------------------------------------------------
def _engine(snap, slots):
    spec = R._Spec(snap)
    return R._Labelled(spec.ctx, R._ceiling(spec.fs, len(slots)), spec.slot_objects(slots), structures=spec.structures)


START = [(8.3, 0.5, 30.0, False), (14.6, 0.6, 50.0, False)]


def _outcomes(eng):
    """The three primitives in turn on one input each: a start's fit, the posterior from its optimum, the Laplace
    evidence at that posterior mode."""
    model = eng.model(eng.variant(2, True, True), ())
    theta, cost, nfev, message = eng._fit_core(model, START, None, "own")
    post = eng._posterior(model, theta)
    f, lo, hi = eng.objective(model, theta)
    z = np.clip(post.x, lo, hi)

    class B:                                    # the basin fields laplace reads
        pass
    b = B()
    b.model, b.theta = model, theta
    lap = eng.laplace(b, f, z, lo, hi)
    return ((theta.tobytes(), np.float64(cost).tobytes(), nfev, message),
            (post.x.tobytes(), post.nit, post.status, post.success, post.message),
            (np.float64(lap.ln_z).tobytes(), lap.status, lap.reason, None if lap.cov is None else lap.cov.tobytes()))


def test_a_primitive_is_a_function_of_its_inputs_alone_not_of_the_engine_s_warm_columns():
    # the stop condition of the plan: the unit-column LRU is the only warm state; a column must come out the same
    # computed alone, in a batch of another make-up, or from the cache
    snap = _entry_snapshot()
    fresh = _outcomes(_engine(snap, ("b1", None)))
    warm = _engine(snap, ("b1", None))
    model = warm.model(warm.variant(2, True, True), ())
    # other work first: fits and prices from nearby starts on another engine, whose columns this one reads...
    other = _engine(snap, ("b1", None))
    for e in (7.9, 8.1, 8.3):
        other.fit(other.variant(2, True, True), [(e, 0.5, 30.0, False), (14.6, 0.6, 50.0, False)], "own")
    other.price_all()
    warm.cache = other.cache
    # ... and the start point's columns one at a time, so the fit's first evaluation reads them from the cache
    # (computed in batches of one) where the fresh engine computes them as one batch of two
    t0 = warm._start(model, START, None)
    for (a, g, _c), p in zip(model.modes, model.variant.peaks):
        warm.units([(t0[a], t0[g])], [p])
    assert _outcomes(warm) == fresh


# 1 equal prices ----------------------------------------------------------------------------------------------
TWO = ("own", [(8.0, 0.4, 40.0), (15.0, 0.4, 60.0)])
CASES = {
    "matched": (("b1", None), TWO, ("neighbour", [(8.2, 0.5, None), (14.8, 0.5, None)])),
    # three modes on a two-mode spectrum: twins, delta alternatives and several basins of one model, so an
    # incomplete primitive key would answer one basin's step with another's
    "three on two": ((None, None, None), ("own", [(8.0, 0.4, 40.0), (12.0, 0.4, 5.0), (15.0, 0.4, 60.0)]),
                     ("step", [(4.0, 0.4, 20.0), (8.0, 0.4, 40.0), (15.0, 0.4, 60.0)])),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_price_through_the_port_equals_the_serial_price_in_every_field_but_its_time(port, case):
    # no hidden pair here: fits, the seed and every wave's basins go out as tasks, and the replay reproduces the
    # serial ledger exactly (exact float equality)
    snap = _entry_snapshot()
    slots, *starts = CASES[case]
    job = _job(snap, slots, *starts)
    serial = R.price(snap, job)
    tasked = R.price(snap, job, tasks=port)
    assert serial.ln_z is not None and len(serial.basins) >= 1
    assert replace(tasked, seconds=0.0) == replace(serial, seconds=0.0)
    assert {"fit", "seed", "basin"} <= set(port.kinds), port.kinds


def test_a_hidden_pair_job_prices_equal_too_while_its_split_integral_runs_in_the_replay(port):
    # packet P1: a split basin's integral runs in the job's thread as today, and its not-converged Laplace
    # fallback's primitives go to the pool one at a time; P2 sends its nodes as tasks and bounds the shift (D1)
    from inter_hidden_case import hidden_snapshot

    from isar.analyze.coupled.types import PricingJob, Start
    snap = hidden_snapshot(3.0, asimov=True)
    job = PricingJob("y", snap.snapshot_id, 2, ("b1", "b2"), "k",
                     (Start("neighbour", ((8.0, 0.4, None), (7.2, 0.4, None))),))
    serial = R.price(snap, job)
    assert any(b[1].endswith("|split") for b in serial.basins)
    assert replace(R.price(snap, job, tasks=port), seconds=0.0) == replace(serial, seconds=0.0)


# 5 Abandoned -------------------------------------------------------------------------------------------------
class AbandoningPort(ThreadPort):
    def __init__(self, at):
        super().__init__(4)
        self.at, self.waits, self.released = at, 0, set()

    def drop(self, handles):
        handles = list(handles)
        self.released.update(handles)
        super().drop(handles)

    def wait(self, handle):
        self.waits += 1
        if self.waits == self.at:
            raise Abandoned()
        return super().wait(handle)


@pytest.mark.parametrize("at", [1, 3])
def test_abandoned_propagates_out_of_price_and_drops_the_job_s_tasks(at):
    # a speculative task's failure is ignored, never an abandoned job: Abandoned is a BaseException no except catches
    snap = _entry_snapshot()
    job = _job(snap, ("b1", None), ("own", [(8.0, 0.4, 40.0), (15.0, 0.4, 60.0)]))
    p = AbandoningPort(at)
    try:
        with pytest.raises(Abandoned):
            R.price(snap, job, tasks=p)
        assert p.released == set(range(len(p.kinds)))   # every handle of the job dropped on the way out
    finally:
        p.close()


class FailingBasinPort(ThreadPort):
    def submit(self, payload, prepare=None, rank=0):
        if payload[1] == "basin":                     # the task fails in its worker; the replay computes instead
            payload = (payload[0], "no such kind", ())
        return super().submit(payload, prepare, rank)


def test_a_failed_speculative_task_is_logged_and_the_replay_prices_without_it(caplog):
    snap = _entry_snapshot()
    slots, *starts = CASES["matched"]
    job = _job(snap, slots, *starts)
    p = FailingBasinPort(4)
    try:
        tasked = R.price(snap, job, tasks=p)
    finally:
        p.close()
    assert replace(tasked, seconds=0.0) == replace(R.price(snap, job), seconds=0.0)
    assert "a speculative task failed" in caplog.text and "prim" in p.kinds

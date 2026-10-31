"""One candidate's price through the task port (plan S1U1, checks 1 to 5) on synthetic spectra generated at test
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

from isar.analyze.coupled import engine as cc
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
            fut.set_result(pickle.loads(pickle.dumps(self.execute(payload))))
        except BaseException as exc:        # the port hands run_task's exception to wait, whatever it is
            fut.set_exception(exc)

    def execute(self, payload):
        return R.run_task(*payload)

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


# 2 and 3 split nodes as tasks (D1) -----------------------------------------------------------------------------
def _hidden():
    from inter_hidden_case import hidden_snapshot

    from isar.analyze.coupled.types import PricingJob, Start
    snap = hidden_snapshot(3.0, asimov=True)
    return snap, PricingJob("y", snap.snapshot_id, 2, ("b1", "b2"), "k",
                            (Start("neighbour", ((8.0, 0.4, None), (7.2, 0.4, None))),))


def _best_label(p):
    return max(p.basins, key=lambda b: b[0])[1]


def test_a_hidden_pair_job_through_the_port_keeps_its_decision_and_its_ln_z_within_the_agreement_tolerance(port):
    # D1: every node starts from the nearest node solved when it is sent, so the split basin's ln Z may shift; the
    # bound is the integral's own agreement tolerance, and the decision (status, count, labelling, best model) holds
    snap, job = _hidden()
    serial = R.price(snap, job)
    tasked = R.price(snap, job, tasks=port)
    assert (tasked.status, tasked.count, tasked.slots) == (serial.status, serial.count, serial.slots)
    assert _best_label(tasked) == _best_label(serial) and _best_label(serial).endswith("|split")
    assert abs(tasked.ln_z - serial.ln_z) <= cc.SPLIT_TOL_NATS
    assert any(b[1].endswith("|split") for b in tasked.basins)
    assert "node" in port.kinds and "split_serial" not in port.kinds


class FailingNodePort(ThreadPort):
    """Fails each integral's second node in chain order (never the first read, which has no solved neighbour): the
    tasks sent with ``prepare`` (the parallel ones), or with ``every`` also the retries. A forced failure reports
    the joint start, so the retry rule always sends one from the neighbour."""

    def __init__(self, every):
        super().__init__(4)
        self.every, self.retries = every, []
        self.target, self.seen = {}, {}     # integral -> its failing f; integral -> nodes sent so far

    def submit(self, payload, prepare=None, rank=0):
        ctx, kind, args = payload
        if kind == "node":
            key = (args[0], args[1], args[2].tobytes(), args[3])
            n = self.seen[key] = self.seen.get(key, 0) + 1
            if n == 2:
                self.target.setdefault(key, args[4])
            if prepare is None:
                self.retries.append((key, args[4]))
            if args[4] == self.target.get(key) and (prepare is not None or self.every):
                payload = (ctx, "forced", args)
        return super().submit(payload, prepare, rank)

    def execute(self, payload):
        if payload[1] == "forced":
            return ("fail", "forced"), None
        return super().execute(payload)


def _count_integrals(monkeypatch):
    """The integrals the job's own thread reads (a worker's serial integral runs on another thread)."""
    read, real, me = [], cc.integrate_split, threading.current_thread()

    def counting(*a, **kw):
        if threading.current_thread() is me:
            read.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(cc, "integrate_split", counting)
    return read


def test_a_node_failing_from_its_parallel_start_is_retried_once_from_its_serial_neighbour(monkeypatch):
    snap, job = _hidden()
    serial = R.price(snap, job)
    read = _count_integrals(monkeypatch)
    p = FailingNodePort(every=False)
    try:
        tasked = R.price(snap, job, tasks=p)
    finally:
        p.close()
    # exactly one retry per integral read, each for that integral's failing f (speculation may send more integrals)
    assert read and len(p.retries) == len({k for k, _f in p.retries}) == len(read)
    assert all(p.target[k] == f for k, f in p.retries)
    assert "split_serial" not in p.kinds
    assert _best_label(tasked) == _best_label(serial) and abs(tasked.ln_z - serial.ln_z) <= cc.SPLIT_TOL_NATS


def test_a_node_failing_after_its_retry_hands_the_integral_to_one_serial_task_whose_result_is_final(monkeypatch):
    # every integral's second node fails in parallel and on its retry, so each integral is recomputed by today's serial path in a
    # worker: the whole price then equals the serial one exactly
    snap, job = _hidden()
    serial = R.price(snap, job)
    read = _count_integrals(monkeypatch)
    p = FailingNodePort(every=True)
    try:
        tasked = R.price(snap, job, tasks=p)
    finally:
        p.close()
    assert read and p.kinds.count("split_serial") == len(read)
    assert replace(tasked, seconds=0.0) == replace(serial, seconds=0.0)


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

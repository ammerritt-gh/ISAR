"""The members event of the batch runner (C-members-event, S1U2P1): the full map of member states per event,
its throttle, the pool's in-flight bound, and the proof that none of it changes a fit, count, save or progress dict.

Runs ``run_batch`` in process on the fake core of ``_inter_fakes`` and the helpers of the runner's own tests. Nothing
reads ``real_data/``; every number is made up.
"""
from __future__ import annotations

import operator
import os
import sys
import threading
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))       # the fake core is imported by name, here and in spawned pool workers
import _inter_fakes as fakes  # noqa: E402
from test_inter_spectra_runner import (  # noqa: E402
    Bench, _anchored, _calibrate, _chain, _keys, _lines, _set_fast, _simultaneous, _when, _workspace)
from isar.intake import inter_values  # noqa: E402
from isar.session.inter_spectra import runner  # noqa: E402

_STATES = {"waiting", "fitting", "done", "refused", "failed", "not_reached", "held"}


def _check_events(events, spec, workers=1):
    """Shape rules of every event stream."""
    assert events
    for a, b in zip(events, events[1:]):
        assert a != b, "consecutive duplicate events"
    for ev in events:
        assert tuple(ev["states"]) == tuple(sorted(spec.members))
        assert set(ev["states"].values()) <= _STATES
        assert set(ev["reasons"]) <= set(ev["states"])
        assert list(ev["counts"]) == ["done", "failed", "not_reached", "fixed", "total"]
        assert sum(s == "fitting" for s in ev["states"].values()) <= workers


def _agrees_with_the_books(events, bench):
    """The last map agrees with the end payload and the last progress dict."""
    last, end = events[-1], bench.end().payload["end"]
    n = {w: sum(s == w for s in last["states"].values()) for w in _STATES}
    assert n["waiting"] == n["fitting"] == 0
    assert n["done"] == end["done"]
    assert n["failed"] + n["refused"] == end["failed"]
    assert n["not_reached"] == len(end["not_reached"])
    assert n["held"] == bench.progress[-1]["fixed"]
    assert last["counts"] == {k: bench.progress[-1][k] for k in last["counts"]}
    assert last["stage"] == bench.progress[-1]["stage"] and bench.progress[-1]["phase"] == "end"


def _history(events, key):
    out = []
    for ev in events:
        if not out or out[-1] != ev["states"][key]:
            out.append(ev["states"][key])
    return out


# ---------------------------------------------------------------------------
# worked example 1: anchored, direction T, two rings (one analyzer at five temperatures, 200 K the anchor)
# ---------------------------------------------------------------------------
def test_worked_example_1_anchored_two_rings(tmp_path):
    ws = _workspace(tmp_path, (100, 150, 200, 250, 300), 1)
    k = _keys(ws)
    t100, t150, t200, t250, t300 = (k["a03", t] for t in (100.0, 150.0, 200.0, 250.0, 300.0))
    _set_fast(ws, {u: (5.0, {"fail": u == t300}) for u in k.values()})
    spec = _anchored(ws, [t200])

    def refuse():                       # 150's inputs change during ring 1: the writer keeps its refit out of use
        scan, det = t150.rsplit(":", 1)
        _calibrate(ws, scan, det, 0.5)
    events = []
    bench = Bench(ws, _when("ring", 1, "refine", refuse))
    bench.run(spec, members=events.append)
    _check_events(events, spec)

    first = events[0]                   # after the start is saved: stage plan, index 0, nothing attempted
    assert (first["stage"], first["index"], first["of"], first["round"], first["kind"]) == ("plan", 0, 2, None, "anchored")
    assert first["states"] == {t100: "waiting", t150: "waiting", t200: "held", t250: "waiting", t300: "waiting"}
    assert first["counts"] == {"done": 0, "failed": 0, "not_reached": 4, "fixed": 1, "total": 5}
    assert first["reasons"] == {}
    assert (events[1]["stage"], events[1]["index"]) == ("ring", 1)            # progress("assign"): a new stage is sent

    assert _history(events, t200) == ["held"]
    assert _history(events, t150) == ["waiting", "fitting", "waiting", "refused"]
    assert _history(events, t250) == ["waiting", "fitting", "waiting", "done"]
    assert _history(events, t100) == ["waiting", "not_reached"]
    assert _history(events, t300) == ["waiting", "fitting", "waiting", "failed"]
    # the verdict lands with the ring's save: 250 done, 150 refused, 1/1/2/1/5 before ring 2 starts
    verdict = next(e for e in events if e["states"][t150] == "refused" and e["stage"] == "ring" and e["index"] == 1)
    assert verdict["states"][t250] == "done" and verdict["counts"] == {"done": 1, "failed": 1, "not_reached": 2,
                                                                          "fixed": 1, "total": 5}
    last = events[-1]
    assert last["counts"] == {"done": 1, "failed": 2, "not_reached": 1, "fixed": 1, "total": 5}
    assert last["reasons"][t100] == runner.CUT_OFF
    assert last["reasons"][t150].startswith("fitted but not saved (") and runner.INPUTS_REMEDY in last["reasons"][t150]
    assert last["reasons"][t300] == "the fake was told to fail"
    assert set(last["reasons"]) == {t100, t150, t300}
    end = bench.end().payload["end"]
    assert (end["done"], end["failed"], [d["unit"] for d in end["not_reached"]]) == (1, 2, [t100])
    assert bench.end().status == "done_warning"
    _agrees_with_the_books(events, bench)


# ---------------------------------------------------------------------------
# worked example 2: simultaneous, a refusal; final map agrees with the books
# ---------------------------------------------------------------------------
def test_simultaneous_batch_with_a_refusal_and_a_lock(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    assert inter_values.save_settings(ws, {"sweep_cap": 6}).ok
    spec = _simultaneous(ws, (x, y, z))
    events = []
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.pick_fit(ws, y, "fast")))
    bench.run(spec, members=events.append)
    _check_events(events, spec)
    assert events[0]["stage"] == "plan" and events[0]["kind"] == "simultaneous"
    sweep1 = [e for e in events if e["stage"] == "sweep" and e["index"] == 1]
    assert any(e["states"][y] == "fitting" for e in sweep1)                   # y is the first group, alone
    assert any(e["states"][x] == "refused" and e["reasons"][x].startswith("fitted but not saved") for e in events)
    last = events[-1]
    assert last["states"][y] == "refused" and last["states"][x] == last["states"][z] == "done"
    _agrees_with_the_books(events, bench)


def test_a_stale_lock_is_held_with_its_reason(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    first = _simultaneous(ws, (x, y, z))
    Bench(ws).run(first)
    (y_line,) = [r for r, _ in _lines(ws, first.batch_id) if r.unit == y and r.accept.decision == "authoritative"][-1:]
    scan, det = z.rsplit(":", 1)
    _calibrate(ws, scan, det, 0.5)
    assert inter_values.set_lock(ws, y, locked=True, fit_id=y_line.fit).ok
    spec = _simultaneous(ws, (x, y))
    events = []
    Bench(ws).run(spec, members=events.append)
    assert all(e["states"][y] == "held" and e["reasons"][y] == runner.STALE_LOCK for e in events)


# ---------------------------------------------------------------------------
# the throttle and the pool bound
# ---------------------------------------------------------------------------
class _SlowPool:
    """Stands in for ProcessPoolExecutor: the job runs at submit, its future completes ``DELAY`` seconds later,
    and the number of unfinished futures at each submit is recorded."""
    DELAY = 0.2
    made: list = []

    def __init__(self, max_workers):
        self.max_workers, self.unfinished = max_workers, []
        self._open = []
        _SlowPool.made.append(self)

    def submit(self, fn, *args):
        f = Future()
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001
            threading.Timer(self.DELAY, f.set_exception, [exc]).start()
        else:
            threading.Timer(self.DELAY, f.set_result, [result]).start()
        self._open.append(f)
        self.unfinished.append(sum(not g.done() for g in self._open))
        return f

    def shutdown(self, *a, **k):
        pass


def test_at_most_workers_jobs_are_in_flight_and_fitting(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _SlowPool)
    monkeypatch.setattr(runner, "MEMBERS_GAP_S", 0.05)
    monkeypatch.setenv("ISAR_WORKERS", "3")                       # the conftest caps it at 2
    _SlowPool.made = []
    ws = _workspace(tmp_path, (100, 150, 200), 4)
    k = _keys(ws)
    spec = _anchored(ws, [k[d, 150.0] for d in ("a03", "a04", "a05", "a06")])      # ring 1: eight refits
    events = []
    bench = Bench(ws)
    bench.run(spec, cores=3, members=events.append)
    (pool,) = _SlowPool.made
    assert pool.max_workers == 3 and max(pool.unfinished) == 3 and len(pool.unfinished) == 8
    _check_events(events, spec, workers=3)
    assert max(sum(s == "fitting" for s in e["states"].values()) for e in events) == 3
    _agrees_with_the_books(events, bench)


def test_throttle_sends_pool_transitions_at_most_every_gap_but_phases_always(tmp_path, monkeypatch):
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    run = runner._Run.__new__(runner._Run)
    run.members, run.fixed, run.stale_locks = (x, y, z), {}, set()
    run.latest, run.failed, run.not_reached, run.fitting, run.refused = {}, {}, {}, {}, set()
    run.batch, run.kind, run.stage_name, run.index, run.of, run.round, run.final = "b", "simultaneous", "sweep", 1, 20, 1, False
    run._members_last, run._members_at = None, float("-inf")
    sent = []
    run._members_cb = sent.append
    clock = [100.0]
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])
    run.publish()                                    # first: sent
    run.fly(x, True)
    clock[0] += 0.1
    run.publish()                                    # inside the gap: held back
    assert len(sent) == 1
    run.publish(True)                                # forced: sent
    assert len(sent) == 2 and sent[1]["states"][x] == "fitting"
    run.fly(x, False)
    clock[0] += 0.3
    run.publish()                                    # past the gap: sent
    assert len(sent) == 3 and sent[2]["states"][x] == "waiting"
    run.publish(True)                                # equal to the last: not sent
    assert len(sent) == 3


# ---------------------------------------------------------------------------
# the identity proof: the callback and the in-flight cap change nothing
# ---------------------------------------------------------------------------
def _fingerprint(ws, bench, spec):
    """What a batch saved and reported, without the random ids and the timing."""
    lines = _lines(ws, spec.batch_id)
    fit_of = {r.fit: r.unit for r, _ in lines}

    def who(fit):
        return fit if fit == "fast" else f"refit-of-{fit_of.get(fit)}"
    refits = [(r.unit, r.stage, r.index, r.status, r.reason, who(r.seed),
               tuple(sorted((c.unit, who(c.fit)) for c in r.conditioned_on)),
               (r.accept.decision, r.accept.reason) if r.accept else None,
               sorted(round(m["omega0"], 9) for m in (p.get("came_out") or {}).get("modes", [])) if isinstance(p, dict) else None)
              for r, p in sorted(lines, key=lambda lp: (lp[0].index, lp[0].unit))]
    end = dict(bench.end().payload["end"])
    end.pop("timing")
    results = [(r.status, r.reason, r.cores) for r in bench.results if r.payload is None or r.payload["event"] != "end"]
    return refits, end, bench.end().status, results, bench.progress


class _DyingPool:
    """Stands in for ProcessPoolExecutor: its first ``ALIVE`` submits run the job at once; then a worker has died, so
    every later submit raises BrokenProcessPool, as the real pool does."""
    ALIVE = 3

    def __init__(self, max_workers):
        self.sent = 0

    def submit(self, fn, *args):
        if self.sent >= self.ALIVE:
            raise BrokenProcessPool("a worker died")
        self.sent += 1
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, *a, **k):
        pass


def test_a_pool_that_breaks_mid_stage_fails_the_rest_of_that_stage_and_the_next_stage_raises(monkeypatch):
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _DyingPool)
    pool = runner._Pool(operator, 2)                 # operator.neg stands in for a core call
    out = pool.map("neg", [(i, (i,), {}) for i in range(6)])
    assert [out[i] for i in range(3)] == [0, -1, -2]              # the jobs sent before the break were collected
    assert all(isinstance(out[i], BrokenProcessPool) for i in range(3, 6))
    with pytest.raises(BrokenProcessPool):                        # the next stage's first submit, as before
        pool.map("neg", [(i, (i,), {}) for i in range(2)])


class _DeadWorkers(_DyingPool):
    """Every job it takes comes back as a dead worker's error; later submits raise, as a broken pool's do."""
    def submit(self, fn, *args):
        if self.sent >= self.ALIVE:
            raise BrokenProcessPool("a worker died")
        self.sent += 1
        f = Future()
        f.set_exception(BrokenProcessPool("a worker died"))
        return f


def test_a_stage_whose_jobs_all_came_back_dead_still_raises_at_the_next_stage(monkeypatch):
    # the next stage's first submit fails before any of its jobs is sent: it must raise, not save the stage as failed
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _DeadWorkers)
    pool = runner._Pool(operator, 2)
    out = pool.map("neg", [(i, (i,), {}) for i in range(3)])
    assert all(isinstance(out[i], BrokenProcessPool) for i in range(3))
    with pytest.raises(BrokenProcessPool):
        pool.map("neg", [(i, (i,), {}) for i in range(2)])


def test_a_map_ends_with_a_forced_publish_and_nothing_in_flight(monkeypatch):
    # a throttled transition must not be the last word: the job that returned has to reach the members map
    monkeypatch.setattr(_DyingPool, "ALIVE", 99)
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _DyingPool)
    for workers, calls in ((1, [(0, (1,), {})]), (2, [(i, (i,), {}) for i in range(3)])):
        flying, seen = set(), []
        pool = runner._Pool(operator, workers, watch=lambda k, on: flying.add(k) if on else flying.discard(k),
                            tick=lambda force: seen.append((force, frozenset(flying))))
        pool.map("neg", calls)
        force, in_flight = seen[-1]
        assert force and not in_flight, (workers, seen)


@pytest.mark.parametrize("kind", ["anchored", "simultaneous"])
def test_the_callback_and_the_pool_change_no_fit_count_save_or_progress(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")])))
    prints = {}
    for i, (cores, record) in enumerate(((1, False), (1, True), (2, False), (2, True))):
        root = tmp_path / f"w{i}"
        root.mkdir()
        if kind == "anchored":
            ws = _workspace(root, (100, 150, 200), 3)
            k = _keys(ws)
            _set_fast(ws, {u: (5.0 + 0.1 * j, {"fail": u == k["a04", 200.0]}) for j, u in enumerate(k.values())})
            spec = _anchored(ws, [k[d, 100.0] for d in ("a03", "a04", "a05")])
        else:
            ws, x, y, z = _chain(root, ({"toy": "average"}, {"toy": "keep"}, {"toy": "average"}))
            spec = _simultaneous(ws, (x, y, z))
        events = []
        bench = Bench(ws)
        bench.run(spec, cores=cores, members=events.append if record else None)
        prints[(cores, record)] = _fingerprint(ws, bench, spec)
        if record:
            _check_events(events, spec, workers=cores)
            _agrees_with_the_books(events, bench)
        assert not any("members" in p for p in bench.progress)
    base = prints[(1, False)]
    assert base[0] and base[4]
    for key, got in prints.items():
        assert got == base, key

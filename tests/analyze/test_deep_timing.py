"""A stopped deep pass retains the completed attempts, including restarts, in its log."""
import json
import logging
from dataclasses import replace

import pytest

from isar.analyze import deep_pass as dp


def _record(label, settings, seed, **kw):
    return dp.RunRecord(label, seed, settings, 2, -2.0, 0.1, 1.0, 17, 5, -1.0,
                        2.5, None, evidence_converged=True, **kw)


def _timings(caplog):
    return [json.loads(r.getMessage().split("deep_timing ", 1)[1])
            for r in caplog.records if r.getMessage().startswith("deep_timing ")]


def test_completed_attempt_timings_survive_a_stop_during_the_next_model(monkeypatch, caplog):
    """A restart and its clean retry are visible before a later model is interrupted."""
    caplog.set_level(logging.INFO)
    calls = []

    def run(label, problem, *, settings, seed, reference_lnl):
        calls.append(label)
        if label == "stopped":
            raise KeyboardInterrupt
        return _record(label, settings, seed,
                       suspect=("missed optimum",) if len(calls) == 1 else ())

    monkeypatch.setattr(dp, "run_nested", run)
    entries = [dp.ModelEntry("finished", 1, 2, 0.0, None, None),
               dp.ModelEntry("stopped", 2, 2, -1.0, None, None)]
    with pytest.raises(KeyboardInterrupt):
        dp.deep_pass(entries, seed=42)
    rows = [r for r in _timings(caplog) if r["event"] == "run_completed"]
    assert len(rows) == 2
    assert [r["seed"] for r in rows] == [42, 42 + 7919]
    assert [r["restart"] for r in rows] == [0, 1]
    assert [r["status"] for r in rows] == ["suspect", "usable"]
    assert [r["settings"]["step_factor"] for r in rows] == [4, 8]
    assert all(r["label"] == "finished" and r["stage"] == "contenders" for r in rows)
    assert all(r["wall_s"] == 2.5 and r["n_like_calls"] == 17 and r["workers"] == 1 for r in rows)


def test_worker_threadpools_are_logged_once_and_failures_keep_their_reason(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(dp, "_timing_worker_pid", None, raising=False)
    monkeypatch.setattr(dp, "MAX_RESTARTS", 0)

    def run(label, problem, *, settings, seed, reference_lnl):
        return replace(_record(label, settings, seed), failure="synthetic sampler failure")

    monkeypatch.setattr(dp, "run_nested", run)
    entries = [dp.ModelEntry(label, i, 2, -float(i), None, None)
               for i, label in enumerate(("one", "two"))]
    result = dp.deep_pass(entries, chunk=2)
    inventory = [r for r in _timings(caplog) if r["event"] == "worker"]
    assert len(inventory) == 1
    assert inventory[0]["pid"] > 0 and inventory[0]["workers"] == 2
    assert "threadpools" in inventory[0]
    runs = [r for r in _timings(caplog) if r["event"] == "run_completed"]
    assert len(runs) == 2 and all(r["status"] == "failed" for r in runs)
    assert all(r["failure"] == "synthetic sampler failure" for r in runs)
    assert not result.complete

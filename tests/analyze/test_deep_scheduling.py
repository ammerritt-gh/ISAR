"""Deep scheduling uses completed work to refill cores, without changing model seeds."""
from concurrent.futures import Future
from dataclasses import replace

import pytest

from isar.analyze import deep_pass as dp


class Clock:
    def __init__(self):
        self.now = 0.0

    def perf_counter(self):
        return self.now


class ControlledPool:
    """A virtual pool: wait advances to the next completion, with no threads or sleeping."""
    def __init__(self, monkeypatch, durations, values):
        self.clock, self.durations, self.values = Clock(), durations, values
        self.pending, self.started, self.finished = {}, [], []
        self.max_pending = 0
        monkeypatch.setattr(dp, "time", self.clock)
        monkeypatch.setattr(dp, "wait", self.wait, raising=False)

    def submit(self, fn, job, *, stage, workers):
        label, _entry, settings, seed = job
        future = Future()
        delay = self.durations.get((label, stage), self.durations.get(label, 1.0))
        self.pending[future] = (self.clock.now + delay, job, stage)
        self.started.append((self.clock.now, label, stage, settings, seed))
        self.max_pending = max(self.max_pending, len(self.pending))
        return future

    def wait(self, futures, *, return_when):
        moment = min(self.pending[f][0] for f in futures)
        self.clock.now = moment
        done = {f for f in futures if self.pending[f][0] == moment}
        for future in done:
            _, job, stage = self.pending.pop(future)
            label, _entry, settings, seed = job
            logz = self.values.get((label, stage), self.values.get(label, -100.0))
            run = dp.RunRecord(label, seed, settings, 2, logz, 0.1, 1.0, 17, 5, 0.0,
                               1.0, None, evidence_converged=True)
            self.finished.append((moment, label, stage))
            future.set_result((label, [run], None))
        return done, set(futures) - done


def entry(label, count, fast, ndim=2):
    return dp.ModelEntry(label, count, ndim, fast, None, None)


def test_stage_width_uses_all_distinct_models_in_the_combined_stage():
    record = {"ln_like_saturated": 0.0,
              "models": [{"label": name, "ln_z": value, "ln_model_prior": 0.0}
                         for name, value in (("a", 0.0), ("b", -1.0), ("c", -30.0), ("c", -31.0),
                                             ("d", None), ("e", -40.0))]}
    assert dp.stage_width(record) == 5


def test_pool_refills_across_the_old_barrier_and_reports_actual_completion(monkeypatch):
    entries = [entry("long", 1, 0.0, 8), entry("short", 2, -1.0, 4),
               entry("explore", 3, -50.0, 10), entry("tail", 4, -60.0, 3)]
    pool = ControlledPool(monkeypatch, {"long": 100, "short": 10, "explore": 20, "tail": 5},
                          {"long": 0.0, "short": -1.0, "explore": -100.0, "tail": -100.0})
    progress = []
    result = dp.deep_pass(entries, executor=pool, workers=2,
                          progress=lambda event: progress.append((pool.clock.now, event)))
    assert [(t, label) for t, label, *_ in pool.started] == [(0, "long"), (0, "short"),
                                                           (10, "explore"), (30, "tail")]
    assert pool.max_pending == 2 and pool.clock.now == 100
    assert [m.label for m in result.models] == [e.label for e in entries]
    assert [(t, e["step"], e["of"]) for t, e in progress] == [
        (0, 0, 4), (10, 1, 4), (30, 2, 4), (35, 3, 4), (100, 4, 4)]
    assert all(e["stage"] == 1 and e["stages"] == 2 for _, e in progress)
    assert result.stage_seconds == {"initial": 100.0, "promoted": 0.0}


def test_categories_then_dimensions_order_work_but_keep_original_label_seeds(monkeypatch):
    entries = [entry("c_small", 1, 0.0, 2), entry("c_large", 2, -1.0, 10),
               entry("e_small", 3, -50.0, 3), entry("e_large", 4, -60.0, 9),
               entry("d_small", 5, None, 4), entry("d_large", 6, None, 8)]
    pool = ControlledPool(monkeypatch, {}, {"c_small": 0.0, "c_large": -1.0,
                                          "d_small": -100.0, "d_large": -100.0})
    result = dp.deep_pass(entries, executor=pool, workers=1, seed=7)
    assert [row[1] for row in pool.started] == ["c_large", "c_small", "d_large", "d_small", "e_large", "e_small"]
    order = {e.label: i for i, e in enumerate(entries)}
    assert all(seed == 7 + 104729 * order[label] + (stage == "direct_reference")
               for _, label, stage, _settings, seed in pool.started)
    assert all(settings == (dp.EXPLORATORY if stage == "exploratory" else dp.ACCEPTED)
               for _, _label, stage, settings, _seed in pool.started)
    assert [m.label for m in result.models] == [e.label for e in entries]


def test_budget_checks_each_submission_and_finishes_every_admitted_job(monkeypatch):
    entries = [entry(f"c{i}", i, -0.1 * i, 8 - i) for i in range(4)]
    pool = ControlledPool(monkeypatch, {"c0": 80, "c1": 40, "c2": 200},
                          {f"c{i}": -0.1 * i for i in range(4)})
    result = dp.deep_pass(entries, executor=pool, workers=2, budget_s=50)
    assert [(t, label) for t, label, *_ in pool.started] == [(0, "c0"), (0, "c1"), (40, "c2")]
    assert pool.clock.now == 240 and not pool.pending
    assert [m.status for m in result.models] == ["reference", "reference", "reference", "not_run"]
    assert result.search_limited == "budget ended before c3"


def test_budget_prioritizes_fast_weight_over_dimensions_in_the_initial_queue(monkeypatch):
    entries = [entry("c_small", 1, 0.0, 2), entry("c_large", 2, -1.0, 10),
               entry("e_small", 3, -6.0, 3), entry("e_large", 4, -7.0, 9)]
    pool = ControlledPool(monkeypatch, {}, {"c_small": 0.0, "c_large": -1.0})
    result = dp.deep_pass(entries, executor=pool, workers=1, budget_s=3)
    assert [row[1] for row in pool.started] == ["c_small", "c_large", "e_small"]
    assert result.models[-1].status == "not_run"


def test_budget_promotes_by_current_evidence_plus_prior_over_dimensions(monkeypatch):
    entries = [entry("lead", 1, 0.0), entry("second", 2, -1.0),
               entry("relevant_small", 3, -6.0, 3),
               replace(entry("less_relevant_large", 4, -7.0, 9), log_model_prior=-10.0)]
    pool = ControlledPool(monkeypatch, {}, {"lead": 0.0, "second": -1.0,
                                          "relevant_small": -2.0, "less_relevant_large": 0.0})
    dp.deep_pass(entries, executor=pool, workers=1, budget_s=5)
    assert [label for _, label, stage, *_ in pool.started if stage == "promoted"] == ["relevant_small"]


def test_promotions_wait_for_initial_completion_then_refill(monkeypatch):
    entries = [entry("lead", 1, 0.0), entry("second", 2, -1.0),
               entry("a", 3, -6.0, 9), entry("b", 4, -7.0, 7), entry("c", 5, -8.0, 5)]
    durations = {("a", "promoted"): 100, ("b", "promoted"): 10, ("c", "promoted"): 5}
    values = {e.label: float(e.fastpass_ln_z) for e in entries}
    pool = ControlledPool(monkeypatch, durations, values)
    dp.deep_pass(entries, executor=pool, workers=2)
    initial_end = max(t for t, _, stage in pool.finished if stage != "promoted")
    promoted = [(t, label) for t, label, stage, *_ in pool.started if stage == "promoted"]
    assert promoted == [(initial_end, "a"), (initial_end, "b"), (initial_end + 10, "c")]


def test_direct_accepted_matches_old_promoted_restarts_and_uses_exploratory_only_on_failure(monkeypatch):
    calls = []
    always_fail = [False]

    def run(label, problem, *, settings, seed, reference_lnl):
        calls.append((label, settings, seed))
        suspect = ("synthetic missed mode",) if label == "direct" and settings.n_live >= dp.ACCEPTED.n_live and (
            always_fail[0] or settings.step_factor == dp.ACCEPTED.step_factor) else ()
        return dp.RunRecord(label, seed, settings, 2, -2.0, 0.1, 1.0, 17, 5, 0.0,
                            1.0, None, suspect=suspect, evidence_converged=True)

    monkeypatch.setattr(dp, "run_nested", run)
    es = [entry("lead", 1, 0.0), entry("second", 2, -1.0), entry("direct", 3, None)]
    promoted_seed = 7 + 2 * 104729 + 1
    old_accepted = dp.run_until_clean("direct", None, settings=dp.ACCEPTED, seed=promoted_seed, reference_lnl=None)
    calls.clear()
    result = dp.deep_pass(es, seed=7)
    direct = result.models[-1]
    assert direct.runs == old_accepted and direct.status == "reference"
    assert not any(label == "direct" and settings == dp.EXPLORATORY for label, settings, _ in calls)

    always_fail[0] = True
    old_exploratory = dp.run_until_clean("direct", None, settings=dp.EXPLORATORY,
                                       seed=promoted_seed - 1, reference_lnl=None)
    old_accepted = dp.run_until_clean("direct", None, settings=dp.ACCEPTED,
                                    seed=promoted_seed, reference_lnl=None)
    calls.clear()
    result = dp.deep_pass(es, seed=7)
    direct = result.models[-1]
    assert direct.runs == old_accepted + old_exploratory
    assert direct.status == "exploratory" and direct.log_z == old_exploratory[-1].log_z
    assert len([1 for label, settings, _ in calls if label == "direct" and settings == dp.ACCEPTED]) == 1
    assert not result.complete and "direct" in result.search_limited


def test_missing_price_contenders_keep_their_original_accepted_seed(monkeypatch):
    es = [entry("first", 0, None), entry("second", 1, None), entry("direct", 2, None)]
    pool = ControlledPool(monkeypatch, {}, {"first": 0.0, "second": -1.0, "direct": -2.0})
    dp.deep_pass(es, seed=5, executor=pool, workers=2)
    assert [(label, seed) for _, label, _, _, seed in pool.started] == [
        ("first", 5), ("second", 5 + 104729), ("direct", 5 + 2 * 104729 + 1)]


@pytest.mark.parametrize("budget", [None, 2])
def test_failed_direct_work_refills_with_fallback_unless_budget_blocks_it(monkeypatch, budget):
    es = [entry("lead", 1, 0.0), entry("second", 2, -1.0), entry("direct", 3, None)]
    pool = ControlledPool(monkeypatch, {"lead": 20, "second": 1, ("direct", "direct_reference"): 2,
                                        ("direct", "exploratory_fallback"): 3},
                          {"lead": 0.0, "second": -1.0, ("direct", "direct_reference"): float("nan"),
                           ("direct", "exploratory_fallback"): -2.0})
    events = []
    result = dp.deep_pass(es, seed=7, executor=pool, workers=2, budget_s=budget, progress=events.append)
    direct = result.models[-1]
    assert not result.complete and "suspect after restarts: direct" in result.search_limited
    assert [(t, stage, seed) for t, label, stage, _, seed in pool.started if label == "direct"] == (
        [(1, "direct_reference", 7 + 2 * 104729 + 1), (3, "exploratory_fallback", 7 + 2 * 104729)]
        if budget is None else [(1, "direct_reference", 7 + 2 * 104729 + 1)])
    if budget is None:
        assert direct.status == "exploratory" and direct.log_z == -2.0 and len(direct.runs) == 2
        assert events[-1]["step"] == events[-1]["of"] == 3
    else:
        assert direct.status == "unresolved" and len(direct.runs) == 1
        assert "budget ended before exploratory fallback of direct" in result.search_limited
        assert events[-1]["step"] == 2 and events[-1]["of"] == 3

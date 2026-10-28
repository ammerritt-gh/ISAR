"""Progress events from the fast and deep pass (C-progress examples 1-5), and the no-clock
demonstrations: a deep pass past the old 60-minute budget completes and says so stage by stage.

The fast-pass cases run one small synthetic spectrum (ceiling 2). The deep-pass cases drive
``deep_pass`` with a fake clock and a fake serial worker (no sampling), as test_deep_pass does.
"""
import logging

import numpy as np

from isar.analyze import deep_pass as dp
from isar.analyze import fast_pass, progress
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.analyze.lineshapes import KB, convolute_voigt_dho
from isar.session.deep_delivery import DeepRequest


def _one_peak_ctx():
    x = np.linspace(-8.0, 16.0, 81)
    a = 500.0 * np.pi / 0.4 * -np.expm1(-5.0 / (KB * 50.0))
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, a, 20.0, 8.0, 50.0, peak="all")
    counts = np.random.default_rng(1).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0, center=0.0,
                       gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")


# --------------------------------------------------------------------------- the emitter
def test_no_callback_means_no_events():
    progress.emitter(None, "fast")("seed")                    # a no-op, no error


def test_an_event_carries_exactly_the_six_keys():
    got = []
    progress.emitter(got.append, "fast")("ladder", step=1, of=2)
    assert got == [{"pass": "fast", "phase": "ladder", "step": 1, "of": 2, "stage": None, "stages": None}]


def test_a_raising_callback_is_logged_once_and_dropped(caplog):
    calls = []

    def broken(event):
        calls.append(event)
        raise RuntimeError("display broke")

    send = progress.emitter(broken, "deep")
    with caplog.at_level(logging.WARNING, logger="isar.analyze.progress"):
        send("sampling", step=1, of=2, stage=1, stages=3)
        send("sampling", step=2, of=2, stage=1, stages=3)
    assert len(calls) == 1
    assert sum("progress callback failed" in r.getMessage() for r in caplog.records) == 1


# --------------------------------------------------------------------------- fast pass: examples 1, 5
def test_ex1_the_fast_pass_steps_in_order_with_the_ladder_counts():
    events = []
    fast_pass.run_fast_pass(_one_peak_ctx(), settings=FastPassSettings(mode_ceiling=2),
                            progress=events.append)
    shape = [(e["phase"], e["step"], e["of"]) for e in events]
    assert shape == [("seed", None, None), ("ladder", 0, 2), ("ladder", 1, 2), ("ladder", 2, 2),
                     ("evidence", None, None), ("audits", None, None), ("evidence", None, None),
                     ("classify", None, None)]
    assert all(e["pass"] == "fast" for e in events)


def test_ex5_a_raising_callback_leaves_the_fast_pass_result_unchanged(caplog):
    ctx = _one_peak_ctx()
    settings = FastPassSettings(mode_ceiling=2)
    free = fast_pass.run_fast_pass(ctx, settings=settings)
    calls = []

    def broken(event):
        calls.append(event)
        raise RuntimeError("display broke")

    with caplog.at_level(logging.WARNING, logger="isar.analyze.progress"):
        res = fast_pass.run_fast_pass(ctx, settings=settings, progress=broken)
    assert len(calls) == 1
    assert sum("progress callback failed" in r.getMessage() for r in caplog.records) == 1
    assert (res.state, res.preferred_count, res.count_probabilities, res.search_limit_reason) == \
        (free.state, free.preferred_count, free.count_probabilities, free.search_limit_reason)
    assert [p.omega0 for p in res.peaks] == [p.omega0 for p in free.peaks]


# --------------------------------------------------------------------------- deep pass: examples 2-4
class _Clock:
    def __init__(self):
        self.t = 0.0

    def perf_counter(self):
        return self.t


# Small sigmas so that the promotion window separates the models in the cases below.
ACC = dp.SamplerSettings(n_live=100, dlogz=0.1, calibrated_sigma=0.5)
EXPL = dp.SamplerSettings(n_live=50, dlogz=0.5, calibrated_sigma=2.0)


def _fake_pool(monkeypatch, values, exploratory=dp.EXPLORATORY):
    """``(executor, clock)``: every job costs 100 s of fake clock; ``values[label] = (accepted lnZ,
    exploratory lnZ)``."""
    clock = _Clock()
    monkeypatch.setattr(dp, "time", clock)

    def run(job, **kwargs):
        label, _entry, settings, _seed = job
        clock.t += 100.0
        lnz = values[label][1 if settings is exploratory else 0]
        record = dp.RunRecord(label=label, seed=0, settings=settings, ndim=2, log_z=lnz, log_z_err=0.1,
                             information_h=1.0, n_like_calls=1, n_iter=1, lnl_max=0.0, wall_s=0.0,
                             reference_lnl=None, evidence_converged=True)
        return label, [record]

    monkeypatch.setattr(dp, "_stage_task", run)
    return None, clock


def _entry(label, count, fast):
    return dp.ModelEntry(label, count, 2, fast, None, None, log_model_prior=0.0)


def _sampling(events):
    return [(e["stage"], e["step"], e["of"]) for e in events if e["phase"] == "sampling"]


def _ex2_entries():
    # c1, c2 are the contenders; c5 is promoted to stage 3; c3, c4 stay exploratory
    entries = [_entry("c1", 1, 0.0), _entry("c2", 2, -1.0), _entry("c3", 3, -40.0),
               _entry("c4", 4, -41.0), _entry("c5", 5, -6.0)]
    values = {"c1": (0.0, 0.0), "c2": (-1.0, -1.0), "c3": (-40.0, -40.0), "c4": (-41.0, -41.0),
              "c5": (-6.0, -6.0)}
    return entries, values


def test_ex2_the_sampling_steps_run_stage_by_stage(monkeypatch):
    entries, values = _ex2_entries()
    executor, _clock = _fake_pool(monkeypatch, values, EXPL)
    events = []
    dp.deep_pass(entries, accepted=ACC, exploratory=EXPL, executor=executor, progress=events.append)
    assert _sampling(events) == [(1, 0, 5), (1, 1, 5), (1, 2, 5), (1, 3, 5), (1, 4, 5), (1, 5, 5), (2, 0, 1), (2, 1, 1)]
    assert all(e["pass"] == "deep" for e in events)


def test_ex3_serial_worker_requests_give_the_same_steps(monkeypatch):
    entries, values = _ex2_entries()
    executor, _clock = _fake_pool(monkeypatch, values, EXPL)
    events = []
    dp.deep_pass(entries, accepted=ACC, exploratory=EXPL, executor=executor, workers=2,
                 progress=events.append)
    assert _sampling(events) == [(1, 0, 5), (1, 1, 5), (1, 2, 5), (1, 3, 5), (1, 4, 5), (1, 5, 5), (2, 0, 1), (2, 1, 1)]


def test_ex4_a_stage_with_no_models_sends_no_step(monkeypatch):
    entries = [_entry("c1", 1, 0.0), _entry("c2", 2, -1.0)]
    executor, _clock = _fake_pool(monkeypatch, {"c1": (0.0, 0.0), "c2": (-1.0, -1.0)}, EXPL)
    events = []
    dp.deep_pass(entries, accepted=ACC, exploratory=EXPL, executor=executor, progress=events.append)
    assert _sampling(events) == [(1, 0, 2), (1, 1, 2), (1, 2, 2)]


# --------------------------------------------------------------------------- the no-clock demonstration
def test_the_default_request_has_no_clock_and_a_deep_pass_past_an_hour_completes(monkeypatch):
    assert DeepRequest().budget_s is None                     # the old default was 3600 s per spectrum
    entries = [_entry(f"c{i}", i, -0.5 * i) for i in range(1, 41)]
    values = {e.label: (-0.1 * i, -0.1 * i) for i, e in enumerate(entries, 1)}
    executor, clock = _fake_pool(monkeypatch, values)
    events = []
    res = dp.deep_pass(entries, budget_s=DeepRequest().budget_s, executor=executor, progress=events.append)
    assert clock.t > 3600.0 and res.complete and res.search_limited is None
    stages = [s for s, _step, _of in _sampling(events)]
    assert stages == sorted(stages) and set(stages) == {1, 2}  # stage by stage, never back
    assert all(step <= of for _s, step, of in _sampling(events))


# --------------------------------------------------------------------------- budget: no completion step for an unrun model
def test_a_zero_budget_announces_no_sampling_step(monkeypatch):
    entries, values = _ex2_entries()
    executor, _clock = _fake_pool(monkeypatch, values, EXPL)
    events = []
    dp.deep_pass(entries, accepted=ACC, exploratory=EXPL, executor=executor, budget_s=0.0,
                 progress=events.append)
    assert _sampling(events) == []


def test_a_model_the_budget_blocks_announces_no_step(monkeypatch):
    # each job costs 100 s: the two contenders run (0-200 s); the budget of 150 s then blocks the remaining initial models
    entries, values = _ex2_entries()
    executor, _clock = _fake_pool(monkeypatch, values, EXPL)
    events = []
    res = dp.deep_pass(entries, accepted=ACC, exploratory=EXPL, executor=executor, budget_s=150.0,
                       progress=events.append)
    assert _sampling(events) == [(1, 0, 5), (1, 1, 5), (1, 2, 5)]
    assert "budget ended before c5, c3, c4" in res.search_limited

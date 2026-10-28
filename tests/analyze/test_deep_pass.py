"""The deep pass core (plan S2U1, C-deep-inputs examples 1-5 and 7, and the engine's own checks).

The sampler checks run on analytically normalised problems (seconds, serial, fixed seeds): a
normalised Gaussian likelihood in the unit cube under a uniform prior, times exp(offset), so
lnZ = offset and H = -d ln(s sqrt(2 pi e)) exactly. A wrong call counter, a wrong H, an
offset-dropping assembly, equal weight per model instead of the model prior, or contender-only
probabilities shown as the full comparison each fail one of these. The budget cases drive
``deep_pass`` with a fake clock and a fake ``_stage_task`` (no sampling).
"""
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import deep_pass as dp
from isar.analyze import loop, prior
from isar.analyze.context import ScanContext
from isar.schema.steering import ParamConstraints


class Gauss:
    def __init__(self, d, s=0.02, offset=0.0):
        self.d, self.s, self.offset = d, s, offset

    def __call__(self):
        d, s, off = self.d, self.s, self.offset
        mu = np.full(d, 0.5)
        c = -0.5 * d * math.log(2 * math.pi * s * s) + off

        self.calls = calls = [0]

        def loglike(x):
            calls[0] += 1
            return c - 0.5 * float(np.sum((x - mu) ** 2)) / (s * s)

        return loglike, (lambda u: u), d


FAST = dp.SamplerSettings(n_live=100, dlogz=0.1, calibrated_sigma=0.5)
EXPL = dp.SamplerSettings(n_live=50, dlogz=0.5, calibrated_sigma=2.0)


def entry(label, count, d, offset, lnl_opt=None):
    p = Gauss(d, offset=offset)
    ll, _, _ = p()
    return dp.ModelEntry(label, count, d, fastpass_ln_z=offset,
                         fastpass_lnl_opt=ll(np.full(d, 0.5)) if lnl_opt is None else lnl_opt,
                         problem=p, log_model_prior=0.0)


# --------------------------------------------------------------------------- the sampler
def test_one_run_recovers_lnz_h_calls_and_best_point():
    d, s = 5, 0.02
    g = Gauss(d, s)
    rec = dp.run_nested("g", g, settings=FAST, seed=1, reference_lnl=None)
    h_true = -d * math.log(s * math.sqrt(2 * math.pi * math.e))
    assert abs(rec.log_z) < 1.0, rec.log_z
    assert abs(rec.information_h - h_true) < 1.5, rec.information_h
    assert rec.n_like_calls == g.calls[0] and rec.usable        # an independent count in the loglike
    assert len(rec.best_theta) == d and np.allclose(rec.best_theta, 0.5, atol=5 * s)


def test_convergence_flag_follows_the_dlogz_target():
    # C-deep-inputs example 7
    ok = dp.run_nested("g", Gauss(3), settings=FAST, seed=2)
    assert ok.evidence_converged and ok.usable
    capped = dp.run_nested("g", Gauss(3), settings=replace(FAST, max_iter=5), seed=2)
    assert "hit max_iter" in capped.suspect
    assert not capped.evidence_converged and not capped.usable


def test_a_sampler_failure_is_a_recorded_run():
    def broken():
        raise ValueError("bundle mismatch")
    rec = dp.run_nested("x", broken, settings=FAST, seed=1)
    assert rec.failure and "bundle mismatch" in rec.failure and not rec.usable


# --------------------------------------------------------------------------- the staged pass
def test_staged_pass_complete():
    # counts 1 (lnZ 0), 2 (lnZ -1.0), 3 (lnZ -30, exploratory, never promoted)
    res = dp.deep_pass([entry("c1", 1, 4, 0.0), entry("c2", 2, 6, -1.0), entry("c3", 3, 8, -30.0)],
                       accepted=FAST, exploratory=EXPL, seed=3)
    st = {m.label: m.status for m in res.models}
    assert st == {"c1": "reference", "c2": "reference", "c3": "exploratory"}, st
    assert res.complete and res.comparison_scope == "full" and res.search_limited is None
    p1_true = 1 / (1 + math.exp(-1.0) + math.exp(-30.0))
    assert abs(res.count_probabilities[1] - p1_true) < 0.25, res.count_probabilities
    assert res.close_call["statistical"] is True and res.best_count == 1    # P(1) ~ 0.73 < 0.80
    assert set(res.stage_seconds) == {"initial", "promoted"}


def test_a_laplace_relevant_model_is_promoted_to_a_reference_run():
    # Not a contender by fast-pass P (~0.002) but its Laplace lnZ (-6) sits inside the relevance gap:
    # a low exploratory lnZ (-30) may not settle it on its own.
    e = entry("c3", 3, 4, -30.0)
    e = replace(e, fastpass_ln_z=-6.0)
    res = dp.deep_pass([entry("c1", 1, 4, 0.0), entry("c2", 2, 4, -1.0), e],
                       accepted=FAST, exploratory=EXPL, seed=4)
    c3 = [m for m in res.models if m.label == "c3"][0]
    assert c3.status == "reference" and len(c3.runs) == 2 and res.exploratory_only == (), c3


def test_a_suspect_model_is_restarted_kept_and_makes_the_comparison_partial():
    res = dp.deep_pass([entry("c1", 1, 4, 0.0), entry("bad", 2, 4, -0.5, lnl_opt=500.0)],
                       accepted=FAST, exploratory=EXPL, seed=5)
    bad = [m for m in res.models if m.label == "bad"][0]
    assert bad.status == "unresolved" and len(bad.runs) == dp.MAX_RESTARTS + 1
    assert bad.runs[1].settings.step_factor == 2 and bad.runs[2].settings.n_live == 2 * FAST.n_live
    assert not res.complete and res.comparison_scope.startswith("partial")
    assert "suspect after restarts: bad" in res.search_limited


def test_zero_budget_runs_nothing():
    res = dp.deep_pass([entry("c1", 1, 4, 0.0)], accepted=FAST, exploratory=EXPL, budget_s=0.0)
    assert res.models[0].status == "not_run" and not res.complete and res.count_probabilities == {}
    assert "budget ended before c1" in res.search_limited


# --------------------------------------------------------------------------- close call, model prior
def test_numerical_close_call_follows_the_calibrated_sigma():
    # a clear statistical winner (P ~ 0.95) whose lnZ gap (3 nats) is inside the calibrated
    # uncertainty (sigma 2 each) is flagged numerical; at sigma 0.1 it is not
    m = [dp.ModelEvidence("a", 1, "reference", 0.0, 2.0), dp.ModelEvidence("b", 2, "reference", -3.0, 2.0)]
    cc = dp.close_call(m)
    assert cc["statistical"] is False and cc["numerical"] is True, cc
    m = [dp.ModelEvidence("a", 1, "reference", 0.0, 0.1), dp.ModelEvidence("b", 2, "reference", -3.0, 0.1)]
    cc = dp.close_call(m)
    assert cc["statistical"] is False and cc["numerical"] is False, cc
    assert dp.close_call(m, close_call_p=0.99)["statistical"] is True    # the fast pass's threshold governs


def test_example1_the_model_prior_weights_the_count_probabilities():
    # count 0 (lnZ 0, prior 0) against two count-1 models (lnZ 0, prior -ln 2 each): P(0) = P(1) = 0.5.
    # Equal weight per model would give 1/3 and 2/3.
    half = -math.log(2.0)
    models = [dp.ModelEvidence("n0", 0, "reference", 0.0, 0.1),
              dp.ModelEvidence("n1a", 1, "reference", 0.0, 0.1, log_model_prior=half),
              dp.ModelEvidence("n1b", 1, "reference", 0.0, 0.1, log_model_prior=half)]
    _, counts = dp._probabilities(models)
    assert counts[0] == pytest.approx(0.5) and counts[1] == pytest.approx(0.5)
    assert dp.close_call(models)["p_best"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- budget and promotion
class _Clock:
    """A fake ``time`` module for deep_pass: the fake serial worker advances it 100 s per job."""
    def __init__(self):
        self.t = 0.0

    def perf_counter(self):
        return self.t


def _fake_run(label, settings, log_z):
    return dp.RunRecord(label=label, seed=0, settings=settings, ndim=2, log_z=log_z, log_z_err=0.1,
                        information_h=1.0, n_like_calls=1, n_iter=1, lnl_max=0.0, wall_s=0.0,
                        reference_lnl=None, evidence_converged=True)


def _fake_pool(monkeypatch, values):
    """``(executor, clock, log)``; ``values[label] = (accepted lnZ, exploratory lnZ)``, jobs logged."""
    clock = _Clock()
    monkeypatch.setattr(dp, "time", clock)
    log = []

    def run(job, **kwargs):
        label, _entry, settings, _seed = job
        clock.t += 100.0
        log.append((label, settings is dp.EXPLORATORY))
        return label, [_fake_run(label, settings, values[label][1 if settings is dp.EXPLORATORY else 0])], None

    monkeypatch.setattr(dp, "_stage_task", run)
    return None, clock, log


def _fake_entry(label, count, fast, prior_=0.0):
    return dp.ModelEntry(label, count, 2, fast, None, None, log_model_prior=prior_)


def test_serial_checks_the_budget_before_each_model_even_if_more_workers_are_requested(monkeypatch):
    entries = [_fake_entry(f"c{i}", i, -0.5 * i) for i in range(1, 5)]       # all contenders
    values = {e.label: (-0.1 * i, None) for i, e in enumerate(entries)}
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, budget_s=150.0, executor=executor, workers=2)
    assert [lb for lb, _ in log] == ["c1", "c2"] and clock.t == 200.0        # the admitted run finishes
    assert [m.status for m in res.models] == ["reference", "reference", "not_run", "not_run"]
    assert not res.complete and res.search_limited == "budget ended before c3, c4"
    assert set(res.count_probabilities) == {1, 2}
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, budget_s=150.0, executor=executor, workers=4)
    assert len(log) == 2 and not res.complete  # no executor: still one actual worker


def test_a_promotion_the_budget_leaves_pending_is_not_a_full_comparison(monkeypatch):
    entries = [_fake_entry("c1", 1, 0.0), _fake_entry("c2", 2, -1.0), _fake_entry("c3", 3, -6.0)]
    # c3's cheap value sits far enough below that +3 sigma stays under the promotion window (holds while the exploratory sigma is below 30)
    values = {"c1": (0.0, None), "c2": (-1.0, None), "c3": (-7.0, -100.0)}
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, budget_s=250.0, executor=executor)
    assert log == [("c1", False), ("c2", False), ("c3", True)]              # the promotion stage never starts
    assert res.exploratory_only == ("c3",) and not res.complete
    assert res.search_limited == "budget ended before promotion of c3"
    assert res.comparison_scope == "partial (search limited)"
    # the same models with time to spare: c3's reference run replaces the exploratory value
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor)
    c3 = [m for m in res.models if m.label == "c3"][0]
    assert log[-1] == ("c3", False) and c3.status == "reference" and c3.log_z == -7.0 and res.complete
    # and when the fast pass also prices it far below, the cheap run stands and nothing is pending
    entries[2] = _fake_entry("c3", 3, -40.0)
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor)
    assert len(log) == 3 and res.exploratory_only == ("c3",) and res.complete


def test_a_search_limited_fast_pass_makes_the_deep_result_partial(monkeypatch):
    # every sampled model is clean, but the fast pass never priced the higher counts
    entries = [_fake_entry("c1", 1, 0.0), _fake_entry("c2", 2, -1.0)]
    values = {"c1": (0.0, None), "c2": (-1.0, None)}
    executor, _, _ = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor)
    assert res.complete and res.comparison_scope == "full"
    executor, _, _ = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor, fast_limit="safety_budget")
    assert not res.complete and res.comparison_scope == "partial (search limited)"
    assert res.search_limited == "fast pass search limited: safety_budget"


def test_a_twin_parent_without_a_fast_pass_value_goes_directly_to_reference(monkeypatch):
    entries = [_fake_entry("c1", 1, 0.0), _fake_entry("c2", 2, -1.0), _fake_entry("parent", 3, None)]
    values = {"c1": (0.0, None), "c2": (-1.0, None), "parent": (-2.0, -30.0)}
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor)
    assert [row for row in log if row[0] == "parent"] == [("parent", False)]
    assert res.models[-1].status == "reference" and res.complete


def test_the_model_prior_enters_the_contender_order_and_the_entries_pass_it_on(monkeypatch):
    # b has the higher Laplace lnZ but a prior ln 4 lower: a leads the order
    entries = [_fake_entry("b", 2, 1.0, -math.log(4.0)), _fake_entry("a", 1, 0.0, 0.0)]
    values = {"a": (0.0, None), "b": (1.0, None)}
    executor, clock, log = _fake_pool(monkeypatch, values)
    res = dp.deep_pass(entries, executor=executor)
    assert [m.label for m in res.models] == ["a", "b"]
    assert res.models[1].log_model_prior == pytest.approx(-math.log(4.0))
    assert res.count_probabilities[1] > res.count_probabilities[2]           # weights 0 vs 1 - ln 4


def test_a_converged_refit_beats_an_unconverged_one_and_a_tie_keeps_the_fast_pass_optimum():
    def fit(converged, cost):
        return SimpleNamespace(optimizer_converged=converged, posterior_cost=None, cost=cost)
    assert dp._better_fit(fit(False, 1.0), fit(True, 9.0))[1] == "nested_refit"
    assert dp._better_fit(fit(True, 1.0), fit(False, 0.0))[1] == "fast_pass_optimum"
    assert dp._better_fit(fit(True, 5.0), fit(True, 4.0))[1] == "nested_refit"
    assert dp._better_fit(fit(True, 5.0), fit(True, 5.0))[1] == "fast_pass_optimum"


# --------------------------------------------------------------------------- models from a record
X = np.linspace(-8.0, 20.0, 113)


def _ctx(constraints=None):
    counts = np.random.default_rng(1).poisson(30.0, X.size).astype(float)
    return ScanContext(x=X, counts=counts, mask=np.ones(X.size, bool), T=300.0, center=0.0,
                       gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute",
                       constraints=constraints)


STOKES2 = ["phonon.omega0", "phonon.Gamma", "phonon.h", "phonon2.omega0", "phonon2.Gamma", "phonon2.h"]
STOKES3 = STOKES2 + ["phonon3.omega0", "phonon3.Gamma", "phonon3.h"]


def _model(label, n_peaks, free, *, delta=(), ln_prior=0.0, ln_z=None, cost=None, basin=0):
    return {"model_id": f"{label}#{basin}", "label": label, "n_peaks": n_peaks,
            "delta_modes": list(delta), "ln_model_prior": ln_prior, "ln_z": ln_z, "cost": cost,
            "k_free": len(free), "free": list(free)}


def _record(ctx, models, *, prior_cfg=None, saturated=-20.0):
    ps = None if prior_cfg is None else prior.PriorSettings(**prior_cfg)
    return {"schema": "isar.fast-pass/1", "arena_fingerprint": loop.data_likelihood_fingerprint(ctx),
            "ln_like_saturated": saturated,
            "prior_provenance": json.loads(json.dumps(prior.recipe_provenance(ctx, ps))),
            "settings": {"mode_ceiling": 3, "prior": prior_cfg}, "models": models}


def test_example2_a_delta_twin_rebuilds_with_its_pinned_width():
    ctx = _ctx()
    free = [n for n in STOKES2 if n != "phonon2.Gamma"] + ["elastic.h", "bg.BG"]
    lp = -math.log(4) - 2 * math.log(2)
    rec = _record(ctx, [_model("elastic+stokes2+bg_delta2", 2, free, delta=[1], ln_prior=lp)])
    (e,) = dp.models_from_record(ctx, rec, dp.plain_hypotheses(ctx, 3))
    assert e.ndim == 7 and e.log_model_prior == pytest.approx(lp)
    assert "phonon2.Gamma" not in [p.name for p in e.problem.bundle.params]
    assert e.problem.ctx.constraints.hard == {"phonon2.Gamma": loop.GAMMA_FLOOR}
    wrong = _record(ctx, [_model("elastic+stokes2+bg_delta2", 2, free, delta=[0], ln_prior=lp)])
    with pytest.raises(ValueError, match="elastic\\+stokes2\\+bg_delta2"):
        dp.models_from_record(ctx, wrong, dp.plain_hypotheses(ctx, 3))


def test_example3_a_twin_parent_is_an_entry_without_a_fast_pass_value():
    ctx = _ctx()
    free = STOKES3 + ["elastic.h", "bg.BG"]
    m = _model("elastic+stokes3+bg", 3, free, cost=212.4)
    m["evidence_status"] = "degenerate_twin"
    (e,) = dp.models_from_record(ctx, _record(ctx, [m]), dp.plain_hypotheses(ctx, 3))
    assert e.fastpass_ln_z is None and e.fastpass_lnl_opt == pytest.approx(-106.2)


def test_example4_basins_of_one_label_are_one_entry():
    ctx = _ctx()
    free = STOKES2 + ["bg.BG"]
    rec = _record(ctx, [_model("stokes2+bg", 2, free, ln_z=-100.0, cost=210.0, basin=0),
                        _model("stokes2+bg", 2, free, ln_z=-101.0, cost=212.0, basin=1)],
                  saturated=-20.0)
    (e,) = dp.models_from_record(ctx, rec, dp.plain_hypotheses(ctx, 3))
    assert e.fastpass_ln_z == pytest.approx(-100 + math.log1p(math.exp(-1)) + 20, abs=1e-9)
    assert e.fastpass_lnl_opt == pytest.approx(-105.0)


def test_example5_refusals_name_their_cause():
    ctx = _ctx()
    hyps = dp.plain_hypotheses(ctx, 3)
    models = [_model("bg", 0, ["bg.BG"], ln_z=-50.0, cost=100.0)]
    good = _record(ctx, models)
    assert len(dp.models_from_record(ctx, good, hyps)) == 1
    with pytest.raises(ValueError, match="no fast-pass record"):
        dp.models_from_record(ctx, {k: v for k, v in good.items() if k != "schema"}, hyps)
    with pytest.raises(ValueError, match="data changed"):
        dp.models_from_record(ctx, {**good, "arena_fingerprint": "other"}, hyps)
    soft = ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)}, hard={}, origins={})
    with pytest.raises(ValueError, match="constrained context"):
        dp.models_from_record(_ctx(soft), _record(_ctx(soft), models), hyps)
    with pytest.raises(ValueError, match="prior provenance"):
        dp.models_from_record(ctx, {**good, "settings": {"mode_ceiling": 3,
                                                         "prior": {"amplitude_margin": 3.0}}}, hyps)
    with pytest.raises(ValueError, match="'nope'"):
        dp.models_from_record(ctx, _record(ctx, [_model("nope", 0, ["bg.BG"])]), hyps)
    with pytest.raises(ValueError, match="'bg'"):
        dp.models_from_record(ctx, _record(ctx, [_model("bg", 0, ["bg.BG", "x"])]), hyps)


def test_example5_a_configured_prior_is_rebuilt_not_refused():
    ctx = _ctx()
    cfg = {"amplitude_margin": 3.0}
    free = STOKES2[:3] + ["bg.BG"]
    rec = _record(ctx, [_model("stokes+bg", 1, free, ln_z=-60.0, cost=100.0)], prior_cfg=cfg)
    (e,) = dp.models_from_record(ctx, rec, dp.plain_hypotheses(ctx, 3))
    top = prior.resolve_prior_for(ctx).max_count
    assert e.problem.bundle.by_name()["phonon.h"].hi == pytest.approx(3.0 * top)

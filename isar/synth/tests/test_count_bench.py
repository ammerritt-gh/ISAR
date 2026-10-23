"""Fast subset of the peak-count benchmark (isar/synth/count_bench.py).

These tests show the harness works and the figures are computed; they never assert the 90% / 5% /
60% targets (ruling R15: targets, not gates). The reference's nested sampling is stubbed in the
fast tests (one real fitter pass runs end to end); the real reference and the process pool are in
the slow tests.
"""
import csv
import json
import time

import numpy as np
import pytest

from isar.analyze.components import PhononDHO
from isar.analyze.lineshapes import KB
from isar.synth import count_bench as cb

TINY = cb.ReferenceBudget(draws=3, null_draws=3, random_starts=0, n_live=24, max_iter=4000)


def _truth(**kw):
    base = dict(stream=cb.STREAM_CASE_EVAL, index=0, kind="single",
                modes=(cb.ModeTruth(6.0, 0.3, 400.0),), elastic_height=800.0, background=2.0,
                T=100.0, exposure=1.0, gauss_fwhm=1.5, lorentz_fwhm=0.4, ceiling=3)
    base.update(kw)
    return cb.CaseTruth(**base)


# --------------------------------------------------------------------------- generator
def test_generator_is_seeded_covers_hard_kinds_and_respects_ceiling(monkeypatch):
    a = [cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 4) for i in range(120)]
    b = [cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 4) for i in range(120)]
    assert a == b                                                   # reproducible
    assert a != [cb.generate_case(7, cb.STREAM_CASE_TUNING, i, 4) for i in range(120)]
    assert a != [cb.generate_case(8, cb.STREAM_CASE_EVAL, i, 4) for i in range(120)]
    assert set(cb.KIND_WEIGHTS) <= {c.kind for c in a}              # every hard kind appears
    assert max(c.n_modes for c in a) == 4 and min(c.n_modes for c in a) == 0
    assert all(m.gamma < m.omega0 for c in a for m in c.modes)      # underdamped by construction
    assert max(cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 1).n_modes for i in range(60)) == 1
    monkeypatch.setenv(cb.MODE_CEILING_ENV, "2")
    assert cb.mode_ceiling() == 2 and cb.mode_ceiling(5) == 5
    monkeypatch.delenv(cb.MODE_CEILING_ENV)
    assert cb.mode_ceiling() == cb.DEFAULT_MODE_CEILING == 6


def test_streams_are_distinct_and_independent():
    assert len(set(cb.STREAMS)) == len(cb.STREAMS)
    firsts = {s: cb.rng_for(1, s, 5, 5).random() for s in cb.STREAMS}
    assert len(set(firsts.values())) == len(cb.STREAMS)
    assert cb.rng_for(1, cb.STREAM_DRAW_AB, 3).random() == cb.rng_for(1, cb.STREAM_DRAW_AB, 3).random()


def test_antistokes_partner_is_derived_by_detailed_balance():
    truth = _truth(elastic_height=0.0, background=0.0, T=100.0)
    comp, theta, ctx = cb._truth_model(truth)
    assert [type(c) for c in comp.components[:1]] == [PhononDHO]
    assert all(c.peak == "stokes+astokes" for c in comp.components if isinstance(c, PhononDHO))
    assert not any("astokes" in n.lower() for n in comp.param_names())     # no free partner
    phonon = comp.evaluate_components(ctx.x, theta, ctx)[0][1]
    ratio = phonon[ctx.x < 0].sum() / phonon[ctx.x > 0].sum()
    assert ratio == pytest.approx(np.exp(-6.0 / (KB * 100.0)), rel=0.05)


def test_counts_are_poisson_with_zeros_and_no_floor():
    truth = _truth(modes=(), kind="empty", elastic_height=0.2, background=0.05)
    obs = cb.draw_observation(truth, cb.rng_for(1, cb.STREAM_DRAW_FIT, 0))
    assert (obs.counts == 0).any()                                  # sigma = 0 at zero counts, kept
    assert np.all(obs.counts >= 0) and np.all(obs.counts == np.round(obs.counts))


def test_the_fitter_context_carries_the_generated_exposure():
    obs = cb.draw_observation(_truth(exposure=2.5), cb.rng_for(1, cb.STREAM_DRAW_FIT, 0))
    assert obs.exposure == 2.5
    ctx = cb.context_of(obs)
    # Relative scale 1, reference = declared exposure, counted in seconds (the recipe's amplitude ceiling).
    assert np.all(ctx.exposure_scale == 1.0) and ctx.exposure_reference == 2.5
    assert ctx.exposure_source == "time"


def test_the_fitter_sees_an_observation_never_a_truth(monkeypatch):
    assert {f for f in cb.Observation.__dataclass_fields__} == {"x", "counts", "T", "gauss_fwhm",
                                                                "lorentz_fwhm", "exposure"}
    seen = []

    def spy(obs, *, seed, errors_tier):
        seen.append(obs)
        return {"best_label": "bg", "n_modes": 0, "candidates": [], "model_manifest": None}

    monkeypatch.setattr(cb, "fit_observation", spy)
    cb.run_case((_truth(modes=(), kind="empty"), 1, TINY, 10, "cheap"))
    assert len(seen) == 1 and type(seen[0]) is cb.Observation


# --------------------------------------------------------------------------- resolvability
def _stub_reference(monkeypatch, *, prefer_n=True, fail=False, calls=None):
    def stub(obs, m, positions, budget, rng):
        if calls is not None:
            calls.append(m)
        if fail:
            return None
        return -5.0 * m + (10.0 if prefer_n else -10.0) * (m == len(positions))
    monkeypatch.setattr(cb, "reference_ln_evidence", stub)


def test_classifier_resolvable_needs_detection_and_few_false_splits(monkeypatch):
    budget = cb.ReferenceBudget(draws=10, null_draws=10)
    truth = _truth(modes=(cb.ModeTruth(5.0, 0.3, 300.0), cb.ModeTruth(8.0, 0.3, 200.0)))
    calls = []
    _stub_reference(monkeypatch, prefer_n=True, calls=calls)
    r = cb.classify_resolvability(truth, 1, budget)
    # This stub prefers N on every draw, null draws included: so N is always found, and the
    # first null draw is already a false split, which exceeds the allowed 0 -> borderline.
    assert (r.detect_found, r.detect_draws) == (10, 10) and r.split_draws == 1
    assert r.label == "borderline"
    _stub_reference(monkeypatch, prefer_n=False)
    r = cb.classify_resolvability(truth, 1, budget)           # N never preferred: exits on 2nd miss
    assert r.label == "borderline" and r.detect_draws == 2 and r.detect_found == 0


def test_classifier_labels_and_indeterminate_draws_count_against(monkeypatch):
    budget = cb.ReferenceBudget(draws=10, null_draws=10)
    truth = _truth(modes=(cb.ModeTruth(5.0, 0.3, 300.0), cb.ModeTruth(8.0, 0.3, 200.0)))
    # A reference that finds the extra mode on truth draws (2 modes in the data) and never on the
    # null draws: decided by the number of strong peaks the data carries, not by the truth object.
    def ref(obs, m, positions, b, rng):
        two_peaks = obs.counts[(obs.x > 7.5) & (obs.x < 8.5)].max() > 100
        return -5.0 * m + (10.0 if (m == 2 and two_peaks) else 0.0)
    monkeypatch.setattr(cb, "reference_ln_evidence", ref)
    r = cb.classify_resolvability(truth, 1, budget)
    assert r.label == "resolvable" and r.detect_found == 10 and r.split_found == 0
    _stub_reference(monkeypatch, fail=True)                   # nothing computable: not resolvable
    r = cb.classify_resolvability(truth, 1, budget)
    assert r.label == "borderline" and r.detect_indeterminate == r.detect_draws == 2
    assert cb.classify_resolvability(_truth(modes=()), 1, budget).label == "not_applicable"


def test_laplace_reference_is_the_default_and_never_samples(monkeypatch):
    from isar.analyze.errors import bayes
    def no_sampler(*args, **kwargs):
        raise AssertionError("the Laplace reference must not run nested sampling")
    monkeypatch.setattr(bayes, "bayesian_posterior", no_sampler)
    truth = _truth(modes=(cb.ModeTruth(8.0, 0.3, 600.0),), elastic_height=500.0)
    obs = cb.draw_observation(truth, cb.rng_for(3, cb.STREAM_DRAW_REFERENCE, 0))
    b = cb.ReferenceBudget(random_starts=1)
    assert b.evidence == "laplace"
    z1 = cb.reference_ln_evidence(obs, 1, [(8.0, 0.3)], b, cb.rng_for(3, cb.STREAM_REF_SEARCH))
    z0 = cb.reference_ln_evidence(obs, 0, [(8.0, 0.3)], b, cb.rng_for(3, cb.STREAM_REF_SEARCH))
    assert z1 is not None and z0 is not None and z1 - z0 > 50          # an obvious extra mode
    with pytest.raises(ValueError):
        cb.ReferenceBudget(evidence="trans-dimensional")


# --------------------------------------------------------------------------- A/B and figures
def test_ab_accuracy_is_a_coin_flip_for_identical_curves_and_certain_for_different_ones():
    lam = cb.truth_lambda(_truth())
    acc, se = cb.ab_accuracy(lam, lam, 400, cb.rng_for(1, cb.STREAM_DRAW_AB, 0))
    assert acc == pytest.approx(0.5, abs=4 * se) and se > 0
    other = lam * 1.5
    acc, _ = cb.ab_accuracy(lam, other, 400, cb.rng_for(1, cb.STREAM_DRAW_AB, 0))
    assert acc > 0.99


def test_summarize_keeps_abstentions_and_failures_in_the_denominators():
    R = cb.CaseResult
    rows = [
        R(case_id="a", n_true=2, resolvability="resolvable", outcome="correct", ab_accuracy=0.55,
          ab_pass=True, n_fit=2, ambiguous=False),
        R(case_id="b", n_true=2, resolvability="resolvable", outcome="abstain", ab_accuracy=0.7,
          ab_pass=False, n_fit=2, ambiguous=True),
        R(case_id="c", n_true=3, resolvability="resolvable", outcome="wrong", ab_accuracy=0.9,
          ab_pass=False, n_fit=2, ambiguous=False),
        R(case_id="d", n_true=2, resolvability="resolvable", status="fit_error", outcome="wrong",
          ab_pass=False),
        R(case_id="e", n_true=2, resolvability="borderline", outcome="n/a"),
        R(case_id="f", n_true=1, resolvability="resolvable", outcome="correct", ab_accuracy=0.6,
          ab_pass=True, confident_extra=False, n_fit=1, ambiguous=False),
        R(case_id="g", n_true=0, resolvability="not_applicable", outcome="n/a", confident_extra=True),
        R(case_id="h", n_true=1, resolvability="borderline", outcome="n/a", confident_extra=False),
    ]
    s = cb.summarize(rows)
    by = {f["name"]: f for f in s["figures"]}
    correct = by["correct_counts_on_resolvable"]
    assert (correct["numerator"], correct["denominator"]) == (2, 5)       # abstention + error stay
    assert s["n_abstentions"] == 1 and s["n_failed_fits"] == 1 and s["n_borderline"] == 2
    extra = by["confident_extra_on_empty_or_single"]
    assert (extra["numerator"], extra["denominator"]) == (1, 3)           # g, h, f: n_true <= 1
    ab = by["ab_share_at_or_below_60pct"]
    assert (ab["numerator"], ab["denominator"]) == (2, 5)
    assert correct["mc_se"] == pytest.approx(np.sqrt(0.4 * 0.6 / 5))
    lo, hi = correct["wilson95"]
    assert 0 < lo < 0.4 < hi < 1


def test_reference_agreement_per_case_is_not_hidden_by_pooled_draws():
    R = cb.CaseResult
    # One hard truth the reference gets wrong on every computed draw, three easy ones it gets right.
    rows = [R(case_id="h", n_true=2, resolvability="borderline", detect_draws=40, detect_found=0,
              split_draws=0, split_found=0),
            R(case_id="u", n_true=1, resolvability="borderline", detect_draws=0)]  # nothing computed
    rows += [R(case_id=f"e{i}", n_true=1, resolvability="resolvable", detect_draws=200,
               detect_found=200, split_draws=200, split_found=0) for i in range(3)]
    s = cb.summarize(rows)
    pooled = s["reference_agreement_pooled_draws"]
    assert (pooled["numerator"], pooled["denominator"]) == (1200, 1240)   # draws: the hard case is 40 of them
    per_case = s["reference_agreement_per_case"]
    assert (per_case["numerator"], per_case["denominator"], per_case["unscored"]) == (3, 4, 1)
    assert per_case["value"] == pytest.approx(0.75) and pooled["value"] > 0.95


def test_count_answer_reads_posterior_by_count_and_prefers_a_published_flag():
    fit = {"best_label": "x", "n_modes": 2, "candidates": [
        {"n_modes": 2, "posterior_probability": 0.6, "akaike_weight": 0.5},
        {"n_modes": 2, "posterior_probability": 0.1, "akaike_weight": 0.2},
        {"n_modes": 3, "posterior_probability": 0.3, "akaike_weight": 0.3}]}
    a = cb.count_answer(fit)
    assert a.n_modes == 2 and a.ambiguous and a.runner_up_p == pytest.approx(0.3 / 1.0)
    assert not cb.count_answer({**fit, "count_ambiguous": False}).ambiguous
    assert cb.count_answer({"best_label": None}).n_modes is None


def test_serial_run_stops_launching_before_the_cap_minus_the_grace(monkeypatch):
    def slow_case(task):
        time.sleep(0.25)
        return cb.CaseResult(case_id=task[0].case_id)
    monkeypatch.setattr(cb, "run_case", slow_case)
    truths = [_truth(index=i, modes=(), kind="empty") for i in range(6)]
    # Cap 2 s, grace min(300, 1) = 1 s: no case starts at or after 1 s, so four 0.25 s cases at most.
    results, info = cb.run_benchmark(truths, base_seed=1, n_workers=1, budget=TINY, deadline_s=2.0)
    assert info.stopped_by_deadline and 3 <= info.n_completed <= 4


def test_benchmark_stops_at_the_deadline_and_says_so():
    results, info = cb.run_benchmark([_truth()], base_seed=1, n_workers=1, budget=TINY,
                                     deadline_s=0.0)
    assert results == [] and info.stopped_by_deadline and info.n_requested == 1
    with pytest.raises(ValueError):
        cb.run_benchmark([], base_seed=1, n_workers=0, budget=TINY)
    assert (cb.TUNING_MAX_CASES, cb.TUNING_MAX_SECONDS, cb.TUNING_WORKERS) == (60, 1800, 16)


# --------------------------------------------------------------------------- end to end (tiny)
def test_tiny_run_goes_through_fit_one_and_writes_figures_and_cases(monkeypatch, tmp_path):
    _stub_reference(monkeypatch, prefer_n=True)       # the reference is exercised by the slow test
    # fit_one takes its ceiling from the environment (no argument yet): ceiling 1 keeps the ladder
    # to counts 0 and 1, which is all this harness test needs, and is seconds instead of minutes.
    monkeypatch.setenv(cb.MODE_CEILING_ENV, "1")
    truths = [cb.generate_case(11, cb.STREAM_CASE_EVAL, i, 1) for i in range(3)]
    used = []
    real = cb.fit_observation
    monkeypatch.setattr(cb, "fit_observation", lambda *a, **k: used.append(1) or real(*a, **k))
    results, info = cb.run_benchmark(truths, base_seed=11, n_workers=1, budget=TINY, n_ab_draws=50)
    assert len(results) == 3 == len(used) and info.n_completed == 3
    summary = cb.summarize(results)
    assert [f["name"] for f in summary["figures"]] == [
        "correct_counts_on_resolvable", "confident_extra_on_empty_or_single",
        "ab_share_at_or_below_60pct"]
    assert all(r.status in ("ok", "no_answer") for r in results)
    figures, cases = cb.write_outputs(tmp_path / "out", results, summary, {"note": "test"})
    assert json.loads(figures.read_text(encoding="utf-8"))["summary"]["n_cases"] == 3
    with open(cases, encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 3


@pytest.mark.slow
def test_real_reference_prefers_the_true_count():
    truth = _truth(modes=(cb.ModeTruth(8.0, 0.3, 600.0),), elastic_height=500.0)
    obs = cb.draw_observation(truth, cb.rng_for(3, cb.STREAM_DRAW_REFERENCE, 0))
    b = cb.ReferenceBudget(evidence="nested", n_live=100, random_starts=1, max_iter=40000)  # the sampler gate wants ESS >= 100
    rng = cb.rng_for(3, cb.STREAM_REF_SEARCH)
    z1 = cb.reference_ln_evidence(obs, 1, [(8.0, 0.3)], b, rng)
    z0 = cb.reference_ln_evidence(obs, 0, [(8.0, 0.3)], b, rng)
    assert z1 is not None and z0 is not None and z1 - z0 > 50          # an obvious extra mode


@pytest.mark.slow
def test_the_worker_pool_runs_cases_with_explicit_workers(monkeypatch):
    monkeypatch.setenv(cb.MODE_CEILING_ENV, "1")      # inherited by the spawned workers
    empties = [_truth(modes=(), kind="empty", index=i) for i in range(2)]
    results, info = cb.run_benchmark(empties, base_seed=3, n_workers=2, budget=TINY, n_ab_draws=20)
    assert info.n_completed == 2 and [r.index for r in results] == [0, 1]


@pytest.mark.slow
def test_the_pool_kills_in_flight_work_at_the_cap_not_after_it(monkeypatch):
    monkeypatch.setattr(cb, "HARD_KILL_GRACE_S", 1.0)
    truths = [cb.generate_case(1, cb.STREAM_CASE_EVAL, 0, 3)] * 2          # minutes of sampling each
    big = cb.ReferenceBudget(draws=3, null_draws=3, evidence="nested", n_live=100)
    results, info = cb.run_benchmark(truths, base_seed=1, n_workers=2, budget=big, deadline_s=2.0)
    assert info.killed_in_flight == 2 and results == []
    assert info.wall_clock_s < 2.6                                          # the cap, not cap + grace


@pytest.mark.slow
def test_a_run_past_the_deadline_kills_workers_and_reports(monkeypatch):
    monkeypatch.setattr(cb, "HARD_KILL_GRACE_S", 0.0)
    truths = [cb.generate_case(1, cb.STREAM_CASE_EVAL, 0, 3)] * 2          # minutes of sampling each
    big = cb.ReferenceBudget(draws=3, null_draws=3, evidence="nested", n_live=100)
    results, info = cb.run_benchmark(truths, base_seed=1, n_workers=2, budget=big, deadline_s=2.0)
    assert info.stopped_by_deadline and info.killed_in_flight == 2 and results == []
    assert info.wall_clock_s < 60

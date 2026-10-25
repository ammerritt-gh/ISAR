"""Routine tests of the fast-pass benchmark (isar/synth/count_bench.py).

Everything here runs on canned fast-pass records and a fake ``fit_observation``: no real fast-pass
fit (the benchmark itself runs once, deliberately, from ``run-isar-count-bench.bat``). The one real
fit is the refit diagnostic's single ``fit_hypothesis``. Figures are targets, never asserted (R15).
"""
import csv
import json
import math
import time

import numpy as np
import pytest

import isar
from isar.analyze.components import PhononDHO
from isar.analyze.lineshapes import KB
from isar.synth import count_bench as cb


def _truth(**kw):
    base = dict(stream=cb.STREAM_CASE_EVAL, index=0, kind="single",
                modes=(cb.ModeTruth(6.0, 0.3, 400.0),), elastic_height=80.0, background=12.0,
                T=100.0, exposure=1.0, gauss_fwhm=cb.curve_of("a06").fwhm, lorentz_fwhm=0.0, ceiling=3,
                resolution="a06", level="high")
    base.update(kw)
    return cb.CaseTruth(**base)


def _peak(omega0, confident=True, delta=False, index=0):
    return {"index": index, "omega0": omega0, "Gamma": 0.3, "delta": delta, "height": 100.0,
            "confident": confident}


def _record(peaks, state="resolved", flags=(), preferred=None):
    n = len(peaks) if preferred is None else preferred
    return {"schema": "isar.fast-pass/1", "state": state, "preferred_count": n,
            "count_probabilities": {str(n): 0.9}, "peaks": peaks, "flags": list(flags),
            "elastic_height": 80.0, "background": 12.0}


def test_the_tests_import_this_worktree():
    # a job worktree must not silently import a sibling checkout's isar
    assert cb.__file__.startswith(str(isar.__path__[0]))


# --------------------------------------------------------------------------- generator
def test_generator_is_seeded_covers_every_kind_and_respects_ceiling(monkeypatch):
    a = [cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 4) for i in range(200)]
    assert a == [cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 4) for i in range(200)]
    assert a != [cb.generate_case(7, cb.STREAM_CASE_TUNING, i, 4) for i in range(200)]
    assert a != [cb.generate_case(8, cb.STREAM_CASE_EVAL, i, 4) for i in range(200)]
    assert set(cb.KIND_WEIGHTS) <= {c.kind for c in a}
    assert {c.level for c in a} == {"high", "low"} and {c.resolution for c in a} <= set(cb.ANALYZERS)
    assert max(c.n_modes for c in a) <= 4 and min(c.n_modes for c in a) == 0
    assert max(cb.generate_case(7, cb.STREAM_CASE_EVAL, i, 1).n_modes for i in range(60)) == 1
    assert any(c.background == 0.0 for c in a) and any(c.background > 0.0 for c in a)
    monkeypatch.setenv(cb.MODE_CEILING_ENV, "2")
    assert cb.mode_ceiling() == 2 and cb.mode_ceiling(5) == 5
    monkeypatch.delenv(cb.MODE_CEILING_ENV)
    assert cb.mode_ceiling() == cb.DEFAULT_MODE_CEILING == 6


def test_clean_truths_show_every_peak_clearly():
    clean = [cb.generate_case(3, cb.STREAM_CASE_EVAL, cb.CLEAN_INDEX0 + i, 6, kind="clean") for i in range(8)]
    for t in clean:
        assert t.kind in ("clean", "single") and 1 <= t.n_modes <= 3
        assert min(g for g, _ in cb._grades_only(t)) >= cb.CLEAN_GRADE_MIN
        assert all(m.gamma <= 0.5 for m in t.modes)


def test_streams_are_distinct_and_independent():
    assert len(set(cb.STREAMS)) == len(cb.STREAMS) and cb.STREAM_DRAW_EVAL == 8
    firsts = {s: cb.rng_for(1, s, 5, 5).random() for s in cb.STREAMS}
    assert len(set(firsts.values())) == len(cb.STREAMS)


def test_named_cases_are_the_fourteen_fixed_truths():
    named = cb.named_cases(1)
    assert len(named) == 14 and len({t.case_id for t in named}) == 14
    assert named == cb.named_cases(1)                       # fixed: no stream, no draw
    by = {t.case_id: t for t in named}
    assert [m.omega0 for m in by["clean_three_high"].modes] == [4.0, 9.5, 14.0]
    assert [m.omega0 for m in by["close_doublet_low"].modes] == [4.0, 9.3, 10.0, 14.0]
    assert by["zero_background_high"].background == 0.0 and by["clean_three_high"].background > 0
    assert by["clean_three_high"].modes[0].height == 600.0 and by["clean_three_low"].modes[0].height == 300.0
    assert (by["soft_high"].T, by["soft_low"].T) == (302.0, 97.0)
    soft = by["soft_low"].modes[by["soft_low"].planted]
    assert (soft.omega0, soft.gamma, soft.height) == (6.0, 4.0, 15.0)
    assert by["shoulder_high"].modes[by["shoulder_high"].planted].omega0 == 10.7
    assert by["clean_three_high"].planted is None
    # the soft-mode pair's analyzer is the shoulder case's own draw; both levels of a case share one
    assert by["clean_three_high"].resolution == by["clean_three_low"].resolution


def test_each_modes_component_peaks_at_its_height():
    """h is the maximum of the convolved mode over E >= 0 (C-dho), so the truth's height is the
    component's own maximum; an overdamped-ish soft mode included."""
    for truth in (cb.named_cases(1)[13], cb.generate_case(5, cb.STREAM_CASE_EVAL, 2, 6, kind="separated")):
        comp, theta, ctx = cb._truth_model(truth)
        x = np.arange(0.0, 20.0, 0.01)
        parts = comp.evaluate_components(x, theta, ctx)
        for i, m in enumerate(truth.modes):
            assert parts[i][1].max() == pytest.approx(m.height, rel=2e-3)   # evaluate() is linear between 0.05 meV nodes: up to dx^2/(8 sigma^2) = 1.3e-3 low


def test_antistokes_partner_is_derived_by_detailed_balance():
    truth = _truth(elastic_height=0.0, background=0.0, T=100.0)
    comp, theta, ctx = cb._truth_model(truth)
    assert [type(c) for c in comp.components[:1]] == [PhononDHO]
    assert all(c.peak == "stokes+astokes" for c in comp.components if isinstance(c, PhononDHO))
    assert not any("astokes" in n.lower() for n in comp.param_names())     # no free partner
    phonon = comp.evaluate_components(ctx.x, theta, ctx)[0][1]
    ratio = phonon[ctx.x < 0].sum() / phonon[ctx.x > 0].sum()
    assert ratio == pytest.approx(np.exp(-6.0 / (KB * 100.0)), rel=0.05)


def test_the_context_carries_the_curve_and_its_fwhm():
    obs = cb.draw_observation(_truth(), cb.rng_for(1, cb.STREAM_DRAW_FIT, 0))
    ctx = cb.context_of(obs)
    curve = cb.curve_of("a06")
    assert ctx.resolution_curve is curve and ctx.gauss_fwhm == curve.fwhm and ctx.lorentz_fwhm == 0.0
    assert np.all(ctx.exposure_scale == 1.0) and ctx.exposure_reference == 1.0
    assert np.all(obs.counts >= 0) and np.all(obs.counts == np.round(obs.counts))   # Poisson, no floor
    assert obs.x.size == 181 and obs.x[0] == -16.0 and obs.x[-1] == pytest.approx(20.0)


# --------------------------------------------------------------------------- grade, refit, matching
def test_deviance_and_a_peaks_grade():
    assert cb.deviance([2.0, 1.0], [1.0, 1.0]) == pytest.approx(2 * (2 * math.log(2) - 1), abs=1e-9)
    assert cb.deviance([0.0, 3.0], [5.0, 3.0]) == 0.0                # mu = 0 terms are dropped
    truth = _truth(modes=(cb.ModeTruth(5.0, 0.3, 300.0), cb.ModeTruth(11.0, 0.3, 100.0)))
    full, per = cb._mode_lambdas(truth)
    by_hand = 2 * sum(m * math.log(m / n) - (m - n) for m, n in zip(full, full - per[1]) if m > 0)
    assert cb._grades_only(truth)[1][0] == pytest.approx(by_hand, rel=1e-9)
    assert full == pytest.approx(cb.truth_lambda(truth))


def test_refit_tells_a_resolved_peak_from_one_a_neighbour_absorbs():
    fwhm = cb.curve_of("a06").fwhm
    for sep, absorbed in ((0.5, True), (5.0, False)):
        truth = _truth(modes=(cb.ModeTruth(6.0, 0.3, 400.0), cb.ModeTruth(6.0 + sep * fwhm, 0.3, 400.0)))
        grade, refit = cb.visibility_grades(truth)[1]
        assert (refit < cb.REFIT_SHARE_MIN * grade) is absorbed, (sep, grade, refit)


def test_matching_is_one_to_one_and_not_greedy():
    # greedy nearest-first takes 6.0<->5.9 and leaves 5.0 without a partner within 1.2
    assert sorted(cb.match_peaks([5.0, 6.0], [5.9, 6.8], 1.2)) == [(0, 0), (1, 1)]
    assert cb.match_peaks([5.0], [7.0], 1.2) == [] and cb.match_peaks([], [1.0], 1.2) == []
    assert len(cb.match_peaks([5.0, 5.2], [5.1], 1.2)) == 1          # one fitted peak cannot match two


def test_should_find_needs_every_condition():
    f = 1.1
    clear = cb.ModeTruth(6.0, 0.3, 300.0)
    assert cb.should_find(clear, 120.0, 100.0, "high", f)
    assert not cb.should_find(clear, 80.0, 70.0, "high", f) and cb.should_find(clear, 80.0, 70.0, "low", f)
    assert not cb.should_find(clear, 24.0, 24.0, "low", f)                      # below grade 25: never a miss
    assert not cb.should_find(clear, 120.0, 20.0, "high", f)                    # absorbed by neighbours
    assert not cb.should_find(clear, 120.0, float("nan"), "high", f)
    assert not cb.should_find(cb.ModeTruth(2.0, 0.3, 300.0), 120.0, 100.0, "high", f)   # within 2 FWHM of zero
    assert not cb.should_find(cb.ModeTruth(6.0, 3.0, 300.0), 120.0, 100.0, "high", f)   # Gamma >= omega0 / 2


# --------------------------------------------------------------------------- scoring (canned records)
GRADES = ((500.0, 400.0), (500.0, 400.0))


def _two_peak_truth():
    return _truth(modes=(cb.ModeTruth(5.0, 0.3, 300.0), cb.ModeTruth(12.0, 0.3, 300.0)))


def test_score_case_gives_every_should_find_peak_one_outcome():
    truth = _two_peak_truth()
    # peak 0 matched confident; peak 1 matched tentative; a confident fitted peak far from both is false
    rec = _record([_peak(5.1, True, index=0), _peak(12.2, False, index=1), _peak(8.0, True, index=2)])
    true_rows, fit_rows = cb.score_case(truth, rec, "ok", GRADES)
    assert [r["outcome"] for r in true_rows] == ["confident", "tentative_or_abstained"]
    assert true_rows[1]["status"] == "tentative" and true_rows[0]["abs_delta_omega0"] == pytest.approx(0.1)
    assert [r["false"] for r in fit_rows] == [False, False, True]
    assert [r["matched_true"] for r in fit_rows] == [0, 1, None]
    # nothing matched: both missed
    assert [r["outcome"] for r in cb.score_case(truth, _record([_peak(8.0)]), "ok", GRADES)[0]] == ["missed"] * 2
    # a delta mode matches like any other peak
    assert cb.score_case(truth, _record([_peak(5.0, True, delta=True)]), "ok", GRADES)[0][0]["outcome"] == "confident"


def test_a_fit_without_a_record_is_tentative_or_abstained_with_its_status():
    truth = _two_peak_truth()
    for status in ("fit_error", "no_answer"):
        true_rows, fit_rows = cb.score_case(truth, None, status, GRADES)
        assert [(r["outcome"], r["status"]) for r in true_rows] == [("tentative_or_abstained", status)] * 2
        assert fit_rows == []


def test_peaks_that_are_not_should_find_are_not_scored():
    truth = _two_peak_truth()
    rows, _ = cb.score_case(truth, _record([]), "ok", ((500.0, 400.0), (500.0, 5.0)))   # second one absorbed
    assert [(r["should_find"], r["outcome"]) for r in rows] == [(True, "missed"), (False, "not_scored")]


def test_flags_text_and_the_planted_flag_hit():
    truth = _truth(modes=(cb.ModeTruth(4.0, 0.3, 300.0), cb.ModeTruth(10.7, 0.4, 24.0)), planted=1)
    flag = {"kind": "ambiguous_decomposition", "e_lo": 9.0, "e_hi": 9.5, "statistic": 1.0, "detail": ""}
    assert cb.flags_text([flag]) == "ambiguous_decomposition 9.00-9.50 meV"
    assert cb.flag_hit(truth, [{**flag, "e_hi": 9.7}]) is True  # 10.7 lies within 9.7 + 1.126 FWHM
    assert cb.flag_hit(truth, [flag]) is False                   # 9.5 + 1.126 falls short
    assert cb.flag_hit(truth, [{**flag, "e_hi": 8.0}]) is False and cb.flag_hit(truth, []) is False
    assert cb.flag_hit(_truth(), [flag]) is None


def test_summarize_keeps_every_should_find_peak_in_the_denominator():
    def row(outcome, sf=True):
        return {"should_find": sf, "outcome": outcome}
    R = cb.CaseResult
    results = [
        R(case_id="a", level="high", kind="clean", state="resolved", seconds=10.0, false_flag=False,
          true_peaks=[row("confident"), row("missed"), row("not_scored", False)],
          fitted_peaks=[{"confident": True, "false": False}, {"confident": True, "false": True},
                        {"confident": False, "false": False}]),
        R(case_id="b", level="low", kind="shoulder", state="close_call", seconds=30.0, flag_hit=True,
          true_peaks=[row("tentative_or_abstained")], fitted_peaks=[]),
        R(case_id="c", level="low", kind="clean", status="fit_error", false_flag=None,
          true_peaks=[row("tentative_or_abstained")], fitted_peaks=[]),
        R(case_id="d", level="low", kind="clean", state="unresolved", false_flag=True, true_peaks=[]),
    ]
    s = cb.summarize(results)
    fig = {(f["name"], f["level"]): f for f in s["figures"]}
    missed = fig["missed_share", "all"]
    assert (missed["numerator"], missed["denominator"]) == (1, 4)          # 4 should-find peaks, 1 missed
    assert fig["missed_share", "low"]["denominator"] == 2 and fig["missed_share", "high"]["numerator"] == 1
    assert (fig["false_share", "all"]["numerator"], fig["false_share", "all"]["denominator"]) == (1, 2)
    assert (fig["flag_hit_rate", "all"]["numerator"], fig["flag_hit_rate", "all"]["denominator"]) == (1, 1)
    assert (fig["false_flag_rate_clean", "all"]["numerator"], fig["false_flag_rate_clean", "all"]["denominator"]) == (1, 2)
    assert fig["state_close_call", "all"]["numerator"] == 1 and fig["state_no_record", "all"]["numerator"] == 1
    lo, hi = missed["wilson95"]
    assert 0 < lo < 0.25 < hi < 1 and missed["target"] == cb.TARGET_MISSED
    assert s["time"]["all"]["median_s"] == 20.0 and s["n_failed_fits"] == 1


# --------------------------------------------------------------------------- isolation, outputs, settings
def _fake_fit(record, seen=None):
    def fake(obs, *, seed, errors_tier, ceiling, settings=None):
        if seen is not None:
            seen.append(obs)
        return {"fast_pass": record, "fast_pass_timing": {"total_seconds": 1.5}}
    return fake


def test_the_fitter_sees_an_observation_never_a_truth(monkeypatch):
    assert {f for f in cb.Observation.__dataclass_fields__} == {
        "x", "counts", "T", "gauss_fwhm", "lorentz_fwhm", "exposure", "resolution"}
    seen = []
    monkeypatch.setattr(cb, "fit_observation", _fake_fit(_record([]), seen))
    res = cb.run_case((_truth(modes=(), kind="empty"), 0, 1, cb.STREAM_DRAW_FIT, "cheap", None))
    assert len(seen) == 1 and type(seen[0]) is cb.Observation and res.status == "ok"
    assert res.state == "resolved" and res.seconds == 1.5 and res.p_preferred == 0.9


def test_a_fit_error_stays_in_the_results(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no convergence")
    monkeypatch.setattr(cb, "fit_observation", boom)
    monkeypatch.setattr(cb, "visibility_grades", lambda t: ((500.0, 400.0),))
    res = cb.run_case((_truth(), 0, 1, cb.STREAM_DRAW_FIT, "cheap", None))
    assert res.status == "fit_error" and "no convergence" in res.note and res.record is None
    assert res.true_peaks[0]["outcome"] == "tentative_or_abstained"


def test_a_run_writes_the_outputs_and_the_record_verbatim(monkeypatch, tmp_path):
    canned = _record([_peak(6.0)], flags=[{"kind": "x", "e_lo": 5.0, "e_hi": 7.0, "statistic": 1.0, "detail": ""}])
    monkeypatch.setattr(cb, "fit_observation", _fake_fit(canned))
    monkeypatch.setattr(cb, "visibility_grades", lambda t: ((500.0, 400.0),) * t.n_modes)
    tasks = [(_truth(index=0), 0), (_truth(index=1, kind="clean"), 0)]
    results, info = cb.run_benchmark(tasks, base_seed=1, n_workers=1, draw_stream=cb.STREAM_DRAW_EVAL)
    assert info.n_completed == 2 and [r.index for r in results] == [0, 1]
    summary = cb.summarize(results)
    paths = cb.write_outputs(tmp_path / "out", results, summary, {"note": "test"})
    assert set(paths) == {"figures.json", "cases.csv", "true_peaks.csv", "fitted_peaks.csv", "records.jsonl"}
    assert json.loads(paths["figures.json"].read_text(encoding="utf-8"))["summary"]["n_spectra"] == 2
    for name, n in (("cases.csv", 2), ("true_peaks.csv", 2), ("fitted_peaks.csv", 2)):
        with open(paths[name], encoding="utf-8", newline="") as fh:
            assert len(list(csv.DictReader(fh))) == n
    with open(paths["cases.csv"], encoding="utf-8", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["state"] == "resolved" and row["n_confident"] == "1" and row["flags"] == "x 5.00-7.00 meV"
    lines = [json.loads(s) for s in paths["records.jsonl"].read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and all(line["fast_pass"] == canned for line in lines)
    assert lines[0]["truth"]["modes"][0]["omega0"] == 6.0 and lines[0]["timing"] == {"total_seconds": 1.5}


def test_a_record_less_spectrum_says_why_in_records_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "fit_observation", _fake_fit(None))
    results, _ = cb.run_benchmark([(_truth(modes=(), kind="empty"), 0)], base_seed=1, n_workers=1)
    line = json.loads(cb.write_outputs(tmp_path, results, cb.summarize(results), {})["records.jsonl"]
                      .read_text(encoding="utf-8"))
    assert line["fast_pass"] is None and line["status"] == "no_answer"


def test_settings_reach_the_fits_only_through_the_card_tables():
    from dataclasses import asdict
    from isar.session import guidelines
    settings = cb.load_settings('{"fast_pass": {"seed_stop_gain": 4.0}, "prior": {"amplitude_margin": 3.0}}')
    card = cb._card(3, settings)
    assert card.policy.fitting["fast_pass"] == {"seed_stop_gain": 4.0}
    assert card.policy.fitting["prior"] == {"amplitude_margin": 3.0} and card.policy.fitting["max_modes"] == 3
    assert guidelines.mode_ceiling(card) == 3
    meta = cb.settings_meta(3, settings)
    assert meta["fast_pass_settings"] == asdict(guidelines.fast_pass_settings(card))
    assert len(meta["settings_sha256"]) == 64 and meta["settings_override"] == settings
    assert "fast_pass" not in cb._card(3, None).policy.fitting and cb.load_settings(None) is None
    with pytest.raises(ValueError):
        cb.load_settings('{"fastpass": {}}')


def test_load_settings_reads_a_json_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"prior": {"amplitude_margin": 2.0}}', encoding="utf-8")
    assert cb.load_settings(str(p)) == {"prior": {"amplitude_margin": 2.0}}


# --------------------------------------------------------------------------- streams and the pool
def test_development_and_evaluation_never_share_a_stream():
    dev, dev_draws = cb.plan_tasks("development", 5, named_draws=2, random=8, clean=4)
    ev, ev_draws = cb.plan_tasks("evaluation", 5, named_draws=2, random=8, clean=4)
    assert dev_draws == cb.STREAM_DRAW_FIT and ev_draws == cb.STREAM_DRAW_EVAL
    assert len(dev) == len(ev) == 14 * 2 + 8 + 4
    named = lambda ts: [t for t, d in ts if t.stream == cb.STREAM_NAMED]   # noqa: E731
    assert named(dev) == named(ev)                                          # the same fixed truths
    dev_random = [t for t, _ in dev if t.stream != cb.STREAM_NAMED]
    ev_random = [t for t, _ in ev if t.stream != cb.STREAM_NAMED]
    assert {t.stream for t in dev_random} == {cb.STREAM_CASE_TUNING}
    assert {t.stream for t in ev_random} == {cb.STREAM_CASE_EVAL}
    assert not set(dev_random) & set(ev_random)
    clean = [t for t in ev_random if t.index >= cb.CLEAN_INDEX0]
    assert len(clean) == 4 and all(t.kind in ("clean", "single") for t in clean)
    truth = dev[0][0]
    assert cb.observe(truth, 0, 5, dev_draws).counts.tolist() != cb.observe(truth, 0, 5, ev_draws).counts.tolist()
    assert cb.observe(truth, 0, 5, dev_draws).counts.tolist() == cb.observe(truth, 0, 5, dev_draws).counts.tolist()
    assert cb.observe(truth, 0, 5, dev_draws).counts.tolist() != cb.observe(truth, 1, 5, dev_draws).counts.tolist()


def test_serial_run_stops_launching_before_the_cap_minus_the_grace(monkeypatch):
    def slow_case(job):
        time.sleep(0.25)
        return cb.CaseResult(case_id=job[0].case_id)
    monkeypatch.setattr(cb, "run_case", slow_case)
    tasks = [(_truth(index=i, modes=(), kind="empty"), 0) for i in range(6)]
    # Cap 2 s, grace min(300, 1) = 1 s: no case starts at or after 1 s, so four 0.25 s cases at most.
    results, info = cb.run_benchmark(tasks, base_seed=1, n_workers=1, deadline_s=2.0)
    assert info.stopped_by_deadline and 3 <= info.n_completed <= 4


def test_benchmark_stops_at_the_deadline_and_says_so():
    results, info = cb.run_benchmark([(_truth(), 0)], base_seed=1, n_workers=1, deadline_s=0.0)
    assert results == [] and info.stopped_by_deadline and info.n_requested == 1
    with pytest.raises(ValueError):
        cb.run_benchmark([], base_seed=1, n_workers=0)


@pytest.mark.slow
def test_the_worker_pool_runs_cases_with_explicit_workers():
    empties = [(_truth(modes=(), kind="empty", index=i), 0) for i in range(2)]
    results, info = cb.run_benchmark(empties, base_seed=3, n_workers=2)
    assert info.n_completed == 2 and [r.index for r in results] == [0, 1]


@pytest.mark.slow
def test_the_pool_kills_in_flight_work_at_the_cap_not_after_it(monkeypatch):
    monkeypatch.setattr(cb, "HARD_KILL_GRACE_S", 1.0)
    tasks = [(cb.named_cases(1)[2], 0)] * 2              # a four-peak truth: minutes of fitting each
    results, info = cb.run_benchmark(tasks, base_seed=1, n_workers=2, deadline_s=2.0)
    assert info.killed_in_flight == 2 and results == []
    assert info.wall_clock_s < 2.6                       # the cap, not cap + grace

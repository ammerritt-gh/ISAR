"""Tests for the Monte-Carlo validation gate (score.py).

The fast tests inject fake FitResults / hand-build CellScores so the statistics and
gate logic are verified without running real fits. Slow tests (real fitting) are
marked and use a tiny config.
"""
import numpy as np
import pytest

from isar.synth import config, score


def _fr(omega0, omega0_err, gamma, gamma_err, *, success=True, gamma_at_floor=False):
    return config.FitResult(
        estimator="baker_cousins", success=success, covariance_ok=True,
        gamma_at_floor=gamma_at_floor,
        omega0=omega0, omega0_err=omega0_err, gamma=gamma, gamma_err=gamma_err,
    )


# ----------------------------------------------------------------- summarize_cell
def test_summarize_basic_stats():
    res = [_fr(5.9, 0.1, 0.5, 0.05), _fr(6.0, 0.1, 0.5, 0.05), _fr(6.1, 0.1, 0.5, 0.05)]
    cs = score.summarize_cell(2.5, 100.0, "baker_cousins", "midzone", res, 6.0, 0.5)
    assert cs.n_total == 3 and cs.n_success == 3 and cs.success_rate == 1.0
    assert np.isclose(cs.omega0_bias_mean, 0.0)
    assert np.isclose(cs.omega0_pull_mean, 0.0)
    assert np.isclose(cs.omega0_pull_std, 1.0)          # pulls are [-1, 0, +1], std(ddof=1)=1
    assert np.isclose(cs.median_omega0_err, 0.1)
    assert cs.n_gamma_used == 3 and cs.gamma_floored_fraction == 0.0


def test_summarize_excludes_failed_from_omega0_stats():
    res = [_fr(6.0, 0.1, 0.5, 0.05),
           _fr(np.nan, np.nan, np.nan, np.nan, success=False),
           _fr(6.0, 0.1, 0.5, 0.05),
           _fr(np.nan, np.nan, np.nan, np.nan, success=False)]
    cs = score.summarize_cell(2.5, 100.0, "baker_cousins", "midzone", res, 6.0, 0.5)
    assert cs.n_total == 4 and cs.n_success == 2 and cs.success_rate == 0.5
    assert np.isclose(cs.omega0_bias_mean, 0.0)         # over the 2 survivors only
    assert np.isfinite(cs.omega0_pull_mean)


def test_summarize_gamma_floor_censoring():
    # 4 normal + 2 floored, all successful. Γ-bias over ALL 6; Γ-pull over the 4 non-floored.
    res = [_fr(6.0, 0.1, 0.5, 0.05) for _ in range(4)]
    res += [_fr(6.0, 0.1, 1e-3, 0.5, gamma_at_floor=True) for _ in range(2)]
    cs = score.summarize_cell(2.5, 30.0, "baker_cousins", "midzone", res, 6.0, 0.5)
    assert cs.n_success == 6
    assert cs.n_gamma_used == 4
    assert np.isclose(cs.gamma_floored_fraction, 2 / 6)
    # Γ-pull over the 4 non-floored: all (0.5-0.5)/0.05 = 0.
    assert np.isclose(cs.gamma_pull_mean, 0.0)
    # Γ-bias INCLUDES the floored fits → pulled below truth (negative), NOT zero.
    expected_bias = np.mean([0.5, 0.5, 0.5, 0.5, 1e-3, 1e-3]) - 0.5
    assert np.isclose(cs.gamma_bias_mean, expected_bias)
    assert cs.gamma_bias_mean < -0.1


def test_summarize_empty_cell_is_graceful():
    res = [_fr(np.nan, np.nan, np.nan, np.nan, success=False) for _ in range(5)]
    cs = score.summarize_cell(2.05, 15.0, "baker_cousins", "boundary", res, 0.94, 0.5)
    assert cs.n_total == 5 and cs.n_success == 0 and cs.success_rate == 0.0
    assert cs.reliable_rate == 0.0
    assert np.isnan(cs.omega0_bias_mean) and np.isnan(cs.omega0_pull_std)


def test_summarize_flags_untrustworthy_sigma_outlier():
    # 9 normal + 1 degenerate fit reporting σ ~100x too small (the Γ→0 corner).
    res = [_fr(6.0, 0.1, 0.5, 0.05) for _ in range(9)]
    res += [_fr(6.0, 0.001, 0.7, 0.0005)]   # σ catastrophically small; Γ way off
    cs = score.summarize_cell(2.5, 15.0, "baker_cousins", "midzone", res, 6.0, 0.5)
    assert cs.n_success == 10            # it converged
    assert cs.n_reliable == 9            # but is counted as a FAILURE (untrustworthy)
    assert cs.n_untrustworthy == 1
    assert np.isclose(cs.reliable_rate, 0.9)
    # Its huge Γ-pull (~400) is excluded → calibration over reliable fits stays clean.
    assert cs.gamma_pull_std == 0.0 or np.isnan(cs.gamma_pull_std)
    assert abs(cs.gamma_bias_mean) < 1e-9   # reliable γ all = 0.5


# ------------------------------------------------------------- seeding determinism
def test_datacell_ordinals_canonical_and_estimator_independent():
    cfg = config.default_run_config(dev_mode=True)
    m1 = score.datacell_ordinal_map(cfg)
    m2 = score.datacell_ordinal_map(cfg)
    assert m1 == m2                                   # deterministic
    cells = list(m1)
    assert cells == sorted(cells)                     # canonical (sorted by (H, target))
    # ordinals are 0..N-1 with no gaps; map keys are (H, target) only (no estimator).
    assert sorted(m1.values()) == list(range(len(m1)))
    assert all(isinstance(k, tuple) and len(k) == 2 for k in m1)


def test_realization_seed_formula():
    assert score.realization_seed(12345, ordinal=3, M=1000, i=7) == 12345 + 3 * 1000 + 7


# ------------------------------------------------------------ check_pass_criteria
def _midzone(**over):
    base = dict(H=2.5, target_peak_counts=100.0, estimator="baker_cousins",
                classification="midzone", n_total=1000, n_success=1000, success_rate=1.0,
                n_reliable=1000, n_untrustworthy=0, reliable_rate=1.0,
                median_omega0_err=0.05, omega0_bias_mean=0.02, gamma_bias_mean=0.02,
                omega0_pull_mean=0.0, omega0_pull_std=1.0, gamma_pull_mean=0.0,
                gamma_pull_std=1.0, n_gamma_used=1000, gamma_floored_fraction=0.0)
    base.update(over)
    return config.CellScore(**base)


def _run(scores):
    return score.ScoreRun(cfg=config.default_run_config(dev_mode=True), scores=scores)


def test_characterize_documents_cells_without_passfail():
    rep = score.characterize_run(_run([_midzone()]), config.default_run_config())
    assert not hasattr(rep, "overall")          # no binary gate
    c = rep.cells[0]
    assert c.classification == "midzone" and c.well_counted is True
    assert np.isclose(c.reliable_rate, 1.0) and np.isclose(c.failure_rate, 0.0)


def test_characterize_only_reports_requested_estimator():
    rep = score.characterize_run(_run([_midzone(estimator="neyman"), _midzone()]),
                                 config.default_run_config())
    assert len(rep.cells) == 1 and rep.cells[0].classification == "midzone"


def test_data_threshold_identifies_min_counts():
    # low counts: unreliable + overconfident Γ; high counts: clean → threshold = 100.
    low = _midzone(target_peak_counts=15.0, reliable_rate=0.90, gamma_pull_std=1.5)
    hi = _midzone(target_peak_counts=100.0, reliable_rate=1.0, gamma_pull_std=1.0)
    thr = score.data_threshold(_run([low, hi]), config.default_run_config())
    assert thr["by_count"][15.0]["ok"] is False
    assert thr["by_count"][100.0]["ok"] is True
    assert thr["min_reliable_counts"] == 100.0


def test_data_threshold_none_when_never_reliable():
    low = _midzone(target_peak_counts=15.0, reliable_rate=0.5)
    thr = score.data_threshold(_run([low]), config.default_run_config())
    assert thr["min_reliable_counts"] is None


def test_threshold_excludes_boundary_cells():
    # boundary cell is unreliable but must not affect the midzone threshold.
    boundary = _midzone(H=2.05, classification="boundary", target_peak_counts=100.0,
                        reliable_rate=0.5, gamma_pull_std=3.0)
    mid = _midzone(target_peak_counts=100.0, reliable_rate=1.0)
    thr = score.data_threshold(_run([boundary, mid]), config.default_run_config())
    assert thr["by_count"][100.0]["ok"] is True
    assert thr["min_reliable_counts"] == 100.0


# --------------------------------------------------------------------- CSV I/O
def test_summary_csv_round_trip(tmp_path):
    scores = [_midzone(), _midzone(H=2.05, classification="boundary"),
              _midzone(estimator="neyman")]
    run = _run(scores)
    path = tmp_path / "summary.csv"
    score.write_summary(run, path)
    back = score.read_summary(path)
    assert len(back) == len(scores)
    for a, b in zip(scores, back):
        da, db = a.to_dict(), b.to_dict()
        assert set(da) == set(db)
        for k in da:
            va, vb = da[k], db[k]
            if isinstance(va, float) and np.isnan(va):
                assert isinstance(vb, float) and np.isnan(vb)
            elif isinstance(va, float):
                assert np.isclose(va, vb)
            else:
                assert va == vb


# --------------------------------------------------------------------- figures
def _fake_run_grid():
    scores = []
    for H in (2.05, 2.25, 2.5, 2.75, 2.95):
        cls = "boundary" if H in (2.05, 2.95) else "midzone"
        for t in (30.0, 100.0, 300.0):
            scores.append(_midzone(H=H, target_peak_counts=t, classification=cls,
                                   omega0_recovered_mean=6.0 * abs(np.sin(np.pi * (H - 2))),
                                   omega0_bias_std=0.05))
    return score.ScoreRun(cfg=config.default_run_config(), scores=scores)


def test_plot_capstone_writes_file(tmp_path):
    out = tmp_path / "capstone.png"
    assert score.plot_capstone(_fake_run_grid(), config.default_run_config(),
                               target=100.0, out_path=out) == out
    assert out.exists()


def test_plot_surfaces_writes_file(tmp_path):
    out = tmp_path / "surfaces.png"
    res = score.plot_surfaces(_fake_run_grid(), config.default_run_config(),
                              estimator="baker_cousins", out_path=out)
    assert out.exists()
    assert res == out


def test_plot_threshold_writes_file(tmp_path):
    out = tmp_path / "threshold.png"
    assert score.plot_threshold(_fake_run_grid(), config.default_run_config(),
                                out_path=out) == out
    assert out.exists()


# --------------------------------------------------------------- slow: real fits
@pytest.mark.slow
def test_score_cell_deterministic_tiny():
    cfg = config.default_run_config(dev_mode=True)
    a = score.score_cell(cfg, 2.5, 100.0, estimator="baker_cousins", datacell_ordinal=0)
    b = score.score_cell(cfg, 2.5, 100.0, estimator="baker_cousins", datacell_ordinal=0)
    assert a.to_dict() == b.to_dict()


@pytest.mark.slow
def test_serial_equals_parallel_tiny():
    cfg = config.RunConfig(H_values=(2.5,), target_counts_levels=(100.0,), M=6,
                           estimators=("baker_cousins",), dev_mode=True)
    s = score.run_scorer(cfg, n_workers=1, make_figures=False, write_csv=False)
    p = score.run_scorer(cfg, n_workers=2, make_figures=False, write_csv=False)
    sa = {(c.H, c.target_peak_counts, c.estimator): c.to_dict() for c in s.scores}
    pa = {(c.H, c.target_peak_counts, c.estimator): c.to_dict() for c in p.scores}
    assert sa == pa


@pytest.mark.slow
def test_dev_smoke_end_to_end(tmp_path):
    cfg = config.RunConfig(H_values=(2.25, 2.5), target_counts_levels=(100.0,), M=5,
                           estimators=("baker_cousins",), dev_mode=True)
    run = score.run_scorer(cfg, n_workers=1, make_figures=False, write_csv=False)
    assert len(run.scores) == 2
    rep = score.characterize_run(run, cfg)
    assert len(rep.cells) == 2
    assert "by_count" in rep.threshold

"""Tests for demo.py (§8 three-estimator demonstration).

Fast tests: Layer-1 algebra, closed-form identities, module invariants, figure smokes.
Slow tests (real DHO fits): Layer-2 end-to-end, normalization straddle, high-count agreement.
"""
import sys
import numpy as np
import pytest

from isar.analyze import estimators
from isar.synth import config, demo


# ----------------------------------------------------------------- Layer 1: algebra
def test_layer1_straddle_ordering():
    rng = np.random.default_rng(1)
    for _ in range(10):
        counts = rng.poisson(20.0, size=64).astype(float)
        # force all-positive draw
        counts = np.maximum(counts, 1.0)
        ne = estimators.constant_rate_estimate(counts, "neyman")
        bc = estimators.constant_rate_estimate(counts, "baker_cousins")
        pe = estimators.constant_rate_estimate(counts, "pearson")
        assert ne <= bc <= pe


def test_layer1_bc_equals_arithmetic_mean():
    rng = np.random.default_rng(2)
    counts = rng.poisson(15.0, size=64).astype(float)
    bc = estimators.constant_rate_estimate(counts, "baker_cousins")
    assert np.isclose(bc, counts.mean())


def test_layer1_theorem_holds():
    rng = np.random.default_rng(3)
    counts = np.maximum(rng.poisson(20.0, size=64).astype(float), 1.0)
    assert demo._layer1_theorem_holds(counts)


def test_layer1_bias_directions_and_shrink():
    result = demo.run_layer1(n_channels=64, mu_levels=(5, 100), n_reps=600, seed=7)
    mu_arr = np.array(result.mu_levels, dtype=float)
    # at mu=5 (low), Neyman biases negative, Pearson positive
    assert result.bias["neyman"][0] < 0
    assert result.bias["pearson"][0] > 0
    # RELATIVE |bias/mu| shrinks as mu grows (Pearson absolute bias ~0.5 constant, but relative shrinks)
    for e in ("neyman", "pearson"):
        rel_low  = abs(result.bias[e][0]) / mu_arr[0]
        rel_high = abs(result.bias[e][1]) / mu_arr[1]
        assert rel_high < rel_low, f"{e} relative bias did not shrink: {rel_low:.4f} -> {rel_high:.4f}"
    # BC fractional bias is smallest at each level
    for i in range(len(result.mu_levels)):
        assert abs(result.bias["baker_cousins"][i] / mu_arr[i]) <= abs(result.bias["neyman"][i] / mu_arr[i]) + 1e-9
        assert abs(result.bias["baker_cousins"][i] / mu_arr[i]) <= abs(result.bias["pearson"][i] / mu_arr[i]) + 1e-9


def test_layer1_theorem_holds_flag_on_result():
    result = demo.run_layer1(n_channels=32, mu_levels=(20,), n_reps=50, seed=9)
    assert result.closed_form_matches_argmin is True


# --------------------------------------------------------------- module invariants
def test_estimator_constants_cover_registry():
    assert set(demo.ESTIMATOR_ORDER) == set(estimators.ESTIMATORS)
    for e in demo.ESTIMATOR_ORDER:
        assert e in demo.ESTIMATOR_LABEL
        assert e in demo.ESTIMATOR_COLOR


# --------------------------------------------------------------- figure smokes (fast)
def test_plot_layer1_smoke(tmp_path):
    result = demo.run_layer1(n_channels=32, mu_levels=(5, 20), n_reps=50, seed=11)
    out = tmp_path / "layer1.png"
    p = demo.plot_layer1(result, out_path=out)
    assert p == out
    assert out.exists() and out.stat().st_size > 0


# ----------------------------------------------------------- slow: real DHO fits
@pytest.mark.slow
def test_layer2_end_to_end_dev():
    cfg = config.default_run_config(dev_mode=True)
    l2 = demo.run_layer2(cfg, n_workers=1)
    assert l2.H == demo.DEMO_H
    # one amp_true and sas_true entry per target
    assert len(l2.amp_true) == len(cfg.target_counts_levels)
    assert len(l2.targets) == len(cfg.target_counts_levels)
    # all amp_true finite and positive
    assert np.all(np.isfinite(l2.amp_true) & (l2.amp_true > 0))
    # amp_bias and amp_bias_sem have right shape
    for e in demo.ESTIMATOR_ORDER:
        assert len(l2.amp_bias[e]) == len(cfg.target_counts_levels)
        assert len(l2.amp_bias_sem[e]) == len(cfg.target_counts_levels)


@pytest.mark.slow
def test_layer2_normalization_straddle_low_counts():
    # At 15 cts the normalization straddle is strongest (M=1000 gate: neyman=-14%, pearson>0).
    # Use M=80 to reduce sampling noise while keeping the test fast (~30 s).
    cfg = config.RunConfig(H_values=(2.5,), target_counts_levels=(15,),
                           M=80, estimators=demo.ESTIMATOR_ORDER, dev_mode=True)
    l2 = demo.run_layer2(cfg, n_workers=1)
    nb = l2.amp_bias["neyman"][0]
    bc_b = l2.amp_bias["baker_cousins"][0]
    pe = l2.amp_bias["pearson"][0]
    assert nb < bc_b < pe, f"straddle violated: neyman={nb:.4f} bc={bc_b:.4f} pearson={pe:.4f}"


@pytest.mark.slow
def test_layer2_high_count_agreement():
    # At 300 cts all three Gamma-bias values agree within 0.05 meV.
    cfg = config.RunConfig(H_values=(2.5,), target_counts_levels=(300,),
                           M=80, estimators=demo.ESTIMATOR_ORDER, dev_mode=True)
    l2 = demo.run_layer2(cfg, n_workers=1)
    biases = [l2.run.for_estimator(e)[0].gamma_bias_mean for e in demo.ESTIMATOR_ORDER]
    assert max(biases) - min(biases) < 0.05


@pytest.mark.slow
def test_plot_layer2_smokes(tmp_path):
    cfg = config.default_run_config(dev_mode=True)
    l2 = demo.run_layer2(cfg, n_workers=1)
    for fn, func in [
        ("gamma.png", demo.plot_gamma_bias),
        ("norm.png", demo.plot_normalization_bias),
        ("cal.png", demo.plot_calibration),
    ]:
        out = tmp_path / fn
        p = func(l2, out_path=out)
        assert p == out
        assert out.exists() and out.stat().st_size > 0


@pytest.mark.slow
def test_main_dev_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["demo", "--dev", "--workers", "1"])
    monkeypatch.setattr(config, "FIGURE_DIR", tmp_path)
    demo.main()
    expected = [
        "demo_layer1_constant_rate.png",
        "demo_dho_gamma_bias.png",
        "demo_dho_normalization_bias.png",
        "demo_dho_calibration.png",
    ]
    for fname in expected:
        assert (tmp_path / fname).exists(), f"missing {fname}"

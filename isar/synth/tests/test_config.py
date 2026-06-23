"""Tests for config data structures and defaults."""
import json

from isar.synth import config


def _example_ground_truth():
    return config.GroundTruth(
        H=2.5, omega0=6.0, gamma=0.5,
        phonon_amplitude=12.3, elastic_amplitude=456.7, BG=1.2,
        gauss_fwhm=0.961, lorentz_fwhm=0.596, T=100.0,
        x_min=-10.0, x_max=10.0, x_step=0.25,
        target_peak_counts=1000.0, seed=42, counts_scale=3.21,
    )


def test_ground_truth_json_round_trip():
    gt = _example_ground_truth()
    # to_dict must be JSON-serializable, and from_dict must reconstruct an equal object.
    restored = config.GroundTruth.from_dict(json.loads(json.dumps(gt.to_dict())))
    assert restored == gt


def test_ground_truth_defaults():
    gt = _example_ground_truth()
    assert gt.center == 0.0
    assert gt.peak == "all"


def test_default_run_config_full():
    cfg = config.default_run_config()
    assert cfg.M == 1000
    assert cfg.gamma == 0.5
    assert cfg.T == 100.0
    assert len(cfg.H_values) == 21
    assert tuple(cfg.target_counts_levels) == (15, 20, 30, 50, 100, 300)
    assert cfg.estimators[0] == "baker_cousins"  # production default first


def test_default_run_config_dev_mode_is_smaller():
    dev = config.default_run_config(dev_mode=True)
    full = config.default_run_config()
    assert dev.dev_mode is True
    assert dev.M < full.M
    assert len(dev.H_values) < len(full.H_values)


def test_fit_param_order():
    assert config.FIT_PARAM_ORDER == (
        "omega_d", "gamma", "phonon_amplitude", "elastic_amplitude", "BG",
    )


def test_out_dirs_under_repo_root():
    # out/ sits at the repo root, sibling of the isar/ package.
    assert config.OUT_DIR.name == "out"
    assert config.MANIFEST_DIR.parent == config.OUT_DIR
    assert config.FIGURE_DIR.parent == config.OUT_DIR

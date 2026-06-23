"""Tests for scan generation + ground-truth manifests."""
import numpy as np

from isar.synth import config, dispersion, generate, noise


def test_energy_grid_81_channels():
    cfg = config.default_run_config()
    x = generate.energy_grid(cfg)
    assert len(x) == 81
    assert (x[0], x[-1]) == (-10.0, 10.0)


def test_make_ground_truth_hits_target_peak_counts():
    cfg = config.default_run_config()
    target = 1000.0
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=target, seed=42)
    x = generate.energy_grid(cfg)
    h = noise.phonon_peak_height(x, gt.omega0, gt.gamma, gt.phonon_amplitude,
                                 gt.gauss_fwhm, gt.lorentz_fwhm, gt.T)
    assert np.isclose(h, target)


def test_make_ground_truth_records_truth():
    cfg = config.default_run_config()
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=500.0, seed=7)
    assert gt.omega0 == dispersion.omega0_of_H(2.5)
    assert gt.gamma == cfg.gamma
    assert gt.T == cfg.T
    assert gt.phonon_amplitude > 0
    assert gt.elastic_amplitude > 0
    assert gt.counts_scale is not None
    assert gt.target_peak_counts == 500.0
    assert gt.seed == 7


def test_make_ground_truth_elastic_height_ratio():
    # Elastic peak height should be elastic_to_phonon_height_ratio × target.
    cfg = config.default_run_config()
    target = 1000.0
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=target, seed=1)
    elastic_height = noise.convolute_voigt_dho(
        np.array([0.0]), gt.omega0, gt.center, gt.gauss_fwhm, gt.lorentz_fwhm,
        gt.gamma, 0.0, gt.elastic_amplitude, 0.0, gt.T, peak="elastic",
    )[0]
    assert np.isclose(elastic_height, cfg.elastic_to_phonon_height_ratio * target, rtol=1e-6)


def test_make_ground_truth_overrides_profile():
    # Per-scan overrides let us sweep the full data-profile space (Γ, T, elastic, BG).
    cfg = config.default_run_config()
    target = 1000.0
    gt = generate.make_ground_truth(
        cfg, H=2.5, target_peak_counts=target, seed=1,
        gamma=0.8, T=300.0,
        elastic_to_phonon_height_ratio=1.0, bg_to_phonon_height_ratio=0.1,
    )
    assert gt.gamma == 0.8
    assert gt.T == 300.0
    assert np.isclose(gt.BG, 0.1 * target)
    elastic_height = noise.convolute_voigt_dho(
        np.array([0.0]), gt.omega0, gt.center, gt.gauss_fwhm, gt.lorentz_fwhm,
        gt.gamma, 0.0, gt.elastic_amplitude, 0.0, gt.T, peak="elastic",
    )[0]
    assert np.isclose(elastic_height, 1.0 * target, rtol=1e-6)
    # phonon peak still hits target under the overridden Γ
    x = generate.energy_grid(cfg)
    h = noise.phonon_peak_height(x, gt.omega0, gt.gamma, gt.phonon_amplitude,
                                 gt.gauss_fwhm, gt.lorentz_fwhm, gt.T)
    assert np.isclose(h, target)


def test_model_on_grid_matches_scaled_model():
    # scaled_model is model_on_grid evaluated on the data grid.
    cfg = config.default_run_config()
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=1000.0, seed=2)
    x = noise.energy_grid(gt.x_min, gt.x_max, gt.x_step)
    assert np.allclose(noise.model_on_grid(gt, x), noise.scaled_model(gt))


def test_generate_scan_shapes():
    cfg = config.default_run_config()
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=1000.0, seed=3)
    x, counts, gt_out = generate.generate_scan(gt)
    assert x.shape == (81,)
    assert counts.shape == (81,)
    assert counts.dtype.kind in "iu"
    assert gt_out is gt


def test_manifest_round_trip(tmp_path):
    cfg = config.default_run_config()
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=1000.0, seed=9)
    path = tmp_path / "gt.json"
    generate.write_manifest(gt, path)
    restored = generate.read_manifest(path)
    assert restored == gt


def test_manifest_path_naming():
    cfg = config.default_run_config()
    gt = generate.make_ground_truth(cfg, H=2.5, target_peak_counts=1000.0, seed=42)
    p = generate.manifest_path(gt)
    assert p.name == "H2.50_c1000_s42.json"
    assert p.parent == config.MANIFEST_DIR


def test_make_ground_truth_rejects_unmeasurable(tmp_path):
    cfg = config.default_run_config()
    # H = 2.0 is a zone center: ω₀ = 0, phonon unmeasurable.
    try:
        generate.make_ground_truth(cfg, H=2.0, target_peak_counts=1000.0, seed=1)
        assert False, "expected ValueError for unmeasurable cell"
    except ValueError:
        pass


def test_plot_example_spectra_writes_file(tmp_path):
    cfg = config.default_run_config()
    out = tmp_path / "step1.png"
    result = generate.plot_example_spectra(cfg, H_list=(2.25, 2.5), target_peak_counts=1000.0,
                                           seed=2024, out_path=out)
    assert out.exists()
    assert result == out


def test_plot_count_sweep_writes_file(tmp_path):
    cfg = config.default_run_config()
    out = tmp_path / "counts.png"
    assert generate.plot_count_sweep(cfg, H=2.5, targets=(30, 100), seed=1, out_path=out) == out
    assert out.exists()


def test_plot_profile_variety_writes_file(tmp_path):
    cfg = config.default_run_config()
    out = tmp_path / "profiles.png"
    assert generate.plot_profile_variety(cfg, H=2.5, target_peak_counts=100.0,
                                         seed=1, out_path=out) == out
    assert out.exists()


def test_generate_series_skips_endpoints():
    cfg = config.default_run_config()
    scans = generate.generate_series(cfg, target_peak_counts=1000.0, write=False)
    # 21 grid points minus the 2 unmeasurable endpoints (H = 2.00, 3.00).
    assert len(scans) == 19
    Hs = [gt.H for (_x, _c, gt) in scans]
    assert 2.0 not in Hs and 3.0 not in Hs

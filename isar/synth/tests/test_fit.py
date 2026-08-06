"""Tests for the (Ω_d,Γ) reparameterized fit layer."""
import numpy as np
import pytest

from isar.synth import config, dispersion, fit, generate, noise
from isar.analyze import phonon
from isar.analyze.lineshapes import convolute_voigt_dho

CFG = config.default_run_config()


def _gt(H=2.5, target=2000.0, seed=0, **over):
    return generate.make_ground_truth(CFG, H, target, seed, **over)


# ---------------------------------------------------------------- reparam / guard
def test_reparam_round_trip():
    omega0, gamma = 6.0, 0.5
    omega_d = phonon.omega_d_from(omega0, gamma)
    assert np.isclose(omega_d, np.sqrt(36 - 0.25))
    assert np.isclose(phonon.omega0_from(omega_d, gamma), omega0)


def test_guard_never_fires_in_model():
    # Across the full H range and down to Ω_d=ε, ω₀²−Γ²=Ω_d²>0 → no 1e6 penalty.
    x = generate.energy_grid(CFG)
    model = fit.make_model(x, CFG.gauss_fwhm, CFG.lorentz_fwhm, CFG.T, center=0.0)
    for H in dispersion.h_values():
        omega0 = dispersion.omega0_of_H(H)
        if omega0 < CFG.eps_omega_d:
            continue
        omega_d = phonon.omega_d_from(omega0, CFG.gamma)
        y = model(np.array([max(omega_d, CFG.eps_omega_d), CFG.gamma, 100.0, 300.0, 5.0]))
        assert np.all(np.isfinite(y))
        assert y.max() < 1e5  # the 1e6 guard would dwarf this


def test_direct_overdamped_call_does_fire_guard():
    # Sanity: the guard exists in the production lib when called overdamped directly.
    x = np.linspace(-10, 10, 81)
    y = convolute_voigt_dho(x, 0.2, 0.0, 0.961, 0.596, 0.5, 100.0, 0.0, 0.0, 100.0, peak="stokes")
    assert y.max() >= 1e6


# ---------------------------------------------------------------- ω₀ propagation
def test_omega0_error_includes_cross_term():
    omega_d, gamma = 5.0, 0.5
    cov = np.array([[0.04, 0.01], [0.01, 0.02]])  # var_od, cov, var_g
    omega0, err = phonon.omega0_value_error(omega_d, gamma, cov)
    o = np.sqrt(omega_d**2 + gamma**2)
    d_od, d_g = omega_d / o, gamma / o
    expected = np.sqrt(d_od**2 * 0.04 + d_g**2 * 0.02 + 2 * d_od * d_g * 0.01)
    assert np.isclose(omega0, o)
    assert np.isclose(err, expected)


def test_omega0_error_none_covariance():
    omega0, err = phonon.omega0_value_error(5.0, 0.5, None)
    assert np.isfinite(omega0) and np.isnan(err)


# ---------------------------------------------------------------- success logic
def test_assess_success_normal():
    x = np.array([5.0, 0.5, 100.0, 300.0, 5.0])
    errs = np.array([0.05, 0.05, 5.0, 10.0, 1.0])
    at_limit = {"omega_d": False, "gamma": False}
    assert phonon.assess_success(x, errs, at_limit, True, True, CFG.eps_omega_d)


def test_assess_success_omega_d_pinned_at_eps_fails():
    x = np.array([CFG.eps_omega_d, 0.5, 100.0, 300.0, 5.0])
    errs = np.array([0.05, 0.05, 5.0, 10.0, 1.0])
    at_limit = {"omega_d": True, "gamma": False}
    assert not phonon.assess_success(x, errs, at_limit, True, True, CFG.eps_omega_d)


def test_assess_success_error_swamps_omega_d_fails():
    x = np.array([0.4, 0.5, 100.0, 300.0, 5.0])
    errs = np.array([0.9, 0.05, 5.0, 10.0, 1.0])  # σ_Ωd > Ω_d
    assert not phonon.assess_success(x, errs, {"omega_d": False}, True, True, CFG.eps_omega_d)


def test_assess_success_requires_covariance_and_finite_errors():
    x = np.array([5.0, 0.5, 100.0, 300.0, 5.0])
    assert not phonon.assess_success(x, None, {}, True, False, CFG.eps_omega_d)
    errs = np.array([0.05, np.nan, 5.0, 10.0, 1.0])
    assert not phonon.assess_success(x, errs, {}, True, True, CFG.eps_omega_d)


def test_assess_success_gamma_at_floor_is_still_success():
    x = np.array([5.0, CFG.gamma_floor, 100.0, 300.0, 5.0])
    errs = np.array([0.05, 0.05, 5.0, 10.0, 1.0])
    assert phonon.assess_success(x, errs, {"omega_d": False, "gamma": True}, True, True, CFG.eps_omega_d)


# ---------------------------------------------------------------- recovery
def test_highcount_midzone_recovery_within_tolerance():
    gt = _gt(H=2.5, target=3000.0, seed=101)
    _, counts, _ = generate.generate_scan(gt)
    res = fit.fit_truth(gt, counts, estimator="baker_cousins")
    assert res.success
    assert abs(res.omega0 - gt.omega0) < 0.1
    assert abs(res.gamma - gt.gamma) < 0.1


def test_fit_truth_matches_explicit_fit():
    gt = _gt(H=2.5, target=2000.0, seed=202)
    x, counts, _ = generate.generate_scan(gt)
    a = fit.fit_truth(gt, counts, estimator="baker_cousins")
    b = fit.fit(x, counts, gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
                T=gt.T, center=gt.center, estimator="baker_cousins")
    assert np.isclose(a.omega0, b.omega0)
    assert np.isclose(a.gamma, b.gamma)


# ---------------------------------------------------------------- initial guess
def test_grid_ic_finds_basin_at_low_counts():
    gt = _gt(H=2.5, target=20.0, seed=303)
    x, counts, _ = generate.generate_scan(gt)
    x0 = fit.initial_guess(x, counts, gt.gauss_fwhm, gt.lorentz_fwhm, gt.T,
                           center=0.0, eps_omega_d=gt.x_step, x_max=gt.x_max, mode="grid")
    omega_d_true = phonon.omega_d_from(gt.omega0, gt.gamma)
    assert abs(x0[0] - omega_d_true) < 1.5  # grid IC lands in the Ω_d basin


# ---------------------------------------------------------------- boundary cell
def test_boundary_cell_omega0_error_finite_and_classified():
    assert dispersion.classify_cell(2.05, CFG.gamma, CFG.eps_omega_d) == "boundary"
    gt = _gt(H=2.05, target=1000.0, seed=404)
    _, counts, _ = generate.generate_scan(gt)
    res = fit.fit_truth(gt, counts, estimator="baker_cousins")
    if res.success:
        assert np.isfinite(res.omega0_err)


# ---------------------------------------------------------------- MINOS asymmetric
def test_minos_populates_asymmetric_errors():
    gt = _gt(H=2.5, target=2000.0, seed=505)
    _, counts, _ = generate.generate_scan(gt)
    res = fit.fit_truth(gt, counts, estimator="baker_cousins", minos=True)
    assert res.success
    assert np.isfinite(res.gamma_err_lo) and np.isfinite(res.gamma_err_hi)
    assert res.gamma_err_lo <= 0 <= res.gamma_err_hi


# ------------------------------------------------ estimator divergence direction
@pytest.mark.slow
def test_backend_compare_reports_all_three():
    cells = []
    for seed in range(3):
        gt = _gt(H=2.5, target=300.0, seed=700 + seed)
        _, counts, _ = generate.generate_scan(gt)
        cells.append((gt, counts))
    report = fit.backend_compare(cells, estimator="baker_cousins")
    assert set(report) == {"iminuit", "scipy", "lmfit"}
    for be, row in report.items():
        assert 0.0 <= row["converged_rate"] <= 1.0
        assert row["n"] == 3
        assert row["mean_time_s"] >= 0.0
    # iminuit is the intended production backend: converges, recovers, and gives MINOS.
    assert report["iminuit"]["converged_rate"] == 1.0
    assert report["iminuit"]["recovered_rate"] == 1.0
    assert report["iminuit"]["asymmetric_errors"] is True
    # All three should at least find the optimum; only iminuit gives asymmetric errors.
    assert report["lmfit"]["converged_rate"] == 1.0
    assert report["scipy"]["asymmetric_errors"] is False


@pytest.mark.slow
def test_estimator_normalization_bias_straddle_low_counts():
    # The constant-rate straddle carries into the DHO amplitude at low counts:
    # Neyman (harmonic-mean-like) biases LOW, modified-Pearson (RMS-like) biases HIGH,
    # Baker–Cousins (arithmetic-mean-like) sits ≈ unbiased between them.
    truth_A = _gt(H=2.5, target=25.0, seed=0).phonon_amplitude
    means = {e: [] for e in ("neyman", "pearson", "baker_cousins")}
    for seed in range(40):
        gt = _gt(H=2.5, target=25.0, seed=900 + seed)
        _, counts, _ = generate.generate_scan(gt)
        for est in means:
            r = fit.fit_truth(gt, counts, estimator=est)
            if r.success:
                means[est].append(r.phonon_amplitude)
    m = {e: np.mean(v) for e, v in means.items() if v}
    assert m["neyman"] < m["baker_cousins"] < m["pearson"]      # the straddle
    # Baker–Cousins is the least biased of the three.
    assert abs(m["baker_cousins"] - truth_A) < abs(m["neyman"] - truth_A)
    assert abs(m["baker_cousins"] - truth_A) < abs(m["pearson"] - truth_A)

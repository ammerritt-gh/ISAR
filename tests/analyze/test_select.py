"""Step-2 model selection (§4): AIC/BIC/AICc, Akaike weights, nested LRT, survivor floor.

``cost`` is the Baker-Cousins -2lnL, so AIC = cost + 2k exactly (no errordef adjustment).
The nested "F-test" against a Poisson likelihood is the likelihood-ratio test: the deviance
drop cost_reduced - cost_full ~ chi2(dk).
"""
import math

import numpy as np

from isar.analyze import loop, select
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho


def _result(label, k, ndof, cost, *, params=None, within_model=None, converged=True):
    """A minimal StructureFitResult carrying just what selection reads (label/k/ndof/cost/
    params/converged); the rest are filled with innocuous values."""
    params = params if params is not None else {f"{label}.p{i}": 0.0 for i in range(k)}
    return loop.StructureFitResult(
        label=label, k=k, ndof=ndof, cost=cost,
        cost_per_dof=cost / ndof if ndof > 0 else float("nan"),
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=True, analysis_usable=converged,
        params=params, param_errors={n: None for n in params},
        gamma=0.4, gamma_err=0.05, omega0=5.0, omega0_err_hesse=0.05,
        message="ok", within_model=within_model,
    )


def _by_label(results, **kw):
    return {s.label: s for s in select.score_models(results, **kw)}


def test_aic_bic_aicc_from_baker_cousins_cost():
    r = _result("m", k=5, ndof=19, cost=90.0)          # n = 24
    sc = _by_label([r])["m"]
    assert sc.n == 24
    assert sc.cost == 90.0
    assert sc.aic == 90.0 + 2 * 5                       # cost + 2k = 100
    assert sc.bic == 90.0 + 5 * math.log(24)
    assert sc.aicc == 100.0 + 2 * 5 * 6 / (24 - 5 - 1)  # aic + 2k(k+1)/(n-k-1)


def test_akaike_weights_sum_to_one_and_track_aic():
    a = _result("a", k=4, ndof=20, cost=100.0)         # aic 108
    b = _result("b", k=5, ndof=19, cost=90.0)          # aic 100 (better)
    scores = _by_label([a, b])
    assert sum(s.weight for s in scores.values()) == 1.0 or \
        abs(sum(s.weight for s in scores.values()) - 1.0) < 1e-12
    assert scores["b"].weight > scores["a"].weight
    assert scores["b"].delta_aic == 0.0
    assert scores["a"].delta_aic == 8.0


def test_survivor_floor_drops_negligible_weight():
    win = _result("win", k=5, ndof=19, cost=80.0)      # aic 90
    junk = _result("junk", k=4, ndof=20, cost=112.0)   # aic 120 -> dAIC 30 -> w ~ 3e-7
    scores = _by_label([win, junk], weight_floor=0.01)
    assert scores["win"].survives is True
    assert scores["junk"].survives is False


def test_nested_lrt_reports_for_subset_and_skips_non_nested():
    reduced = _result("stokes+bg", k=4, ndof=20, cost=120.0,
                      params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0, "bg.BG": 0})
    full = _result("elastic+stokes+bg", k=5, ndof=19, cost=90.0,
                   params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0,
                           "elastic.E": 0, "bg.BG": 0})
    scores = _by_label([reduced, full])
    nt = scores["elastic+stokes+bg"].nested
    assert nt is not None
    assert nt["reduced"] == "stokes+bg"
    assert nt["dof"] == 1
    assert nt["delta_deviance"] == 30.0                # cost_reduced - cost_full
    assert 0.0 <= nt["p_value"] <= 1.0
    assert nt["p_value"] < 1e-3                        # the extra elastic param is warranted
    assert scores["stokes+bg"].nested is None          # nothing is nested inside the reduced model


def test_non_converged_results_excluded_from_selection():
    good = _result("good", k=5, ndof=19, cost=90.0)
    bad = _result("bad", k=4, ndof=20, cost=10.0, converged=False)  # spuriously low cost
    assert {s.label for s in select.score_models([good, bad])} == {"good"}


def test_all_non_converged_yields_empty_scores():
    bad = _result("bad", k=5, ndof=19, cost=10.0, converged=False)
    assert select.score_models([bad]) == []


def test_nested_lrt_pairs_one_mode_subset_two_mode():
    """The 1-mode stokes+bg params are a strict subset of the 2-mode stokes2+bg params, so the
    unchanged nested LRT pairs them (mode 1 keeps the ``phonon`` prefix — that is what makes
    the subset relation hold)."""
    one = _result("stokes+bg", k=4, ndof=20, cost=140.0,
                  params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0, "bg.BG": 0})
    two = _result("stokes2+bg", k=7, ndof=17, cost=95.0,
                  params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0,
                          "phonon2.Omega_d": 0, "phonon2.Gamma": 0, "phonon2.A": 0, "bg.BG": 0})
    scores = _by_label([one, two])
    nt = scores["stokes2+bg"].nested
    assert nt is not None and nt["reduced"] == "stokes+bg"
    assert nt["dof"] == 3                                 # a whole extra phonon (Ω_d, Γ, A)
    assert nt["delta_deviance"] == 45.0
    assert scores["stokes+bg"].nested is None


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121, seed=7):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = np.random.default_rng(seed).poisson(np.clip(p1 + p2 + bg, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _one_peak_ctx(o1=5.0, a1=500.0, g=0.4, bg=8.0, n=81, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    lam = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes") + bg
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_two_stage_select_accepts_second_mode_on_true_double_peak():
    """End-to-end: a wide two-peak spectrum → the two-stage loop offers 2-mode, AIC selects it,
    and the PRIMARY (largest-amplitude, acoustic-like ~4 meV) ω₀ interval brackets the truth."""
    ctx = _two_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, with_errors=True, M=40, seed=1)
    sel = select.select_models(results)
    assert sel.best == "stokes2+bg"
    win = {r.label: r for r in results}["stokes2+bg"]
    assert win.n_modes == 2
    assert abs(win.omega0 - 4.0) < 0.6                    # primary = the stronger (a1>a2) mode
    lo, med, hi = win.within_model["freq_interval"]
    assert lo <= win.omega0 <= hi                         # interval brackets the fitted primary
    assert abs(med - 4.0) < 0.3                           # …sits on the true acoustic peak (~4 meV)
    assert hi < 7.0                                        # …cleanly separated from the 10 meV mode


def test_two_stage_select_rejects_second_mode_on_true_single_peak():
    """When ≥2 peaks are declared the 2-mode set is fit even on a true single peak, but AIC
    rejects the extra mode: the 1-mode stokes+bg wins."""
    ctx = _one_peak_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]
    # with_errors=False on purpose: this test's contract is AIC rejection only -- the assertions
    # never read bootstrap output, and the M=40 bootstrap was ~27s of pure cost.
    results = loop.fit_structures_two_stage(ctx, with_errors=False, M=40, seed=1, peaks=peaks)
    assert "stokes2+bg" in {r.label for r in results}     # it WAS offered
    sel = select.select_models(results)
    assert sel.best == "stokes+bg"                        # …and rejected on AIC


def _spectrum_ctx(seed=0, omega0=5.0, n=81):
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_select_end_to_end_real_spectrum_folds():
    """Wiring: fit + bootstrap the admissible structures, then select. The elastic+stokes+bg
    model wins decisively, stokes+bg drops, the lone survivor's bootstrap is unimodal -> fold,
    and the scan decision is stamped onto across_model."""
    ctx = _spectrum_ctx()
    results = loop.fit_admissible_structures(ctx, errors=True, M=40, seed=1)
    sel = select.select_models(results)
    assert sel.best == "elastic+stokes+bg"
    assert "elastic+stokes+bg" in sel.survivors
    assert sel.modality == "fold"
    lo, med, hi = sel.pooled.interval
    assert lo <= 5.0 <= hi
    win = {r.label: r for r in results}["elastic+stokes+bg"]
    assert win.across_model["scan"]["modality"] == "fold"
    assert win.across_model["score"]["survives"] is True
    assert win.across_model["score"]["weight"] > 0.99

"""Parametric bootstrap (§5.2): generate from the best fit, refit M Poisson draws, the
scatter is the error. The authoritative ω₀ interval comes from transforming each draw
(ω₀=√(Ω_d²+Γ²)), not from propagating the point HESSE σ (review F4)."""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bootstrap as B
from isar.analyze.structures import enumerate_structures
from isar.synth._ff import convolute_voigt_dho


def _spectrum_ctx(seed=0, omega0=5.0, n=81):
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _fitted(ctx):
    hyp = {h.label: h for h in enumerate_structures(ctx)}["elastic+stokes+bg"]
    return hyp, loop.fit_hypothesis(hyp, ctx, minos=False)


def test_bootstrap_omega0_interval_brackets_best_fit():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=60, seed=1)
    lo, med, hi = boot.omega0_interval
    assert lo < hi
    assert lo <= fit.omega0 <= hi
    assert abs(med - fit.omega0) < 0.2          # bootstrap centre near the fit


def test_bootstrap_tracks_convergence_and_sample_lengths():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=60, seed=1)
    assert 0 < boot.n_converged <= 60
    assert boot.omega0_samples.size == boot.n_converged
    assert boot.gamma_samples.size == boot.n_converged


def test_bootstrap_seed_reproducible():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    a = B.parametric_bootstrap(hyp, ctx, fit, M=40, seed=7)
    b = B.parametric_bootstrap(hyp, ctx, fit, M=40, seed=7)
    assert np.array_equal(a.omega0_samples, b.omega0_samples)
    assert a.omega0_interval == b.omega0_interval


def test_bootstrap_gamma_interval_ordered():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=50, seed=3)
    glo, gmed, ghi = boot.gamma_interval
    assert glo <= gmed <= ghi


def test_is_bimodal_detects_two_clusters():
    rng = np.random.default_rng(0)
    unimodal = rng.normal(5.0, 0.15, 300)
    bimodal = np.concatenate([rng.normal(5.0, 0.1, 150), rng.normal(8.0, 0.1, 150)])
    assert B.is_bimodal(bimodal) is True
    assert B.is_bimodal(unimodal) is False


def test_is_bimodal_degenerate_inputs():
    assert B.is_bimodal(np.full(50, 5.0)) is False     # zero spread
    assert B.is_bimodal(np.array([5.0, 5.1])) is False  # too few

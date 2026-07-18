"""Parametric bootstrap (§5.2): generate from the best fit, refit M Poisson draws, the
scatter is the error. The authoritative ω₀ interval comes from transforming each draw
(ω₀=√(Ω_d²+Γ²)), not from propagating the point HESSE σ (review F4)."""
import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bootstrap as B
from isar.analyze.ic import greedy_multimode_ic
from isar.analyze.structures import enumerate_multimode, enumerate_structures
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


def test_shared_executor_failure_logs_and_retries_identically(caplog):
    class BrokenExecutor:
        def map(self, *_args, **_kwargs):
            raise RuntimeError("pool broken")

    ctx = _spectrum_ctx(n=41)
    hyp, fit = _fitted(ctx)
    serial = B.parametric_bootstrap(hyp, ctx, fit, M=16, seed=7, workers=1)
    recovered = B.parametric_bootstrap(
        hyp, ctx, fit, M=16, seed=7, workers=2, executor=BrokenExecutor())
    assert np.array_equal(serial.omega0_samples, recovered.omega0_samples)
    assert serial.omega0_interval == recovered.omega0_interval
    assert "retrying serially" in caplog.text


def test_adaptive_no_early_stop_matches_fixed():
    # rel_eps=0 never declares stability → runs to M; block draws come off the same RNG in the same
    # order as the fixed path → bit-identical samples and interval.
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    fixed = B.parametric_bootstrap(hyp, ctx, fit, M=64, seed=5)
    adap = B.parametric_bootstrap(hyp, ctx, fit, M=64, seed=5, adaptive=True, block=32,
                                  m_min=64, rel_eps=0.0)
    assert np.array_equal(fixed.omega0_samples, adap.omega0_samples)
    assert fixed.omega0_interval == adap.omega0_interval
    assert adap.M == 64


def test_adaptive_stops_early_on_stable_interval():
    # A clean single peak: the ω₀ half-width stabilises fast, so adaptive stops before the cap.
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    adap = B.parametric_bootstrap(hyp, ctx, fit, M=200, seed=5, adaptive=True, block=32,
                                  m_min=64, rel_eps=0.25)
    assert 64 <= adap.M < 200
    lo, _med, hi = adap.omega0_interval
    assert lo <= fit.omega0 <= hi


@pytest.mark.slow
def test_bootstrap_parallel_and_injected_executor_match_serial():
    # Determinism across execution modes: serial == self-created pool == injected shared pool,
    # bit-identical for a fixed seed (the workorder rests on this).
    from concurrent.futures import ProcessPoolExecutor
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    serial = B.parametric_bootstrap(hyp, ctx, fit, M=32, seed=11, workers=1)
    created = B.parametric_bootstrap(hyp, ctx, fit, M=32, seed=11, workers=4)
    assert np.array_equal(serial.omega0_samples, created.omega0_samples)
    assert serial.omega0_interval == created.omega0_interval
    with ProcessPoolExecutor(max_workers=4) as ex:
        injected = B.parametric_bootstrap(hyp, ctx, fit, M=32, seed=11, workers=4, executor=ex)
    assert np.array_equal(serial.omega0_samples, injected.omega0_samples)
    assert serial.omega0_interval == injected.omega0_interval


def test_bootstrap_gamma_interval_ordered():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=50, seed=3)
    glo, gmed, ghi = boot.gamma_interval
    assert glo <= gmed <= ghi


def _two_peak_ctx(o1=5.0, a1=650.0, o2=8.5, a2=300.0, g=0.35, bg=8.0, n=121, seed=5):
    x = np.linspace(-6.0, 22.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = np.random.default_rng(seed).poisson(np.clip(p1 + p2 + bg, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_bootstrap_canonicalizes_two_close_modes_no_label_switch():
    """Two nearby modes (~5 and ~8.5 meV): per-replicate sort-by-ω₀ pins the primary sample to
    the primary's rank, so the PRIMARY interval stays tight around its truth and does NOT
    inflate to span both peaks (the label-switching failure mode)."""
    ctx = _two_peak_ctx()
    one = loop.fit_hypothesis({h.label: h for h in enumerate_structures(ctx)}["stokes+bg"], ctx)
    hyp = {h.label: h for h in enumerate_multimode(ctx)}["stokes2+bg"]
    x0 = greedy_multimode_ic(hyp.build(), ctx, one)
    fit = loop.fit_hypothesis(hyp, ctx, x0=x0)
    assert fit.n_modes == 2
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=80, seed=1)
    lo, med, hi = boot.omega0_interval
    # primary = the larger-amplitude ~5 meV mode; its interval brackets 5 and stays well below
    # the ~3.5 meV mode separation (not straddling the 8.5 meV mode).
    assert lo <= fit.omega0 <= hi
    assert abs(med - 5.0) < 0.8
    assert (hi - lo) < 1.5
    assert hi < 7.0


def test_is_bimodal_detects_two_clusters():
    rng = np.random.default_rng(0)
    unimodal = rng.normal(5.0, 0.15, 300)
    bimodal = np.concatenate([rng.normal(5.0, 0.1, 150), rng.normal(8.0, 0.1, 150)])
    assert B.is_bimodal(bimodal) is True
    assert B.is_bimodal(unimodal) is False


def test_is_bimodal_degenerate_inputs():
    assert B.is_bimodal(np.full(50, 5.0)) is False     # zero spread
    assert B.is_bimodal(np.array([5.0, 5.1])) is False  # too few


def test_is_bimodal_weights_can_split_or_merge():
    """Weighting feeds the pooled across-model density: down-weighting a cluster below the
    relative-height floor collapses the fork to a fold."""
    rng = np.random.default_rng(0)
    s = np.concatenate([rng.normal(5.0, 0.1, 200), rng.normal(8.0, 0.1, 200)])
    assert B.is_bimodal(s, weights=np.ones(s.size)) is True
    w = np.where(s < 6.5, 1.0, 1e-3)                    # crush the upper cluster
    assert B.is_bimodal(s, weights=w) is False


def test_kde_modes_reports_split_between_two_clusters():
    rng = np.random.default_rng(1)
    s = np.concatenate([rng.normal(5.0, 0.1, 200), rng.normal(8.0, 0.1, 200)])
    m = B.kde_modes(s)
    assert m.bimodal is True
    assert m.split is not None and 5.0 < m.split < 8.0  # the valley sits between the modes
    assert len(m.peaks) >= 2


def test_kde_modes_unimodal_has_no_split():
    rng = np.random.default_rng(2)
    m = B.kde_modes(rng.normal(6.0, 0.2, 300))
    assert m.bimodal is False
    assert m.split is None

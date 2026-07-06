"""The structure loop: per-hypothesis problem builder + fit + StructureFitResult.

Key guarantees: each hypothesis is a real model variant (its own k/ndof, review F1); the
full elastic+stokes+bg fit reproduces the frozen synth.fit(); and the result separates
optimizer convergence from error availability (review F2) so step-3 bootstrap is not
starved of converged-but-covariance-less fits.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_structures
from isar.synth import fit
from isar.synth._ff import convolute_voigt_dho
from isar.synth.backends import BackendResult


def _spectrum_ctx(n=61, status="needs_zero", center=-0.4):
    x = np.linspace(-10.0, 10.0, n)
    counts = convolute_voigt_dho(x, 5.0, center, 1.1, 0.0, 0.45, 400.0, 70.0, 12.0, 150.0,
                                 peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=150.0,
                       center=center, gauss_fwhm=1.1, lorentz_fwhm=0.0, instrument="S30",
                       energy_status=status)


def _by_label(ctx):
    return {h.label: h for h in enumerate_structures(ctx)}


def test_build_problem_per_hypothesis_k_and_ndof():
    ctx = _spectrum_ctx(n=41)
    hyps = _by_label(ctx)
    p_so = loop.build_problem(hyps["stokes+bg"], ctx)
    p_full = loop.build_problem(hyps["elastic+stokes+bg"], ctx)
    assert p_so.k == 4 and p_so.ndof == 41 - 4
    assert p_full.k == 5 and p_full.ndof == 41 - 5


def test_fit_hypothesis_matches_synth_fit_full_model():
    ctx = _spectrum_ctx()
    hyp = _by_label(ctx)["elastic+stokes+bg"]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    ref = fit.fit(ctx.x, ctx.counts, gauss_fwhm=ctx.gauss_fwhm, lorentz_fwhm=ctx.lorentz_fwhm,
                  T=ctx.T, center=ctx.center, minos=False)
    assert np.isclose(res.omega0, ref.omega0, rtol=1e-5)
    assert np.isclose(res.gamma, ref.gamma, rtol=1e-5)
    assert np.isclose(res.cost, ref.cost, rtol=1e-5)
    assert res.k == 5 and res.ndof == ref.ndof


def test_fit_hypothesis_recovers_truth_on_clean_spectrum():
    ctx = _spectrum_ctx()
    hyp = _by_label(ctx)["elastic+stokes+bg"]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    assert abs(res.omega0 - 5.0) < 0.1
    assert res.optimizer_converged


def test_result_separates_convergence_from_covariance():
    """A converged fit whose HESSE covariance failed must stay usable (review F2)."""
    comp = CompositeModel([PhononDHO(), ConstantBackground()])  # k = 4
    theta = np.array([4.0, 0.4, 300.0, 10.0])
    br = BackendResult(x=theta, success=True, message="converged", n_eval=10,
                       covariance=None, covariance_ok=False, at_limit={}, merrors=None)
    res = loop.result_from_backend("stokes+bg", comp, lambda th: 5.0, n_used=20, k=4, br=br)
    assert res.optimizer_converged is True
    assert res.covariance_ok is False
    assert res.profile_ok is False
    assert res.well_constrained is False        # strict Phase-1 rule needs covariance
    assert res.analysis_usable is True          # but it is NOT discarded
    assert np.isnan(res.omega0_err_hesse)
    assert np.isclose(res.omega0, np.sqrt(4.0**2 + 0.4**2))
    assert res.ndof == 16


def test_fit_admissible_structures_emits_both_then_offwindow_one():
    both = loop.fit_admissible_structures(_spectrum_ctx(), minos=False)
    assert {r.label for r in both} == {"stokes+bg", "elastic+stokes+bg"}

    x = np.arange(1.0, 21.5, 0.5)
    counts = convolute_voigt_dho(x, 4.0, 0.0, 0.6, 0.0, 0.3, 300.0, 0.0, 20.0, 290.0, peak="all")
    off = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=290.0,
                      center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0, instrument="IN8",
                      energy_status="absolute")
    res = loop.fit_admissible_structures(off, minos=False)
    assert [r.label for r in res] == ["stokes+bg"]


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121, seed=None):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = p1 + p2 + bg
    if seed is not None:
        counts = np.random.default_rng(seed).poisson(np.clip(counts, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _one_peak_ctx(o1=5.0, a1=500.0, g=0.4, bg=8.0, n=81, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    counts = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + bg
    if seed is not None:
        counts = np.random.default_rng(seed).poisson(np.clip(counts, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_gate_fires_on_two_peak_residual_not_on_clean_single():
    two = _two_peak_ctx()
    one_results = loop.fit_admissible_structures(two, minos=False)
    assert loop._offer_multimode(one_results, two) is True     # one DHO can't fit two peaks

    single = _one_peak_ctx()
    ok_results = loop.fit_admissible_structures(single, minos=False)
    assert loop._offer_multimode(ok_results, single) is False  # a single DHO fits fine


def test_two_stage_adds_multimode_when_gate_fires():
    ctx = _two_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False)
    labels = {r.label for r in results}
    assert "stokes+bg" in labels                                # 1-mode set always present
    assert "stokes2+bg" in labels                               # escalated on the gate
    two = {r.label: r for r in results}["stokes2+bg"]
    assert two.n_modes == 2 and len(two.modes) == 2
    # modes canonicalized ascending by ω₀ and bracket the two true peaks (~4 and ~10 meV)
    o = [m["omega0"] for m in two.modes]
    assert o == sorted(o)
    assert abs(o[0] - 4.0) < 1.0 and abs(o[1] - 10.0) < 1.0


def test_two_stage_single_peak_stays_one_mode():
    ctx = _one_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False)
    # gate did not fire → no 2-mode ("stokes2…") hypothesis was offered
    assert not any("stokes2" in r.label for r in results)
    assert all(r.n_modes == 1 for r in results)


def test_two_stage_offers_multimode_when_two_peaks_declared():
    """Even a clean single-peak scan gets the 2-mode set offered when ≥2 peaks are declared
    (the driver-supplied ``peaks`` hint); AIC is then left to reject the extra mode."""
    ctx = _one_peak_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]
    results = loop.fit_structures_two_stage(ctx, minos=False, peaks=peaks)
    assert "stokes2+bg" in {r.label for r in results}

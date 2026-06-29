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

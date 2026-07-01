"""Wiring: the error layer is opt-in per call and lands in StructureFitResult.within_model
(the slot step 1 reserved), leaving step 1's fast path unchanged."""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import attach
from isar.analyze.errors.bayes import BayesResult
from isar.analyze.structures import enumerate_structures
from isar.synth._ff import convolute_voigt_dho


def _ctx(seed=0):
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _fitted(ctx):
    hyp = {h.label: h for h in enumerate_structures(ctx)}["elastic+stokes+bg"]
    return hyp, loop.fit_hypothesis(hyp, ctx, minos=False)


def _canned_bayes(converged=True, omega0_interval=(4.9, 5.0, 5.1)):
    """A stand-in BayesResult so the attach() WIRING is tested without a 60 s sampler run
    (the real sampler is exercised in test_bayes / test_bayes_coverage)."""
    return BayesResult(omega0_interval=omega0_interval, gamma_interval=(0.30, 0.40, 0.50),
                       omega0_samples=np.array([4.9, 5.0, 5.1]), gamma_samples=np.array([0.4]),
                       log_evidence=-85.0, log_evidence_err=0.5, ess=500.0, bimodal=False,
                       converged=converged, n_live=100, seed=1, prior_used=False)


def test_errors_flag_populates_within_model():
    results = loop.fit_admissible_structures(_ctx(), errors=True, M=40, seed=2)
    es = {r.label: r for r in results}["elastic+stokes+bg"]
    assert es.within_model is not None
    lo, med, hi = es.within_model["freq_interval"]
    assert lo <= es.omega0 <= hi
    assert es.within_model["n_converged"] > 0


def test_fast_path_leaves_within_model_none():
    for r in loop.fit_admissible_structures(_ctx(), errors=False):
        assert r.within_model is None


def test_without_bayes_writes_no_bayes_keys():
    """Backward compat: the default (with_bayes=False) leaves within_model bayes-free."""
    ctx = _ctx()
    hyp, fit = _fitted(ctx)
    attach(fit, hyp, ctx, M=40, seed=2)
    assert "bayes_interval" not in fit.within_model
    assert "bayes_ok" not in fit.within_model


def test_with_bayes_populates_bayes_keys_and_three_method_agreement(monkeypatch):
    ctx = _ctx()
    hyp, fit = _fitted(ctx)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: _canned_bayes())
    attach(fit, hyp, ctx, M=40, seed=2, with_bayes=True)
    wm = fit.within_model
    assert wm["bayes_interval"] == (4.9, 5.0, 5.1)
    assert wm["bayes_ok"] is True
    assert np.isfinite(wm["log_evidence"]) and np.isfinite(wm["log_evidence_err"])
    assert wm["bayes_samples"].size == 3
    ag = wm["agreement"]
    assert ag.bayes_available is True
    assert np.isfinite(ag.bayes_freq_ratio)
    assert np.isfinite(ag.hesse_freq_ratio)          # HESSE σ was fed into the cross-check


def test_with_bayes_failure_keeps_frequentist_interval(monkeypatch):
    """§5.4 fallback: a non-converged posterior sets bayes_ok=False but the authoritative
    frequentist interval remains, so the scan is still analysable."""
    ctx = _ctx()
    hyp, fit = _fitted(ctx)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: _canned_bayes(converged=False,
                                                      omega0_interval=(float("nan"),) * 3))
    attach(fit, hyp, ctx, M=40, seed=2, with_bayes=True)
    wm = fit.within_model
    assert wm["bayes_ok"] is False
    lo, _med, hi = wm["freq_interval"]
    assert lo <= fit.omega0 <= hi
    assert wm["agreement"].bayes_available is False   # a failed posterior is not used in the cross-check

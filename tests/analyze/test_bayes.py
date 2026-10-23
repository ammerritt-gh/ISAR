"""Bayesian posterior error path (§5.4): credible interval, evidence, reproducibility, the
load-bearing convergence gate, and the fall-back-on-failure contract.

Kept fast with a small live-point budget and ``sample="unif"`` (production uses the robust
``rwalk``/nlive=400 defaults); the calibration/coverage gate is the slow ``test_bayes_coverage``
companion. Mirrors ``test_bootstrap`` idioms so both paradigms are exercised the same way (§5.5).
One shared posterior is sampled per module and reused across assertions to stay affordable."""
import types

import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import agree as A
from isar.analyze.errors import bayes as BZ
from isar.analyze.errors import bootstrap as B
from isar.analyze.prior import PhononPrior
from isar.analyze.phonon import enumerate_phonon_structures
from isar.analyze.lineshapes import convolute_voigt_dho

# The tiny-nlive unif runs emit a benign ellipsoid-efficiency note; production nlive does not.
pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor")

FAST = dict(n_live=100, dlogz=1.0, sample="unif")


def _spectrum_ctx(seed=0, omega0=5.0, n=81):
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _sparse_ctx(seed=21, omega0=5.0, n=81):
    """A deliberately marginal peak (weak amplitude under a large flat background): the data
    alone constrains ω₀ only weakly, so the bootstrap is wide. Fixed seed ⇒ reproducible."""
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 1.5, 5.0, 5.0, 25.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _fitted(ctx):
    hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}["elastic+stokes+bg"]
    return hyp, loop.fit_hypothesis(hyp, ctx, minos=False)


def _hw(interval):
    return (interval[2] - interval[0]) / 2.0


def test_tight_prior_dominates_a_sparse_scan():
    """§5.4/§5.5: on a weakly-constrained scan a tight dispersion prior narrows the posterior
    below the data-only bootstrap, and the extended agreement flags it as prior-driven — the
    'this ω is mostly prior, little data' signal that routes to a re-measure/circularity caution."""
    ctx = _sparse_ctx()
    hyp, fit = _fitted(ctx)
    boot = B.parametric_bootstrap(hyp, ctx, fit, M=60, seed=1)
    assert _hw(boot.omega0_interval) > 0.3          # the data alone constrains ω₀ only weakly
    prior = PhononPrior(omega0=5.0, omega0_sigma=0.1)
    bz = BZ.bayesian_posterior(hyp, ctx, fit, prior=prior, seed=1, **FAST)
    assert bz.prior_used is True
    assert _hw(bz.omega0_interval) < _hw(boot.omega0_interval)   # the prior did the work
    ag = A.agreement(boot, None, bayes=bz, prior=prior)
    assert ag.prior_driven is True


@pytest.fixture(scope="module")
def clean():
    """One posterior sampled from a clean, well-counted spectrum, reused across assertions."""
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    res = BZ.bayesian_posterior(hyp, ctx, fit, seed=1, **FAST)
    return ctx, hyp, fit, res


def test_bayes_credible_interval_brackets_best_fit(clean):
    _ctx, _hyp, fit, res = clean
    lo, med, hi = res.omega0_interval
    assert lo < hi
    assert lo <= fit.omega0 <= hi
    assert abs(med - fit.omega0) < 0.3          # posterior centre near the fit (uninformative prior)


def test_bayes_reports_finite_evidence_and_samples(clean):
    _ctx, _hyp, _fit, res = clean
    assert np.isfinite(res.log_evidence) and np.isfinite(res.log_evidence_err)
    assert res.omega0_samples.size > 0
    assert res.gamma_samples.size == res.omega0_samples.size
    glo, gmed, ghi = res.gamma_interval
    assert glo <= gmed <= ghi
    assert res.prior_used is False


def test_bayes_gate_passes_on_clean_spectrum(clean):
    _ctx, _hyp, _fit, res = clean
    assert res.converged is True
    assert res.fail_reason is None
    assert res.ess >= BZ.ESS_MIN


def test_bayes_seed_reproducible():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    a = BZ.bayesian_posterior(hyp, ctx, fit, seed=7, n_live=60, dlogz=1.5, sample="unif")
    b = BZ.bayesian_posterior(hyp, ctx, fit, seed=7, n_live=60, dlogz=1.5, sample="unif")
    assert a.omega0_interval == b.omega0_interval
    assert np.array_equal(a.omega0_samples, b.omega0_samples)


def test_bayes_falls_back_on_sampler_failure(monkeypatch):
    """§5.4 silent-failure guard: a sampler exception must NOT propagate — it returns
    converged=False + a reason so the scan routes to the frequentist interval + flag."""
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)

    def boom(*a, **k):
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(BZ.dynesty, "NestedSampler", boom)
    res = BZ.bayesian_posterior(hyp, ctx, fit, seed=1, **FAST)
    assert res.converged is False
    assert res.fail_reason is not None
    assert res.omega0_samples.size == 0


def test_bayes_no_op_on_unconverged_fit():
    """A non-converged fit short-circuits (no sampling): bayes cannot rescue a fit that failed."""
    ctx = _spectrum_ctx()
    hyp, _fit = _fitted(ctx)
    stub = types.SimpleNamespace(optimizer_converged=False)
    res = BZ.bayesian_posterior(hyp, ctx, stub, seed=1, **FAST)
    assert res.converged is False
    assert res.fail_reason == "fit did not converge"
    assert res.omega0_samples.size == 0


def test_evidence_lane_samples_a_model_whose_optimizer_did_not_converge():
    """The evidence lane judges the model, not the fit: an unconverged optimizer is no refusal."""
    from isar.analyze import catalogue

    ctx = _spectrum_ctx()
    hyp, _fit = _fitted(ctx)
    prob = loop.build_problem(hyp, ctx)
    bundle = catalogue.resolve_priors(prob.composite, ctx, prob.bounds)
    stub = types.SimpleNamespace(optimizer_converged=False)
    res = BZ.bayesian_posterior(hyp, ctx, stub, prior_bundle=bundle, seed=1, **FAST)
    assert res.fail_reason != "fit did not converge"
    assert np.isfinite(res.log_evidence)
    assert res.parameter_samples.shape[0] > 0

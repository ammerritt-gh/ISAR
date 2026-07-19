"""Bayesian posterior sampling — the second within-model error paradigm (§5.4).

The frequentist paths (bootstrap, MINOS) give the sampling distribution of the estimator;
this gives the posterior given the data and a prior. It answers the complementary question
(§0.5) and brings what the frequentist side structurally cannot: the dispersion prior acts as
a **literal prior on ω**, nuisances (background, elastic amplitude) are **marginalized by
integration**, and boundaries (Γ≥0, A≥0) are handled by **prior support** — the clean fix to
the Γ→0 corner ``TRUST_SIGMA_FACTOR`` patches on the frequentist side.

Sampler = **dynesty static nested sampling** (D9). The forward model (``convolute_voigt_dho``,
numpy FFT + interpolation) is not differentiable, so a gradient sampler (NUTS) would need a
JAX reimplementation that risks diverging from the frozen numpy model and contaminating the
§5.5 agreement diagnostic. A gradient-free sampler calls the **exact same** Baker–Cousins
log-likelihood the bootstrap/MINOS use, so the statistical model is provably identical across
paradigms. Nested sampling also returns the marginal-likelihood evidence (``log_evidence``,
for §4) and has a self-contained ``dlogz`` stopping criterion.

The load-bearing catch (§5.4): a poorly-mixing run returns a confident, *wrong* interval with
no exception. So :func:`_diagnose` is not polish — a failed gate sets ``converged=False`` and
routes the scan to "Bayesian interval untrustworthy → fall back to frequentist + flag".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import dynesty
from dynesty.utils import resample_equal

from isar.analyze import loop
from isar.analyze.errors.bootstrap import _percentile_interval, is_bimodal
from isar.synth import estimators

# Production defaults + diagnostic thresholds (D10-adjacent empirical pins, like loop.GAMMA_FLOOR).
DEFAULT_NLIVE = 400
DEFAULT_DLOGZ = 0.5
# ESS is the PRIMARY interval-quality gate: it is nlive-robust and directly measures how well the
# posterior was resolved. logzerr ≈ √(H/nlive) scales with the live-point budget, so it conflates
# run quality with budget — it governs *evidence* precision (the §4 evidence-weight follow-on), not
# interval validity, and is kept here only as a coarse "evidence didn't blow up / go NaN" guard.
ESS_MIN = 100.0          # effective posterior sample size below which the run is under-sampled
LOGZERR_MAX = 5.0        # coarse evidence-sanity bound (a tight §4 pin is a follow-on)
RAIL_RTOL = 0.02         # a draw is "railed" if within 2 % of a parameter's span from a bound
RAIL_FRAC = 0.15         # >15 % of Ω_d draws railed ⇒ ill-posed position (hard fail)

_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)


@dataclass
class BayesResult:
    omega0_interval: tuple[float, float, float]   # (lo, median, hi) credible interval
    gamma_interval: tuple[float, float, float]
    omega0_samples: np.ndarray                    # equal-weight posterior draws (ω₀ = √(Ω_d²+Γ²))
    gamma_samples: np.ndarray
    log_evidence: float                           # logz  (for the §4 evidence-weight follow-on)
    log_evidence_err: float                       # logzerr
    ess: float                                    # effective posterior sample size (Kish)
    bimodal: bool                                 # is_bimodal(omega0_samples) — reused from bootstrap
    converged: bool                               # the §5.4 diagnostic gate
    n_live: int
    seed: int
    prior_used: bool
    fail_reason: str | None = None
    parameter_names: tuple[str, ...] = ()
    parameter_samples: np.ndarray | None = None


def _gauss_logpdf(x: float, mu: float, sigma: float) -> float:
    z = (x - mu) / sigma
    return -0.5 * z * z - math.log(sigma) - _LOG_SQRT_2PI


def _rail_fraction(col: np.ndarray, lo: float, hi: float, rtol: float) -> float:
    """Fraction of draws within ``rtol`` of the span from either bound."""
    span = hi - lo
    if not np.isfinite(span) or span <= 0.0 or col.size == 0:
        return 0.0
    tol = rtol * span
    near = (col <= lo + tol) | (col >= hi - tol)
    return float(np.count_nonzero(near)) / float(col.size)


def _diagnose(res, bounds, od_i, od_draws, omega0_interval, ess, max_iter):
    """The silent-failure gate (§5.4). Returns ``(ok, reason)``; ALL hard checks must pass.

    Γ piled at its floor is deliberately NOT a hard failure — it is the overdamped/Γ→0 physics
    the Bayesian path exists to handle by prior support; only Ω_d running to the window edge
    (an ill-posed *position*) hard-fails on railing."""
    logz = float(res.logz[-1])
    logzerr = float(res.logzerr[-1])
    if not np.isfinite(logz):
        return False, "logz not finite"
    if max_iter is not None and int(res.niter) >= int(max_iter):
        return False, "hit max_iter before dlogz target"
    if not np.isfinite(ess) or ess < ESS_MIN:                # primary interval-quality gate
        return False, f"ess={ess:.0f} below {ESS_MIN:.0f}"
    if not np.isfinite(logzerr) or logzerr > LOGZERR_MAX:    # coarse evidence blow-up guard
        return False, f"logzerr={logzerr:.2f} exceeds {LOGZERR_MAX}"
    od_lo, od_hi = bounds[od_i]
    if _rail_fraction(od_draws, od_lo, od_hi, RAIL_RTOL) > RAIL_FRAC:
        return False, "Omega_d railed against the window (ill-posed position)"
    lo, _med, hi = omega0_interval
    if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
        return False, "omega0 interval degenerate"
    return True, None


def bayesian_posterior(hyp, ctx, fit_result, *, prior=None, seed: int = 12345,
                       n_live: int = DEFAULT_NLIVE, dlogz: float = DEFAULT_DLOGZ,
                       level: float = 0.68, max_iter: int | None = None,
                       sample: str = "rwalk", bound: str = "multi") -> BayesResult:
    """Sample the posterior of one fitted hypothesis; return ω₀/Γ credible intervals, the raw
    equal-weight posterior draws (for §4 pooling), the marginal-likelihood evidence, and the
    §5.4 convergence gate. No-op (``converged=False``) on a non-converged fit, mirroring the
    bootstrap. ``prior`` (a :class:`~isar.analyze.prior.PhononPrior`) makes the dispersion
    expectation a literal Normal prior on ω₀; ``None`` ⇒ weakly-informative (posterior ≈
    likelihood ≈ bootstrap in the well-counted regime, by Bernstein–von Mises)."""
    prior_used = prior is not None
    nan3 = (float("nan"), float("nan"), float("nan"))

    def _failed(reason: str) -> BayesResult:
        return BayesResult(omega0_interval=nan3, gamma_interval=nan3,
                           omega0_samples=np.empty(0), gamma_samples=np.empty(0),
                           log_evidence=float("nan"), log_evidence_err=float("nan"),
                           ess=0.0, bimodal=False, converged=False, n_live=int(n_live),
                           seed=int(seed), prior_used=prior_used, fail_reason=reason,
                           parameter_samples=np.empty((0, 0)))

    if not fit_result.optimizer_converged:
        return _failed("fit did not converge")

    prob = loop.build_problem(hyp, ctx)
    # NB (steering contract 1): nested sampling carries its own ω₀ prior (below, via ``prior_used``);
    # it does NOT apply ``ctx.constraints`` soft penalties. Soft Γ/A constraints are honoured by the
    # authoritative bootstrap + profile intervals, not this opt-in Bayesian path.
    names = prob.free_names
    x = ctx.x_used
    counts = ctx.counts_used
    est = estimators.get_estimator("baker_cousins")
    composite = prob.composite
    od_i = names.index(next(n for n in names if n.endswith(".Omega_d")))
    g_i = names.index(next(n for n in names if n.endswith(".Gamma")))

    lows = np.array([b[0] for b in prob.bounds], dtype=float)
    spans = np.array([b[1] - b[0] for b in prob.bounds], dtype=float)

    def ptform(u):
        return lows + np.asarray(u) * spans

    mu = float(prior.omega0) if prior_used else 0.0
    sig = float(prior.omega0_sigma) if prior_used else 1.0

    def loglike(theta):
        f = composite.evaluate(x, theta, ctx)
        ll = -0.5 * est.cost(counts, f)                     # exact Baker–Cousins −2lnL/2
        if prior_used:
            w0 = math.sqrt(theta[od_i] ** 2 + theta[g_i] ** 2)
            ll += _gauss_logpdf(w0, mu, sig)
        return ll if np.isfinite(ll) else -1e300

    rng = np.random.default_rng(seed)
    try:                                                    # §5.4: sampler failure must not escape
        sampler = dynesty.NestedSampler(loglike, ptform, ndim=len(names), nlive=int(n_live),
                                        bound=bound, sample=sample, rstate=rng)
        sampler.run_nested(dlogz=dlogz, maxiter=max_iter, print_progress=False)
        res = sampler.results
        logz = float(res.logz[-1])
        logzerr = float(res.logzerr[-1])
        weights = np.exp(res.logwt - res.logz[-1])
        weights = weights / weights.sum()
        ess = 1.0 / float(np.sum(weights ** 2))             # Kish effective sample size
        eq = resample_equal(res.samples, weights, rstate=rng)
    except Exception as exc:                                # → frequentist fallback + flag
        return _failed(f"sampler failed: {exc}")

    od, g = eq[:, od_i], eq[:, g_i]
    omega0_samples = np.sqrt(od ** 2 + g ** 2)
    gamma_samples = np.asarray(g, dtype=float)
    omega0_interval = _percentile_interval(omega0_samples, level)
    gamma_interval = _percentile_interval(gamma_samples, level)

    ok, reason = _diagnose(res, prob.bounds, od_i, od, omega0_interval, ess, max_iter)
    return BayesResult(omega0_interval=omega0_interval, gamma_interval=gamma_interval,
                       omega0_samples=omega0_samples, gamma_samples=gamma_samples,
                       log_evidence=logz, log_evidence_err=logzerr, ess=ess,
                       bimodal=is_bimodal(omega0_samples), converged=ok,
                       n_live=int(n_live), seed=int(seed), prior_used=prior_used,
                       fail_reason=reason, parameter_names=tuple(names),
                       parameter_samples=np.asarray(eq, dtype=float))

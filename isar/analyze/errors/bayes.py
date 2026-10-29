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
import logging
from dataclasses import dataclass

import numpy as np
import dynesty
from dynesty.utils import resample_equal

from isar.analyze import loop
from isar.analyze.errors.bootstrap import _percentile_interval, is_bimodal
from isar.schema.statistics import SemanticsConstructionError, semantics_fingerprint
from isar.analyze import estimators

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
RAIL_FRAC = 0.15         # >15 % of ω₀ draws railed ⇒ ill-posed position (hard fail)

_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)
log = logging.getLogger(__name__)


@dataclass
class BayesResult:
    omega0_interval: tuple[float, float, float]   # (lo, median, hi) credible interval
    gamma_interval: tuple[float, float, float]
    omega0_samples: np.ndarray                    # equal-weight posterior draws of ω₀ (``.omega0``)
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
    prior_fingerprint: str | None = None
    prior_specification: dict | None = None
    # The evidence half of the gate (:func:`_evidence_gate`), for a caller that needs lnZ, not the
    # interval: the count ladder's rung 2.
    evidence_converged: bool = False
    evidence_fail_reason: str | None = None
    # The highest-likelihood sample by parameter name: rung 2 refits from it.
    max_likelihood_sample: dict | None = None


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


def _evidence_gate(res, max_iter):
    """The checks that judge the evidence itself, ``(ok, reason)``: a finite logz, the dlogz target
    reached before ``max_iter``, and logzerr within the coarse bound. The rest of :func:`_diagnose`
    judges the posterior's interval (its ESS, a railed ω₀, a degenerate ω₀ interval)."""
    logz = float(res.logz[-1])
    logzerr = float(res.logzerr[-1])
    if not np.isfinite(logz):
        return False, "logz not finite"
    if max_iter is not None and int(res.niter) >= int(max_iter):
        return False, "hit max_iter before dlogz target"
    if not np.isfinite(logzerr) or logzerr > LOGZERR_MAX:    # coarse evidence blow-up guard
        return False, f"logzerr={logzerr:.2f} exceeds {LOGZERR_MAX}"
    return True, None


def _diagnose(res, bounds, omega0_i, omega0_draws, omega0_interval, ess, max_iter):
    """The silent-failure gate (§5.4). Returns ``(ok, reason)``; ALL hard checks must pass.

    Γ piled at its floor is deliberately NOT a hard failure — it is the overdamped/Γ→0 physics
    the Bayesian path exists to handle by prior support; only ω₀ running to the window edge
    (an ill-posed *position*) hard-fails on railing."""
    ok, reason = _evidence_gate(res, max_iter)
    if not ok:
        return ok, reason
    if not np.isfinite(ess) or ess < ESS_MIN:                # primary interval-quality gate
        return False, f"ess={ess:.0f} below {ESS_MIN:.0f}"
    if omega0_i is None:                                     # phonon-less: position checks vacuous
        return True, None
    omega0_lo, omega0_hi = bounds[omega0_i]
    if _rail_fraction(omega0_draws, omega0_lo, omega0_hi, RAIL_RTOL) > RAIL_FRAC:
        return False, "omega0 railed against the window (ill-posed position)"
    lo, _med, hi = omega0_interval
    if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
        return False, "omega0 interval degenerate"
    return True, None


@dataclass(frozen=True)
class EvidenceProblem:
    """The prior-bundle sampling problem of one candidate, shared by :func:`bayesian_posterior` and
    the deep pass (:mod:`isar.analyze.deep_pass`) so both sample the same loglike and transform.

    Picklable: it holds ``(hyp, ctx, bundle)``, the objects ``evidence._structure_evidence_task``
    already ships to pool workers, and builds its closures where it is called. ``ctx`` is the
    candidate's own context (``loop.candidate_context``); the loglike is the pure Baker-Cousins
    ``-cost/2`` (the priors enter through ``bundle.transform_ordered``)."""

    hyp: object
    ctx: object          # loop.candidate_context(hyp, scan ctx)
    bundle: object       # catalogue.PriorBundle from evidence.candidate_bundle(hyp, scan ctx)

    def setup(self):
        """``(problem, names, free_pos, template)``; ``ValueError`` when the bundle does not match
        the sampled (free, non-hard-fixed) parameters. ``template`` is the full parameter vector
        with the hard-fixed values in place (``None`` when nothing is fixed)."""
        prob = loop.build_problem(self.hyp, self.ctx)
        names = prob.free_names
        by_name = self.bundle.by_name()
        free_pos = list(range(len(names)))
        template = None
        if prob.fixed_pos:
            # A hard-fixed coordinate is held at its value and never integrated: the sampler works
            # in the free subspace, as the optimizer does (loop._minimize_fixed). A prior on it
            # would integrate a dimension the evidence must not have, so that bundle is refused.
            frozen = [names[i] for i in prob.fixed_pos]
            if any(n in by_name for n in frozen):
                raise ValueError(f"prior_bundle carries a prior for hard-fixed parameter(s) "
                                 f"{[n for n in frozen if n in by_name]}; a frozen coordinate "
                                 f"is never integrated")
            fixed = set(prob.fixed_pos)
            free_pos = [i for i in range(len(names)) if i not in fixed]
            template = np.asarray(prob.x0, dtype=float).copy()
            template[list(prob.fixed_pos)] = np.asarray(prob.fixed_vals, dtype=float)
        sampled = [names[i] for i in free_pos]
        missing = [n for n in sampled if n not in by_name]
        if missing:
            raise ValueError(f"prior_bundle has no prior for sampled parameter(s) {missing}; "
                             f"every free parameter must carry exactly one prior factor")
        if [p.name for p in self.bundle.params] != sampled:
            raise ValueError("prior_bundle parameters are not in the sampled parameter order; "
                             "the ordered transform assigns by position")
        return prob, names, free_pos, template

    def __call__(self):
        """``(loglike, ptform, ndim)`` for dynesty."""
        prob, _names, free_pos, template = self.setup()
        x, counts, ctx = self.ctx.x_used, self.ctx.counts_used, self.ctx
        est = estimators.get_estimator("baker_cousins")
        composite = prob.composite

        def loglike(theta):
            if template is not None:
                full = template.copy()
                full[free_pos] = theta
                theta = full
            ll = -0.5 * est.cost(counts, composite.evaluate(x, np.asarray(theta, dtype=float), ctx))
            return ll if np.isfinite(ll) else -1e300

        return loglike, self.bundle.transform_ordered, len(free_pos)

    def full_params(self, theta) -> dict:
        """The sampled coordinates ``theta`` (bundle order) as every parameter by name, the
        hard-fixed ones at their pinned values."""
        _prob, names, free_pos, template = self.setup()
        if template is None:
            full = np.asarray(theta, dtype=float)
        else:
            full = template.copy()
            full[free_pos] = theta
        return {n: float(v) for n, v in zip(names, full)}


def bayesian_posterior(hyp, ctx, fit_result, *, prior=None, prior_bundle=None,
                       seed: int = 12345,
                       n_live: int = DEFAULT_NLIVE, dlogz: float = DEFAULT_DLOGZ,
                       level: float = 0.68, max_iter: int | None = None,
                       sample: str = "rwalk", bound: str = "multi") -> BayesResult:
    """Sample the posterior of one fitted hypothesis; return ω₀/Γ credible intervals, the raw
    equal-weight posterior draws (for §4 pooling), the marginal-likelihood evidence, and the
    §5.4 convergence gate. In the diagnostics tier (no ``prior_bundle``) a no-op
    (``converged=False``) on a non-converged fit, mirroring the bootstrap; the evidence lane samples
    it anyway. ``prior`` (a :class:`~isar.analyze.prior.PhononPrior`) makes the dispersion
    expectation a literal Normal prior on ω₀; ``None`` ⇒ weakly-informative (posterior ≈
    likelihood ≈ bootstrap in the well-counted regime, by Bernstein–von Mises).

    ``prior_bundle`` (a :class:`~isar.analyze.catalogue.PriorBundle`, SCENE-1 PR-b) is the
    per-parameter proper-prior path the evidence ladder escalates to: each parameter is sampled
    through its own truncated prior ppf, so the priors enter via the prior transform and the
    loglike stays pure data — ``log_evidence`` is then the properly prior-weighted lnZ,
    consistent with the Laplace lanes. Mutually exclusive with ``prior`` (the ω₀-diagnostics
    tier), which stays a loglike term on the DERIVED ω₀ and is untouched. The bundle must cover
    every sampled (free, non-hard-fixed) parameter, in order — a missing name raises. A hard-fixed
    parameter is held at its value and never sampled (the sampler works in the free subspace), so
    a bundle carrying a prior for it raises: it would integrate a dimension the evidence must not
    have."""
    if prior is not None and prior_bundle is not None:
        raise ValueError("prior and prior_bundle are mutually exclusive: the PhononPrior "
                         "diagnostics tier and the evidence prior-transform tier answer "
                         "different questions")
    prior_used = prior is not None
    prior_specification = None
    prior_fingerprint = None
    nan3 = (float("nan"), float("nan"), float("nan"))

    def _failed(reason: str) -> BayesResult:
        return BayesResult(omega0_interval=nan3, gamma_interval=nan3,
                           omega0_samples=np.empty(0), gamma_samples=np.empty(0),
                           log_evidence=float("nan"), log_evidence_err=float("nan"),
                           ess=0.0, bimodal=False, converged=False, n_live=int(n_live),
                           seed=int(seed), prior_used=prior_used, fail_reason=reason,
                           parameter_samples=np.empty((0, 0)),
                           prior_fingerprint=prior_fingerprint,
                           prior_specification=prior_specification)

    # The diagnostics tier interprets a fitted winner and mirrors the bootstrap's refusal. The
    # evidence lane judges the model, not the fit: the sampler never reads θ̂, so a model that was
    # hard to optimize is still sampled (design 2026-10-22 §3.1).
    if prior_bundle is None and not fit_result.optimizer_converged:
        return _failed("fit did not converge")

    # NB (steering contract 1): nested sampling carries its own ω₀ prior (below, via ``prior_used``);
    # it does NOT apply ``ctx.constraints`` soft penalties. Soft Γ/A constraints are honoured by the
    # authoritative bootstrap + profile intervals, not this opt-in Bayesian path.
    if prior_bundle is not None:
        problem = EvidenceProblem(hyp, ctx, prior_bundle)
        prob, names, free_pos, template = problem.setup()
    else:
        problem = None
        prob = loop.build_problem(hyp, ctx)
        names = prob.free_names
        free_pos = list(range(len(names)))
        template = None
    x = ctx.x_used
    counts = ctx.counts_used
    est = estimators.get_estimator("baker_cousins")
    composite = prob.composite
    # Phonon-less composites (the PR-a first-class nulls: bg / elastic+bg) have no ω₀/Γ —
    # they sample and return evidence like any structure, with the ω₀/Γ deliverables vacuous
    # (empty samples, NaN intervals) rather than faked. Only the evidence lane sends them here.
    omega0_name = next((n for n in names if n.endswith(".omega0")), None)
    omega0_i = names.index(omega0_name) if omega0_name is not None else None
    g_name = next((n for n in names if n.endswith(".Gamma")), None)
    g_i = names.index(g_name) if g_name is not None else None
    if prior_used and omega0_i is None:
        raise ValueError("PhononPrior is a prior on omega0; it cannot apply to a "
                         "phonon-less composite")

    lows = np.array([b[0] for b in prob.bounds], dtype=float)
    spans = np.array([b[1] - b[0] for b in prob.bounds], dtype=float)
    if prior_bundle is not None:
        by_name = prior_bundle.by_name()
        sampled = [names[i] for i in free_pos]
        prior_specification = {
            "prior_kind": "catalogue_prior_bundle",
            "parameter_prior": "per_parameter_truncated_ppf",
            "parameters": [by_name[n].spec() for n in sampled],
            "hard_fixed": {names[i]: float(template[i]) for i in prob.fixed_pos},
            # Each group of exchangeable mode energies is sampled as ordered statistics of its
            # penalised energy density (N! prod g(E_i), the Laplace lane's logpdf_ordered), so N
            # swapped labellings are one solution, not N!.
            "mode_energy_order": [[sampled[i] for i in g] for g in prior_bundle._energy_groups()],
            "omega_loglike_term": None,
        }
        semantics_id = "isar.catalogue_prior_bundle_nested_prior/4"
    else:
        prior_specification = {
            "prior_kind": "optimizer_bound_data_dependent",
            "parameter_prior": "uniform_over_optimizer_bounds",
            "optimizer_bounds": [
                {"parameter": str(name), "lower": float(bounds[0]), "upper": float(bounds[1])}
                for name, bounds in zip(names, prob.bounds)
            ],
            "omega_loglike_term": (
                {
                    "kind": "normalized_gaussian",
                    "mean": float(prior.omega0),
                    "sigma": float(prior.omega0_sigma),
                }
                if prior_used
                else None
            ),
        }
        semantics_id = "isar.optimizer_bound_nested_prior/1"

        def ptform(u):
            return lows + np.asarray(u) * spans

    try:
        prior_fingerprint = semantics_fingerprint(semantics_id, prior_specification)
    except SemanticsConstructionError as exc:
        log.warning(
            "semantics_construction_failed producer=isar.analyze.errors.bayes "
            "quantity=nested_prior reason_code=%s",
            exc.reason_code,
        )
        prior_fingerprint = None

    mu = float(prior.omega0) if prior_used else 0.0
    sig = float(prior.omega0_sigma) if prior_used else 1.0

    def embed(theta):
        if template is None:
            return np.asarray(theta, dtype=float)
        full = template.copy()
        full[free_pos] = theta
        return full

    if problem is not None:                                 # the evidence tier: pure data loglike
        loglike, ptform, _ndim = problem()
    else:
        def loglike(theta):
            theta = embed(theta)
            f = composite.evaluate(x, theta, ctx)
            ll = -0.5 * est.cost(counts, f)                 # exact Baker–Cousins −2lnL/2
            if prior_used:                                  # guarded above: omega0_i exists
                ll += _gauss_logpdf(float(theta[omega0_i]), mu, sig)
            return ll if np.isfinite(ll) else -1e300

    rng = np.random.default_rng(seed)
    try:                                                    # §5.4: sampler failure must not escape
        sampler = dynesty.NestedSampler(loglike, ptform, ndim=len(free_pos), nlive=int(n_live),
                                        bound=bound, sample=sample, rstate=rng)
        sampler.run_nested(dlogz=dlogz, maxiter=max_iter, print_progress=False)
        res = sampler.results
        logz = float(res.logz[-1])
        logzerr = float(res.logzerr[-1])
        weights = np.exp(res.logwt - res.logz[-1])
        weights = weights / weights.sum()
        ess = 1.0 / float(np.sum(weights ** 2))             # Kish effective sample size
        eq = resample_equal(res.samples, weights, rstate=rng)
        if template is not None:                            # back to the full parameter vector
            eq = np.array([embed(row) for row in eq])
    except Exception as exc:                                # → frequentist fallback + flag
        return _failed(f"sampler failed: {exc}")

    if omega0_i is not None:
        omega0_samples = np.asarray(eq[:, omega0_i], dtype=float)
        gamma_samples = np.asarray(eq[:, g_i], dtype=float)
        omega0_interval = _percentile_interval(omega0_samples, level)
        gamma_interval = _percentile_interval(gamma_samples, level)
    else:
        # No phonon → no ω₀/Γ deliverable. Empty samples and NaN intervals, never fakes —
        # the delivery contract for "no number" is None/NaN (PR-a null-winner rule).
        omega0_samples = gamma_samples = np.empty(0)
        omega0_interval = gamma_interval = nan3

    ok, reason = _diagnose(res, prob.bounds, omega0_i, omega0_samples, omega0_interval, ess,
                           max_iter)
    evidence_ok, evidence_reason = _evidence_gate(res, max_iter)
    best = int(np.argmax(res.logl))
    return BayesResult(omega0_interval=omega0_interval, gamma_interval=gamma_interval,
                       evidence_converged=evidence_ok, evidence_fail_reason=evidence_reason,
                       max_likelihood_sample={n: float(v)
                                              for n, v in zip(names, embed(res.samples[best]))},
                       omega0_samples=omega0_samples, gamma_samples=gamma_samples,
                       log_evidence=logz, log_evidence_err=logzerr, ess=ess,
                       bimodal=is_bimodal(omega0_samples), converged=ok,
                       n_live=int(n_live), seed=int(seed), prior_used=prior_used,
                       fail_reason=reason, parameter_names=tuple(names),
                       parameter_samples=np.asarray(eq, dtype=float),
                       prior_fingerprint=prior_fingerprint,
                       prior_specification=prior_specification)

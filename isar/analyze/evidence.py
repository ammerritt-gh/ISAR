"""Structure-level Bayesian evidence via Laplace, with a validity ladder (SCENE-1 PR-b).

Selection is moving from AIC to the posterior over structures, which needs a per-candidate
marginal likelihood ``lnZ``. The cheap route is the Laplace approximation around the existing
MLE fit — but a silent Gaussian-volume estimate on a boundary-pinned or prior-dominated fit is
exactly the kind of confident wrong number ISAR exists to refuse. So every evidence is produced
by a **ladder** that records which rung it came from and why:

1. ``"laplace"`` — Laplace at the existing θ̂. Valid when every informative prior the fit did
   NOT see is much wider than the posterior curvature (the fit's transported soft priors are
   already inside its objective: θ̂ is their MAP and HESSE is the posterior curvature, so no
   refit is needed for them).
2. ``"laplace_map"`` — a warm-started MAP refit under the full prior bundle, when an unseen
   prior is informative at the fit's scale. HESSE of the penalized objective IS the posterior
   curvature.
3. ``"dynesty"`` — nested sampling through :func:`~isar.analyze.errors.bayes.bayesian_posterior`
   with the same bundle in the prior transform, when the Gaussian picture itself is invalid
   (boundary-pinned parameter, missing/non-PD covariance, failed MAP refit). Never silent: the
   escalation reason is recorded in ``validity``.

Evidence is only comparable inside one **arena** — one observed-data likelihood. The arena is
pinned by :func:`isar.analyze.loop.data_likelihood_fingerprint`; :class:`EvidenceSet` refuses a
record from a different arena (:class:`ArenaMismatchError`) instead of quietly comparing
incommensurable numbers.

Prior counting is the H3/H4 exactly-once rule throughout: ``lnZ`` reads the DATA-only cost and
adds ``PriorBundle.logpdf`` once — a transported soft constraint IS the prior for its parameter
(never the penalty on top of the prior), and hard-fixed parameters contribute nothing (their k
is already reduced and :func:`~isar.analyze.catalogue.resolve_priors` excluded them).

Nothing consumes this module yet (commit 3 of the PR-b plan); select.py rebuilds on it next.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

import numpy as np

from isar.analyze import loop

# Rung-1 validity: an unseen informative prior must be at least this many times wider than the
# posterior width for Laplace-at-θ̂ to stand (else θ̂ is not the MAP and we refit). Empirical
# pin like loop.GAMMA_FLOOR; 5.0 keeps the prior's variation across the posterior mass ≲ a few
# percent in log-density.
CURVATURE_RATIO = 5.0
EVIDENCE_REVISION = "isar.laplace_evidence/1"

_LN_2PI = math.log(2.0 * math.pi)
log = logging.getLogger(__name__)


class ArenaMismatchError(ValueError):
    """Evidence from different observed-data likelihoods was about to be compared."""


@dataclass(frozen=True)
class EvidenceRecord:
    """One candidate structure's evidence, with full provenance of how it was obtained."""

    label: str
    log_evidence: float                 # lnZ (NaN ⇒ no valid evidence; see validity)
    log_evidence_err: float | None      # dynesty logzerr; None for the Laplace lanes
    evidence_method: str                # "laplace" | "laplace_map" | "dynesty" | "invalid"
    k_free: int
    arena_fingerprint: str
    prior_spec: tuple                   # PriorBundle.spec(), frozen
    validity: dict                      # per-rung flags + escalation reasons


@dataclass
class EvidenceSet:
    """The per-scan collection of candidate evidences, all pinned to one arena."""

    arena_fingerprint: str
    records: list = field(default_factory=list)

    def add(self, record: EvidenceRecord) -> None:
        if record.arena_fingerprint != self.arena_fingerprint:
            raise ArenaMismatchError(
                f"evidence for {record.label!r} carries arena "
                f"{record.arena_fingerprint!r}, set is pinned to {self.arena_fingerprint!r}; "
                f"cross-arena evidence comparison is meaningless")
        self.records.append(record)

    def by_label(self) -> dict:
        return {r.label: r for r in self.records}


def laplace_ln_z(cost: float, ln_prior: float, sigma_free: np.ndarray) -> float:
    """``lnZ = −cost/2 + ln p(θ̂) + (k/2)·ln 2π + ½·ln|Σ_free|``.

    ``cost`` is the Baker–Cousins data-only −2lnL (saturated constant inside ⇒ the result is
    arena-relative, which is all selection needs); ``sigma_free`` must be the covariance over
    the FREE parameters only. Raises if Σ is not positive definite — the caller's ladder must
    escalate, never swallow."""
    sigma = np.asarray(sigma_free, dtype=float)
    k = sigma.shape[0]
    sign, logdet = np.linalg.slogdet(sigma)
    if sign <= 0 or not np.isfinite(logdet):
        raise np.linalg.LinAlgError("covariance over free parameters is not positive definite")
    return float(-0.5 * cost + ln_prior + 0.5 * k * _LN_2PI + 0.5 * logdet)


def _effective_sigma(p) -> float:
    """A prior's width on the parameter's linear scale, for the curvature comparison.
    Lognormal σ is in log space; σ·μ (μ = linear-space median) is the small-σ linear width."""
    if p.kind == "lognormal":
        return float(p.sigma) * float(p.mu)
    return float(p.sigma)


def _free_submatrix(matrix, names, free_names):
    idx = [names.index(n) for n in free_names]
    m = np.asarray(matrix, dtype=float)
    return m[np.ix_(idx, idx)]


def _is_pd(sigma: np.ndarray) -> bool:
    try:
        np.linalg.cholesky(np.asarray(sigma, dtype=float))
        return True
    except np.linalg.LinAlgError:
        return False


def structure_evidence(hyp, ctx, fit_result, bundle, *, backend: str = "iminuit",
                       seed: int = 12345, n_live: int = 400,
                       max_iter: int | None = 50_000,
                       sample: str = "rwalk", dlogz: float = 0.5) -> EvidenceRecord:
    """Evidence for one fitted candidate through the validity ladder.

    ``bundle`` must be the :class:`~isar.analyze.catalogue.PriorBundle` resolved for exactly
    this candidate's free parameters — a name mismatch is a construction error, not a fit
    property, and raises. ``max_iter`` budgets the dynesty escalation (rung 3); a budget or
    sampler failure yields ``log_evidence=NaN`` with the reason recorded, never an exception."""
    arena = loop.data_likelihood_fingerprint(ctx)
    names = list(fit_result.params)
    free_names = [p.name for p in bundle.params]
    if set(free_names) - set(names) or len(free_names) != int(fit_result.k):
        raise ValueError(
            f"prior bundle names do not match the candidate's free parameters: bundle has "
            f"{len(free_names)} ({sorted(free_names)}), fit {fit_result.label!r} has "
            f"k={fit_result.k} of {sorted(names)}")

    validity: dict = {
        "optimizer_converged": bool(fit_result.optimizer_converged),
        "covariance_ok": bool(fit_result.covariance_ok),
    }

    def _record(ln_z, err, method) -> EvidenceRecord:
        return EvidenceRecord(label=fit_result.label, log_evidence=float(ln_z),
                              log_evidence_err=err, evidence_method=method,
                              k_free=int(fit_result.k), arena_fingerprint=arena,
                              prior_spec=tuple(bundle.spec()), validity=validity)

    if not fit_result.optimizer_converged:
        validity["reason"] = "fit did not converge"
        return _record(float("nan"), None, "invalid")

    # --- Gaussian-picture validity (escalate to dynesty when it breaks) ----------------------
    at_limit = fit_result.at_limit
    # An older fit without per-param flags is unknown → fail-safe: treat as pinned.
    pinned = ([n for n in free_names if at_limit.get(n, False)]
              if at_limit is not None else list(free_names))
    validity["boundary_pinned"] = pinned
    sigma_free = None
    if fit_result.covariance_ok and fit_result.parameter_covariance is not None:
        sigma_free = _free_submatrix(fit_result.parameter_covariance, names, free_names)
        validity["sigma_pd"] = _is_pd(sigma_free)
    else:
        validity["sigma_pd"] = False

    if pinned or not validity["sigma_pd"]:
        validity["escalation"] = "laplace_invalid"
        return _dynesty_evidence(hyp, ctx, fit_result, bundle, _record, validity,
                                 seed=seed, n_live=n_live, max_iter=max_iter,
                                 sample=sample, dlogz=dlogz)

    # --- Rung 1: Laplace at θ̂ ----------------------------------------------------------------
    # Transport priors are already in the fit objective (θ̂ is their MAP, HESSE their posterior
    # curvature); only priors the fit never saw can invalidate Laplace-at-θ̂.
    sig_post = np.sqrt(np.diag(sigma_free))
    narrow = [p.name for p in bundle.nontrivial if p.origin != "transport"
              and _effective_sigma(p) < CURVATURE_RATIO * sig_post[free_names.index(p.name)]]
    validity["unseen_informative"] = narrow
    if not narrow:
        ln_prior = bundle.logpdf(fit_result.params)
        try:
            return _record(laplace_ln_z(fit_result.cost, ln_prior, sigma_free), None, "laplace")
        except np.linalg.LinAlgError as exc:
            log.warning("laplace evidence failed for %s: %s", fit_result.label, exc)
            validity["escalation"] = "laplace_invalid"
            return _dynesty_evidence(hyp, ctx, fit_result, bundle, _record, validity,
                                     seed=seed, n_live=n_live, max_iter=max_iter,
                                     sample=sample, dlogz=dlogz)

    # --- Rung 2: warm-started MAP refit under the full bundle --------------------------------
    prob = loop.build_problem(hyp, ctx, x0=dict(fit_result.params))
    prob_names = prob.free_names
    by_name = bundle.by_name()
    prior_idx = [(i, by_name[n]) for i, n in enumerate(prob_names) if n in by_name]

    def neg2_log_posterior(theta):
        # Exactly-once: the bundle replaces prob.penalty outright (a transport prior appears
        # here as its normalized self, never penalty + prior).
        lp = sum(p.logpdf(float(theta[i])) for i, p in prior_idx)
        val = prob.costv(theta) - 2.0 * lp
        return val if np.isfinite(val) else 1e300

    be_names = [n.replace(".", "_") for n in prob_names]
    br = loop._minimize_fixed(backend, neg2_log_posterior, prob.x0, prob.bounds, be_names,
                              prob.fixed_pos, prob.fixed_vals)
    map_pinned = [n for n in free_names
                  if (br.at_limit or {}).get(n.replace(".", "_"), False)]
    validity["map_refit"] = {"success": bool(br.success),
                             "covariance_ok": bool(br.covariance_ok),
                             "boundary_pinned": map_pinned}
    if br.success and br.covariance_ok and br.covariance is not None and not map_pinned:
        sigma_map = _free_submatrix(br.covariance, prob_names, free_names)
        theta_map = {n: float(v) for n, v in zip(prob_names, br.x)}
        if _is_pd(sigma_map):
            cost_map = float(prob.costv(np.asarray(br.x, dtype=float)))
            return _record(laplace_ln_z(cost_map, bundle.logpdf(theta_map), sigma_map),
                           None, "laplace_map")
    validity["escalation"] = "map_refit_invalid"
    return _dynesty_evidence(hyp, ctx, fit_result, bundle, _record, validity,
                             seed=seed, n_live=n_live, max_iter=max_iter,
                             sample=sample, dlogz=dlogz)


def _dynesty_evidence(hyp, ctx, fit_result, bundle, _record, validity, *,
                      seed, n_live, max_iter, sample, dlogz) -> EvidenceRecord:
    """Rung 3: nested sampling with the bundle in the prior transform (budgeted, fail-closed)."""
    from isar.analyze.errors.bayes import bayesian_posterior

    try:
        bz = bayesian_posterior(hyp, ctx, fit_result, prior_bundle=bundle, seed=seed,
                                n_live=n_live, max_iter=max_iter, sample=sample, dlogz=dlogz)
    except ValueError as exc:
        # e.g. a hard-fixed candidate: the bundle rightly excludes the frozen parameter, and
        # sampling it would integrate a dimension the evidence must not have. Fail closed.
        validity["dynesty"] = {"attempted": False, "reason": str(exc)}
        return _record(float("nan"), None, "invalid")
    validity["dynesty"] = {"attempted": True, "converged": bool(bz.converged),
                           "fail_reason": bz.fail_reason, "ess": float(bz.ess),
                           "n_live": int(n_live)}
    if not np.isfinite(bz.log_evidence):
        return _record(float("nan"), None, "invalid")
    return _record(float(bz.log_evidence), float(bz.log_evidence_err), "dynesty")

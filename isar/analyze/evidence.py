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
2. ``"laplace_truncated"`` — rung 1 with a boundary-mass correction when the ONLY pins are
   Γ at its floor (H8, SCENE-1 PR-c): GAMMA_FLOOR is a modelling artifact and the DHO ⊗
   resolution model is regular as Γ → 0, so the one-sided pin is an integrable boundary —
   the Gaussian mass outside the support is shed analytically instead of paying for dynesty.
3. ``"laplace_map"`` — a warm-started MAP refit under the full prior bundle, when an unseen
   prior is informative at the fit's scale. HESSE of the penalized objective IS the posterior
   curvature.
4. ``"dynesty"`` — nested sampling through :func:`~isar.analyze.errors.bayes.bayesian_posterior`
   with the same bundle in the prior transform, when the Gaussian picture itself is invalid
   (non-Γ boundary-pinned parameter — Ω_d at the window edge is real physics and always pays
   for the sampler — missing/non-PD covariance, failed MAP refit). Never silent: the
   escalation reason is recorded in ``validity``.

Evidence is only comparable inside one **arena** — one observed-data likelihood. The arena is
pinned by :func:`isar.analyze.loop.data_likelihood_fingerprint`; :class:`EvidenceSet` refuses a
record from a different arena (:class:`ArenaMismatchError`) instead of quietly comparing
incommensurable numbers.

Prior counting is the H3/H4 exactly-once rule throughout: ``lnZ`` reads the DATA-only cost and
adds ``PriorBundle.logpdf`` once — a transported soft constraint IS the prior for its parameter
(never the penalty on top of the prior), and hard-fixed parameters contribute nothing (their k
is already reduced and :func:`~isar.analyze.catalogue.resolve_priors` excluded them).

:mod:`isar.analyze.select` consumes this module: it calls :func:`candidate_set_evidence` for the
converged candidates and carries the resulting :class:`EvidenceSet` on ``Selection.evidence``.
"""
from __future__ import annotations

import math
import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np

from isar.analyze import loop

# Rung-1 validity: an unseen informative prior must be at least this many times wider than the
# posterior width for Laplace-at-θ̂ to stand (else θ̂ is not the MAP and we refit). Empirical
# pin like loop.GAMMA_FLOOR; 5.0 keeps the prior's variation across the posterior mass ≲ a few
# percent in log-density.
CURVATURE_RATIO = 5.0
EVIDENCE_REVISION = "isar.laplace_evidence/1"
# Dominance pruning (owner ruling 2026-08-08): a deferred escalation whose evidence CEILING
# (lnZ ≤ −cost/2) sits this many nats below the trusted leader is recorded "dominated" instead
# of sampled. Reaching the 0.01 survivor floor needs the ceiling within ~ln(N/0.01) ≈ 7 nats
# of the leader; 100 leaves ~93 nats of slack for optimizer slop before pruning could matter.
PRUNE_MARGIN = 100.0
# Scoped escalation budget (owner ruling 2026-08-08): post-pruning deferred escalations sample
# at n_live=128 — their job is separating Occam gaps of 10–40 nats, where the larger logzerr
# (~0.6 vs ~0.3 nats) is decision-irrelevant. D4's n_live=400 stays for the bayes-tier error
# diagnostics (errors --errors bayes) and for direct structure_evidence calls.
ESCALATION_N_LIVE = 128
# Cost knob, read at call time so a child process spawned by a session pool honours it too
# (the constant alone cannot: a spawned child re-imports this module fresh). Production leaves it
# unset and gets ESCALATION_N_LIVE; the test suite caps it from the repo-root conftest, where the
# reasoning for that cap is written down. Documented alongside ISAR_WORKERS /
# ISAR_BOOTSTRAP_WORKERS as an ISAR_* environment knob.
ESCALATION_N_LIVE_ENV = "ISAR_ESCALATION_N_LIVE"
# Escalation wall-clock budget for ONE candidate set. This is a backstop against a pathological
# set, not a throughput lever: the expensive class (a superset of the winner) has an evidence
# ceiling ≥ the leader's BY CONSTRUCTION — that is exactly why dominance pruning cannot touch it
# — so a candidate skipped for budget is one whose posterior genuinely cannot be bounded away
# from the winner's, and the set degrades to the recorded-AIC lane rather than delivering an
# evidence claim it did not earn. Size it so only pathological sets reach it; a tight value
# would push ordinary campaign scans into that degraded lane. Checked BEFORE each escalation
# starts, so a set may overrun by the duration of one dynesty run (itself bounded by max_iter).
ESCALATION_BUDGET_S = 600.0
# Sentinel so the budget is resolved at CALL time from the constant above. Binding
# ``budget_s=ESCALATION_BUDGET_S`` directly in the signature would capture the value at import,
# which makes monkeypatching the constant a silent no-op and a test that tried it pass vacuously
# — the same trap ``escalation_n_live()`` exists to avoid. ``None`` still means "no budget".
_BUDGET_DEFAULT = object()

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
    evidence_method: str    # "laplace" | "laplace_truncated" | "laplace_map" | "dynesty" | "invalid"
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

    def unresolved_contenders(self) -> list:
        """Labels the escalation budget skipped whose evidence ceiling still sits at or above the
        best trusted evidence in the set — i.e. candidates that could have won had they been
        sampled. Non-empty means the set **cannot name a winner on evidence**: the caller must
        degrade to the recorded-AIC lane rather than rank on a posterior computed over an
        admittedly incomplete candidate set.

        A budget-skipped candidate whose ceiling is *below* the leader is not listed — its own
        rigorous bound already rules it out, so the winner stands. Compare
        :attr:`EvidenceRecord.evidence_method` ``"dominated"``, which is the same argument with
        ``PRUNE_MARGIN`` of slack and is never a contender."""
        best = max((r.log_evidence for r in self.records if _trusted_evidence(r)),
                   default=float("-inf"))

        def _contends(rec) -> bool:
            # Fail safe on a missing or non-finite ceiling: a bound we cannot read is a bound we
            # cannot use to rule the candidate out, so it counts as a contender. `nan >= best` is
            # False, which would silently take the OTHER direction and let a winner stand on an
            # incomplete set — the one outcome this method exists to prevent.
            bound = float(rec.validity.get("log_evidence_upper_bound", float("inf")))
            return True if not math.isfinite(bound) else bound >= best

        return [r.label for r in self.records
                if r.evidence_method == "unresolved" and _contends(r)]


def escalation_n_live() -> int:
    """The n_live the post-pruning escalation lane samples at: ``$ISAR_ESCALATION_N_LIVE`` when
    set, else :data:`ESCALATION_N_LIVE`. A malformed or non-positive value raises rather than
    silently falling back — a cost knob that quietly ignores what it was told would make a run's
    sampling resolution unknowable after the fact."""
    raw = os.environ.get(ESCALATION_N_LIVE_ENV)
    if raw is None or raw == "":
        return ESCALATION_N_LIVE
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{ESCALATION_N_LIVE_ENV}={raw!r} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"{ESCALATION_N_LIVE_ENV}={value} must be positive")
    return value


def _trusted_evidence(rec) -> bool:
    """Whether a record's lnZ may advance the leader / anchor a comparison: finite, and from a
    Laplace lane or a dynesty run that passed its own convergence gate."""
    if not np.isfinite(rec.log_evidence):
        return False
    if rec.evidence_method == "dynesty":
        return bool((rec.validity.get("dynesty") or {}).get("converged"))
    return rec.evidence_method in ("laplace", "laplace_truncated", "laplace_map")


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


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _boundary_mass_correction(pinned, params, sig_post, free_names, bundle):
    """ln of the Gaussian mass fraction inside the prior bounds along each pinned axis.

    The Laplace rung integrates the full Gaussian over ℝ^k; for a parameter pinned at an
    integrable boundary (the Γ floor, H8) roughly half that mass lies outside the support and
    lnZ must shed it: correction = Σ_pinned ln(Φ((hi−θ̂)/σ) − Φ((lo−θ̂)/σ)) ≈ ln ½ per pin.
    Cross-correlations with the pinned axes are ignored (the same independence the diagonal
    σ read already assumes). KNOWN BIAS, deliberate: at a genuinely railed bound the likelihood
    gradient is nonzero, so the local shape is one-sided-exponential rather than half-Gaussian
    and this factor over-corrects — measured ≈1–2 nats conservative against dynesty on a railed-Γ
    synthetic (2026-08-09). Selection-material gaps in this candidate class are 10–40 nats, so
    the cheap rung stays; a gradient-aware factor is the upgrade path if the bias ever matters.
    Returns None when the mass is degenerate or a pinned parameter has no resolved prior — the
    caller must escalate, never guess."""
    by_name = bundle.by_name()
    correction = 0.0
    for name in pinned:
        prior = by_name.get(name)
        if prior is None:
            return None
        theta = float(params[name])
        sigma = float(sig_post[free_names.index(name)])
        if not (np.isfinite(sigma) and sigma > 0):
            return None
        mass = _phi((prior.hi - theta) / sigma) - _phi((prior.lo - theta) / sigma)
        if not (np.isfinite(mass) and mass > 0.0):
            return None
        correction += math.log(mass)
    return correction


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
                       sample: str = "rwalk", dlogz: float = 0.5,
                       escalation: str = "run") -> EvidenceRecord:
    """Evidence for one fitted candidate through the validity ladder.

    ``bundle`` must be the :class:`~isar.analyze.catalogue.PriorBundle` resolved for exactly
    this candidate's free parameters — a name mismatch is a construction error, not a fit
    property, and raises. ``max_iter`` budgets the dynesty escalation (rung 3); a budget or
    sampler failure yields ``log_evidence=NaN`` with the reason recorded, never an exception.

    ``escalation="defer"`` stops at the Gaussian rungs: an UNPENALISED candidate that would
    escalate returns a ``"deferred_escalation"`` marker carrying its rigorous evidence ceiling
    (``log_evidence_upper_bound`` = −cost/2, from Z = ∫L·p ≤ L(θ̂) for a proper prior)
    instead of paying for dynesty — :func:`candidate_set_evidence` uses this to prune
    candidates whose ceiling cannot reach the survivor floor (dominance pruning, owner
    ruling 2026-08-08). A penalised (soft-prior) fit ignores the deferral and samples: θ̂ is its
    MAP, so −cost/2 is not a valid ceiling for it.

    A dynesty run that fails its own convergence gate yields ``log_evidence=NaN`` with
    ``evidence_method="invalid"``; the untrusted value and the reason stay in ``validity``."""
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

    def _escalate(reason: str) -> EvidenceRecord:
        validity["escalation"] = reason
        # The ceiling below is only a ceiling for an UNPENALISED fit. Z = ∫L·p ≤ L(θ̂) needs L(θ̂)
        # to be the maximum of the data likelihood; for a soft-constrained fit θ̂ is the MAP, so
        # cost(θ̂) ≥ cost(θ̂_MLE) and −cost/2 UNDERstates the bound. Pruning on an understated
        # ceiling can drop a candidate whose true lnZ sits above it, so a penalised fit is never
        # deferred — it pays for the sampler. Pruning was always an optimisation, never a
        # shortcut around one.
        if escalation == "defer" and fit_result.prior_penalty is None:
            validity["log_evidence_upper_bound"] = -0.5 * float(fit_result.cost)
            return _record(float("nan"), None, "deferred_escalation")
        return _dynesty_evidence(hyp, ctx, fit_result, bundle, _record, validity,
                                 seed=seed, n_live=n_live, max_iter=max_iter,
                                 sample=sample, dlogz=dlogz)

    # Γ pins are carved out of the escalation rule (H8, SCENE-1 PR-c): GAMMA_FLOOR is a
    # modelling artifact — the DHO ⊗ resolution model is regular as Γ → 0, so a Γ jammed
    # against the floor is an integrable boundary, not broken physics. With a healthy PD
    # covariance the one-sided pin is handled analytically below (truncated-Gaussian mass
    # correction) instead of paying for dynesty. Every other pin — Ω_d at the window edge
    # above all — keeps escalating: there the boundary IS physics (H8) and the sampler is
    # spent on a real question.
    # `at_limit is not None` keeps the fail-safe honest: an older fit with UNKNOWN pin status
    # must escalate, never take the truncated rung on a name coincidence.
    gamma_pinned = (at_limit is not None and bool(pinned)
                    and all(n.endswith(".Gamma") for n in pinned))
    if (pinned and not gamma_pinned) or not validity["sigma_pd"]:
        return _escalate("laplace_invalid")

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
            ln_z = laplace_ln_z(fit_result.cost, ln_prior, sigma_free)
        except np.linalg.LinAlgError as exc:
            log.warning("laplace evidence failed for %s: %s", fit_result.label, exc)
            return _escalate("laplace_invalid")
        if not gamma_pinned:
            return _record(ln_z, None, "laplace")
        correction = _boundary_mass_correction(
            pinned, fit_result.params, sig_post, free_names, bundle)
        if correction is None:
            return _escalate("laplace_invalid")
        validity["gamma_boundary_truncated"] = list(pinned)
        return _record(ln_z + correction, None, "laplace_truncated")

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

    be_names = [loop._sanitize(n) for n in prob_names]
    br = loop._minimize_fixed(backend, neg2_log_posterior, prob.x0, prob.bounds, be_names,
                              prob.fixed_pos, prob.fixed_vals)
    map_pinned = [n for n in free_names
                  if (br.at_limit or {}).get(loop._sanitize(n), False)]
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
    return _escalate("map_refit_invalid")


def candidate_set_evidence(candidates, ctx, *, budget_s=_BUDGET_DEFAULT,
                           **kwargs) -> EvidenceSet:
    """Evidence for one scan's candidate set, with dominance pruning of the dynesty rung.

    ``candidates`` is ``[(hyp, fit_result, bundle), ...]`` (converged fits only — the caller
    owns that filter). Strategy (owner ruling 2026-08-08): the Gaussian rungs run for every
    candidate first; escalations are then processed in descending order of their evidence
    ceiling ``lnZ ≤ −cost(θ̂)/2`` (exact for proper priors on an unpenalised fit, where θ̂ is the
    data MLE: Z = ∫L·p ≤ L(θ̂)·∫p = L(θ̂); a penalised fit is excluded from pruning entirely).
    A candidate whose ceiling sits more than ``PRUNE_MARGIN`` nats below the current leader
    is recorded ``"dominated"`` — its true posterior is bounded above by e^−PRUNE_MARGIN of
    the leader's, orders of magnitude under the survivor floor, so skipping the sampler
    changes no decision and no reported number beyond float precision. Everything closer
    than the margin pays for its dynesty run — precisely the genuinely ambiguous scans
    (blank-scan null-vs-phonon contests, overdamped Γ-at-bound physics that FITS well).

    The pruning inherits exactly the trust AIC always placed in the same number: cost(θ̂) is
    the constrained optimum the optimizer reported. The leader only advances on evidence
    worth trusting (Laplace lanes, or dynesty with its §5.4 gate passed), so a blown-up
    sampler cannot over-prune the rest of the set.

    ``budget_s`` (default :data:`ESCALATION_BUDGET_S`, ``None`` disables) caps the wall clock
    the escalation phase may spend. Candidates left unsampled are recorded ``"unresolved"`` with
    their ceiling — see :meth:`EvidenceSet.unresolved_contenders`, which decides whether the set
    can still name a winner. The budget is a backstop against a pathological set, not a
    throughput lever; see the constant."""
    # ponytail: escalations run serially; parallelize the surviving dynesty runs through the
    # session process pool (fit_one's boot_executor, task tuples like _fit_hypothesis_task)
    # when per-scan latency matters — owner-flagged follow-up 2026-08-08.
    if budget_s is _BUDGET_DEFAULT:
        budget_s = ESCALATION_BUDGET_S
    kwargs.setdefault("n_live", escalation_n_live())
    es = EvidenceSet(arena_fingerprint=loop.data_likelihood_fingerprint(ctx))
    deferred = []
    for hyp, fit, bundle in candidates:
        rec = structure_evidence(hyp, ctx, fit, bundle, escalation="defer", **kwargs)
        if rec.evidence_method == "deferred_escalation":
            deferred.append((hyp, fit, bundle, rec))
        else:
            es.add(rec)

    leader = max((r.log_evidence for r in es.records if _trusted_evidence(r)),
                 default=float("-inf"))
    deferred.sort(key=lambda item: item[3].validity["log_evidence_upper_bound"], reverse=True)
    started = time.monotonic()
    for hyp, fit, bundle, marker in deferred:
        bound = marker.validity["log_evidence_upper_bound"]
        if bound < leader - PRUNE_MARGIN:
            validity = dict(marker.validity)
            validity["dominated_by_leader_log_evidence"] = float(leader)
            validity["prune_margin"] = PRUNE_MARGIN
            es.add(EvidenceRecord(label=marker.label, log_evidence=float("nan"),
                                  log_evidence_err=None, evidence_method="dominated",
                                  k_free=marker.k_free,
                                  arena_fingerprint=marker.arena_fingerprint,
                                  prior_spec=marker.prior_spec, validity=validity))
            continue
        elapsed = time.monotonic() - started
        if budget_s is not None and elapsed >= budget_s:
            # Budget-exhausted honesty: the candidate is RECORDED as unresolved with its ceiling
            # intact, never dropped and never given a number it did not earn. NaN lnZ keeps it out
            # of the posterior exactly like "dominated"; unlike "dominated" it is not ruled out, so
            # unresolved_contenders() reports it and selection degrades. Ordered descending by
            # ceiling, so what survives the budget is always the strongest remaining claim.
            validity = dict(marker.validity)
            validity["escalation_budget_s"] = float(budget_s)
            validity["escalation_elapsed_s"] = float(elapsed)
            validity["leader_log_evidence"] = float(leader)
            log.warning("escalation budget %.1fs exhausted after %.1fs; %r left unresolved "
                        "(ceiling %.2f vs leader %.2f)",
                        budget_s, elapsed, marker.label, bound, leader)
            es.add(EvidenceRecord(label=marker.label, log_evidence=float("nan"),
                                  log_evidence_err=None, evidence_method="unresolved",
                                  k_free=marker.k_free,
                                  arena_fingerprint=marker.arena_fingerprint,
                                  prior_spec=marker.prior_spec, validity=validity))
            continue
        rec = structure_evidence(hyp, ctx, fit, bundle, escalation="run", **kwargs)
        es.add(rec)
        if _trusted_evidence(rec):
            leader = max(leader, rec.log_evidence)
    return es


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
    if not bz.converged:
        # A sampler that failed its own §5.4 gate returns a number, not evidence. The gates that
        # fail here (max_iter, ess < ESS_MIN, logzerr > LOGZERR_MAX, a railed Omega_d, a degenerate
        # interval) all leave logz finite, so finiteness alone cannot separate them — and logzerr
        # is allowed up to 5 nats, noise on the 10-40 nat Occam gaps selection turns on. Refusing
        # here rather than at each reader keeps one trust standard: this is the same condition
        # ``candidate_set_evidence._trusted`` already requires before a record may lead pruning.
        # The value survives in validity for diagnosis; it just may not steer.
        validity["dynesty"]["log_evidence_untrusted"] = float(bz.log_evidence)
        return _record(float("nan"), None, "invalid")
    return _record(float(bz.log_evidence), float(bz.log_evidence_err), "dynesty")

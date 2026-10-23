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
2. ``"laplace_truncated"`` — rung 1 with a boundary-mass correction when the fit's SINGLE pin
   is a Γ at its floor (H8, SCENE-1 PR-c): GAMMA_FLOOR is a modelling artifact and the DHO ⊗
   resolution model is regular as Γ → 0, so the one-sided pin is an integrable boundary —
   the Gaussian mass outside the support is shed analytically instead of paying for dynesty.
   Two or more pinned Γs escalate: the per-axis mass product would ignore their covariance.
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
adds ``PriorBundle.logpdf_ordered`` once (the per-parameter priors, with exchangeable mode energies
under the ordered-energy prior, so swapped labels are one solution) — a transported soft constraint IS the prior for its parameter
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
# Dominance pruning (owner ruling 2026-08-08, amended 2026-09-03): a deferred escalation whose
# evidence CEILING (lnZ ≤ −cost/2) sits this many nats below the trusted leader is recorded
# "dominated" instead of sampled. Reaching the 0.01 survivor floor needs the ceiling within
# ~ln(N/0.01) ≈ 7 nats of the leader; 30 leaves ~23 nats of slack for optimizer slop before
# pruning could matter. The original 100 sampled four structures per fit on the acoustic
# reference campaign (2026-09-03 profile: 136 of 140 s in nested sampling for one quicklook and
# its master), most of them tens of nats below the leader's ceiling with no route to survival.
PRUNE_MARGIN = 30.0
# Scoped escalation budget (owner ruling 2026-08-08, amended 2026-09-03): post-pruning deferred
# escalations sample at n_live=32 — their job is separating Occam gaps of 10–40 nats, where a
# logzerr of ~1.2 nats (vs ~0.6 at 128, ~0.3 at 400) is still decision-irrelevant. Measured on
# the acoustic reference campaign 2026-09-02: 128 live points cost ~5 min per fit and 32 cost
# ~1 min, with the same structures selected and the same points certified; the 128 default made
# a live campaign unwatchable for no decision benefit. D4's n_live=400 stays for the bayes-tier
# error diagnostics (errors --errors bayes) and for direct structure_evidence calls.
ESCALATION_N_LIVE = 32
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
    # Full vocabulary lives in isar.schema.statistics.EVIDENCE_METHODS: the ladder rungs
    # ("laplace" | "laplace_truncated" | "laplace_map" | "dynesty") plus the non-rung records
    # ("deferred_escalation" | "dominated" | "unresolved" | "invalid").
    evidence_method: str
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


def _inward_cost_gradients(hyp, ctx, fit_result, pinned, sig_post, free_names):
    """One-sided inward ``g = ∂(cost/2)/∂θ`` at θ̂ for each pinned axis, from the DATA cost.

    ``cost`` is −2lnL, so ``g`` is the decay rate of the likelihood as the parameter moves off
    its bound into the support — the shape parameter :func:`_boundary_mass_correction` needs.
    Forward difference with a step of 1e-2·σ (clipped to stay inside the box), since a pinned
    axis has no usable outward side. A non-finite cost at either point yields NaN for that
    axis, which the correction turns into an escalation."""
    prob = loop.build_problem(hyp, ctx, x0=dict(fit_result.params))
    order = list(prob.free_names)
    theta = np.array([float(fit_result.params[n]) for n in order], dtype=float)
    c0 = float(prob.costv(theta))
    gradients = {}
    for name in pinned:
        i = order.index(name)
        sigma = float(sig_post[free_names.index(name)])
        step = min(1e-2 * sigma, float(prob.bounds[i][1]) - theta[i])
        if not (np.isfinite(c0) and np.isfinite(step) and step > 0.0):
            gradients[name] = float("nan")
            continue
        probe = theta.copy()
        probe[i] += step
        c1 = float(prob.costv(probe))
        gradients[name] = (float("nan") if not np.isfinite(c1)
                           else max(0.0, (c1 - c0) / (2.0 * step)))
    return gradients


def _boundary_mass_correction(pinned, params, sig_post, free_names, bundle, gradients):
    """ln of the likelihood mass fraction inside the support along the pinned axis.

    ``pinned`` holds exactly ONE name by construction — the caller's ``gamma_pinned`` gate admits
    a single pin and escalates everything else — so the loop below sums one term and is exact.
    It stays a loop only so the shape of the call site does not have to change if a multivariate
    orthant mass ever lands; with two or more correlated pins a product of per-axis masses is
    wrong (see the gate's note), which is why the gate, not this function, refuses them.

    The Laplace rung integrates the full Gaussian over ℝ^k; for a parameter pinned at an
    integrable boundary (the Γ floor, H8) the mass outside the support must be shed. Along a
    pinned axis, with ``t = θ − θ̂`` and ``a = θ̂ − lo ≥ 0`` the offset of the estimate above the
    floor, the local likelihood is ``L(t) ∝ exp(−g·t − t²/2σ²)`` on the support ``t ≥ −a``.
    Completing the square gives the mass relative to the full Gaussian in closed form::

        ∫_{−a}^{∞} exp(−g·t − t²/2σ²) dt / (σ√2π)  =  exp(g²σ²/2) · Φ(a/σ − gσ)

    evaluated in log space as ``½·(gσ)² + log_ndtr(a/σ − gσ)``, which is stable in both tails:
    ``a/σ ≫ gσ`` (θ̂ well inside, correction → 0) would overflow the equivalent
    ``erfcx``-times-``exp`` product past ``a/σ ≈ 40``, and ``gσ`` large would underflow a plain
    ``exp × Φ``. At ``a = 0`` it reduces exactly to ``ln ½·erfcx(gσ/√2)``, and at ``g = 0, a = 0``
    to the plain half-Gaussian ``ln ½``; large ``gσ`` tends to ``1/(gσ√2π)``, the
    one-sided-exponential integral. Verified against ``scipy.integrate.quad`` on the raw
    integrand to ≤1e-15 nats. That gradient- and offset-awareness is what removes the old
    half-Gaussian factor's conservative bias at a genuinely railed bound, where the local shape
    is one-sided-exponential rather than half-Gaussian and θ̂ need not sit exactly on the floor.
    ``g`` is the one-sided numerical gradient from :func:`_inward_cost_gradients` (step 1e-2·σ).

    Cross-correlations with the pinned axes are ignored (the same independence the diagonal σ
    read already assumes), and the far bound's mass is neglected: the caller's gate guarantees
    θ̂ within ``GAMMA_BOUND_TOL`` of the floor while ``hi`` is ``GAMMA_MAX`` ≫ σ, so the upper
    tail is numerically nothing. That gate is re-asserted below — beyond it the local expansion
    is not the admitted-at-the-floor case this factor models, and the function refuses rather
    than extrapolate.

    Returns None when the mass is degenerate, a gradient is unavailable, or a pinned parameter
    has no resolved prior — the caller must escalate, never guess."""
    from scipy.special import log_ndtr

    from isar.analyze.phonon import GAMMA_BOUND_TOL

    by_name = bundle.by_name()
    correction = 0.0
    for name in pinned:
        prior = by_name.get(name)
        if prior is None:
            return None
        sigma = float(sig_post[free_names.index(name)])
        g = float(gradients.get(name, float("nan")))
        if not (np.isfinite(sigma) and sigma > 0 and np.isfinite(g) and g >= 0):
            return None
        offset = float(params[name]) - float(prior.lo)
        if not offset <= GAMMA_BOUND_TOL:
            return None
        a = max(0.0, offset)                            # θ̂ a hair below lo is numerical, not real
        ln_mass = 0.5 * (g * sigma) ** 2 + float(log_ndtr(a / sigma - g * sigma))
        if not np.isfinite(ln_mass):
            return None
        correction += ln_mass
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
    # A constrained-variant candidate carries the derived context it was fitted under, so the MAP
    # refit and the sampler see the same pinned parameter space as the fit. The arena is unchanged
    # by construction: constraints are not part of the data-likelihood fingerprint.
    ctx = loop.candidate_context(hyp, ctx)
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
        if escalation == "defer":
            if fit_result.prior_penalty is None:
                validity["log_evidence_upper_bound"] = -0.5 * float(fit_result.cost)
                return _record(float("nan"), None, "deferred_escalation")
            # A penalised fit is still deferred (so the caller can batch/parallelize it with the
            # rest), but it carries no valid ceiling and must never be dominance-pruned — see the
            # comment above. ``candidate_set_evidence`` reads this flag and routes such candidates
            # straight into the escalation set, skipping the dominance check entirely.
            validity["prune_exempt"] = True
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
    # must escalate, never take the truncated rung on a name coincidence. The value test against
    # the FLOOR is load-bearing: Γ has two bounds (phonon.GAMMA_FLOOR and GAMMA_MAX), and only
    # the floor is the H8 modelling artifact — a Γ railed at the CEILING is overdamped physics
    # the model cannot express, its likelihood gradient at the bound is genuinely nonzero, and
    # an overdamped-vs-two-mode contest is selection-material, so it pays for the sampler.
    from isar.analyze.phonon import GAMMA_BOUND_TOL

    # ponytail: single-pin only — the product of per-axis masses ignores the pinned block's
    # covariance (bivariate orthant ≈0.5-1.3 nats off at |rho|=0.9, which is selection-material);
    # multivariate truncated-Gaussian orthant via scipy.stats.multivariate_normal if multi-railed
    # candidates ever matter. Two or more pins escalate like any other invalid-Gaussian case.
    gamma_pinned = (at_limit is not None and len(pinned) == 1
                    and all(n.endswith(".Gamma")
                            and float(fit_result.params.get(n, float("inf"))) <= GAMMA_BOUND_TOL
                            for n in pinned))
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
        # Ordered mode energies (N!/W^N): the Gaussian at θ̂ covers one labelling, so the prior is
        # the ordered one, or lnZ undercounts every N-mode model by ln N! against the sampler.
        ln_prior = bundle.logpdf_ordered(fit_result.params)
        try:
            ln_z = laplace_ln_z(fit_result.cost, ln_prior, sigma_free)
        except np.linalg.LinAlgError as exc:
            log.warning("laplace evidence failed for %s: %s", fit_result.label, exc)
            return _escalate("laplace_invalid")
        if not gamma_pinned:
            return _record(ln_z, None, "laplace")
        gradients = _inward_cost_gradients(hyp, ctx, fit_result, pinned, sig_post, free_names)
        # Only finite gradients enter validity: this dict is persisted through candidate rows,
        # and canonical JSON refuses NaN. Axes whose gradient could not be measured are named
        # separately — on that path the correction returns None and the record escalates.
        validity["boundary_gradient"] = {
            n: float(g) for n, g in gradients.items() if np.isfinite(g)}
        unavailable = sorted(n for n, g in gradients.items() if not np.isfinite(g))
        if unavailable:
            validity["boundary_gradient_unavailable"] = unavailable
        correction = _boundary_mass_correction(
            pinned, fit_result.params, sig_post, free_names, bundle, gradients)
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
            return _record(laplace_ln_z(cost_map, bundle.logpdf_ordered(theta_map), sigma_map),
                           None, "laplace_map")
    return _escalate("map_refit_invalid")


def _structure_evidence_task(args):
    """Picklable process-worker entry point for one deferred escalation (mirrors
    :func:`isar.analyze.loop._fit_hypothesis_task`)."""
    hyp, ctx, fit_result, bundle, kwargs = args
    return structure_evidence(hyp, ctx, fit_result, bundle, escalation="run", **kwargs)


def _is_prune_exempt(marker) -> bool:
    """A penalised fit's deferred marker: no valid ceiling (θ̂ is its MAP, not the data MLE, so
    −cost/2 would understate lnZ), so it must never be dominance-pruned — it always escalates."""
    return bool(marker.validity.get("prune_exempt"))


def _ceiling_str(marker) -> str:
    bound = marker.validity.get("log_evidence_upper_bound")
    return "n/a (prune-exempt)" if bound is None else f"{bound:.2f}"


def _run_deferred_serial(es, deferred, leader, started, budget_s, ctx, kwargs) -> float:
    """The original single-threaded ladder: per-item dominance against the live-updating
    ``leader``, budget checked before each escalation starts. A prune-exempt (penalised-fit)
    candidate skips BOTH checks — it has no ceiling to be dominated by, and it must always be
    sampled (never left "unresolved": an unresolved candidate can force ``select_models`` onto
    the AIC fallback, and a penalised fit's own posterior is exactly the number selection needs).
    Returns the updated leader."""
    for hyp, fit, bundle, marker in deferred:
        exempt = _is_prune_exempt(marker)
        if not exempt and marker.validity["log_evidence_upper_bound"] < leader - PRUNE_MARGIN:
            validity = dict(marker.validity)
            validity["dominated_by_leader_log_evidence"] = float(leader)
            validity["prune_margin"] = PRUNE_MARGIN
            es.add(EvidenceRecord(label=marker.label, log_evidence=float("nan"),
                                  log_evidence_err=None, evidence_method="dominated",
                                  k_free=marker.k_free,
                                  arena_fingerprint=marker.arena_fingerprint,
                                  prior_spec=marker.prior_spec, validity=validity))
            continue
        if not exempt:
            elapsed = time.monotonic() - started
            if budget_s is not None and elapsed >= budget_s:
                # Budget-exhausted honesty: the candidate is RECORDED as unresolved with its
                # ceiling intact, never dropped and never given a number it did not earn. NaN lnZ
                # keeps it out of the posterior exactly like "dominated"; unlike "dominated" it is
                # not ruled out, so unresolved_contenders() reports it and selection degrades.
                # Ordered descending by ceiling, so what survives the budget is always the
                # strongest remaining claim. Never reached for a prune-exempt candidate above.
                validity = dict(marker.validity)
                validity["escalation_budget_s"] = float(budget_s)
                validity["escalation_elapsed_s"] = float(elapsed)
                validity["leader_log_evidence"] = float(leader)
                log.warning("escalation budget %.1fs exhausted after %.1fs; %r left unresolved "
                            "(ceiling %s vs leader %.2f)",
                            budget_s, elapsed, marker.label, _ceiling_str(marker), leader)
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
    return leader


def candidate_set_evidence(candidates, ctx, *, budget_s=_BUDGET_DEFAULT, executor=None,
                           **kwargs) -> EvidenceSet:
    """Evidence for one scan's candidate set, with dominance pruning of the dynesty rung.

    ``candidates`` is ``[(hyp, fit_result, bundle), ...]`` (converged fits only — the caller
    owns that filter). Strategy (owner ruling 2026-08-08): the Gaussian rungs run for every
    candidate first; escalations are then processed in descending order of their evidence
    ceiling ``lnZ ≤ −cost(θ̂)/2`` (exact for proper priors on an unpenalised fit, where θ̂ is the
    data MLE: Z = ∫L·p ≤ L(θ̂)·∫p = L(θ̂); a penalised fit carries no valid ceiling and is
    excluded from pruning entirely — it is still *deferred* rather than run inline, so it can
    join the same escalation batch, but it is marked ``validity["prune_exempt"]`` and sorted
    ahead of the ceiling-ranked candidates, since it has no ceiling to rank by).
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
    throughput lever; see the constant.

    ``executor`` (default ``None``, serial) parallelizes the surviving deferred escalations —
    the per-fit dynesty runs that are ~85% of quicklook wall time (2026-09-03 profile) — through
    a :class:`concurrent.futures.Executor` such as the session's ``boot_executor`` process pool.
    Used only when more than one candidate survives the pre-batch dominance prune; the prune is
    then taken against the leader known *before* the batch runs, so a candidate sampled inside
    the batch can no longer prune a lower-ceiling batch-mate the way the serial loop's
    mid-loop leader update could. With ``PRUNE_MARGIN=30`` nats of slack that difference is
    decision-irrelevant. The serial path (``executor=None``, or a set with ≤1 survivor) keeps
    the exact original per-item semantics. Submission failure (unpicklable candidate, broken
    pool) cancels every future still pending, logs a warning, and falls back to the serial path
    — on the SAME budget clock when any future was actually submitted (real sampler time may
    already be spent, in-flight or discarded), on a fresh one only when submission failed before
    anything started. The budget clock itself starts once, before the Gaussian pass, and is
    shared by both paths — on the parallel path the budget gates only whether the batch STARTS;
    once it starts, every survivor in it completes (no partial unresolve after the fact). A
    prune-exempt (penalised-fit) candidate is further exempt from the budget itself, in both
    paths: it always gets sampled, never recorded "unresolved"."""
    if budget_s is _BUDGET_DEFAULT:
        budget_s = ESCALATION_BUDGET_S
    kwargs.setdefault("n_live", escalation_n_live())
    es = EvidenceSet(arena_fingerprint=loop.data_likelihood_fingerprint(ctx))
    started = time.monotonic()
    deferred = []
    for hyp, fit, bundle in candidates:
        rec = structure_evidence(hyp, ctx, fit, bundle, escalation="defer", **kwargs)
        if rec.evidence_method == "deferred_escalation":
            deferred.append((hyp, fit, bundle, rec))
        else:
            es.add(rec)

    leader = max((r.log_evidence for r in es.records if _trusted_evidence(r)),
                 default=float("-inf"))
    # Prune-exempt (penalised-fit) candidates always escalate, so ordering them against a
    # ceiling is meaningless — they sort first, ahead of the ceiling-descending rest.
    deferred_exempt = [item for item in deferred if _is_prune_exempt(item[3])]
    deferred_ceiling = [item for item in deferred if not _is_prune_exempt(item[3])]
    deferred_ceiling.sort(key=lambda item: item[3].validity["log_evidence_upper_bound"],
                          reverse=True)
    deferred = deferred_exempt + deferred_ceiling

    pre_leader = leader
    survivors = [item for item in deferred
                 if _is_prune_exempt(item[3])
                 or item[3].validity["log_evidence_upper_bound"] >= pre_leader - PRUNE_MARGIN]

    if executor is not None and len(survivors) > 1:
        dominated = [item for item in deferred
                     if not _is_prune_exempt(item[3])
                     and item[3].validity["log_evidence_upper_bound"] < pre_leader - PRUNE_MARGIN]
        for _, _, _, marker in dominated:
            validity = dict(marker.validity)
            validity["dominated_by_leader_log_evidence"] = float(pre_leader)
            validity["prune_margin"] = PRUNE_MARGIN
            es.add(EvidenceRecord(label=marker.label, log_evidence=float("nan"),
                                  log_evidence_err=None, evidence_method="dominated",
                                  k_free=marker.k_free,
                                  arena_fingerprint=marker.arena_fingerprint,
                                  prior_spec=marker.prior_spec, validity=validity))
        # Budget gates only whether the batch STARTS. A prune-exempt survivor is exempt from the
        # budget too — it must always run — so only non-exempt survivors are ever marked
        # "unresolved" here; an exhausted budget with nothing but exempt survivors left simply
        # runs a smaller batch.
        elapsed = time.monotonic() - started
        budget_exhausted = budget_s is not None and elapsed >= budget_s
        if budget_exhausted:
            to_unresolve = [item for item in survivors if not _is_prune_exempt(item[3])]
            to_run = [item for item in survivors if _is_prune_exempt(item[3])]
        else:
            to_unresolve = []
            to_run = survivors
        for _, _, _, marker in to_unresolve:
            validity = dict(marker.validity)
            validity["escalation_budget_s"] = float(budget_s)
            validity["escalation_elapsed_s"] = float(elapsed)
            validity["leader_log_evidence"] = float(pre_leader)
            log.warning("escalation budget %.1fs exhausted after %.1fs; %r left unresolved "
                        "(ceiling %s vs leader %.2f)",
                        budget_s, elapsed, marker.label, _ceiling_str(marker), pre_leader)
            es.add(EvidenceRecord(label=marker.label, log_evidence=float("nan"),
                                  log_evidence_err=None, evidence_method="unresolved",
                                  k_free=marker.k_free,
                                  arena_fingerprint=marker.arena_fingerprint,
                                  prior_spec=marker.prior_spec, validity=validity))
        if to_run:
            log.debug("parallel evidence escalation batch: %d candidate(s) via %s",
                      len(to_run), type(executor).__name__)
            results = None
            futures = []
            try:
                task_kwargs = dict(kwargs)
                for hyp, fit, bundle, _ in to_run:
                    futures.append(executor.submit(_structure_evidence_task,
                                                   (hyp, ctx, fit, bundle, task_kwargs)))
                results = [f.result() for f in futures]
            except Exception as exc:  # noqa: BLE001
                # Cancel every future still pending so a partial failure does not leave the rest
                # of the batch running to a discarded result. A future already running cannot be
                # cancelled (ThreadPoolExecutor/ProcessPoolExecutor both refuse); it still
                # completes in the background and its result is simply never collected.
                for f in futures:
                    if not f.cancel():
                        log.debug("a submitted escalation could not be cancelled (already "
                                 "running or done); its result completes and is discarded")
                log.warning("parallel evidence escalation failed (%s: %s); falling back to "
                            "serial escalation", type(exc).__name__, exc)
            if results is None:
                # Same budget clock once real work was submitted (a duplicate may already be
                # sampling); a fresh one only when submission failed before anything started.
                fallback_started = started if futures else time.monotonic()
                leader = _run_deferred_serial(es, to_run, pre_leader, fallback_started, budget_s,
                                              ctx, kwargs)
            else:
                for rec in results:
                    es.add(rec)
                trusted_vals = [r.log_evidence for r in results if _trusted_evidence(r)]
                if trusted_vals:
                    leader = max(pre_leader, max(trusted_vals))
    else:
        leader = _run_deferred_serial(es, deferred, leader, started, budget_s, ctx, kwargs)

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

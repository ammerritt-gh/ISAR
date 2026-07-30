"""Evidence-only candidate fitting and information-criterion selection for STAT-2b.

Nothing in this module is imported by production enumeration.  It deliberately adapts
production hypotheses and components into a compact campaign-owned representation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable, Mapping, Sequence

import numpy as np

from isar.analyze.components import ConstantBackground, Elastic, QuasiElastic
from isar.analyze.loop import _minimize_fixed, _sanitize, build_problem
from isar.analyze.structures import (
    Hypothesis,
    elastic_admissible,
    enumerate_multimode,
    quasielastic_admissible,
)


CRITERIA = ("aic", "aicc", "bic")
NULL_LABELS = ("bg", "elastic+bg", "qe+bg", "elastic+qe+bg")


@dataclass(frozen=True)
class CandidateEvidence:
    """One fitted candidate plus its prediction on the kept bins."""

    label: str
    k: int
    n: int
    cost: float
    optimizer_converged: bool
    prediction: np.ndarray
    result: object
    hypothesis: Hypothesis
    source: str


@dataclass(frozen=True)
class CriterionRow:
    label: str
    k: int
    n: int
    cost: float
    aic: float
    aicc: float
    bic: float
    delta: float
    weight: float


@dataclass(frozen=True)
class CriterionSelection:
    criterion: str
    status: str
    reason: str | None
    selected_label: str | None
    rows: tuple[CriterionRow, ...]


@dataclass(frozen=True)
class _NullResult:
    """The portion of StructureFitResult needed by the campaign."""

    label: str
    k: int
    ndof: int
    cost: float
    optimizer_converged: bool
    params: Mapping[str, float]
    fit_kind: str = "unconstrained"
    n_modes: int = 0
    modes: tuple = ()
    fit_semantics: None = None


def _finite(value: float) -> float:
    value = float(value)
    return value if math.isfinite(value) else float("inf")


def criterion_values(cost: float, k: int, n: int) -> dict[str, float]:
    """Return AIC/AICc/BIC with the campaign's explicit small-sample rule."""

    cost = _finite(cost)
    k = int(k)
    n = int(n)
    aic = cost + 2.0 * k
    denom = n - k - 1
    aicc = (
        aic + 2.0 * k * (k + 1) / denom
        if math.isfinite(aic) and denom > 0
        else float("inf")
    )
    bic = cost + k * math.log(n) if math.isfinite(cost) and n > 0 else float("inf")
    return {"aic": aic, "aicc": aicc, "bic": bic}


def select_by_criterion(
    candidates: Sequence[CandidateEvidence],
    criterion: str,
    *,
    required_labels: Sequence[str] | None = None,
) -> CriterionSelection:
    """Select from a complete fitted family; never normalize a surviving subset."""

    criterion = str(criterion).lower()
    if criterion not in CRITERIA:
        raise ValueError(f"unknown criterion: {criterion}")
    by_label = {candidate.label: candidate for candidate in candidates}
    required = tuple(required_labels or by_label)
    if len(by_label) != len(candidates):
        return CriterionSelection(criterion, "unavailable", "duplicate_candidate_label", None, ())
    missing = sorted(set(required) - set(by_label))
    if missing:
        return CriterionSelection(
            criterion, "unavailable", "missing_required_candidate", None, ()
        )
    failed = [
        label
        for label in required
        if not bool(by_label[label].optimizer_converged)
        or not math.isfinite(float(by_label[label].cost))
    ]
    if failed:
        return CriterionSelection(
            criterion, "unavailable", "required_candidate_fit_failed", None, ()
        )

    scored = []
    for label in required:
        candidate = by_label[label]
        values = criterion_values(candidate.cost, candidate.k, candidate.n)
        scored.append((candidate, values))
    finite = [values[criterion] for _, values in scored if math.isfinite(values[criterion])]
    if not finite:
        reason = "all_infinite_aicc" if criterion == "aicc" else "all_infinite_criterion"
        return CriterionSelection(criterion, "unavailable", reason, None, ())

    minimum = min(finite)
    relative = np.asarray(
        [
            math.exp(-0.5 * (values[criterion] - minimum))
            if math.isfinite(values[criterion])
            else 0.0
            for _, values in scored
        ],
        dtype=float,
    )
    total = float(relative.sum())
    weights = relative / total
    rows = tuple(
        CriterionRow(
            label=candidate.label,
            k=int(candidate.k),
            n=int(candidate.n),
            cost=float(candidate.cost),
            aic=float(values["aic"]),
            aicc=float(values["aicc"]),
            bic=float(values["bic"]),
            delta=(
                float(values[criterion] - minimum)
                if math.isfinite(values[criterion])
                else float("inf")
            ),
            weight=float(weight),
        )
        for (candidate, values), weight in zip(scored, weights)
    )
    winner = min(
        (row for row in rows if math.isfinite(getattr(row, criterion))),
        key=lambda row: (getattr(row, criterion), row.label),
    )
    return CriterionSelection(criterion, "available", None, winner.label, rows)


def prediction_for(hypothesis: Hypothesis, result: object, ctx) -> np.ndarray:
    """Evaluate a fitted production hypothesis in its declared parameter order."""

    composite = hypothesis.build()
    params = getattr(result, "params")
    theta = np.asarray([params[name] for name in composite.param_names()], dtype=float)
    return np.asarray(composite.evaluate(ctx.x_used, theta, ctx), dtype=float)


def evidence_from_results(
    results: Iterable[object],
    hypotheses: Iterable[Hypothesis],
    ctx,
    *,
    source: str,
) -> list[CandidateEvidence]:
    """Join fitted production results to hypotheses without a label-only fallback."""

    hypothesis_by_label = {hypothesis.label: hypothesis for hypothesis in hypotheses}
    evidence = []
    for result in results:
        label = str(result.label)
        hypothesis = hypothesis_by_label.get(label)
        if hypothesis is None:
            raise ValueError(f"result has no declared hypothesis: {label}")
        evidence.append(
            CandidateEvidence(
                label=label,
                k=int(result.k),
                n=int(result.ndof + result.k),
                cost=float(result.cost),
                optimizer_converged=bool(result.optimizer_converged),
                prediction=prediction_for(hypothesis, result, ctx),
                result=result,
                hypothesis=hypothesis,
                source=source,
            )
        )
    return evidence


def null_hypotheses(ctx) -> list[Hypothesis]:
    """Campaign-only null family under the production elastic/QE gates."""

    hypotheses = [Hypothesis("bg", (ConstantBackground,))]
    elastic = elastic_admissible(ctx)
    qe = quasielastic_admissible(ctx)
    if elastic:
        hypotheses.append(Hypothesis("elastic+bg", (Elastic, ConstantBackground)))
    if qe:
        hypotheses.append(Hypothesis("qe+bg", (QuasiElastic, ConstantBackground)))
    if elastic and qe:
        hypotheses.append(
            Hypothesis("elastic+qe+bg", (Elastic, QuasiElastic, ConstantBackground))
        )
    return hypotheses


def fit_null_candidates(ctx, *, backend: str = "iminuit") -> list[CandidateEvidence]:
    """Fit nulls through the production problem builder and optimizer boundary."""

    evidence = []
    for hypothesis in null_hypotheses(ctx):
        problem = build_problem(hypothesis, ctx)
        backend_names = [_sanitize(name) for name in problem.free_names]
        backend_result = _minimize_fixed(
            backend,
            problem.objective,
            problem.x0,
            problem.bounds,
            backend_names,
            problem.fixed_pos,
            problem.fixed_vals,
            minos=False,
        )
        theta = np.asarray(backend_result.x, dtype=float)
        params = {
            name: float(value) for name, value in zip(problem.free_names, theta)
        }
        cost = float(problem.costv(theta))
        result = _NullResult(
            label=hypothesis.label,
            k=int(problem.k),
            ndof=int(problem.n_used - problem.k),
            cost=cost,
            optimizer_converged=bool(backend_result.success),
            params=params,
        )
        evidence.append(
            CandidateEvidence(
                label=hypothesis.label,
                k=result.k,
                n=int(problem.n_used),
                cost=cost,
                optimizer_converged=result.optimizer_converged,
                prediction=np.asarray(
                    problem.composite.evaluate(ctx.x_used, theta, ctx), dtype=float
                ),
                result=result,
                hypothesis=hypothesis,
                source="campaign_null",
            )
        )
    return evidence


def oracle_hypotheses(ctx) -> list[Hypothesis]:
    """Complete two-mode production family; caller supplies the one-mode family."""

    return list(enumerate_multimode(ctx))


def candidate_identity(candidate: CandidateEvidence) -> str:
    """Stable evidence identity for reuse/deduplication checks."""

    record = {
        "label": candidate.label,
        "k": candidate.k,
        "n": candidate.n,
        "source": candidate.source,
        "params": dict(sorted(getattr(candidate.result, "params", {}).items())),
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mean_poisson_kl(truth_mean, fitted_mean, keep=None) -> float:
    """Mean-per-kept-bin analytic Poisson KL D(Pois(λ)||Pois(μ))."""

    lam = np.asarray(truth_mean, dtype=float)
    mu = np.asarray(fitted_mean, dtype=float)
    mask = np.ones(lam.shape, dtype=bool) if keep is None else np.asarray(keep, dtype=bool)
    lam = lam[mask]
    mu = mu[mask]
    if lam.size == 0:
        return float("nan")
    mu = np.maximum(mu, np.finfo(float).tiny)
    terms = mu.copy()
    positive = lam > 0.0
    terms[positive] = (
        lam[positive] * np.log(lam[positive] / mu[positive])
        + mu[positive]
        - lam[positive]
    )
    return float(np.mean(terms))


def mean_poisson_log_score(observed, fitted_mean, keep=None) -> float:
    """Mean log predictive density for one shared realized Poisson draw."""

    observed = np.asarray(observed, dtype=float)
    mu = np.asarray(fitted_mean, dtype=float)
    mask = (
        np.ones(observed.shape, dtype=bool)
        if keep is None
        else np.asarray(keep, dtype=bool)
    )
    observed = observed[mask]
    mu = np.maximum(mu[mask], np.finfo(float).tiny)
    if observed.size == 0:
        return float("nan")
    log_factorial = np.fromiter(
        (math.lgamma(float(value) + 1.0) for value in observed),
        dtype=float,
        count=observed.size,
    )
    return float(np.mean(observed * np.log(mu) - mu - log_factorial))

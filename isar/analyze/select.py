"""Model selection + modality (§4) — the layer that turns "we fit several structures" into
one honest answer.

Selection is by information criterion, **never raw chi^2** (D2): raw chi^2 always prefers the
model with more peaks and so fits noise as a phantom phonon — the named ``fit_phonons``
anti-pattern (§0.2). The structure loop's ``cost`` is the Baker-Cousins -2lnL
(``isar.synth.estimators.baker_cousins_cost``), so the implemented formulas are:

    AIC  = cost + 2k
    AICc = AIC + 2k(k+1)/(n-k-1)         (small-n correction; n/k ~ 5-9 on these scans)
    BIC  = cost + k*ln(n)

A data-only cost evaluated at a constrained optimum is not automatically a comparable data-only
MLE. STAT-1 records that eligibility without changing this legacy calculation; STAT-2 owns
enforcement. Akaike weights ``w_i = exp(-dAIC_i/2) / sum_j exp(-dAIC_j/2)`` rank the structures; those with
negligible weight drop out and the survivors are pooled (:mod:`pool_across_model`, built next)
into the across-model omega0 distribution that the modality test disposes (fold vs fork).

For strictly nested structures (e.g. ``stokes+bg`` subset of ``elastic+stokes+bg``) the
"nested F-test" of D2 is realised as the likelihood-ratio diagnostic: the deviance drop
``cost_reduced - cost_full`` is compared with chi2(dk). That reference is not generally calibrated
when an extra component lies on a boundary or is unidentified under the null. Non-converged fits
are excluded — a fit the optimizer never settled is not a structure "the data supports".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from isar.analyze.errors.bootstrap import kde_modes

DEFAULT_WEIGHT_FLOOR = 0.01
MODEL_SELECTION_REVISION = "isar.model_selection/1"


@dataclass
class ModelScore:
    """One structure's selection metrics. ``weight`` is the Akaike weight (primary);
    ``aicc``/``bic`` ride alongside; ``nested`` is the LRT vs the closest nested model (or
    ``None``); ``survives`` is ``weight >= weight_floor``."""

    label: str
    k: int
    n: int
    cost: float
    aic: float
    aicc: float
    bic: float
    delta_aic: float
    weight: float
    nested: dict | None
    survives: bool
    fit_semantics: dict | None = None


def _aicc(aic: float, k: int, n: int) -> float:
    denom = n - k - 1
    return aic + (2.0 * k * (k + 1) / denom) if denom > 0 else float("inf")


def _nested_test(full, candidates) -> dict | None:
    """Likelihood-ratio test of ``full`` against the most complex model strictly nested
    within it (params a proper subset). ``None`` if nothing is nested in ``full``."""
    from scipy.stats import chi2

    fp = set(full.params)
    nested_in = [r for r in candidates if set(r.params) < fp]
    if not nested_in:
        return None
    reduced = max(nested_in, key=lambda r: r.k)
    stat = float(reduced.cost - full.cost)          # deviance drop (>=0 when full fits better)
    dof = int(full.k - reduced.k)
    p = float(chi2.sf(max(stat, 0.0), dof)) if dof > 0 else float("nan")
    return {"reduced": reduced.label, "delta_deviance": stat, "dof": dof, "p_value": p}


def score_models(results, *, weight_floor: float = DEFAULT_WEIGHT_FLOOR) -> list[ModelScore]:
    """Score the converged structure fits by AIC/AICc/BIC, form Akaike weights, run the
    nested LRT, and flag survivors. Non-converged fits are excluded; all-non-converged
    returns ``[]`` (the verdict layer turns that into *unconstrained*)."""
    conv = [r for r in results if r.optimizer_converged]
    if not conv:
        return []
    aics = [r.cost + 2.0 * r.k for r in conv]
    aic_min = min(aics)
    rel = np.exp(-0.5 * (np.asarray(aics) - aic_min))
    weights = rel / float(rel.sum())
    out = []
    for r, aic, w in zip(conv, aics, weights):
        n = r.ndof + r.k
        out.append(ModelScore(
            label=r.label, k=r.k, n=n, cost=r.cost,
            aic=aic, aicc=_aicc(aic, r.k, n), bic=r.cost + r.k * math.log(n),
            delta_aic=aic - aic_min, weight=float(w),
            nested=_nested_test(r, conv), survives=bool(w >= weight_floor),
            fit_semantics=getattr(r, "fit_semantics", None),
        ))
    return out


# --- pooled across-model distribution + modality (fold / fork) -----------------------------


@dataclass
class Mode:
    """One peak of a forked across-model distribution: its location, normalised weight, and
    (lo, med, hi) interval over the samples assigned to it."""

    location: float
    weight: float
    interval: tuple


@dataclass
class Pooled:
    """The across-model omega0 distribution. ``modality == "fold"`` carries one ``interval``
    (already including the structural spread); ``"fork"`` carries ``modes`` + a
    ``disambiguation`` string and no single interval."""

    modality: str                 # "fold" | "fork"
    samples: np.ndarray
    weights: np.ndarray
    interval: tuple | None
    modes: list | None
    disambiguation: str | None


@dataclass
class Selection:
    """Scan-level model-selection outcome: per-structure ``scores``, the min-AIC ``best``, the
    surviving labels, and the pooled across-model ``Pooled`` (or ``None`` when nothing
    converged / no survivor carried bootstrap samples)."""

    scores: list
    best: str | None
    survivors: list
    pooled: Pooled | None
    modality: str
    fit_selection_semantics: dict | None = None


def _weighted_quantile(values, weights, q: float) -> float:
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    cdf = (np.cumsum(w) - 0.5 * w) / np.sum(w)
    return float(np.interp(q, cdf, v))


def _interval(values, weights, level: float) -> tuple:
    a = (1.0 - level) / 2.0
    return (_weighted_quantile(values, weights, a),
            _weighted_quantile(values, weights, 0.5),
            _weighted_quantile(values, weights, 1.0 - a))


def pool_across_model(survivors, *, level: float = 0.68) -> Pooled | None:
    """Pool the survivors' within-model omega0 samples into the across-model distribution and
    run the modality test. ``survivors`` is a list of ``(weight, omega0_samples)``; each
    structure's samples carry its Akaike weight (per-sample ``w/n``), so the mixture is the
    honest "what does this scan say omega0 is, not knowing the structure" (§4).

    Unimodal → **fold**: one weighted (lo, med, hi) interval that already includes the
    structural spread. Multimodal → **fork**: split at the KDE valley, one :class:`Mode` per
    side (location, normalised weight, interval) + a disambiguation message. Deterministic —
    weighted KDE + weighted quantiles, no resampling. Returns ``None`` if no samples."""
    samp, wts = [], []
    for w, s in survivors:
        s = np.asarray(s, dtype=float)
        s = s[np.isfinite(s)]
        if s.size == 0:
            continue
        samp.append(s)
        wts.append(np.full(s.size, w / s.size))
    if not samp:
        return None
    pooled = np.concatenate(samp)
    pw = np.concatenate(wts)
    info = kde_modes(pooled, weights=pw)
    if not info.bimodal:
        return Pooled("fold", pooled, pw, _interval(pooled, pw, level), None, None)
    modes = []
    for sel in (pooled <= info.split, pooled > info.split):
        if not np.any(sel):
            continue
        sv, sw = pooled[sel], pw[sel]
        modes.append(Mode(_weighted_quantile(sv, sw, 0.5), float(sw.sum()),
                          _interval(sv, sw, level)))
    total = sum(m.weight for m in modes)
    for m in modes:
        m.weight /= total
    modes.sort(key=lambda m: m.location)
    msg = ("omega0 is either " + " or ".join(f"{m.location:.2f}" for m in modes) +
           " meV (weights " + "/".join(f"{m.weight:.2f}" for m in modes) +
           "); re-measure to separate.")
    return Pooled("fork", pooled, pw, None, modes, msg)


def _stamp(results, scores, pooled) -> None:
    """Write the per-scan deliverable onto each result's ``across_model`` slot:
    ``{"score": <this structure's metrics>, "scan": {modality, folded_interval | fork}}``."""
    by_label = {s.label: s for s in scores}
    scan = None
    if pooled is not None:
        scan = {"modality": pooled.modality}
        if pooled.modality == "fold":
            scan["folded_interval"] = pooled.interval
        else:
            scan["fork"] = [{"location": m.location, "weight": m.weight, "interval": m.interval}
                            for m in pooled.modes]
            scan["disambiguation"] = pooled.disambiguation
    for r in results:
        s = by_label.get(r.label)
        per = None
        if s is not None:
            per = {"aic": s.aic, "aicc": s.aicc, "bic": s.bic, "delta_aic": s.delta_aic,
                   "weight": s.weight, "nested": s.nested, "survives": s.survives}
        r.across_model = {"score": per, "scan": scan}


def select_models(results, *, weight_floor: float = DEFAULT_WEIGHT_FLOOR, level: float = 0.68,
                  stamp: bool = True) -> Selection:
    """Run the full step-2 layer: score → survivors → pooled across-model distribution +
    modality. With ``stamp`` (default), writes the per-scan deliverable onto each result's
    ``across_model``. Survivors lacking bootstrap samples are dropped from the pool (run
    ``fit_admissible_structures(errors=True)`` first so every converged fit carries them)."""
    scores = score_models(results, weight_floor=weight_floor)
    if not scores:
        if stamp:
            _stamp(results, scores, None)
        return Selection([], None, [], None, "none")
    best = min(scores, key=lambda s: s.aic).label
    survivors = [s for s in scores if s.survives]
    conv = {r.label: r for r in results if r.optimizer_converged}
    pool_inputs = []
    for s in survivors:
        samp = (conv[s.label].within_model or {}).get("omega0_samples")
        if samp is not None and np.size(samp) > 0:
            pool_inputs.append((s.weight, np.asarray(samp, dtype=float)))
    pooled = pool_across_model(pool_inputs, level=level) if pool_inputs else None
    if stamp:
        _stamp(results, scores, pooled)
    modality = pooled.modality if pooled is not None else "unpooled"
    return Selection(scores, best, [s.label for s in survivors], pooled, modality)

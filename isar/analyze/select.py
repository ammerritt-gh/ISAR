"""Model selection + modality (§4) — the layer that turns "we fit several structures" into
one honest answer.

Selection is by **Bayesian evidence** (SCENE-1 PR-b, ``isar.model_selection/2``): each
candidate's marginal likelihood lnZ comes from :mod:`isar.analyze.evidence` (Laplace with the
validity ladder — never a silent Gaussian volume), and the posterior over structures is

    P(s) = pi_s * Z_s / sum_j pi_j * Z_j        (log-space, max-subtraction)

with ``pi`` uniform over the admissible candidates (converged fits with valid evidence — the
same implicit stance AIC took). ``best = argmax P(s)``, and P(s) fills the ``weight`` slot
every downstream consumer (pooling, stamps, delivery) already reads. Raw chi^2 remains banned
(D2): it always prefers more peaks and fits noise as a phantom phonon — the named
``fit_phonons`` anti-pattern (§0.2).

The information criteria stay as **persisted diagnostics** (H4 demotes them from driving):
:func:`score_models` still computes, from the Baker-Cousins -2lnL ``cost``,

    AIC  = cost + 2k
    AICc = AIC + 2k(k+1)/(n-k-1)         (small-n correction; n/k ~ 5-9 on these scans)
    BIC  = cost + k*ln(n)

plus Akaike weights and the nested LRT — the shadow lane and STAT-2b harness keep consuming
exactly that AIC-labeled scorer (D6). When evidence cannot be computed at all (no
hypothesis/context supplied, or every candidate's evidence invalid), selection falls back to
the AIC winner and **records** it (``Selection.selection_fallback``) — a flagged degraded
mode, never a silent one.

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
MODEL_SELECTION_REVISION = "isar.model_selection/2"


@dataclass
class ModelScore:
    """One structure's selection metrics. ``weight`` is the slot downstream consumers read —
    the evidence posterior P(s) (or the Akaike weight in the recorded fallback);
    ``posterior_probability``/``log_evidence``/``evidence_method`` carry the evidence lane
    explicitly, ``akaike_weight``/``aic``/``aicc``/``bic`` the diagnostics lane (D6/H4);
    ``nested`` is the LRT vs the closest nested model (or ``None``); ``survives`` is
    ``weight >= weight_floor``."""

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
    # diagnostics lane (always populated by score_models)
    akaike_weight: float = 0.0
    # evidence lane (populated by select_models when evidence is computable)
    log_evidence: float | None = None
    log_evidence_err: float | None = None
    evidence_method: str | None = None
    evidence_validity: dict | None = None
    candidate_prior: float | None = None
    posterior_probability: float | None = None


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
            delta_aic=aic - aic_min, weight=float(w), akaike_weight=float(w),
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
    """Scan-level model-selection outcome: per-structure ``scores``, the max-posterior
    ``best`` (min-AIC only in the recorded fallback), the surviving labels, and the pooled
    across-model ``Pooled`` (or ``None`` when nothing converged / no survivor carried
    bootstrap samples). ``evidence`` is the per-candidate
    :class:`~isar.analyze.evidence.EvidenceSet` (reusable across the prelim/final calls of one
    fit); ``selection_fallback`` is ``None`` on the evidence path, else the recorded reason
    selection had to fall back to AIC."""

    scores: list
    best: str | None
    survivors: list
    pooled: Pooled | None
    modality: str
    fit_selection_semantics: dict | None = None
    evidence: object | None = None
    selection_fallback: str | None = None


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


def _stamp(results, scores, pooled, fallback=None) -> None:
    """Write the per-scan deliverable onto each result's ``across_model`` slot:
    ``{"score": <this structure's metrics>, "scan": {modality, folded_interval | fork}}``."""
    by_label = {s.label: s for s in scores}
    scan = None
    # A recorded fallback must reach the stamp even with nothing to pool — a degraded selection
    # whose survivors carried no bootstrap samples would otherwise stamp no flag at all. The
    # pooling-less modality matches what ``Selection.modality`` already reports for that case.
    if pooled is not None or fallback is not None:
        scan = {"modality": pooled.modality if pooled is not None else "unpooled"}
    if pooled is not None:
        if pooled.modality == "fold":
            scan["folded_interval"] = pooled.interval
        else:
            scan["fork"] = [{"location": m.location, "weight": m.weight, "interval": m.interval}
                            for m in pooled.modes]
            scan["disambiguation"] = pooled.disambiguation
    if scan is not None:
        scan["selection_fallback"] = fallback
    for r in results:
        s = by_label.get(r.label)
        per = None
        if s is not None:
            per = {"aic": s.aic, "aicc": s.aicc, "bic": s.bic, "delta_aic": s.delta_aic,
                   "weight": s.weight, "nested": s.nested, "survives": s.survives,
                   "akaike_weight": s.akaike_weight,
                   "posterior_probability": s.posterior_probability,
                   "log_evidence": s.log_evidence, "evidence_method": s.evidence_method}
        r.across_model = {"score": per, "scan": scan}


def _candidate_evidence(results, hyps, ctx):
    """One :class:`~isar.analyze.evidence.EvidenceSet` for the converged candidates (via
    :func:`~isar.analyze.evidence.candidate_set_evidence`, which owns the escalation and
    dominance-pruning policy). A converged label with no hypothesis is a wiring error and
    raises loudly — silent partial evidence would quietly bias every posterior it entered."""
    from isar.analyze import catalogue, evidence, loop

    triples = []
    for r in results:
        hyp = hyps.get(r.label)
        if hyp is None:
            raise ValueError(f"no hypothesis supplied for converged candidate {r.label!r}; "
                             f"evidence selection needs every scored label in hyps")
        # A catalogue variant is defined under its own derived context (its pins live in that
        # context's constraints); resolving its bundle under the plain ctx would re-free the
        # pinned parameter and hand structure_evidence a bundle that does not match the fit's k.
        cctx = loop.candidate_context(hyp, ctx)
        prob = loop.build_problem(hyp, cctx)
        bundle = catalogue.resolve_priors(
            prob.composite, cctx, prob.bounds,
            exclude=[prob.free_names[i] for i in prob.fixed_pos])
        triples.append((hyp, r, bundle))
    return evidence.candidate_set_evidence(triples, ctx)


def select_models(results, hyps=None, ctx=None, *, evidence=None,
                  weight_floor: float = DEFAULT_WEIGHT_FLOOR, level: float = 0.68,
                  stamp: bool = True) -> Selection:
    """Run the full step-2 layer: score → evidence posterior → survivors → pooled
    across-model distribution + modality. ``hyps`` (label → Hypothesis, spanning every scored
    label) and ``ctx`` feed the evidence ladder; ``evidence`` reuses a previously computed
    :class:`~isar.analyze.evidence.EvidenceSet` (the prelim/final pair of one fit computes it
    once). Without evidence inputs the selection is the **recorded** AIC fallback — honest
    for hand-built diagnostics, never the production path. With ``stamp`` (default), writes
    the per-scan deliverable onto each result's ``across_model``. Survivors lacking bootstrap
    samples are dropped from the pool (run ``fit_admissible_structures(errors=True)`` first so
    every converged fit carries them)."""
    scores = score_models(results, weight_floor=weight_floor)
    if not scores:
        if stamp:
            _stamp(results, scores, None)
        return Selection([], None, [], None, "none")
    conv = {r.label: r for r in results if r.optimizer_converged}

    fallback = None
    if evidence is None:
        if hyps is not None and ctx is not None:
            evidence = _candidate_evidence(list(conv.values()), hyps, ctx)
        else:
            fallback = "aic_no_evidence_inputs"
    if evidence is not None:
        # A reused EvidenceSet is internally arena-consistent by construction, which says nothing
        # about whether it belongs to THIS scan: applying another scan's lnZ by label would be the
        # exact cross-arena comparison EvidenceSet.add exists to refuse. Only checkable with a
        # context — hand-built diagnostics legitimately pass evidence with ctx=None.
        if ctx is not None:
            from isar.analyze.evidence import ArenaMismatchError
            from isar.analyze.loop import data_likelihood_fingerprint

            arena = data_likelihood_fingerprint(ctx)
            supplied = getattr(evidence, "arena_fingerprint", None)
            if supplied != arena:
                raise ArenaMismatchError(
                    f"supplied evidence carries arena {supplied!r}, this context is {arena!r}; "
                    f"cross-arena evidence comparison is meaningless")
        recs = evidence.by_label()
        finite = []
        for s in scores:
            rec = recs.get(s.label)
            if rec is not None:
                s.log_evidence = float(rec.log_evidence)
                s.log_evidence_err = rec.log_evidence_err
                s.evidence_method = rec.evidence_method
                s.evidence_validity = rec.validity
                if np.isfinite(rec.log_evidence):
                    finite.append(s)
        # A set that ran out of escalation budget with a candidate whose ceiling still reaches the
        # leader has an incomplete comparison, not a close one: ranking on the posterior over what
        # happened to get sampled would publish an evidence claim the set did not earn. Degrade to
        # the recorded AIC lane, which EIG eligibility already fails closed on.
        unresolved = evidence.unresolved_contenders()
        if unresolved:
            fallback = "evidence_budget_exhausted"
        elif finite:
            # D3: P(s) = pi_s Z_s / sum pi Z, pi uniform over the admissible (finite-lnZ)
            # candidates; log-space with max-subtraction. Evidence-invalid candidates get
            # posterior 0 — excluded from the comparison, recorded, never silently AIC-ranked.
            lnz = np.array([s.log_evidence for s in finite], dtype=float)
            p = np.exp(lnz - lnz.max())
            p /= p.sum()
            prior = 1.0 / len(finite)
            posterior = {s.label: float(v) for s, v in zip(finite, p)}
            for s in scores:
                s.candidate_prior = prior if s.label in posterior else 0.0
                s.posterior_probability = posterior.get(s.label, 0.0)
                s.weight = s.posterior_probability
                s.survives = bool(s.weight >= weight_floor)
        else:
            fallback = "aic_all_evidence_invalid"

    if fallback is None:
        best = max(scores, key=lambda s: s.posterior_probability).label
    else:
        # Recorded degraded mode: scores keep their score_models Akaike weights/survivors.
        best = min(scores, key=lambda s: s.aic).label
    survivors = [s for s in scores if s.survives]
    pool_inputs = []
    for s in survivors:
        samp = (conv[s.label].within_model or {}).get("omega0_samples")
        if samp is not None and np.size(samp) > 0:
            pool_inputs.append((s.weight, np.asarray(samp, dtype=float)))
    pooled = pool_across_model(pool_inputs, level=level) if pool_inputs else None
    if stamp:
        _stamp(results, scores, pooled, fallback)
    modality = pooled.modality if pooled is not None else "unpooled"
    return Selection(scores, best, [s.label for s in survivors], pooled, modality,
                     evidence=evidence, selection_fallback=fallback)

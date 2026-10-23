"""Rung 2 of the count ladder and the per-spectrum count state (design 2026-10-22, §3.1). Qt-free.

Rung 1 (:func:`isar.analyze.loop._rung1_ladder`, Laplace evidence through
:func:`isar.analyze.select.select_models`, copied onto the record by
:meth:`~isar.analyze.loop.Rung1Record.apply_selection`) decides most spectra. This module reads
that record and:

1. checks the escalation triggers (:func:`triggers`), each a named predicate with its threshold a
   named constant below;
2. names the contending models (:func:`contending`): every count and elastic variant within
   :data:`CONTENDING_MARGIN` of the best rung-1 log-evidence, every model whose rung-1 evidence
   could not be computed, and the count one above the best in both elastic variants;
3. samples each contending model by nested sampling (:func:`sample_model`, one model per call, so
   a caller can slice the work in time) and refits each from its maximum-likelihood sample;
4. names the spectrum's one state with its reasons, the separate flags and the ambiguity marker
   (:func:`conclude`).

:func:`run` composes the four for a caller that does not slice. A failed calculation is never
evidence against a model: it leaves the spectrum *unresolved*. A search-limited or unresolved
spectrum keeps its rung-1 result as the shown result, and neither ever stops a campaign.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

from isar.analyze import evidence, loop
from isar.analyze.prior import mode_bounds, resolution_fwhm

log = logging.getLogger(__name__)

# --- the states (design §3.1), in the precedence that picks the shown one ------------------------
FITTING = "fitting"                # no result yet
PROVISIONAL = "provisional"        # the rung-1 result is shown while rung 2 runs
SETTLED = "settled"
SEARCH_LIMITED = "search limited"  # the ceiling or the time budget stopped the search
UNRESOLVED = "unresolved"          # a contending model's evidence could not be computed
HELD = "held"                      # calibration could not be set; not fitted
STATES = (FITTING, PROVISIONAL, SETTLED, SEARCH_LIMITED, UNRESOLVED, HELD)

# --- thresholds -----------------------------------------------------------------------------------
# A model contends when its rung-1 log-evidence is within this of the best (a factor of about
# 20 000). The design's default; tuned against the benchmark only with the change recorded.
CONTENDING_MARGIN = 10.0
# Measured 2026-10-23 on the S1U2 benchmark's tuning stream (seed 20261023, cases T0000-T0059 at
# ceiling 4, 57 finished inside the 30-minute cap; reference 20+20 Laplace draws; rung 2 forced on
# every contending set at n_live 32). Rung 2 moved the count on 10 of 56 spectra: 4 to the truth
# (T0004, T0027, T0044, T0057), 4 away. The structured-residual trigger caught 3 of the 4, the
# missing-evidence trigger all 4; the four below caught none, at any margin tried, so they keep
# their starting values and are recorded rather than tuned (R15).
SPLIT_MARGIN = 3.0               # nats. Fired on 1/57; at 8 on 7/57, at 10 on 13/57 (catching 1/4)
START_DISAGREEMENT_MARGIN = 3.0  # nats. Fired on 0/57; at 10 on 6/57 (catching 0/4)
MERGED_FWHM_FRACTION = 0.5       # x resolution FWHM, the split seeds' separation. No selected model
#                                  had two modes closer than 1.0 FWHM, so 0.25-1.0 all fired on 0/57
AMBIGUITY_RUNNER_UP_P = 0.20     # Runner-up probabilities were <= 0.06 or >= 0.21 on every case, so
#                                  0.05-0.30 classify alike; kept equal to the benchmark's stand-in
# Live points for rung 2's count decisions. ESCALATION_N_LIVE (32) was sized for one- and two-mode
# Occam gaps; measured 2026-10-23 on case T0040 (2 and 3 modes, 8 and 11 free parameters) its lnZ
# sat 39 and 29 nats below the 400-point value with a reported logzerr of 1.7 and 2.0, flipping the
# count, while 128 points came within 1.8 and 0.2 nats of it at 4x the time.
# shortcut: measured up to 11 free parameters; check it at the 6-mode ceiling (20) before relying
# on rung 2 there. $ISAR_ESCALATION_N_LIVE, when set (the test suite sets it), still wins.
RUNG2_N_LIVE = 128
# The central region, for the flag "central residual, quasi-elastic not tested": this many
# resolution widths either side of the energy zero.
CENTRAL_HALF_WIDTH_FWHM = 2.0

# Per-spectrum rung-2 budget in single-worker wall time, sized to ruling R6 ("assume a scan can
# take 30 minutes"). Checked before each contending model starts; read at call time from the
# environment so a spawned pool child and a test both honour it.
RUNG2_BUDGET_S = 30 * 60.0
RUNG2_BUDGET_ENV = "ISAR_RUNG2_BUDGET_S"

FLAG_STRUCTURED = "structured residual"
FLAG_CENTRAL = "central residual, quasi-elastic not tested"


def rung2_n_live() -> int:
    """``$ISAR_ESCALATION_N_LIVE`` when set (validated as there), else :data:`RUNG2_N_LIVE`."""
    if os.environ.get(evidence.ESCALATION_N_LIVE_ENV):
        return evidence.escalation_n_live()
    return RUNG2_N_LIVE


def rung2_budget_s() -> float:
    """``$ISAR_RUNG2_BUDGET_S`` when set, else :data:`RUNG2_BUDGET_S`. A malformed or negative value
    raises: a budget that quietly ignored what it was told would leave a state's boundary unknown."""
    raw = os.environ.get(RUNG2_BUDGET_ENV)
    if raw is None or raw == "":
        return RUNG2_BUDGET_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{RUNG2_BUDGET_ENV}={raw!r} is not a number") from exc
    if not (math.isfinite(value) and value >= 0.0):
        raise ValueError(f"{RUNG2_BUDGET_ENV}={value} must be a finite number >= 0")
    return value


@dataclass(frozen=True)
class Trigger:
    """One escalation trigger that fired, with what it saw."""

    name: str
    reason: str


@dataclass
class Rung2Model:
    """One contending model after rung 2. ``log_evidence`` ``None``: it could not be computed (the
    reason says why); never a low evidence. ``result`` is what is published for this model: the
    optimizer refit seeded at the nested-sampling maximum-likelihood sample, or the rung-1
    representative when that is better (``result_source`` says which)."""

    label: str
    count: int
    log_evidence: float | None
    log_evidence_err: float | None
    method: str                  # "nested" | "failed" (sampling failed or could not start)
    reason: str | None
    result: object
    result_source: str           # "nested_refit" | "rung1"
    seconds: float
    probability: float | None = None


@dataclass
class Rung2Plan:
    """What rung 2 must do for one spectrum: the triggers that fired and the contending models, in
    sampling order. Nothing to do when no trigger fired."""

    triggers: list
    contending: list             # labels

    @property
    def escalate(self) -> bool:
        return bool(self.triggers)


@dataclass
class CountLadderResult:
    """A spectrum's count outcome: exactly one ``state`` with every applicable reason, the flags
    (quality warnings, separate from the state), the ambiguity marker (``None`` when the count is
    clear), and which rung's result is shown. ``rung2`` holds every model sampled so far; it is
    shown only when ``shown == "rung2"``, so a partial rung 2 never is."""

    state: str
    reasons: list
    flags: list = field(default_factory=list)
    ambiguity: dict | None = None
    triggers: list = field(default_factory=list)
    contending: list = field(default_factory=list)
    rung2: dict = field(default_factory=dict)
    shown: str = "rung1"         # "rung1" | "rung2" | "none"
    best_label: str | None = None
    count_probabilities: dict = field(default_factory=dict)
    rung2_seconds: float = 0.0

    @property
    def shown_result(self):
        """The published fit of the shown best model, when rung 2 is shown; else ``None`` (the
        caller already holds the rung-1 result)."""
        if self.shown != "rung2" or self.best_label is None:
            return None
        return self.rung2[self.best_label].result


def held(reason: str) -> CountLadderResult:
    """The state of a spectrum that was not fitted because its calibration could not be set."""
    return CountLadderResult(state=HELD, reasons=[reason], shown="none")


# --- rung-1 reading -------------------------------------------------------------------------------
def _has_evidence(model) -> bool:
    return model.log_evidence is not None and math.isfinite(model.log_evidence)


def _distinct(model) -> bool:
    """Not resolved to a degenerate twin (:func:`isar.analyze.loop._resolve_degenerate`): a resolved
    model carries its twin's evidence, so it is that twin, never a second model or a missing one."""
    return model.resolved_to is None


def _best(models):
    """The distinct model with the highest finite log-evidence, or ``None``."""
    scored = [m for m in models if _has_evidence(m) and _distinct(m)]
    return max(scored, key=lambda m: m.log_evidence) if scored else None


def _variant(model) -> tuple:
    return (model.elastic, model.quasielastic, model.variant)


def _start_log_evidence(model, ctx) -> list:
    """Laplace log-evidence of each start of ``model`` (``None`` where the Gaussian picture is
    invalid or the start did not converge): the same rung as the representative's, per start."""
    bundle = evidence.candidate_bundle(model.hypothesis, ctx)
    out = []
    for start in model.starts:
        if not start.result.optimizer_converged:
            out.append(None)
            continue
        rec = evidence.structure_evidence(model.hypothesis, ctx, start.result, bundle,
                                          escalation="defer")
        out.append(float(rec.log_evidence) if np.isfinite(rec.log_evidence) else None)
    return out


def _model_curve(model, result, ctx):
    hyp = model.hypothesis
    cctx = loop.candidate_context(hyp, ctx)
    comp = hyp.build()
    theta = np.array([result.params[n] for n in comp.param_names()], dtype=float)
    return comp.evaluate(cctx.x_used, theta, cctx)


def _pull(model, result, ctx):
    y = ctx.counts_used
    return (y - _model_curve(model, result, ctx)) / np.sqrt(np.maximum(y, 1.0))


def _flags(model, result, ctx) -> list:
    """Quality flags of a shown fit: a structured residual (the runs test the verdict uses), and a
    structured or significant residual in the central region, where the quasi-elastic component was
    not tested (ruling R13: the record never says it is absent)."""
    from isar.analyze.verdict import RESID_Z, _residual_structured

    pull = _pull(model, result, ctx)
    flags = [FLAG_STRUCTURED] if _residual_structured(pull) else []
    x = ctx.x_used
    central = np.abs(x - float(ctx.center)) <= CENTRAL_HALF_WIDTH_FWHM * resolution_fwhm(ctx)
    if np.any(central):
        y, curve = ctx.counts_used[central], _model_curve(model, result, ctx)[central]
        z = float(np.sum(y - curve)) / math.sqrt(max(float(np.sum(np.maximum(curve, 1.0))), 1.0))
        if _residual_structured(pull[central]) or abs(z) >= RESID_Z:
            flags.append(FLAG_CENTRAL)
    return flags


# --- the escalation triggers ----------------------------------------------------------------------
def _split_trigger(record, best, per_start) -> list:
    """A split candidate within :data:`SPLIT_MARGIN` of its unsplit model, where one of the two is
    the best model."""
    out = []
    for model in record.models:
        parent = next((m for m in record.models if m.count == model.count - 1
                       and _variant(m) == _variant(model)), None)
        if parent is None or not _has_evidence(parent) or best not in (model, parent):
            continue
        for start, ln_z in zip(model.starts, per_start[model.label]):
            if start.source == "split" and ln_z is not None \
                    and abs(ln_z - parent.log_evidence) <= SPLIT_MARGIN:
                out.append(Trigger("split_competitive",
                                   f"a split of {parent.label} into {model.label} "
                                   f"({start.detail.get('parent')}, "
                                   f"{start.detail.get('separation_fwhm')} FWHM) is within "
                                   f"{abs(ln_z - parent.log_evidence):.1f} nats of it"))
                break
    return out


def _start_trigger(record, per_start) -> list:
    """Start families (residual, split, descriptor, declared, ...) whose own best starts lie within
    :data:`START_DISAGREEMENT_MARGIN` of the overall best but pick different counts."""
    family_best = {}
    for model in record.models:
        for start, ln_z in zip(model.starts, per_start[model.label]):
            if ln_z is not None and (start.source not in family_best
                                     or ln_z > family_best[start.source][0]):
                family_best[start.source] = (ln_z, model.count)
    if not family_best:
        return []
    top = max(v[0] for v in family_best.values())
    near = {f: v for f, v in family_best.items() if v[0] >= top - START_DISAGREEMENT_MARGIN}
    if len({count for _, count in near.values()}) < 2:
        return []
    said = ", ".join(f"{f} {count} ({ln_z - top:+.1f})" for f, (ln_z, count) in sorted(near.items()))
    return [Trigger("starts_disagree", f"start families pick different counts: {said}")]


def _merged_trigger(best, ctx) -> list:
    """Two modes of the best model closer than :data:`MERGED_FWHM_FRACTION` of the resolution width,
    or a mode amplitude at its lower bound or within one standard error of zero."""
    out = []
    result = best.representative
    width = resolution_fwhm(ctx)
    energies = [m["omega0"] for m in (result.modes or [])]
    for lo_e, hi_e in zip(energies, energies[1:]):
        if hi_e - lo_e < MERGED_FWHM_FRACTION * width:
            out.append(Trigger("modes_merged", f"modes at {lo_e:.2f} and {hi_e:.2f} meV are "
                                               f"{(hi_e - lo_e) / width:.2f} resolution widths apart"))
            break
    amps = [n for n in result.params if n.endswith(".A")]
    cctx = loop.candidate_context(best.hypothesis, ctx)
    for name, (lo, _hi) in zip(amps, mode_bounds(amps, cctx)):
        value = float(result.params[name])
        err = (result.param_errors or {}).get(name)
        if (result.at_limit or {}).get(name) or value <= lo:
            out.append(Trigger("amplitude_zero", f"{name} sits at its lower bound"))
        elif err is not None and math.isfinite(err) and value < err:
            out.append(Trigger("amplitude_zero", f"{name} = {value:.3g} is within one standard "
                                                 f"error ({err:.3g}) of zero"))
    return out


def triggers(record, ctx, contenders=None) -> list:
    """Every escalation trigger that fires on ``record`` (rung 1 with its evidence applied)."""
    contenders = contending(record) if contenders is None else contenders
    by_label = record.by_label()
    best = _best(record.models)
    out = []
    invalid = [label for label in contenders
               if not _has_evidence(by_label[label]) or not by_label[label].converged]
    if invalid:
        out.append(Trigger("laplace_invalid", "no usable rung-1 evidence for "
                           + ", ".join(invalid)))
    if best is None:
        return out
    per_start = {m.label: _start_log_evidence(m, ctx) for m in record.models}
    out += _split_trigger(record, best, per_start)
    out += _start_trigger(record, per_start)
    out += _merged_trigger(best, ctx)
    from isar.analyze.verdict import _residual_structured

    if _residual_structured(_pull(best, best.representative, ctx)):
        out.append(Trigger("structured_residual", f"the runs test finds structure left by "
                                                  f"{best.label}"))
    if best.count >= record.ceiling:
        out.append(Trigger("at_ceiling", f"the best count {best.count} is the ceiling"))
    return out


def contending(record) -> list:
    """Labels of the contending models, in sampling order: the best, the other distinct models
    within :data:`CONTENDING_MARGIN` by evidence, the count one above the best with and without the
    elastic line (the variant the best does not use may be the one whose local fit was poor), then
    every distinct model with no rung-1 evidence, fewest parameters first. A model resolved to its
    degenerate twin is not a contender of its own; the count above is sampled even when resolved,
    since nested sampling explores the mode the optimizer railed."""
    best = _best(record.models)
    if best is None:
        return [m.label for m in sorted(record.models, key=lambda m: m.representative.k)
                if _distinct(m)]
    near = sorted((m for m in record.models if _has_evidence(m) and _distinct(m)
                   and m.log_evidence >= best.log_evidence - CONTENDING_MARGIN),
                  key=lambda m: -m.log_evidence)
    above = [m for m in record.models if m.count == best.count + 1 and m.variant is None
             and m.quasielastic == best.quasielastic and m not in near]
    missing = sorted((m for m in record.models
                      if not _has_evidence(m) and _distinct(m) and m not in above),
                     key=lambda m: m.representative.k)
    return [m.label for m in near + above + missing]


def plan(record, ctx) -> Rung2Plan:
    """The triggers and the contending set; rung 2 runs only when a trigger fired."""
    contenders = contending(record)
    fired = triggers(record, ctx, contenders)
    return Rung2Plan(triggers=fired, contending=contenders if fired else [])


# --- rung 2 ---------------------------------------------------------------------------------------
def sample_model(model, ctx, *, seed: int = 12345, n_live: int | None = None) -> Rung2Model:
    """Nested sampling of one contending model, then an optimizer refit from the sampler's
    maximum-likelihood sample; the better of the refit and the rung-1 representative is published.
    A model whose sampling fails or cannot start has no rung-2 evidence (``"failed"``): its rung-1
    Laplace value is never carried into the comparison, so the spectrum is left unresolved, never
    settled on a comparison that did not run. ``n_live`` ``None`` is :func:`rung2_n_live`."""
    started = time.monotonic()
    hyp = model.hypothesis
    rep = model.representative
    rec, bz = evidence.nested_evidence(hyp, ctx, rep, evidence.candidate_bundle(hyp, ctx),
                                       seed=seed, n_live=rung2_n_live() if n_live is None else n_live)
    result, source = rep, "rung1"
    ml = getattr(bz, "max_likelihood_sample", None)
    if ml:
        refit = loop.fit_hypothesis(hyp, loop.candidate_context(hyp, ctx), x0=ml)
        if loop._objective(refit) < loop._objective(rep):
            result, source = refit, "nested_refit"
    if rec.evidence_method == "dynesty":
        ln_z, err, method, reason = rec.log_evidence, rec.log_evidence_err, "nested", None
    else:
        dyn = rec.validity.get("dynesty", {})
        ln_z, err, method = None, None, "failed"
        reason = dyn.get("evidence_fail_reason") or dyn.get("fail_reason") or dyn.get("reason")
    return Rung2Model(label=model.label, count=model.count, log_evidence=ln_z,
                      log_evidence_err=err, method=method, reason=reason, result=result,
                      result_source=source, seconds=time.monotonic() - started)


def _probabilities(models) -> dict:
    """Equal-prior posterior probabilities over ``models`` (label -> P), from their log-evidence."""
    ln_z = np.array([m.log_evidence for m in models], dtype=float)
    p = np.exp(ln_z - ln_z.max())
    p /= p.sum()
    return {m.label: float(v) for m, v in zip(models, p)}


def count_ambiguity(count_probabilities: dict, best_count: int) -> dict | None:
    """The ambiguity marker: the best count and the runner-up count with both probabilities, when
    the runner-up reaches :data:`AMBIGUITY_RUNNER_UP_P`; ``None`` when the count is clear."""
    others = sorted(((p, n) for n, p in count_probabilities.items() if n != best_count),
                    reverse=True)
    if not others or others[0][0] < AMBIGUITY_RUNNER_UP_P:
        return None
    return {"count": int(best_count), "probability": float(count_probabilities.get(best_count, 0.0)),
            "runner_up_count": int(others[0][1]), "runner_up_probability": float(others[0][0])}


def conclude(record, ctx, plan_, sampled: dict, *, elapsed_s: float = 0.0,
             budget_s: float | None = None) -> CountLadderResult:
    """The spectrum's state from rung 1, the plan and the models sampled so far (``sampled``:
    label -> :class:`Rung2Model`). Rung 2 still has work and budget: *provisional*. Otherwise one
    fixed precedence picks the state (unresolved, then search limited, then settled) and every
    applicable reason is kept. Rung 2 is shown only when it finished and the state is settled."""
    budget = rung2_budget_s() if budget_s is None else float(budget_s)
    by_label = record.by_label()
    best1 = _best(record.models)
    pending = [label for label in plan_.contending if label not in sampled]
    unresolved, limited, notes = [], [], []

    failed = [sampled[label] for label in plan_.contending
              if label in sampled and sampled[label].log_evidence is None]
    unresolved += [f"evidence for {m.label} could not be computed ({m.reason})" for m in failed]
    if not record.models:
        unresolved.append("no model was fitted")

    decided = None                       # rung 2's best, when it finished with every evidence
    if plan_.escalate and not pending and not failed:
        models = [sampled[label] for label in plan_.contending]
        for label, p in _probabilities(models).items():
            sampled[label].probability = p
        decided = max(models, key=lambda m: m.probability)
        notes.append(f"rung 2 sampled {len(models)} contending model(s); {decided.label} has "
                     f"P = {decided.probability:.3f}")
    if pending and elapsed_s >= budget:
        limited.append(f"rung-2 budget of {budget:.0f} s single-worker time reached after "
                       f"{len(sampled)} of {len(plan_.contending)} contending models")
    best_count = decided.count if decided is not None else (best1.count if best1 else None)
    if best_count is not None and best_count >= record.ceiling:
        limited.append(f"the best count {best_count} is the mode ceiling {record.ceiling}")

    result = CountLadderResult(state=SETTLED, reasons=[], triggers=list(plan_.triggers),
                               contending=list(plan_.contending), rung2=dict(sampled),
                               rung2_seconds=float(elapsed_s))
    if plan_.escalate and pending and elapsed_s < budget:
        result.state = PROVISIONAL
        result.reasons = [f"rung 2 running: {len(sampled)} of {len(plan_.contending)} contending "
                          f"models sampled"] + unresolved
    elif unresolved:
        result.state, result.reasons = UNRESOLVED, unresolved + limited + notes
    elif limited:
        result.state, result.reasons = SEARCH_LIMITED, limited + notes
    else:
        result.reasons = notes or ["no escalation trigger fired; rung 1 decides"]

    if result.state == SETTLED and decided is not None:
        result.shown, result.best_label = "rung2", decided.label
        probs = {}
        for m in sampled.values():
            probs[m.count] = probs.get(m.count, 0.0) + (m.probability or 0.0)
        shown_model, shown_fit = by_label[decided.label], decided.result
    else:
        result.best_label = best1.label if best1 is not None else None
        probs = record.count_probabilities()
        shown_model = best1
        shown_fit = best1.representative if best1 is not None else None
    result.count_probabilities = probs
    if shown_model is not None:
        result.ambiguity = count_ambiguity(probs, shown_model.count)
        result.flags = _flags(shown_model, shown_fit, ctx)
    else:
        result.shown = "none"
    return result


def run(record, ctx, *, budget_s: float | None = None, seed: int = 12345,
        n_live: int | None = None) -> CountLadderResult:
    """The one entry point: plan, sample every contending model while the per-spectrum budget
    lasts (checked before each model starts), conclude. ``record`` is rung 1 with its selection
    applied (:meth:`~isar.analyze.loop.Rung1Record.apply_selection`)."""
    budget = rung2_budget_s() if budget_s is None else float(budget_s)
    plan_ = plan(record, ctx)
    sampled = {}
    started = time.monotonic()
    by_label = record.by_label()
    for i, label in enumerate(plan_.contending):
        if time.monotonic() - started >= budget:
            break
        sampled[label] = sample_model(by_label[label], ctx, seed=seed + i, n_live=n_live)
        log.info("rung 2: %s %s (%.1f s)", label,
                 "lnZ=%.2f" % sampled[label].log_evidence
                 if sampled[label].log_evidence is not None else sampled[label].reason,
                 sampled[label].seconds)
    return conclude(record, ctx, plan_, sampled, elapsed_s=time.monotonic() - started,
                    budget_s=budget)

"""The deep pass: calibrated nested-sampling evidence for the competing counts of ONE spectrum the
user chose, with staged coverage (design 2026-10-25 s2.5, picks D1/D2).

The caller supplies, per model, a picklable ``problem`` callable that returns
``(loglike, ptform, ndim)`` built exactly as ``errors/bayes.py::bayesian_posterior`` builds them on
the prior-bundle path (:class:`~isar.analyze.errors.bayes.EvidenceProblem`: pure Baker-Cousins data
loglike, the bundle's ordered ppf transform, hard-fixed coordinates held out).
:func:`models_from_record` builds those entries from a persisted fast-pass record. Sampling always
starts from the prior: the fast pass orders the work and feeds the suspect check, it never seeds or
restricts a run.

Every figure below that is a default (SUSPECT gap, sigmas, budgets) is PROVISIONAL until the S2U2
calibration installs measured values; the names say which.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import numpy as np

from isar.analyze import catalogue, evidence, loop, prior
from isar.analyze.errors.bayes import EvidenceProblem
from isar.analyze.progress import DEEP_STAGES, ProgressFn, emitter

CLOSE_CALL_P = 0.80             # the fast pass's threshold (s2.3); ``deep_pass`` takes the record's
NUMERIC_FLIP_FRACTION = 0.05    # winner changes in more than this share of perturbed draws => numeric
PERTURB_DRAWS = 4000
CONTENDER_MIN_P = 0.01          # fast-pass probability that makes a model a stage-1 contender
RELEVANCE_GAP = 10.0            # nats below the leader beyond which more precision cannot matter
MAX_RESTARTS = 2
FAST_PASS_SCHEMA = "isar.fast-pass/1"    # scan_delivery.FAST_PASS_RECORD_SCHEMA; analyze imports no session code
# The one calibration pass missed both aims (1 and 2 nats); the numbers below are the screen's most stable
# configuration on the deciding models, not a settled accuracy. The next step is a method change.
CALIBRATION_STATUS = ("provisional: one-pass calibration on le Tacon 302 K a05, 2026-10-26; 2-nat limit missed "
                      "(reference reached only counts 0-2; seed std 9.7 nats at 400 live x step factor 4 on the "
                      "deciding models); UltraNest not finished (6 of 6 runs hit the 3600 s per-run limit); "
                      "report out/j6420291926ab/deep-calibration/a05/ (calibration.json, calibration.md)")
SUSPECT_GAP_BASE = 5.0          # PROVISIONAL, unchanged: the pass's large deficits are failed runs, not a gap to widen
SUSPECT_GAP_PER_DIM = 0.5       # PROVISIONAL, unchanged


@dataclass(frozen=True)
class SamplerSettings:
    n_live: int = 400
    sample: str = "rwalk"           # "rwalk" | "rslice"
    step_factor: int = 1            # rwalk walks = step_factor*(ndim+20); rslice slices = step_factor*(ndim+3)
    dlogz: float = 0.05
    max_iter: int | None = None     # the deep pass has no iteration cap
    bound: str = "multi"
    calibrated_sigma: float = 1.0   # lnZ error in nats; installed values are the one-pass screen's seed std

    def step_kwargs(self, ndim: int) -> dict:
        if self.sample == "rwalk":
            return {"walks": int(self.step_factor * (ndim + 20))}
        if self.sample == "rslice":
            return {"slices": int(self.step_factor * (ndim + 3))}
        raise ValueError(f"unknown sample method {self.sample!r}")


# PROVISIONAL, from the one-pass screen on a05 (sigma = largest 3-seed std on counts 4-6; EXPLORATORY adds 0.5 for dlogz).
ACCEPTED = SamplerSettings(n_live=400, step_factor=4, calibrated_sigma=9.7)
EXPLORATORY = SamplerSettings(n_live=128, step_factor=4, dlogz=0.5, calibrated_sigma=12.38)


@dataclass(frozen=True)
class RunRecord:
    label: str
    seed: int
    settings: SamplerSettings
    ndim: int
    log_z: float
    log_z_err: float                # dynesty's own logzerr: reported, never the uncertainty used
    information_h: float            # results.information[-1], nats
    n_like_calls: int               # real loglike calls, counted here (dynesty ncall overcounts)
    n_iter: int
    lnl_max: float
    wall_s: float
    reference_lnl: float | None     # the fast-pass optimum's -cost/2 for the same model
    suspect: tuple = ()
    failure: str | None = None
    evidence_converged: bool = False  # finite lnZ and the dlogz target reached (no max_iter stop)
    best_theta: tuple = ()          # the max-lnL sample, sampled coordinates in bundle order

    @property
    def usable(self) -> bool:
        return self.failure is None and not self.suspect and math.isfinite(self.log_z)


class _Counted:
    """Wraps the loglike to count real calls; single process per run, so a plain int is exact."""

    def __init__(self, fn):
        self.fn, self.n = fn, 0

    def __call__(self, theta):
        self.n += 1
        return self.fn(theta)


def suspect_gap(ndim: int) -> float:
    """PROVISIONAL nats a run's best lnL may sit below the optimizer optimum before the run is suspect.
    The best of n_live terminal points sits ~ (d/2)(1 - n_live**(-2/d)) below the peak for a Gaussian
    posterior (a few nats at d = 14); 5 + d/2 leaves room for that. The one-pass deficits are in
    STATS_PRIMER 2.4."""
    return SUSPECT_GAP_BASE + SUSPECT_GAP_PER_DIM * ndim


def run_nested(label, problem, *, settings: SamplerSettings, seed: int,
               reference_lnl: float | None = None) -> RunRecord:
    """One dynesty run from the prior; never raises for a sampler failure (recorded instead)."""
    import dynesty

    t0 = time.perf_counter()
    counted, ndim = _Counted(None), 0
    try:
        loglike, ptform, ndim = problem()       # a bundle mismatch (ValueError) is a recorded failure
        counted = _Counted(loglike)
        sampler = dynesty.NestedSampler(counted, ptform, ndim=int(ndim), nlive=int(settings.n_live),
                                        bound=settings.bound, sample=settings.sample,
                                        rstate=np.random.default_rng(seed),
                                        **settings.step_kwargs(int(ndim)))
        sampler.run_nested(dlogz=settings.dlogz, maxiter=settings.max_iter, print_progress=False)
        res = sampler.results
    except Exception as exc:     # a sampler failure is a recorded run, never an escaped exception
        return RunRecord(label, int(seed), settings, int(ndim), float("nan"), float("nan"),
                         float("nan"), counted.n, 0, float("nan"), time.perf_counter() - t0,
                         reference_lnl, (), f"run failed: {type(exc).__name__}: {exc}")
    wall = time.perf_counter() - t0
    rec = RunRecord(label=label, seed=int(seed), settings=settings, ndim=int(ndim),
                    log_z=float(res.logz[-1]), log_z_err=float(res.logzerr[-1]),
                    information_h=float(res.information[-1]), n_like_calls=counted.n,
                    n_iter=int(res.niter), lnl_max=float(np.max(res.logl)), wall_s=wall,
                    reference_lnl=reference_lnl,
                    best_theta=tuple(float(v) for v in res.samples[int(np.argmax(res.logl))]))
    converged = math.isfinite(rec.log_z) and (settings.max_iter is None
                                             or rec.n_iter < settings.max_iter)
    return replace(rec, suspect=suspect_reasons(rec), evidence_converged=bool(converged))


def suspect_reasons(rec: RunRecord) -> tuple:
    out = []
    if not math.isfinite(rec.log_z):
        out.append("lnZ not finite")
    if rec.settings.max_iter is not None and rec.n_iter >= rec.settings.max_iter:
        out.append("hit max_iter")
    if rec.reference_lnl is not None and math.isfinite(rec.reference_lnl):
        gap = rec.reference_lnl - rec.lnl_max
        if not math.isfinite(gap) or gap > suspect_gap(rec.ndim):
            out.append(f"best lnL {gap:.1f} nats below the fast-pass optimum")
    return tuple(out)


def escalate(settings: SamplerSettings, attempt: int) -> SamplerSettings:
    """Restart ladder for a suspect run: attempt 1 doubles the step length, attempt 2 doubles n_live."""
    if attempt == 1:
        return replace(settings, step_factor=2 * settings.step_factor)
    return replace(settings, n_live=2 * settings.n_live)


def run_until_clean(label, problem, *, settings, seed, reference_lnl):
    """Run; on a suspect run restart with a new seed and escalated settings, up to MAX_RESTARTS.
    Returns every run (the failures are kept: "save the failure and its settings"). Never rejects."""
    runs = []
    s = settings
    for attempt in range(MAX_RESTARTS + 1):
        rec = run_nested(label, problem, settings=s, seed=seed + 7919 * attempt,
                         reference_lnl=reference_lnl)
        runs.append(rec)
        if rec.usable:
            break
        s = escalate(s, attempt + 1)
    return runs


@dataclass(frozen=True)
class ModelEntry:
    """What the deep pass reads from the fast-pass record for one model (C-fastpass-record)."""
    label: str
    count: int
    ndim: int
    fastpass_ln_z: float | None     # Laplace lnZ (basins summed) - ln_like_saturated, the sampler's
                                    #   -cost/2 scale; orders the work only; None when none is priced
    fastpass_lnl_opt: float | None  # max over basins of -cost/2, feeds the suspect check
    problem: object                 # picklable () -> (loglike, ptform, ndim)
    log_model_prior: float = 0.0    # the record's ln_model_prior


@dataclass
class ModelEvidence:
    label: str
    count: int
    status: str                     # "reference" | "exploratory" | "unresolved" | "not_run"
    log_z: float = float("nan")
    sigma: float = float("nan")     # calibrated lnZ sd used for the close-call qualification
    runs: list = field(default_factory=list)
    log_model_prior: float = 0.0


@dataclass
class DeepPassResult:
    models: list
    complete: bool                  # every model has a usable lnZ and no promotion is pending
    search_limited: str | None      # why not complete (budget ended / unresolved suspect runs)
    count_probabilities: dict       # over the models that have lnZ; FULL comparison only if complete
    comparison_scope: str           # "full" | "partial (search limited)"
    best_count: int | None
    close_call: dict
    wall_s: float
    exploratory_only: tuple = ()    # models in the comparison at exploratory precision only
    stage_seconds: dict = field(default_factory=dict)   # contenders, exploratory, promoted


def _weights(models):
    """The models with a usable lnZ and their log weights ``log_z + log_model_prior`` (the record's
    model prior, the same definition as the fast pass's)."""
    have = [m for m in models if math.isfinite(m.log_z)]
    return have, np.array([m.log_z + m.log_model_prior for m in have])


def _probabilities(models):
    """Posterior probabilities from ``log_z + log_model_prior``; a count's P is the sum over its
    models (equal prior per count, shared over its variants and delta masks)."""
    have, w = _weights(models)
    if not have:
        return {}, {}
    p = np.exp(w - w.max())
    p /= p.sum()
    per_model = {m.label: float(q) for m, q in zip(have, p)}
    counts: dict = {}
    for m, q in zip(have, p):
        counts[m.count] = counts.get(m.count, 0.0) + float(q)
    return per_model, counts


def close_call(models, *, close_call_p: float = CLOSE_CALL_P, seed: int = 0) -> dict:
    """Statistical and numerical close-call qualification. Statistical: preferred count P below
    ``close_call_p``. Numerical: perturb each model's weight by its calibrated sigma (independent
    normals) and ask how often the winning count changes, and whether the preferred-count P's 5-95%
    band crosses ``close_call_p``."""
    have, w = _weights(models)
    _, counts = _probabilities(have)
    if not counts:
        return {"statistical": None, "numerical": None}
    best = max(counts, key=counts.get)
    rng = np.random.default_rng(seed)
    sig = np.array([m.sigma if math.isfinite(m.sigma) else 0.0 for m in have])
    cnt = np.array([m.count for m in have])
    draws = w + rng.standard_normal((PERTURB_DRAWS, len(have))) * sig
    p = np.exp(draws - draws.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    uniq = sorted(set(cnt.tolist()))
    pc = np.stack([p[:, cnt == c].sum(axis=1) for c in uniq], axis=1)
    winners = np.array(uniq)[np.argmax(pc, axis=1)]
    flip = float(np.mean(winners != best))
    p_best = pc[:, uniq.index(best)]
    lo, hi = (float(v) for v in np.quantile(p_best, [0.05, 0.95]))
    return {"best_count": int(best), "p_best": float(counts[best]),
            "statistical": bool(counts[best] < close_call_p),
            "winner_flip_fraction": flip, "p_best_band_5_95": (lo, hi),
            "numerical": bool(flip > NUMERIC_FLIP_FRACTION or (lo < close_call_p <= hi))}


def _fast_weight(e: ModelEntry) -> float:
    z = e.fastpass_ln_z
    return z + e.log_model_prior if z is not None and math.isfinite(z) else -math.inf


def deep_pass(entries, *, accepted: SamplerSettings = ACCEPTED,
              exploratory: SamplerSettings = EXPLORATORY, budget_s: float | None = None,
              seed: int = 20261025, map_fn=map, close_call_p: float = CLOSE_CALL_P,
              chunk: int = 1, fast_limit: str = "", progress: ProgressFn | None = None) -> DeepPassResult:
    """Staged coverage over EVERY model the fast pass admitted (all counts/variants to the ceiling).

    Stage 1: contenders (fast-pass P >= CONTENDER_MIN_P, and always the top two) at ``accepted``.
    Stage 2: every other model at ``exploratory``; it orders work and stays in the comparison at its
             calibrated (wider) sigma, never excluded on its own.
    Stage 3: an exploratory model whose exploratory lnZ + 3 sigma, or its fast-pass lnZ, sits within
             RELEVANCE_GAP of the leader (weights include the model prior) is rerun at ``accepted``.
    Each stage runs in chunks of ``chunk`` models. ``budget_s`` (None: no clock, the default) is
    checked before each chunk; a started chunk is never cut. A model left without a usable lnZ, or
    promoted but not yet rerun, makes the result "search limited": its probabilities are then labelled
    partial. So does a non-empty ``fast_limit`` (the record's ``search_limit_reason``): the fast pass
    stopped before pricing every count, so the models here are not all the competing ones.
    ``map_fn`` lets the caller run a chunk's models in a process pool (each run single-threaded).
    ``progress`` gets C-progress ``sampling`` steps as each stage starts and as each model returns."""
    t0 = time.perf_counter()
    chunk = max(1, int(chunk))
    send = emitter(progress, "deep")
    order = sorted(entries, key=lambda e: -_fast_weight(e))
    fz = np.array([_fast_weight(e) for e in order])
    fp = np.exp(fz - fz.max()) if np.isfinite(fz).any() else np.zeros(len(order))
    fp = fp / fp.sum() if fp.sum() > 0 else fp
    contenders = {e.label for i, e in enumerate(order) if i < 2 or fp[i] >= CONTENDER_MIN_P}
    out = {e.label: ModelEvidence(e.label, e.count, "not_run", log_model_prior=e.log_model_prior)
           for e in order}
    by_label = {e.label: e for e in order}
    seeds = {e.label: seed + 104729 * i for i, e in enumerate(order)}
    stage_seconds = {"contenders": 0.0, "exploratory": 0.0, "promoted": 0.0}
    attempted_promotion: set = set()

    def over_budget():
        return budget_s is not None and time.perf_counter() - t0 >= budget_s

    def run_stage(name, labels, settings, status, stage, seed_offset=0):
        started = time.perf_counter()
        if labels:
            send("sampling", step=1, of=len(labels), stage=stage, stages=DEEP_STAGES)
        done = 0
        for i in range(0, len(labels), chunk):
            if over_budget():
                break
            jobs = [(lb, by_label[lb], settings, seeds[lb] + seed_offset)
                    for lb in labels[i:i + chunk]]
            for lb, runs in map_fn(_stage_task, jobs):
                done += 1
                m = out[lb]
                m.runs.extend(runs)
                good = [r for r in runs if r.usable]
                if good:
                    r = good[-1]
                    m.status, m.log_z = status, r.log_z
                    err = r.log_z_err if math.isfinite(r.log_z_err) else 0.0
                    m.sigma = max(r.settings.calibrated_sigma, err)
                elif m.status == "not_run":
                    m.status = "unresolved"     # suspect after restarts: kept, never rejected
                if name == "promoted":
                    attempted_promotion.add(lb)
                if done < len(labels):
                    send("sampling", step=done + 1, of=len(labels), stage=stage, stages=DEEP_STAGES)
        stage_seconds[name] = time.perf_counter() - started

    run_stage("contenders", [e.label for e in order if e.label in contenders], accepted, "reference", 1)
    run_stage("exploratory", [e.label for e in order if e.label not in contenders], exploratory,
              "exploratory", 2)
    have, w = _weights(out.values())
    lead = float(w.max()) if have else -math.inf
    # An exploratory run alone may not settle a model as irrelevant: low-n_live runs underestimate
    # lnZ by tens of nats with a best lnL at the optimum (measured, opus/check_problem.py). It stays
    # unrefined only when its fast-pass Laplace lnZ also sits beyond the gap.
    promote = sorted((lb for lb, m in out.items() if m.status == "exploratory"
                      and max(m.log_z + 3.0 * m.sigma, _finite(by_label[lb].fastpass_ln_z))
                      + m.log_model_prior >= lead - RELEVANCE_GAP),
                     key=lambda lb: -(out[lb].log_z + out[lb].log_model_prior))
    run_stage("promoted", promote, accepted, "reference", 3, seed_offset=1)  # independent of the exploratory run

    models = [out[e.label] for e in order]
    pending = [lb for lb in promote if out[lb].status != "reference"]
    waiting = [lb for lb in pending if lb not in attempted_promotion]
    suspect = [m.label for m in models if m.status == "unresolved"] + \
        [lb for lb in pending if lb in attempted_promotion]
    unrun = [m.label for m in models if m.status == "not_run"]
    complete = not (unrun or waiting or suspect or fast_limit)
    reasons = [f"fast pass search limited: {fast_limit}"] if fast_limit else []
    if unrun:
        reasons.append("budget ended before " + ", ".join(unrun))
    if waiting:
        reasons.append("budget ended before promotion of " + ", ".join(waiting))
    if suspect:
        reasons.append("suspect after restarts: " + ", ".join(suspect))
    _, counts = _probabilities(models)
    cc = close_call(models, close_call_p=close_call_p, seed=seed)
    return DeepPassResult(models=models, complete=complete,
                          search_limited="; ".join(reasons) or None,
                          count_probabilities=counts,
                          comparison_scope="full" if complete else "partial (search limited)",
                          best_count=cc.get("best_count"), close_call=cc,
                          wall_s=time.perf_counter() - t0,
                          exploratory_only=tuple(m.label for m in models
                                                 if m.status == "exploratory"),
                          stage_seconds=stage_seconds)


def _finite(v) -> float:
    """None or NaN -> +inf: a missing fast-pass value cannot rule a model out, so it promotes."""
    return float(v) if v is not None and math.isfinite(v) else math.inf


def _stage_task(job):
    label, entry, settings, seed = job
    return label, run_until_clean(label, entry.problem, settings=settings, seed=seed,
                                  reference_lnl=entry.fastpass_lnl_opt)


def _better_fit(fast_fit, refit) -> tuple:
    """``(result, source)``: what the chosen model publishes, the fast-pass optimum's fit or the
    refit from the nested maximum-likelihood sample. A converged fit always beats an unconverged
    one; between two of the same status the lower objective wins (ties keep the fast-pass optimum)."""
    if refit.optimizer_converged != fast_fit.optimizer_converged:
        better = bool(refit.optimizer_converged)
    else:
        better = loop._objective(refit) < loop._objective(fast_fit)
    return (refit, "nested_refit") if better else (fast_fit, "fast_pass_optimum")


# --- the models of a fast-pass record -------------------------------------------------------------
def plain_hypotheses(ctx, ceiling: int) -> dict:
    """``label -> Hypothesis`` for every plain structure of counts 0..``ceiling`` and its
    no-background twin (the fast pass's variants); the delta twins are built from these. ``ceiling``
    is the record's ``settings.mode_ceiling``, never the environment."""
    out = {}
    for n in range(int(ceiling) + 1):
        for hyp in catalogue.enumerate_count_structures(ctx, n):
            out[str(hyp.label)] = hyp
            twin = loop.without_background(hyp)
            if twin is not None:
                out[str(twin.label)] = twin
    return out


def _model_hypothesis(ctx, label: str, first: dict, hypotheses: Mapping):
    """The catalogue hypothesis a record model was fitted as: the plain structure, or its delta twin
    (the mode numbers of the label suffix must agree with the record's ``delta_modes``)."""
    plain_label, sep, suffix = label.rpartition("_delta")
    if not sep:
        plain_label, suffix = label, ""
    delta = [int(i) for i in first.get("delta_modes") or ()]
    if suffix != "-".join(str(i + 1) for i in delta):
        raise ValueError(f"model {label!r}: label suffix disagrees with delta_modes {delta}")
    plain = hypotheses.get(plain_label)
    if plain is None:
        raise ValueError(f"model {label!r}: no hypothesis {plain_label!r} among the plain structures")
    if not delta:
        return plain
    if not all(0 <= i < int(first["n_peaks"]) for i in delta):
        raise ValueError(f"model {label!r}: delta_modes {delta} outside its {first['n_peaks']} modes")
    # Mode i (0-based, ascending omega0) is prefix _mode_prefix(i + 1): the ordered energy transform
    # puts the (i+1)-th smallest energy there, as the fast pass built the twin.
    twin = loop.delta_twin_hypothesis(plain, [loop._mode_prefix(i + 1) for i in delta], ctx)
    if twin is None or str(twin.label) != label:
        raise ValueError(f"model {label!r}: the delta twin rebuilds as "
                         f"{None if twin is None else twin.label!r}")
    return twin


def models_from_record(ctx, record: dict, hypotheses: Mapping) -> list:
    """One :class:`ModelEntry` per label of a fast-pass record (basins of one label are one entry),
    twin parents and ``not_fitted`` models included: nested sampling starts from the prior and
    needs no fit. No refit and no evidence computation. ``ValueError`` naming the cause (never a
    partial list) when the record is not one the deep pass can sample: no fast-pass record, the data
    changed since the fast pass, a constrained context, a prior provenance that disagrees with the
    record's settings, or a model that does not rebuild."""
    if record.get("schema") != FAST_PASS_SCHEMA:
        raise ValueError("no fast-pass record")
    if record.get("arena_fingerprint") != loop.data_likelihood_fingerprint(ctx):
        raise ValueError("data changed since the fast pass")
    constraints = getattr(ctx, "constraints", None)
    if getattr(constraints, "soft", None) or getattr(constraints, "hard", None):
        raise ValueError("constrained context (campaign rescue): the deep pass samples "
                         "unconstrained models only")
    configured = (record.get("settings") or {}).get("prior")
    ps = None if configured is None else prior.PriorSettings(**configured)
    if record.get("prior_provenance") != json.loads(json.dumps(prior.recipe_provenance(ctx, ps))):
        raise ValueError("prior provenance differs from the record's settings")
    saturated = record.get("ln_like_saturated")
    groups: dict = {}
    for m in record["models"]:
        groups.setdefault(m["label"], []).append(m)
    entries = []
    for label, basins in groups.items():
        first = basins[0]
        hyp = _model_hypothesis(ctx, label, first, hypotheses)
        try:
            bundle = evidence.candidate_bundle(hyp, ctx, prior_settings=ps)
        except ValueError as exc:
            raise ValueError(f"model {label!r}: {exc}") from exc
        names = [p.name for p in bundle.params]
        if len(names) != int(first["k_free"]) or set(names) != set(first["free"]):
            raise ValueError(f"model {label!r}: the rebuilt model samples {names}, the record "
                             f"fitted {list(first['free'])}")
        priced = [b["ln_z"] for b in basins if b.get("ln_z") is not None]
        costed = [-0.5 * b["cost"] for b in basins if b.get("cost") is not None]
        if priced and saturated is None:
            raise ValueError("the record prices a model but carries no ln_like_saturated")
        entries.append(ModelEntry(
            label=label, count=int(first["n_peaks"]), ndim=len(names),
            fastpass_ln_z=(float(np.logaddexp.reduce(priced)) - float(saturated)) if priced else None,
            fastpass_lnl_opt=max(costed) if costed else None,
            problem=EvidenceProblem(hyp, loop.candidate_context(hyp, ctx), bundle),
            log_model_prior=float(first["ln_model_prior"])))
    return entries

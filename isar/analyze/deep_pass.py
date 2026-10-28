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
import logging
import math
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from concurrent.futures import FIRST_COMPLETED, wait

import numpy as np

from isar.analyze import catalogue, evidence, loop, prior
from isar.analyze.errors.bayes import EvidenceProblem
from isar.analyze.progress import DEEP_STAGES, ProgressFn, emitter

log = logging.getLogger(__name__)
_timing_log = logging.getLogger(__name__ + ".timing")
_timing_worker_pid: int | None = None
_timing_worker_info: dict | None = None


def _log_timing(event: str, **fields) -> None:
    """Console diagnostics only; windowless workers deliver timings through the progress pipe."""
    if sys.stderr is None:
        return
    _timing_log.setLevel(logging.INFO)
    if not _timing_log.hasHandlers():
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(message)s"))
        _timing_log.addHandler(handler)
    _timing_log.info("deep_timing %s", json.dumps({"event": event, "pid": os.getpid(), **fields},
                                               separators=(",", ":")))


def _log_worker_once(workers: int) -> dict:
    """Collect once per worker; return the inventory with every result for its item's progress pipe."""
    global _timing_worker_pid, _timing_worker_info
    pid = os.getpid()
    if _timing_worker_pid == pid:
        return {**_timing_worker_info, "workers": workers}
    _timing_worker_pid = pid
    try:
        from threadpoolctl import threadpool_info
        pools = threadpool_info()
    except Exception as exc:  # diagnostics must not prevent an otherwise runnable fit
        log.warning("deep pass threadpool inventory unavailable: %s: %s", type(exc).__name__, exc)
        _timing_worker_info = {"event": "worker", "pid": pid, "workers": workers, "threadpools": None,
                               "inventory_failure": f"{type(exc).__name__}: {exc}"}
    else:
        _timing_worker_info = {"event": "worker", "pid": pid, "workers": workers, "threadpools": pools}
    _log_timing(**_timing_worker_info)
    return _timing_worker_info


CLOSE_CALL_P = 0.80             # the fast pass's threshold (s2.3); ``deep_pass`` takes the record's
NUMERIC_FLIP_FRACTION = 0.05    # winner changes in more than this share of perturbed draws => numeric
PERTURB_DRAWS = 4000
CONTENDER_MIN_P = 0.01          # fast-pass probability that makes a model a stage-1 contender
RELEVANCE_GAP = 10.0            # nats below the leader beyond which more precision cannot matter
MAX_RESTARTS = 2
FAST_PASS_SCHEMA = "isar.fast-pass/1"    # scan_delivery.FAST_PASS_RECORD_SCHEMA; analyze imports no session code
# Q2 measures final usable production retries. ACCEPTED uses a conservative SD upper bound;
# EXPLORATORY keeps its historical sigma because its usable model groups were incomplete.
# Neither repeatability nor the suspect check establishes absolute evidence accuracy.
CALIBRATION_STATUS = ("provisional: production-retry uncertainty measurement, 2026-10-28; "
                      "accepted sigma uses the largest per-model one-sided 95% SD upper bound, rounded up, "
                      "from nine usable outcomes per deciding model; exploratory measurement incomplete, "
                      "historical sigma retained; normal-seed approximation on one spectrum, not accuracy; "
                      "report out/deep-retry-20261028-r2/report/ (calibration.json, calibration.md)")
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
    calibrated_sigma: float = 1.0   # provisional lnZ uncertainty in nats; level-specific provenance below

    def step_kwargs(self, ndim: int) -> dict:
        if self.sample == "rwalk":
            return {"walks": int(self.step_factor * (ndim + 20))}
        if self.sample == "rslice":
            return {"slices": int(self.step_factor * (ndim + 3))}
        raise ValueError(f"unknown sample method {self.sample!r}")


# Q2: largest per-model 95% upper SD bound on final usable retries, rounded upward to 0.01 nat.
ACCEPTED = SamplerSettings(n_live=400, step_factor=4, calibrated_sigma=5.55)
# Historical one-pass screen: largest three-seed SD plus 0.5 for dlogz; Q2 incomplete at this level.
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


def run_until_clean(label, problem, *, settings, seed, reference_lnl, timing_context=None):
    """Run; on a suspect run restart with a new seed and escalated settings, up to MAX_RESTARTS.
    Returns every run (the failures are kept: "save the failure and its settings"). Never rejects."""
    runs = []
    s = settings
    for attempt in range(MAX_RESTARTS + 1):
        rec = run_nested(label, problem, settings=s, seed=seed + 7919 * attempt,
                         reference_lnl=reference_lnl)
        runs.append(rec)
        if timing_context is not None:
            _log_timing(**_attempt_timing(rec, attempt, pid=os.getpid(), **timing_context))
        if rec.usable:
            break
        s = escalate(s, attempt + 1)
    return runs


def _attempt_timing(rec, restart, **context):
    return {"event": "run_completed", **context, "label": rec.label, "restart": restart,
            "seed": rec.seed, "settings": asdict(rec.settings), "ndim": rec.ndim,
            "wall_s": rec.wall_s, "n_like_calls": rec.n_like_calls, "n_iter": rec.n_iter,
            "status": "usable" if rec.usable else "failed" if rec.failure else "suspect",
            "failure": rec.failure, "suspect": list(rec.suspect)}


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
    stage_seconds: dict = field(default_factory=dict)   # initial (combined), promoted


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


def _weight(ln_z, log_model_prior: float) -> float:
    return ln_z + log_model_prior if ln_z is not None and math.isfinite(ln_z) else -math.inf


def _fast_weight(e: ModelEntry) -> float:
    return _weight(e.fastpass_ln_z, e.log_model_prior)


def _contender_count(weights) -> int:
    """How many of these fast weights, ranked, are stage-1 contenders: always the top two, and every model
    whose normalised fast-pass P is at least CONTENDER_MIN_P. P falls with rank, so they are a prefix."""
    fz = np.array(sorted(weights, reverse=True))
    fp = np.exp(fz - fz.max()) if np.isfinite(fz).any() else np.zeros(len(fz))
    fp = fp / fp.sum() if fp.sum() > 0 else fp
    return sum(1 for i in range(len(fz)) if i < 2 or fp[i] >= CONTENDER_MIN_P)


def _label_groups(models) -> dict:
    """A fast-pass record's models grouped by label: the basins of one model."""
    groups: dict = {}
    for m in models:
        groups.setdefault(m["label"], []).append(m)
    return groups


def _record_ln_z(basins, saturated) -> float | None:
    """A model's fast-pass lnZ from its basins' prices, relative to the saturated likelihood; None unpriced."""
    priced = [b["ln_z"] for b in basins if b.get("ln_z") is not None]
    return float(np.logaddexp.reduce(priced)) - float(saturated) if priced and saturated is not None else None


@dataclass(frozen=True)
class _StageJob:
    entry: ModelEntry
    settings: SamplerSettings
    status: str
    category: str
    seed: int


def stage_width(record: dict) -> int:
    """Distinct models available together in the initial stage; promotion cannot be wider."""
    return max(1, len(_label_groups(record.get("models") or ())))


def deep_pass(entries, *, accepted: SamplerSettings = ACCEPTED,
              exploratory: SamplerSettings = EXPLORATORY, budget_s: float | None = None,
              seed: int = 20261025, executor=None, close_call_p: float = CLOSE_CALL_P,
              workers: int = 1, fast_limit: str = "", progress: ProgressFn | None = None) -> DeepPassResult:
    """Cover every admitted model with two continuously refilled stages.

    The initial queue admits contenders at accepted settings, then noncontenders without a finite
    fast-pass price directly at accepted settings, then the remaining exploratory models. Within
    each category, unlimited runs start the largest dimension first; budgeted runs keep fast-weight
    priority. Seeds and output order remain keyed to the original fast-weight order. A direct
    accepted run uses its old promotion seed (+1); only if
    it exhausts its retries does its original exploratory sequence run as fallback. Such a model
    stays search limited even if fallback succeeds, and is not promoted again.

    Promotion is decided after all admitted initial jobs finish, including fallback. Its leader
    therefore includes the new direct-reference values. Relevant exploratory models then run at
    accepted settings, longest first without a budget and highest lnZ plus model prior first with
    a budget, with immediate refill. With no executor, work is serial.
    The budget is checked before each submission; admitted jobs finish even beyond the budget.
    Missing models, pending promotions, failed accepted attempts and fast-pass limits qualify the
    comparison as partial. Progress reports a zero on first admission and completed-model counts;
    a fallback model completes only when its fallback has returned.
    """
    t0 = time.perf_counter()
    workers = max(1, int(workers)) if executor is not None else 1
    send = emitter(progress, "deep")
    order = sorted(entries, key=lambda e: -_fast_weight(e))
    ranks = {e.label: i for i, e in enumerate(order)}
    contenders = {e.label for e in order[:_contender_count([_fast_weight(e) for e in order])]}
    out = {e.label: ModelEvidence(e.label, e.count, "not_run", log_model_prior=e.log_model_prior)
           for e in order}
    by_label = {e.label: e for e in order}
    seeds = {e.label: seed + 104729 * i for i, e in enumerate(order)}
    stage_seconds = {"initial": 0.0, "promoted": 0.0}
    attempted_promotion: set = set()
    failed_direct: set = set()
    fallback_waiting: set = set()
    reported_workers: set = set()

    def over_budget():
        return budget_s is not None and time.perf_counter() - t0 >= budget_s

    def job_key(job):
        rank = {"contenders": 0, "direct_reference": 1}.get(job.category, 2)
        if budget_s is None:
            priority = -job.entry.ndim
        elif job.category == "promoted":
            model = out[job.entry.label]
            priority = -(model.log_z + model.log_model_prior)
        else:
            priority = -_fast_weight(job.entry)
        return rank, priority, ranks[job.entry.label]

    def plan(models, settings, status, category, offset=0):
        return [_StageJob(e, settings, status, category, seeds[e.label] + offset) for e in models]

    def run_stage(name, jobs, stage):
        started = time.perf_counter()
        queue = sorted(jobs, key=job_key)
        total, done, announced = len(queue), 0, False

        def announce():
            nonlocal announced
            if not announced:
                send("sampling", step=0, of=total, stage=stage, stages=DEEP_STAGES)
                announced = True

        def accept(job, result):
            nonlocal done
            lb, runs, worker = result
            timing = []
            if worker is not None:
                if worker["pid"] not in reported_workers:
                    timing.append(worker)
                    reported_workers.add(worker["pid"])
                timing.extend(_attempt_timing(r, attempt, pid=worker["pid"], stage=job.category,
                                              workers=workers) for attempt, r in enumerate(runs))
            m = out[lb]
            m.runs.extend(runs)
            good = [r for r in runs if r.usable]
            if good:
                r = good[-1]
                m.status, m.log_z = job.status, r.log_z
                err = r.log_z_err if math.isfinite(r.log_z_err) else 0.0
                m.sigma = max(r.settings.calibrated_sigma, err)
            elif m.status == "not_run":
                m.status = "unresolved"
            if job.category == "promoted":
                attempted_promotion.add(lb)
            if job.category == "direct_reference" and not good:
                failed_direct.add(lb)
                fallback_waiting.add(lb)
                queue.append(_StageJob(job.entry, exploratory, "exploratory",
                                       "exploratory_fallback", seeds[lb]))
                queue.sort(key=job_key)
                send("sampling", step=done, of=total, stage=stage, stages=DEEP_STAGES, timing=timing)
                return
            if job.category == "exploratory_fallback":
                fallback_waiting.remove(lb)
            done += 1
            # The item owns this callback. Its pipe reaches the retained GUI queue log even when
            # pythonw gives both the item and its spawned workers no stderr stream. A model's
            # attempts become durable together when its retry sequence has returned.
            send("sampling", step=done, of=total, stage=stage, stages=DEEP_STAGES, timing=timing)

        if executor is None:
            while queue and not over_budget():
                job = queue.pop(0)
                announce()
                args = (job.entry.label, job.entry, job.settings, job.seed)
                accept(job, _stage_task(args, stage=job.category, workers=1))
        else:
            pending = {}
            while queue or pending:
                while queue and len(pending) < workers and not over_budget():
                    job = queue.pop(0)
                    announce()
                    args = (job.entry.label, job.entry, job.settings, job.seed)
                    pending[executor.submit(_stage_task, args, stage=job.category, workers=workers)] = job
                if not pending:
                    break
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                # Several futures may be ready together; scientific records never follow set order.
                for future in sorted(completed, key=lambda f: ranks[pending[f].entry.label]):
                    job = pending.pop(future)
                    accept(job, future.result())
        stage_seconds[name] = time.perf_counter() - started

    initial = plan([e for e in order if e.label in contenders], accepted, "reference", "contenders")
    direct = [e for e in order if e.label not in contenders and
              (e.fastpass_ln_z is None or not math.isfinite(e.fastpass_ln_z))]
    direct_labels = {e.label for e in direct}
    initial += plan(direct, accepted, "reference", "direct_reference", offset=1)
    initial += plan([e for e in order if e.label not in contenders and e.label not in direct_labels],
                    exploratory, "exploratory", "exploratory")
    run_stage("initial", initial, 1)
    have, w = _weights(out.values())
    lead = float(w.max()) if have else -math.inf
    # Low-live runs may underestimate lnZ; both their upper uncertainty and the finite fast-pass
    # price must lie below the relevance window before a model remains exploratory.
    promote = [lb for lb, m in out.items() if m.status == "exploratory" and lb not in failed_direct
               and max(m.log_z + 3.0 * m.sigma, by_label[lb].fastpass_ln_z)
               + m.log_model_prior >= lead - RELEVANCE_GAP]
    run_stage("promoted", plan([by_label[lb] for lb in promote], accepted, "reference", "promoted", 1), 2)

    models = [out[e.label] for e in order]
    pending = [lb for lb in promote if out[lb].status != "reference"]
    waiting = [lb for lb in pending if lb not in attempted_promotion]
    suspect = [m.label for m in models if m.status == "unresolved" or m.label in failed_direct or
               (m.label in pending and m.label in attempted_promotion)]
    unrun = [m.label for m in models if m.status == "not_run"]
    complete = not (unrun or waiting or suspect or fallback_waiting or fast_limit)
    reasons = [f"fast pass search limited: {fast_limit}"] if fast_limit else []
    if unrun:
        reasons.append("budget ended before " + ", ".join(unrun))
    if waiting:
        reasons.append("budget ended before promotion of " + ", ".join(waiting))
    if fallback_waiting:
        reasons.append("budget ended before exploratory fallback of " +
                       ", ".join(e.label for e in order if e.label in fallback_waiting))
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
                          exploratory_only=tuple(m.label for m in models if m.status == "exploratory"),
                          stage_seconds=stage_seconds)


def _stage_task(job, *, stage=None, workers=1):
    label, entry, settings, seed = job
    worker = _log_worker_once(workers)
    runs = run_until_clean(label, entry.problem, settings=settings, seed=seed,
                           reference_lnl=entry.fastpass_lnl_opt,
                           timing_context={"stage": stage, "workers": workers})
    return label, runs, worker


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
    entries = []
    for label, basins in _label_groups(record["models"]).items():
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
        costed = [-0.5 * b["cost"] for b in basins if b.get("cost") is not None]
        if saturated is None and any(b.get("ln_z") is not None for b in basins):
            raise ValueError("the record prices a model but carries no ln_like_saturated")
        entries.append(ModelEntry(
            label=label, count=int(first["n_peaks"]), ndim=len(names),
            fastpass_ln_z=_record_ln_z(basins, saturated),
            fastpass_lnl_opt=max(costed) if costed else None,
            problem=EvidenceProblem(hyp, loop.candidate_context(hyp, ctx), bundle),
            log_model_prior=float(first["ln_model_prior"])))
    return entries

"""The deep pass as ``isar run --deep-pass`` delivers it: the entry that runs
:func:`isar.analyze.deep_pass.deep_pass` on one spectrum's fast-pass record, the persisted record
(``isar.deep-pass/1``, ``FitOutcome.deep_pass``) and its timing (``run_timings.json``).

The fast pass runs first, in the same ``_process_one``; this module reads its record only for the
model context, the contender ordering and the suspect-run check. Sampling starts from the prior.
The record carries no wall time (like the fast-pass record); the timing entry does.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, replace

from isar.analyze import deep_pass, errors, loop
from isar.analyze.progress import ProgressFn, emitter, relay
from isar.session.state import canonical_hash

log = logging.getLogger(__name__)

SCHEMA = "isar.deep-pass/1"
ALGORITHM_REVISION = 3  # budgeted admission retains evidence relevance rather than dimension priority
UNTRUSTED = "provisional sampler settings: untrusted until calibration"


@dataclass(frozen=True)
class DeepRequest:
    """What one ``isar run --deep-pass`` asks for. The worker count is excluded from the signature:
    a complete deep pass is the same on any worker count, but a search-limited one depends on the
    worker count and wall clock. A carried result is not redone for a new worker count; a changed
    algorithm revision, budget or ``--force`` redoes it. ``budget_s`` None (the default) is no clock."""
    budget_s: float | None = None   # per spectrum, sampling stages only; typed on the CLI only
    workers: int = 1
    M: int = 200                    # bootstrap draws of the errors step
    seed: int = 20261025

    def signature(self) -> str:
        """Everything that decides the result, read at call time so an installed calibration
        (new constants), algorithm revision or changed budget makes an earlier deep pass stale."""
        return canonical_hash({
            "schema": SCHEMA, "algorithm_revision": ALGORITHM_REVISION,
            "accepted": asdict(deep_pass.ACCEPTED),
            "exploratory": asdict(deep_pass.EXPLORATORY),
            "calibration": deep_pass.CALIBRATION_STATUS,
            "contender_min_p": deep_pass.CONTENDER_MIN_P, "relevance_gap": deep_pass.RELEVANCE_GAP,
            "suspect_gap_base": deep_pass.SUSPECT_GAP_BASE,
            "suspect_gap_per_dim": deep_pass.SUSPECT_GAP_PER_DIM,
            "budget_s": _budget(self.budget_s), "seed": int(self.seed), "M": int(self.M)})


def _budget(budget_s):
    return None if budget_s is None else float(budget_s)


def is_current(oc, request: DeepRequest) -> bool:
    """True when the stored outcome ``oc`` (a dict) holds a deep pass under this request's settings
    signature that is not a refusal: it is carried, not rerun."""
    dp = (oc or {}).get("deep_pass") or {}
    return dp.get("settings_signature") == request.signature() and dp.get("state") != "refused"


def _refused(request: DeepRequest, reason: str) -> dict:
    return {"schema": SCHEMA, "state": "refused", "reason": reason,
            "settings_signature": request.signature(), "calibration": deep_pass.CALIBRATION_STATUS,
            "trusted": False, "budget_s": _budget(request.budget_s), "seed": int(request.seed),
            "M": int(request.M)}


def _run_record(r) -> dict:
    return {"seed": r.seed, "settings": asdict(r.settings), "ndim": r.ndim, "log_z": r.log_z,
            "log_z_err": r.log_z_err, "information_h": r.information_h,
            "n_like_calls": r.n_like_calls, "n_iter": r.n_iter, "lnl_max": r.lnl_max,
            "suspect": list(r.suspect), "failure": r.failure,
            "evidence_converged": r.evidence_converged}


def _run_timing(r) -> dict:
    s = r.settings
    return {"seed": r.seed, "n_live": s.n_live, "sample": s.sample, "step_factor": s.step_factor,
            "dlogz": s.dlogz, "wall_s": r.wall_s, "n_like_calls": r.n_like_calls,
            "n_iter": r.n_iter, "information_h": r.information_h, "lnl_max": r.lnl_max,
            "suspect": list(r.suspect)}


def _chosen_model(res):
    """The highest-weight model of the best count, whatever its stage, so the fit matches the weights."""
    cands = [m for m in res.models if m.count == res.best_count and math.isfinite(m.log_z)]
    return max(cands, key=lambda m: m.log_z + m.log_model_prior, default=None)


def _chosen(m, entry, record: dict, request: DeepRequest, executor) -> dict:
    """The chosen model's published fit and its errors, once: refit from the nested maximum and
    from the fast pass's best basin, keep the better, attach the errors (``errors.attach``)."""
    problem, label = entry.problem, m.label
    out = {"label": label, "status": m.status, "source": None, "params": None, "errors": None,
           "errors_failure": None, "model_manifest": None}
    basins = [b for b in record.get("models") or () if b.get("label") == label and b.get("params")]
    starts = {}
    runs = [r for r in m.runs if r.usable and r.best_theta]
    if runs:
        starts["nested_refit"] = problem.full_params(max(runs, key=lambda r: r.lnl_max).best_theta)
    if basins:
        starts["fast_pass_optimum"] = max(
            basins, key=lambda b: -math.inf if b.get("ln_z") is None else b["ln_z"])["params"]
    fits = {}
    for source, x0 in starts.items():
        try:
            fits[source] = loop.fit_hypothesis(problem.hyp, problem.ctx, x0=x0)
        except Exception as exc:  # noqa: BLE001 — the other start, or a recorded failure, remains
            log.warning("deep pass: refit of %s from %s failed: %s: %s",
                        label, source, type(exc).__name__, exc)
    if not fits:
        out["errors_failure"] = "no refit of the chosen model succeeded"
        return out
    if len(fits) == 2:
        result, out["source"] = deep_pass._better_fit(fits["fast_pass_optimum"], fits["nested_refit"])
    else:
        (out["source"], result), = fits.items()
    out["params"] = dict(result.params)
    from isar.session.predictive_artifact import model_manifest_of
    out["model_manifest"] = model_manifest_of(result, {label: problem.hyp})
    try:
        errors.attach(result, problem.hyp, problem.ctx, M=request.M, seed=request.seed,
                      with_profile=True, boot_workers=request.workers, boot_executor=executor)
    except Exception as exc:  # noqa: BLE001 — never a lost deep-pass record
        log.warning("deep pass: errors on %s failed: %s: %s", label, type(exc).__name__, exc,
                    exc_info=True)
        out["errors_failure"] = f"{type(exc).__name__}: {exc}"
        return out
    wm = result.within_model
    if not wm:
        out["errors_failure"] = ("refit did not converge" if not result.optimizer_converged
                                 else "no within-model errors for this model")
        return out
    out["errors"] = {k: wm.get(k) for k in ("freq_interval", "gamma_interval", "profile_interval",
                                            "minos", "M_effective", "n_converged", "bimodal", "method_seconds")}
    return out


def _state_reason(state: str, res, cc: dict, trusted: bool) -> str:
    p = cc.get("p_best")
    best = f"count {res.best_count} (P {p:.2f})" if p is not None else "no count"
    if state == "search_limited":
        text = f"search limited ({res.search_limited}); preferred {best}, partial comparison"
    elif state == "close_call":
        text = f"close call: preferred {best} below {cc.get('close_call_p'):.2f}"
    else:
        text = f"resolved: {best}"
    if cc.get("numerical"):
        text += "; numerically marginal"
    return text if trusted else f"{text}; {UNTRUSTED}"


def deliver(fit: dict, ctx, request: DeepRequest, *, executor=None, refuse=None,
            progress: ProgressFn | None = None) -> tuple:
    """``(record, timing)`` of the deep pass on one fitted spectrum. ``fit`` is ``fit_one``'s dict
    (its ``fast_pass`` record) and ``ctx`` the scan context it was fitted on; ``executor`` a process
    pool for the stage runs and the bootstrap (``None`` = serial); ``refuse`` a reason the caller
    already knows; ``progress`` a C-progress callback (never changes the result). A refusal or a
    failure is a ``state="refused"`` record with ``None`` timing, never an exception: one spectrum
    must not end the batch."""
    t0 = time.perf_counter()
    send = emitter(progress, "deep")
    if refuse:
        return _refused(request, refuse), None
    record = fit.get("fast_pass")
    if not record:
        return _refused(request, "no fast-pass record"), None
    try:
        settings = record.get("settings") or {}
        entries = deep_pass.models_from_record(
            ctx, record, deep_pass.plain_hypotheses(ctx, settings["mode_ceiling"]))
        if not entries:
            return _refused(request, "the fast-pass record holds no models"), None
        close_call_p = float(settings.get("close_call_p", deep_pass.CLOSE_CALL_P))
        res = deep_pass.deep_pass(
            entries, accepted=deep_pass.ACCEPTED, exploratory=deep_pass.EXPLORATORY,
            budget_s=request.budget_s, seed=request.seed,
            executor=executor, close_call_p=close_call_p,
            workers=request.workers, fast_limit=record.get("search_limit_reason") or "",
            progress=relay(send))
        send("errors")
        t_errors = time.perf_counter()
        m = _chosen_model(res)
        by_label = {e.label: e for e in entries}
        chosen = _chosen(m, by_label[m.label], record, request, executor) if m is not None else None
        errors_s = time.perf_counter() - t_errors
        error_methods = ((chosen.get("errors") or {}).pop("method_seconds", None) or {}
                         if chosen is not None else {})
    except ValueError as exc:
        return _refused(request, str(exc)), None
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("deep pass failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        return _refused(request, f"deep pass failed ({type(exc).__name__}: {exc})"), None

    if res.best_count == int(settings["mode_ceiling"]):
        reason = "; ".join(filter(None, (res.search_limited, "deep winner at count ceiling")))
        res = replace(res, complete=False, search_limited=reason,
                      comparison_scope="partial (search limited)")
    cc = {**res.close_call, "close_call_p": close_call_p}
    trusted = deep_pass.CALIBRATION_STATUS.startswith("accepted")
    state = ("search_limited" if not res.complete
             else "close_call" if cc.get("statistical") else "resolved")
    models = [{"label": e.label, "count": e.count, "log_model_prior": e.log_model_prior,
               "status": x.status, "log_z": x.log_z, "sigma": x.sigma,
               "fastpass_ln_z": e.fastpass_ln_z, "runs": [_run_record(r) for r in x.runs]}
              for x in res.models for e in (by_label[x.label],)]
    out = {"schema": SCHEMA, "state": state, "reason": _state_reason(state, res, cc, trusted),
           "calibration": deep_pass.CALIBRATION_STATUS, "trusted": trusted,
           "settings_signature": request.signature(),
           "accepted": asdict(deep_pass.ACCEPTED), "exploratory": asdict(deep_pass.EXPLORATORY),
           "budget_s": _budget(request.budget_s), "seed": int(request.seed), "M": int(request.M),
           "close_call_p": close_call_p, "complete": res.complete,
           "comparison_scope": res.comparison_scope, "search_limited": res.search_limited,
           "count_probabilities": {str(k): v for k, v in sorted(res.count_probabilities.items())},
           "best_count": res.best_count, "p_best": cc.get("p_best"),
           "close_call": {"statistical": cc.get("statistical"), "numerical": cc.get("numerical"),
                          "winner_flip_fraction": cc.get("winner_flip_fraction"),
                          "p_best_band_5_95": cc.get("p_best_band_5_95")},
           "exploratory_only": list(res.exploratory_only), "models": models, "chosen": chosen,
           "arena_fingerprint": record.get("arena_fingerprint")}
    timing = {"total_seconds": time.perf_counter() - t0, "sampling_seconds": res.wall_s,
              "errors_seconds": errors_s, "budget_s": _budget(request.budget_s),
              "errors_method_seconds": error_methods,
              "workers": max(1, int(request.workers)) if executor is not None else 1,
              "stages": dict(res.stage_seconds),
              "models": [{"label": x.label, "status": x.status,
                          "runs": [_run_timing(r) for r in x.runs]} for x in res.models]}
    return out, timing

"""Within-model error estimation for the structure loop (§5).

The reported uncertainty is built side-by-side from independent paradigms; their agreement
is itself the trust signal. This slice ships the frequentist half: the parametric bootstrap
(:mod:`~isar.analyze.errors.bootstrap`, the authoritative interval), MINOS + an ω₀ profile
(:mod:`~isar.analyze.errors.profile`), and the MINOS↔bootstrap agreement
(:mod:`~isar.analyze.errors.agree`). The Bayesian path (§5.4) is step 7.
"""
import logging
import time

import numpy as np

from isar.analyze.errors.bootstrap import BootstrapResult, parametric_bootstrap
from isar.schema.statistics import (
    INTERVAL_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    validate_for_write,
)

__all__ = ["BootstrapResult", "parametric_bootstrap", "attach", "attach_batch"]
log = logging.getLogger(__name__)

# Bayes draws its own RNG stream via its own seed; offsetting it from the bootstrap seed
# avoids two independent procedures consuming an identical stream while staying deterministic.
_BAYES_SEED_OFFSET = 1_000_003


def _bootstrap_measure(estimator_kind: str) -> str:
    return {
        "data_likelihood_mle": "unconstrained_parametric_bootstrap",
        "soft_penalized_likelihood": "soft_penalized_parametric_bootstrap",
        "hard_conditioned_likelihood": "hard_conditioned_parametric_bootstrap",
    }[estimator_kind]


def _profile_kind(estimator_kind: str) -> str:
    return {
        "data_likelihood_mle": "likelihood_profile",
        "soft_penalized_likelihood": "penalized_profile",
        "hard_conditioned_likelihood": "conditional_profile",
    }[estimator_kind]


def _interval_semantics_for(result, within: dict, *, level: float = 0.68) -> dict:
    fit_semantics = getattr(result, "fit_semantics", None)
    if fit_semantics is None:
        raise SemanticsConstructionError("missing_candidate_fit_semantics", str(result.label))
    estimator_kind = fit_semantics["estimator_kind"]
    common = {
        "availability": True,
        "nominal_level": float(level),
        "estimator_kind": estimator_kind,
        "effective_constraints": list(fit_semantics.get("effective_constraints") or []),
        "conditioning_fingerprint": fit_semantics["conditioning_fingerprint"],
        "calibration_target": None,
    }
    candidate_scopes = authority_scopes(
        single_fit_delivery="candidate",
        legacy_master_certification="candidate",
        stopping="candidate",
        user_diagnostic="active",
    )
    quantities = {
        "freq_interval": {
            **common,
            "availability": within.get("freq_interval") is not None,
            "interval_kind": "parametric_bootstrap_percentile",
            "bootstrap_measure_kind": _bootstrap_measure(estimator_kind),
            "particle_measure_kind": "parametric_bootstrap_sampling_distribution",
            "draw_measure_kind": "equal_weight_empirical_particles",
            "inference_paradigm": "frequentist_sampling_distribution",
            # The producer can offer this interval to the delivery ladder.  The driver replaces
            # ``authority_scopes`` with the realized selected/trust-gated authority for the outcome.
            "producer_authority_scopes": candidate_scopes,
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
    }
    if "profile_interval" in within:
        quantities["profile_interval"] = {
            **common,
            "availability": within.get("profile_interval") is not None,
            "interval_kind": _profile_kind(estimator_kind),
            "inference_paradigm": "frequentist_profile",
            "minos_parameter_intervals_present": bool(within.get("minos")),
            "producer_authority_scopes": candidate_scopes,
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
    if "bayes_interval" in within:
        prior_fingerprint = within.get("bayes_prior_fingerprint")
        if prior_fingerprint is None:
            raise SemanticsConstructionError("missing_bayesian_prior_fingerprint")
        quantities["bayes_interval"] = {
            "availability": within.get("bayes_interval") is not None,
            "nominal_level": float(level),
            "interval_kind": "optimizer_bound_nested_credible",
            "particle_measure_kind": "optimizer_bound_nested_parameter_distribution",
            "draw_measure_kind": "equal_weight_empirical_particles",
            "inference_paradigm": "bayesian_posterior",
            "prior_kind": "optimizer_bound_data_dependent",
            "prior_data_dependent": True,
            "prior_fingerprint": prior_fingerprint,
            "prior_specification": within.get("bayes_prior_specification"),
            "posterior_measure_kind": "posterior_resample",
            "evidence_comparable": False,
            "bayesian_model_average": False,
            "conditioning_fingerprint": prior_fingerprint,
            "calibration_target": None,
            "producer_authority_scopes": authority_scopes(
                user_diagnostic="active"
            ),
            "authority_scopes": authority_scopes(user_diagnostic="active"),
        }
    record = {
        "schema": INTERVAL_SCHEMA,
        "quantities": quantities,
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }
    return validate_for_write(record, INTERVAL_SCHEMA)


def _attach_interval_semantics(result, within: dict) -> None:
    try:
        within["interval_semantics"] = _interval_semantics_for(result, within)
    except (SemanticsConstructionError, KeyError) as exc:
        reason_code = (
            exc.reason_code if isinstance(exc, SemanticsConstructionError)
            else "missing_fit_semantics_field"
        )
        log.warning(
            "semantics_construction_failed producer=isar.analyze.errors "
            "quantity=%s reason_code=%s",
            result.label,
            reason_code,
        )
        within["interval_semantics"] = None


def attach(result, hyp, ctx, *, M=200, seed=12345, backend="iminuit",
           with_profile=False, with_bayes=False, prior=None, boot_workers=1,
           boot_executor=None, boot_adaptive=False, bayes_opts=None):
    """Run the within-model error layer on a fitted hypothesis and populate
    ``result.within_model`` (the slot step 1 reserved). No-op on a non-converged fit
    (bootstrapping a degenerate model is meaningless — it stays for step-3 bootstrap only
    when the optimizer actually converged). Returns the (mutated) result.

    ``with_profile`` adds the MINOS/ω₀-profile (§5.3). ``with_bayes`` adds the Bayesian
    posterior (§5.4) with ``prior`` (a :class:`~isar.analyze.prior.PhononPrior`) as a literal
    prior on ω₀; ``bayes_opts`` forwards extra kwargs (e.g. ``n_live``, ``dlogz``, ``max_iter``)
    to :func:`~isar.analyze.errors.bayes.bayesian_posterior`. Whenever a second road is
    present, the agreement object carries the §5.5 three-method (HESSE / bootstrap /
    posterior) cross-check as data. ``within_model["method_seconds"]`` records the wall time
    of each sub-method that actually ran."""
    if not result.optimizer_converged:
        return result
    t0 = time.perf_counter()
    boot = parametric_bootstrap(hyp, ctx, result, M=M, seed=seed, backend=backend,
                                workers=boot_workers, executor=boot_executor,
                                adaptive=boot_adaptive)
    method_seconds = {"bootstrap": round(time.perf_counter() - t0, 4)}
    within = {
        "freq_interval": boot.omega0_interval,     # (lo, median, hi) ω₀ — authoritative
        "gamma_interval": boot.gamma_interval,
        "n_converged": boot.n_converged,
        "M": boot.M,
        "M_requested": int(M),
        "M_effective": int(boot.M),
        "bimodal": boot.bimodal,
        "omega0_samples": boot.omega0_samples,
        "parameter_names": boot.parameter_names,
        "parameter_samples": boot.parameter_samples,
        "method_seconds": method_seconds,
    }
    minos = prof = bres = None
    if with_profile:
        from isar.analyze.errors import profile as _profile
        t1 = time.perf_counter()
        minos = _profile.minos_intervals(hyp, ctx, result)
        method_seconds["minos"] = round(time.perf_counter() - t1, 4)
        t2 = time.perf_counter()
        prof = _profile.omega0_profile_interval(hyp, ctx, result)
        method_seconds["profile"] = round(time.perf_counter() - t2, 4)
        within["profile_interval"] = prof
        within["minos"] = minos
    if with_bayes:
        from isar.analyze.errors import bayes as _bayes
        t3 = time.perf_counter()
        bres = _bayes.bayesian_posterior(hyp, ctx, result, prior=prior,
                                         seed=seed + _BAYES_SEED_OFFSET,
                                         **(bayes_opts or {}))
        method_seconds["bayes"] = round(time.perf_counter() - t3, 4)
        within["bayes_interval"] = bres.omega0_interval
        within["bayes_gamma_interval"] = bres.gamma_interval
        within["bayes_samples"] = bres.omega0_samples
        within["log_evidence"] = bres.log_evidence
        within["log_evidence_err"] = bres.log_evidence_err
        within["bayes_ok"] = bres.converged
        within["bayes_ess"] = bres.ess
        within["bayes_fail_reason"] = bres.fail_reason
        within["bayes_n_live"] = bres.n_live
        within["bayes_seed"] = bres.seed
        within["bayes_parameter_names"] = bres.parameter_names
        within["bayes_parameter_samples"] = bres.parameter_samples
        within["bayes_prior_fingerprint"] = bres.prior_fingerprint
        within["bayes_prior_specification"] = bres.prior_specification
    if with_profile or with_bayes:
        from isar.analyze.errors import agree as _agree
        within["agreement"] = _agree.agreement(
            boot, minos, profile_omega0=prof, bayes=bres, prior=prior,
            hesse_omega0_sigma=result.omega0_err_hesse)
    result.within_model = within
    _attach_interval_semantics(result, within)
    if with_profile:
        result.profile_ok = bool(minos) and all(bool(v[2]) for v in minos.values())
    return result


def _profile_method_task(args):
    """Picklable MINOS/profile-side task returning its own wall time."""
    kind, hyp, ctx, result, backend = args
    from isar.analyze.errors import profile as _profile
    started = time.perf_counter()
    if kind == "minos":
        value = _profile.minos_intervals(hyp, ctx, result, backend=backend)
    elif kind == "profile-low":
        value = _profile._omega0_profile_crossing(hyp, ctx, result, -1)
    elif kind == "profile-high":
        value = _profile._omega0_profile_crossing(hyp, ctx, result, +1)
    else:  # defensive: worker input is parent-authored, but never swallow corruption
        raise ValueError(f"unknown profile task {kind!r}")
    return value, time.perf_counter() - started


def attach_batch(items, *, M=200, seed=12345, backend="iminuit", with_profile=False,
                 bayes_label=None, prior=None, boot_workers=1, executor=None,
                 boot_adaptive=False, bayes_opts=None):
    """Attach errors to ordered ``(result, hypothesis, context)`` items.

    Independent MINOS and lower/upper profile tasks run first, while each result is still small
    enough to pickle cheaply. Bootstraps then retain exclusive use of the shared pool, one survivor
    at a time. Mutations happen only in this parent process.
    """
    active = [(result, hyp, ctx) for result, hyp, ctx in items if result.optimizer_converged]
    profile_values = {}
    profile_batch_seconds = 0.0
    profile_ran_parallel = False
    if with_profile and active:
        profile_started = time.perf_counter()
        tasks = []
        for result, hyp, ctx in active:
            tasks.extend((
                ("minos", hyp, ctx, result, backend),
                ("profile-low", hyp, ctx, result, backend),
                ("profile-high", hyp, ctx, result, backend),
            ))
        if executor is not None and len(tasks) > 1:
            try:
                outputs = list(executor.map(_profile_method_task, tasks))
                profile_ran_parallel = True
            except Exception as exc:  # noqa: BLE001
                log.warning("parallel survivor error methods failed; retrying serially: %s: %s",
                            type(exc).__name__, exc)
                outputs = [_profile_method_task(task) for task in tasks]
        else:
            outputs = [_profile_method_task(task) for task in tasks]
        profile_batch_seconds = round(time.perf_counter() - profile_started, 4)
        for index, (result, _hyp, _ctx) in enumerate(active):
            minos, minos_s = outputs[3 * index]
            low, low_s = outputs[3 * index + 1]
            high, high_s = outputs[3 * index + 2]
            profile_values[id(result)] = (minos, (low, high), minos_s, low_s, high_s)

    # One bootstrap at a time gets the entire shared pool; parallel survivor bootstraps would split
    # that same fixed budget and introduce nested scheduling without increasing throughput.
    for result, hyp, ctx in active:
        attach(result, hyp, ctx, M=M, seed=seed, backend=backend, with_profile=False,
               with_bayes=False, prior=prior, boot_workers=boot_workers,
               boot_executor=executor, boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)

    if with_profile:
        for result, _hyp, _ctx in active:
            minos, (low, high), minos_s, low_s, high_s = profile_values[id(result)]
            within = result.within_model
            within["minos"] = minos
            within["profile_interval"] = (low, high)
            within["method_seconds"]["minos"] = round(minos_s, 4)
            profile_s = max(low_s, high_s) if profile_ran_parallel else low_s + high_s
            within["method_seconds"]["profile"] = round(profile_s, 4)
            # The driver reports this true batch makespan as profile_seconds. It includes queueing
            # waves when workers < tasks and becomes additive after a logged serial fallback.
            within["method_seconds"]["profile_batch"] = profile_batch_seconds
            result.profile_ok = bool(minos) and all(bool(v[2]) for v in minos.values())

    from isar.analyze.errors import agree as _agree
    for result, hyp, ctx in active:
        within = result.within_model
        prof_data = profile_values.get(id(result))
        minos, prof = (prof_data[0], prof_data[1]) if prof_data is not None else (None, None)
        bres = None
        if bayes_label is not None and result.label == bayes_label:
            from isar.analyze.errors import bayes as _bayes
            started = time.perf_counter()
            bres = _bayes.bayesian_posterior(
                hyp, ctx, result, prior=prior, seed=seed + _BAYES_SEED_OFFSET,
                **(bayes_opts or {}))
            within["method_seconds"]["bayes"] = round(time.perf_counter() - started, 4)
            within.update({
                "bayes_interval": bres.omega0_interval,
                "bayes_gamma_interval": bres.gamma_interval,
                "bayes_samples": bres.omega0_samples,
                "log_evidence": bres.log_evidence,
                "log_evidence_err": bres.log_evidence_err,
                "bayes_ok": bres.converged,
                "bayes_ess": bres.ess,
                "bayes_fail_reason": bres.fail_reason,
                "bayes_n_live": bres.n_live,
                "bayes_seed": bres.seed,
                "bayes_parameter_names": bres.parameter_names,
                "bayes_parameter_samples": bres.parameter_samples,
                "bayes_prior_fingerprint": bres.prior_fingerprint,
                "bayes_prior_specification": bres.prior_specification,
            })
        if with_profile or bres is not None:
            # Reconstruct the BootstrapResult expected by agreement without repeating any work.
            boot = BootstrapResult(
                omega0_interval=within["freq_interval"], gamma_interval=within["gamma_interval"],
                omega0_samples=within["omega0_samples"], gamma_samples=np.empty(0),
                n_converged=within["n_converged"], M=within["M"],
                interval_type="percentile", bimodal=within["bimodal"], seed=int(seed),
                parameter_names=tuple(within.get("parameter_names") or ()),
                parameter_samples=np.asarray(
                    within["parameter_samples"] if within.get("parameter_samples") is not None
                    else np.empty((0, 0))))
            within["agreement"] = _agree.agreement(
                boot, minos, profile_omega0=prof, bayes=bres, prior=prior,
                hesse_omega0_sigma=result.omega0_err_hesse)
        _attach_interval_semantics(result, within)
    return [result for result, _hyp, _ctx in items]


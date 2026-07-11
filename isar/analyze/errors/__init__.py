"""Within-model error estimation for the structure loop (§5).

The reported uncertainty is built side-by-side from independent paradigms; their agreement
is itself the trust signal. This slice ships the frequentist half: the parametric bootstrap
(:mod:`~isar.analyze.errors.bootstrap`, the authoritative interval), MINOS + an ω₀ profile
(:mod:`~isar.analyze.errors.profile`), and the MINOS↔bootstrap agreement
(:mod:`~isar.analyze.errors.agree`). The Bayesian path (§5.4) is step 7.
"""
import time

from isar.analyze.errors.bootstrap import BootstrapResult, parametric_bootstrap

__all__ = ["BootstrapResult", "parametric_bootstrap", "attach"]

# Bayes draws its own RNG stream via its own seed; offsetting it from the bootstrap seed
# avoids two independent procedures consuming an identical stream while staying deterministic.
_BAYES_SEED_OFFSET = 1_000_003


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
        "bimodal": boot.bimodal,
        "omega0_samples": boot.omega0_samples,
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
    if with_profile or with_bayes:
        from isar.analyze.errors import agree as _agree
        within["agreement"] = _agree.agreement(
            boot, minos, profile_omega0=prof, bayes=bres, prior=prior,
            hesse_omega0_sigma=result.omega0_err_hesse)
    result.within_model = within
    return result


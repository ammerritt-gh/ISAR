"""Within-model error estimation for the structure loop (§5).

The reported uncertainty is built side-by-side from independent paradigms; their agreement
is itself the trust signal. This slice ships the frequentist half: the parametric bootstrap
(:mod:`~isar.analyze.errors.bootstrap`, the authoritative interval), MINOS + an ω₀ profile
(:mod:`~isar.analyze.errors.profile`), and the MINOS↔bootstrap agreement
(:mod:`~isar.analyze.errors.agree`). The Bayesian path (§5.4) is step 7.
"""
from isar.analyze.errors.bootstrap import BootstrapResult, parametric_bootstrap

__all__ = ["BootstrapResult", "parametric_bootstrap", "attach"]


def attach(result, hyp, ctx, *, M=200, seed=12345, backend="iminuit",
           with_profile=False, with_bayes=False, prior=None, boot_workers=1):
    """Run the within-model error layer on a fitted hypothesis and populate
    ``result.within_model`` (the slot step 1 reserved). No-op on a non-converged fit
    (bootstrapping a degenerate model is meaningless — it stays for step-3 bootstrap only
    when the optimizer actually converged). Returns the (mutated) result.

    ``with_profile`` adds the MINOS/ω₀-profile (§5.3). ``with_bayes`` adds the Bayesian
    posterior (§5.4) with ``prior`` (a :class:`~isar.analyze.prior.PhononPrior`) as a literal
    prior on ω₀. Whenever a second road is present, the agreement object carries the §5.5
    three-method (HESSE / bootstrap / posterior) cross-check as data."""
    if not result.optimizer_converged:
        return result
    boot = parametric_bootstrap(hyp, ctx, result, M=M, seed=seed, backend=backend,
                                workers=boot_workers)
    within = {
        "freq_interval": boot.omega0_interval,     # (lo, median, hi) ω₀ — authoritative
        "gamma_interval": boot.gamma_interval,
        "n_converged": boot.n_converged,
        "M": boot.M,
        "bimodal": boot.bimodal,
        "omega0_samples": boot.omega0_samples,
    }
    minos = prof = bres = None
    if with_profile:
        from isar.analyze.errors import profile as _profile
        minos = _profile.minos_intervals(hyp, ctx, result)
        prof = _profile.omega0_profile_interval(hyp, ctx, result)
        within["profile_interval"] = prof
        within["minos"] = minos
    if with_bayes:
        from isar.analyze.errors import bayes as _bayes
        bres = _bayes.bayesian_posterior(hyp, ctx, result, prior=prior, seed=seed)
        within["bayes_interval"] = bres.omega0_interval
        within["bayes_gamma_interval"] = bres.gamma_interval
        within["bayes_samples"] = bres.omega0_samples
        within["log_evidence"] = bres.log_evidence
        within["log_evidence_err"] = bres.log_evidence_err
        within["bayes_ok"] = bres.converged
    if with_profile or with_bayes:
        from isar.analyze.errors import agree as _agree
        within["agreement"] = _agree.agreement(
            boot, minos, profile_omega0=prof, bayes=bres, prior=prior,
            hesse_omega0_sigma=result.omega0_err_hesse)
    result.within_model = within
    return result


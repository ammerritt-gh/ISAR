"""Evidence ladder (SCENE-1 PR-b commit 3): Laplace exactness, arena guard, rung selection,
exactly-once prior counting, and the generalized prior-bundle dynesty lane.

Mirrors ``test_bayes.py`` idioms (small nlive + ``sample="unif"`` for the sampler lanes; one
fitted spectrum reused across assertions). Nothing consumes evidence yet — these tests pin the
engine's contract before select.py rebuilds on it."""
import math
import types
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import catalogue as C
from isar.analyze import evidence as E
from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bayes as BZ
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.analyze.phonon import enumerate_phonon_structures
from isar.schema.steering import ParamConstraints

pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor")

FAST = dict(n_live=80, dlogz=1.5, sample="unif", seed=3)


def _spectrum_ctx(seed=0, omega0=5.0, n=81, constraints=None):
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    kw = {} if constraints is None else {"constraints": constraints}
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero", **kw)


def _fitted(ctx, label="elastic+stokes+bg"):
    hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}[label]
    return hyp, loop.fit_hypothesis(hyp, ctx, minos=False)


def _bundle(hyp, ctx, *, entry=None):
    prob = loop.build_problem(hyp, ctx)
    exclude = [prob.free_names[i] for i in prob.fixed_pos]
    return C.resolve_priors(prob.composite, ctx, prob.bounds, entry=entry, exclude=exclude)


@pytest.fixture(scope="module")
def clean():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    return ctx, hyp, fit


# ------------------------------------------------------------------ Laplace exactness
def test_laplace_matches_analytic_gaussian_product():
    """Gaussian likelihood × Gaussian prior: Laplace at the MAP is EXACT, and the analytic
    marginal is ∫L·p = √(2πs²)·N(m; μ₀, √(s²+τ²)). Formula check with no fitter involved."""
    m, s = 2.0, 0.5          # likelihood: L(θ) = exp(−(θ−m)²/2s²), so cost = (θ−m)²/s²
    mu0, tau = 1.0, 0.8      # prior N(μ₀, τ), truncation negligible on wide bounds
    prior = C.ResolvedPrior("p.x", "gaussian", mu0, tau, -100.0, 100.0, "identity")
    var_post = 1.0 / (1.0 / s**2 + 1.0 / tau**2)
    theta_map = var_post * (m / s**2 + mu0 / tau**2)
    cost = (theta_map - m) ** 2 / s**2
    ln_z = E.laplace_ln_z(cost, prior.logpdf(theta_map), np.array([[var_post]]))
    expected = (0.5 * math.log(2.0 * math.pi * s**2)
                - 0.5 * (m - mu0) ** 2 / (s**2 + tau**2)
                - 0.5 * math.log(2.0 * math.pi * (s**2 + tau**2)))
    assert ln_z == pytest.approx(expected, abs=1e-9)


def test_laplace_refuses_non_pd_covariance():
    with pytest.raises(np.linalg.LinAlgError):
        E.laplace_ln_z(0.0, 0.0, np.array([[1.0, 2.0], [2.0, 1.0]]))  # indefinite


# ------------------------------------------------------------------ arena guard
def test_arena_mismatch_raises(clean):
    ctx, hyp, fit = clean
    rec = E.structure_evidence(hyp, ctx, fit, _bundle(hyp, ctx))
    es = E.EvidenceSet(arena_fingerprint=rec.arena_fingerprint)
    es.add(rec)                                           # same arena: fine
    assert es.by_label()[fit.label] is rec
    other = E.EvidenceSet(arena_fingerprint="isar-fp:something-else")
    with pytest.raises(E.ArenaMismatchError):
        other.add(rec)


def test_bundle_name_mismatch_is_a_construction_error(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    short = C.PriorBundle(bundle.params[:-1])             # dropped a free param
    with pytest.raises(ValueError, match="do not match"):
        E.structure_evidence(hyp, ctx, fit, short)


# ------------------------------------------------------------------ rung 1: Laplace at θ̂
def test_flat_bundle_stays_on_rung_one(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace"
    assert np.isfinite(rec.log_evidence)
    assert rec.log_evidence_err is None
    assert rec.k_free == fit.k
    assert rec.validity["unseen_informative"] == []
    assert rec.validity["boundary_pinned"] == []
    # exactly-once, numerically: lnZ is data cost + one bundle.logpdf, nothing else
    names = list(fit.params)
    free = [p.name for p in bundle.params]
    sigma = np.asarray(fit.parameter_covariance)[np.ix_(
        [names.index(n) for n in free], [names.index(n) for n in free])]
    assert rec.log_evidence == pytest.approx(
        E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma))


def test_transport_prior_counts_exactly_once():
    """A soft-constrained fit: the transported Gaussian IS the prior — already in the fit
    objective (θ̂ its MAP, HESSE its curvature), so no refit fires, and lnZ adds its normalized
    logpdf once on top of the DATA-only cost (never posterior_cost)."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "transport_test"})
    ctx = _spectrum_ctx(constraints=constraints)
    hyp, fit = _fitted(ctx)
    assert fit.fit_kind == "soft_prior" and fit.prior_penalty is not None
    bundle = _bundle(hyp, ctx)
    (gamma_prior,) = [p for p in bundle.params if p.name == "phonon.Gamma"]
    assert gamma_prior.origin == "transport"
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace"               # transport never triggers the refit
    names = list(fit.params)
    free = [p.name for p in bundle.params]
    sigma = np.asarray(fit.parameter_covariance)[np.ix_(
        [names.index(n) for n in free], [names.index(n) for n in free])]
    expected = E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma)
    assert rec.log_evidence == pytest.approx(expected)
    wrong_double_count = E.laplace_ln_z(fit.posterior_cost, bundle.logpdf(fit.params), sigma)
    assert rec.log_evidence != pytest.approx(wrong_double_count)


# ------------------------------------------------------------------ rung 2: MAP refit
def _narrow_entry(fit, param_suffix="Gamma", sigma=1e-3):
    """A catalogue entry declaring a narrow identity prior the fit never saw, centred at the
    fitted value so the warm MAP refit converges immediately."""
    name = next(n for n in fit.params if n.endswith("." + param_suffix))
    return types.SimpleNamespace(
        name="test-entry",
        parameter_priors=(C.ParamPrior(param=param_suffix, kind="gaussian",
                                       mu=max(float(fit.params[name]), 1e-3), sigma=sigma),))


def test_unseen_informative_prior_triggers_map_refit(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx, entry=_narrow_entry(fit))
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace_map"
    assert np.isfinite(rec.log_evidence)
    assert rec.validity["unseen_informative"] == ["phonon.Gamma"]
    assert rec.validity["map_refit"]["success"] is True


def test_map_refit_agrees_with_dynesty(clean):
    """The escalated lane and the analytic lane answer the same integral: on a well-behaved
    scan laplace_map ≈ dynesty within sampler error (coarse budget ⇒ coarse tolerance)."""
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx, entry=_narrow_entry(fit, sigma=5e-2))
    lap = E.structure_evidence(hyp, ctx, fit, bundle)
    assert lap.evidence_method == "laplace_map"
    pinned = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.A": True}))
    dyn = E.structure_evidence(hyp, ctx, pinned, bundle, **FAST)
    assert dyn.evidence_method == "dynesty"
    assert abs(lap.log_evidence - dyn.log_evidence) < max(2.0, 5.0 * dyn.log_evidence_err)


# ------------------------------------------------------------------ rung 3: dynesty escalation
def test_boundary_pin_escalates_to_dynesty(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    pinned = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.Omega_d": True}))
    rec = E.structure_evidence(hyp, ctx, pinned, bundle, **FAST)
    assert rec.validity["escalation"] == "laplace_invalid"
    assert rec.validity["boundary_pinned"] == ["phonon.Omega_d"]
    assert rec.evidence_method == "dynesty"
    assert np.isfinite(rec.log_evidence)
    assert rec.log_evidence_err is not None and rec.log_evidence_err > 0.0


def test_missing_at_limit_is_fail_safe(clean):
    """A fit persisted before the at_limit field exists is UNKNOWN, not clean: it escalates."""
    ctx, hyp, fit = clean
    old = replace(fit, at_limit=None)
    rec = E.structure_evidence(hyp, ctx, old, _bundle(hyp, ctx), **FAST)
    assert rec.evidence_method == "dynesty"


def test_nonconverged_fit_yields_no_evidence(clean):
    ctx, hyp, fit = clean
    bad = replace(fit, optimizer_converged=False)
    rec = E.structure_evidence(hyp, ctx, bad, _bundle(hyp, ctx))
    assert rec.evidence_method == "invalid"
    assert math.isnan(rec.log_evidence)
    assert rec.validity["reason"] == "fit did not converge"


# ------------------------------------------------------------------ proper-prior guard
def test_resolve_priors_refuses_non_finite_bounds(clean):
    ctx, hyp, _fit = clean
    prob = loop.build_problem(hyp, ctx)
    bad = list(prob.bounds)
    bad[-1] = (float("-inf"), float("inf"))
    with pytest.raises(ValueError, match="non-finite optimizer bounds"):
        C.resolve_priors(prob.composite, ctx, bad)


def test_qe_candidate_gets_finite_laplace_evidence():
    """Regression for the unbounded quasielastic amplitude: a qe-bearing candidate must get
    finite lnZ on rung 1, not a silent −inf that drops it from the posterior (found on the
    first real IN8 scan: the best-cost model lost with no flag)."""
    x = np.linspace(-10.0, 10.0, 81)
    lorentz = 40.0 / (1.0 + (x / 1.5) ** 2)
    counts = np.random.default_rng(5).poisson(lorentz + 30.0).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero", quasielastic_fwhm=3.0)
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    label = next(l for l in hyps if "qe" in l)
    prob = loop.build_problem(hyps[label], ctx)
    qe_lo, qe_hi = next(b for n, b in zip(prob.free_names, prob.bounds)
                        if n.endswith(".QE"))
    assert np.isfinite(qe_lo) and np.isfinite(qe_hi)      # the root cause, pinned directly
    fit = loop.fit_hypothesis(hyps[label], ctx, minos=False)
    assert fit.optimizer_converged
    rec = E.structure_evidence(hyps[label], ctx, fit, _bundle(hyps[label], ctx))
    assert np.isfinite(rec.log_evidence), rec.validity
    assert rec.evidence_method in ("laplace", "laplace_map", "dynesty")


# ------------------------------------------------------------------ dominance pruning
def _set_triples(ctx, results):
    from isar.analyze.phonon import enumerate_phonon_structures
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    triples = []
    for r in results:
        if not r.optimizer_converged:
            continue
        prob = loop.build_problem(hyps[r.label], ctx)
        bundle = C.resolve_priors(prob.composite, ctx, prob.bounds,
                                  exclude=[prob.free_names[i] for i in prob.fixed_pos])
        triples.append((hyps[r.label], r, bundle))
    return triples


def test_dominated_candidate_never_pays_for_dynesty(clean, monkeypatch):
    """On a decisive phonon scan the railed elastic+bg null's ceiling (−cost/2) sits
    thousands of nats under the leader: it must be recorded dominated WITHOUT sampling."""
    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)

    def _no_sampling(*a, **k):
        raise AssertionError("dynesty must not run for a dominated candidate")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    es = E.candidate_set_evidence(_set_triples(ctx, results), ctx)
    recs = es.by_label()
    dom = recs["elastic+bg"]
    assert dom.evidence_method == "dominated"
    assert math.isnan(dom.log_evidence)
    bound = dom.validity["log_evidence_upper_bound"]
    leader = dom.validity["dominated_by_leader_log_evidence"]
    assert bound < leader - E.PRUNE_MARGIN                # the certificate is quantitative
    assert recs["elastic+stokes+bg"].evidence_method == "laplace"


def test_failed_sampler_gate_yields_no_usable_evidence(clean, monkeypatch):
    """A dynesty run that fails its own §5.4 gate returns a finite logz — and must NOT be
    usable evidence. Finiteness cannot separate the failures (max_iter, low ESS, logzerr blow-up,
    a railed Omega_d all leave logz finite), so the gate is convergence, not finiteness.
    Otherwise a sampler the module already distrusts for pruning decides the production
    posterior, since select_models admits every finite lnZ."""
    ctx, hyp, fit = clean
    failed = types.SimpleNamespace(converged=False, fail_reason="ess=12 below 100",
                                   ess=12.0, log_evidence=-123.5, log_evidence_err=7.0)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: failed)
    rec = E.structure_evidence(hyp, ctx, replace(fit, at_limit=None), _bundle(hyp, ctx), **FAST)

    assert rec.evidence_method == "invalid"
    assert math.isnan(rec.log_evidence)
    # The number is kept for diagnosis, with the sampler's own reason — refused, not discarded.
    assert rec.validity["dynesty"]["converged"] is False
    assert rec.validity["dynesty"]["fail_reason"] == "ess=12 below 100"
    assert rec.validity["dynesty"]["log_evidence_untrusted"] == -123.5


def test_penalised_fit_is_never_dominance_pruned(monkeypatch):
    """−cost/2 is a ceiling only when θ̂ is the data MLE. For a soft-constrained fit θ̂ is the
    MAP, so the bound UNDERstates lnZ and pruning on it could drop a competitive candidate
    unsampled. A penalised fit must therefore ignore the deferral and sample."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "transport_test"})
    ctx = _spectrum_ctx(constraints=constraints)
    hyp, fit = _fitted(ctx)
    assert fit.prior_penalty is not None                  # the precondition the guard reads
    sampled = types.SimpleNamespace(converged=True, fail_reason=None, ess=500.0,
                                    log_evidence=-42.0, log_evidence_err=0.3)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: sampled)

    rec = E.structure_evidence(hyp, ctx, replace(fit, at_limit=None), _bundle(hyp, ctx),
                               escalation="defer", **FAST)

    assert rec.evidence_method == "dynesty"               # sampled despite escalation="defer"
    assert "log_evidence_upper_bound" not in rec.validity


def test_genuine_contest_still_pays_for_dynesty():
    """On a blank (bg-only) scan a railed phonon candidate fits AS WELL as the winner —
    its ceiling is near the leader, so pruning must NOT fire and dynesty must run: the
    ambiguous scans are exactly where the sampler earns its cost."""
    x = np.linspace(-10.0, 10.0, 81)
    counts = np.random.default_rng(11).poisson(np.full_like(x, 60.0)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    results = loop.fit_admissible_structures(ctx, errors=False)
    es = E.candidate_set_evidence(_set_triples(ctx, results), ctx, **FAST)
    methods = {label: r.evidence_method for label, r in es.by_label().items()}
    escalated = [m for m in methods.values() if m == "dynesty"]
    assert escalated, f"expected at least one close-contest escalation, got {methods}"
    assert "dominated" not in set(methods.values()) or all(
        r.validity["log_evidence_upper_bound"]
        < r.validity["dominated_by_leader_log_evidence"] - E.PRUNE_MARGIN
        for r in es.records if r.evidence_method == "dominated")


def test_escalation_n_live_reads_the_environment_and_refuses_nonsense(monkeypatch):
    """The cost knob exists so a SPAWNED pool child samples where the parent does — a module
    constant cannot cross that boundary, an environment variable can. An explicit ``n_live``
    still wins, and a malformed value raises instead of silently reverting: a run whose sampling
    resolution is unknowable after the fact is worse than a run that refused to start."""
    monkeypatch.delenv(E.ESCALATION_N_LIVE_ENV, raising=False)
    assert E.escalation_n_live() == E.ESCALATION_N_LIVE

    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "32")
    assert E.escalation_n_live() == 32

    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "not-a-number")
    with pytest.raises(ValueError):
        E.escalation_n_live()
    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "0")
    with pytest.raises(ValueError):
        E.escalation_n_live()


def _blank_scan_ctx():
    """The blank (bg-only) scan of :func:`test_genuine_contest_still_pays_for_dynesty`: a railed
    phonon candidate fits as well as the winner, so its ceiling sits near the leader and the
    escalation is genuinely contested — the only case where the budget can bite."""
    x = np.linspace(-10.0, 10.0, 81)
    counts = np.random.default_rng(11).poisson(np.full_like(x, 60.0)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_the_suite_cap_still_produces_sampling_that_counts():
    """The cap must make escalations CHEAPER, not fake. A dynesty run that fails its own §5.4
    convergence gate is fast and worthless — it yields no usable evidence, so a cap set too low
    would quietly convert every escalation into invalid evidence and push selection onto the
    recorded-AIC lane while the suite still went green. This runs the genuine-contest scan at
    whatever n_live the environment actually imposes (the repo-root conftest's cap in a suite
    run, ESCALATION_N_LIVE otherwise) and demands a converged sampler."""
    ctx = _blank_scan_ctx()
    results = loop.fit_admissible_structures(ctx, errors=False)
    es = E.candidate_set_evidence(_set_triples(ctx, results), ctx)   # no explicit n_live

    sampled = [r for r in es.records if r.evidence_method == "dynesty"]
    assert sampled, "expected a contested escalation to reach the sampler"
    for r in sampled:
        assert r.validity["dynesty"]["converged"], (
            f"{r.label} sampled at n_live={r.validity['dynesty'].get('n_live')} but failed its "
            f"convergence gate ({r.validity['dynesty'].get('fail_reason')}) — the cap is too low "
            f"and is buying speed by discarding evidence")
        assert np.isfinite(r.log_evidence)


def test_exhausted_budget_records_unresolved_and_never_samples(monkeypatch):
    """Budget-exhausted honesty: a contested candidate the budget skipped is RECORDED with its
    ceiling, not dropped and not given a number. NaN lnZ keeps it out of the posterior, and
    unresolved_contenders() reports it so selection can refuse to rank on an incomplete set."""
    ctx = _blank_scan_ctx()
    results = loop.fit_admissible_structures(ctx, errors=False)

    def _no_sampling(*a, **k):
        raise AssertionError("dynesty must not run once the escalation budget is exhausted")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    es = E.candidate_set_evidence(_set_triples(ctx, results), ctx, budget_s=0.0, **FAST)

    unresolved = [r for r in es.records if r.evidence_method == "unresolved"]
    assert unresolved, f"expected a budget-skipped candidate, got {[r.evidence_method for r in es.records]}"
    for r in unresolved:
        assert math.isnan(r.log_evidence)                     # never a number it did not earn
        assert math.isfinite(r.validity["log_evidence_upper_bound"])   # ceiling kept
        assert r.validity["escalation_budget_s"] == 0.0
    assert set(es.unresolved_contenders()) == {r.label for r in unresolved}


def test_budget_never_costs_a_certain_dominance_ruling(clean, monkeypatch):
    """The budget must not convert a FREE certain ruling into an unresolved one. Dominance is
    decided from an already-computed ceiling at no sampler cost, so it is checked first: on a
    decisive scan the railed null stays "dominated" (ruled out) under a zero budget, and the
    set still names its winner on evidence."""
    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sampling")))
    es = E.candidate_set_evidence(_set_triples(ctx, results), ctx, budget_s=0.0)

    assert es.by_label()["elastic+bg"].evidence_method == "dominated"
    assert es.unresolved_contenders() == []                   # nothing left in doubt


def test_a_ruled_out_budget_skip_is_not_a_contender():
    """Symmetry check on the predicate itself: an unresolved candidate whose own ceiling sits
    BELOW the best trusted evidence is already ruled out by that bound, so the winner stands
    and the set stays resolvable. Only a ceiling that could still overtake the leader degrades
    selection — otherwise the backstop would punish every set it ever touched."""
    def _rec(label, lnz, method, bound=None):
        validity = {} if bound is None else {"log_evidence_upper_bound": bound}
        return E.EvidenceRecord(label=label, log_evidence=lnz, log_evidence_err=None,
                                evidence_method=method, k_free=3, arena_fingerprint="arena",
                                prior_spec=(), validity=validity)

    es = E.EvidenceSet(arena_fingerprint="arena")
    es.add(_rec("stokes+bg", -10.0, "laplace"))
    es.add(_rec("ruled_out", float("nan"), "unresolved", bound=-40.0))
    assert es.unresolved_contenders() == []

    es.add(_rec("could_have_won", float("nan"), "unresolved", bound=-5.0))
    assert es.unresolved_contenders() == ["could_have_won"]


def test_unresolved_contender_degrades_selection_to_recorded_aic(clean):
    """The consumer half: an incomplete comparison must not be ranked as if it were complete.
    select_models records ``evidence_budget_exhausted`` and falls back to the AIC winner —
    the lane EIG eligibility already fails closed on — instead of publishing a posterior
    computed over whichever candidates happened to get sampled."""
    from isar.analyze.select import select_models

    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)
    triples = _set_triples(ctx, results)
    es = E.candidate_set_evidence(triples, ctx)
    resolved = select_models(results, ctx=None, evidence=es, stamp=False)
    assert resolved.selection_fallback is None                # precondition: the set was fine

    best_lnz = max(r.log_evidence for r in es.records if math.isfinite(r.log_evidence))
    es.records.append(E.EvidenceRecord(
        label="stokes2+bg", log_evidence=float("nan"), log_evidence_err=None,
        evidence_method="unresolved", k_free=7, arena_fingerprint=es.arena_fingerprint,
        prior_spec=(), validity={"log_evidence_upper_bound": best_lnz + 1.0,
                                 "escalation_budget_s": 600.0}))
    degraded = select_models(results, ctx=None, evidence=es, stamp=False)

    assert degraded.selection_fallback == "evidence_budget_exhausted"
    assert degraded.best == min(degraded.scores, key=lambda s: s.aic).label


def test_all_railed_set_escalates_highest_ceiling_first(clean, monkeypatch):
    """No trusted leader exists when every candidate railed: the highest-ceiling candidate
    is sampled first, becomes the leader, and prunes the rest. Pinned with stubbed records
    so only the ordering/leader policy is under test."""
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    strong = replace(fit, label="strong", cost=100.0)
    weak = replace(fit, label="weak", cost=700.0)          # ceiling 300 nats below strong's
    ran = []

    def _stub(h, c, f, b, *, escalation="run", **k):
        if escalation == "defer":
            return E.EvidenceRecord(
                label=f.label, log_evidence=float("nan"), log_evidence_err=None,
                evidence_method="deferred_escalation", k_free=f.k,
                arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
                validity={"log_evidence_upper_bound": -0.5 * f.cost})
        ran.append(f.label)
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k,
            arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
            validity={"dynesty": {"converged": True}})

    monkeypatch.setattr(E, "structure_evidence", _stub)
    es = E.candidate_set_evidence([(hyp, strong, bundle), (hyp, weak, bundle)], ctx)
    assert ran == ["strong"]                               # exactly one sampler run
    assert es.by_label()["strong"].evidence_method == "dynesty"
    assert es.by_label()["weak"].evidence_method == "dominated"


def test_candidate_set_escalations_use_the_scoped_n_live_cut(clean, monkeypatch):
    """Owner ruling 2026-08-08: post-pruning escalations sample at ESCALATION_N_LIVE=128;
    the direct-call default and the bayes diagnostics tier (D4) stay at 400. An explicit
    caller n_live wins over the cut (setdefault, not override).

    The environment is cleared first, so this pins the PRODUCTION lane: the repo-root conftest
    caps the suite at ``ISAR_ESCALATION_N_LIVE=32`` for cost, and without the delenv this test
    would silently be asserting the test-suite cap rather than what ships."""
    import inspect

    ctx, hyp, fit = clean
    monkeypatch.delenv(E.ESCALATION_N_LIVE_ENV, raising=False)
    bundle = _bundle(hyp, ctx)
    original = E.structure_evidence
    seen = []

    def _stub(h, c, f, b, *, escalation="run", **k):
        if escalation == "defer":
            return E.EvidenceRecord(
                label=f.label, log_evidence=float("nan"), log_evidence_err=None,
                evidence_method="deferred_escalation", k_free=f.k,
                arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
                validity={"log_evidence_upper_bound": -0.5 * f.cost})
        seen.append(k.get("n_live"))
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k,
            arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
            validity={"dynesty": {"converged": True, "n_live": k.get("n_live")}})

    monkeypatch.setattr(E, "structure_evidence", _stub)
    es = E.candidate_set_evidence([(hyp, fit, bundle)], ctx)
    assert seen == [E.ESCALATION_N_LIVE] and E.ESCALATION_N_LIVE == 128
    # the record carries the cut honestly (rides validity into the /2 semantics record)
    assert es.by_label()[fit.label].validity["dynesty"]["n_live"] == 128

    seen.clear()
    E.candidate_set_evidence([(hyp, fit, bundle)], ctx, n_live=80)
    assert seen == [80]                                   # explicit caller n_live wins

    # The cap must actually REACH the sampler — that is the whole point of routing it through the
    # environment rather than a patched constant, since a spawned pool child re-imports the module.
    seen.clear()
    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "32")
    es_capped = E.candidate_set_evidence([(hyp, fit, bundle)], ctx)
    assert seen == [32]
    assert es_capped.by_label()[fit.label].validity["dynesty"]["n_live"] == 32

    # the two-lane split: direct calls and the bayes diagnostics tier keep D4's 400
    assert inspect.signature(original).parameters["n_live"].default == 400
    assert BZ.DEFAULT_NLIVE == 400


# ------------------------------------------------------------------ generalized bayes lane
def test_prior_bundle_ppf_drives_the_sampler(clean):
    """The bundle enters via the prior transform: a near-delta prior on Ω_d pins the sampled
    column at its μ regardless of the likelihood's preference."""
    ctx, hyp, fit = clean
    prob = loop.build_problem(hyp, ctx)
    target = 3.0
    params = tuple(
        C.ResolvedPrior(p.name, "gaussian", target, 1e-4, p.lo, p.hi, "identity")
        if p.name == "phonon.Omega_d" else p
        for p in _bundle(hyp, ctx).params)
    bz = BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=C.PriorBundle(params), **FAST)
    od_i = bz.parameter_names.index("phonon.Omega_d")
    draws = bz.parameter_samples[:, od_i]
    assert np.all(np.abs(draws - target) < 1e-2)
    assert bz.prior_used is False                         # the ω₀ diagnostics tier is untouched
    del prob


def test_prior_and_prior_bundle_are_mutually_exclusive(clean):
    ctx, hyp, fit = clean
    from isar.analyze.prior import PhononPrior
    with pytest.raises(ValueError, match="mutually exclusive"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior=PhononPrior(omega0=5.0, omega0_sigma=0.5),
                              prior_bundle=_bundle(hyp, ctx), **FAST)


def test_prior_bundle_refuses_hard_fixed_candidates():
    """The sampler works full-space; a frozen coordinate must never become an integrated
    dimension. Explicitly closed, whether or not the bundle happens to cover the frozen name."""
    ctx = _spectrum_ctx(constraints=ParamConstraints(hard={"phonon.Gamma": 0.4},
                                                     origins={"phonon.Gamma": "test"}))
    hyp, fit = _fitted(ctx)
    prob = loop.build_problem(hyp, ctx)
    full = C.resolve_priors(prob.composite, ctx, prob.bounds)   # covers the frozen param too
    with pytest.raises(ValueError, match="hard-fixed"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=full, **FAST)


def test_prior_bundle_must_cover_every_sampled_parameter(clean):
    ctx, hyp, fit = clean
    bundle = C.PriorBundle(_bundle(hyp, ctx).params[:-1])
    with pytest.raises(ValueError, match="no prior for sampled parameter"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=bundle, **FAST)


# ------------------------------------------------------------------ at_limit persistence
def test_fit_persists_per_param_at_limit(clean):
    _ctx, _hyp, fit = clean
    assert isinstance(fit.at_limit, dict)
    assert set(fit.at_limit) == set(fit.params)           # real dotted names, every param
    assert all(isinstance(v, bool) for v in fit.at_limit.values())

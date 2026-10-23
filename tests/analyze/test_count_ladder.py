"""Rung 2 of the count ladder and the per-spectrum state (design 2026-10-22, §3.1): each escalation
trigger, the contending set, nested sampling of a hard model with its refit published, and every
state with its reason. Small synthetic spectra and small ceilings; the state logic is driven with
stub rung-2 results so only the sampling tests pay for the sampler."""
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import count_ladder as CL
from isar.analyze import loop, select
from isar.analyze.backends import BackendResult
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho

pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor",
                                        "ignore:The sampling was stopped short")

X = np.linspace(-8.0, 16.0, 61)


def _ctx(lam, seed=3):
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=X, counts=counts, mask=np.ones_like(X, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _dho(omega0, gamma, amp):
    return convolute_voigt_dho(X, omega0, 0.0, 1.0, 0.0, gamma, amp, 0.0, 0.0, 50.0,
                               peak="stokes+astokes")


def _rung1(ctx, ceiling):
    out, hyps = {}, {}
    results = loop.fit_structures_two_stage(ctx, mode_ceiling=ceiling, rung1_out=out,
                                            hypotheses_out=hyps)
    record = out["rung1"]
    record.apply_selection(select.select_models(results, hyps, ctx, stamp=False))
    return record


@pytest.fixture(scope="module")
def one_peak():
    ctx = _ctx(_dho(5.0, 0.4, 500.0) + 8.0)
    return ctx, _rung1(ctx, 2)


@pytest.fixture(scope="module")
def doublet():
    ctx = _ctx(_dho(5.0, 0.3, 400.0) + _dho(5.8, 0.3, 200.0) + 8.0)
    return ctx, _rung1(ctx, 2)


def _with_evidence_only(record):
    """The record without the models rung 1 could not price: the control for laplace_invalid."""
    return replace(record, models=[m for m in record.models if CL._has_evidence(m)])


def _unpriced(record, *labels):
    """The record with ``labels`` left without rung-1 evidence, as a failed Laplace calculation
    leaves a model (on these clean spectra every distinct model is priced)."""
    return replace(record, models=[
        replace(m, log_evidence=float("nan"), evidence_method="deferred_escalation")
        if m.label in labels else m for m in record.models])


def _names(fired):
    return {t.name for t in fired}


# --- the contending set --------------------------------------------------------------------------
def test_contending_keeps_every_model_without_evidence_and_the_count_above(one_peak):
    """A model rung 1 could not price contends however far its -cost/2 'ceiling' sits below the
    best: that bound holds only at a global optimum, so it never prunes a count decision."""
    ctx, record = one_peak
    unpriced = _unpriced(record, "bg")
    by_label = unpriced.by_label()
    best = CL._best(unpriced.models)
    assert best.label == "stokes+bg"
    null = by_label["bg"]
    assert not CL._has_evidence(null)
    assert -0.5 * null.representative.cost < best.log_evidence - 1000.0    # the old prune's case
    contenders = CL.contending(unpriced)
    assert contenders[0] == "stokes+bg"
    assert "bg" in contenders
    assert "stokes2+bg" in contenders                                     # the count one above
    assert "bg" not in CL.contending(record)                             # priced, far below
    assert all(m.label in contenders for m in unpriced.models
               if not CL._has_evidence(m) and m.resolved_to is None)


def test_a_model_resolved_to_its_twin_is_not_a_contender_without_evidence(one_peak):
    """elastic+bg rails its elastic height at 0, so it is bg (the degenerate-limit rule): with the
    twin unpriced, the twin contends and the resolved model neither contends nor fires the
    missing-evidence trigger on its own."""
    ctx, record = one_peak
    assert record.by_label()["elastic+bg"].resolved_to == "bg"
    both = _unpriced(record, "elastic+bg", "bg")
    contenders = CL.contending(both)
    assert "bg" in contenders and "elastic+bg" not in contenders
    alone = _unpriced(record, "elastic+bg")
    assert "elastic+bg" not in CL.contending(alone)
    assert "laplace_invalid" not in _names(CL.triggers(alone, ctx))


# --- the triggers ---------------------------------------------------------------------------------
def test_laplace_invalid_triggers_and_its_control(one_peak):
    ctx, record = one_peak
    fired = CL.triggers(_unpriced(record, "bg"), ctx)
    assert "laplace_invalid" in _names(fired)
    assert "bg" in next(t.reason for t in fired if t.name == "laplace_invalid")
    assert "laplace_invalid" not in _names(CL.triggers(record, ctx))


def test_an_unconverged_contender_triggers(one_peak):
    ctx, record = one_peak
    clean = _with_evidence_only(record)
    best = CL._best(clean.models)
    starts = list(best.starts)
    i = best.representative_start
    starts[i] = replace(starts[i], result=replace(starts[i].result, optimizer_converged=False))
    stuck = replace(clean, models=[replace(m, starts=starts) if m is best else m
                                   for m in clean.models])
    assert "laplace_invalid" in _names(CL.triggers(stuck, ctx, CL.contending(clean)))


def test_a_clean_spectrum_below_the_ceiling_fires_nothing(one_peak):
    ctx, record = one_peak
    assert CL.triggers(_with_evidence_only(record), ctx) == []


def test_at_ceiling_triggers_only_at_the_ceiling(doublet):
    ctx, record = doublet
    assert CL._best(record.models).count == 2
    assert "at_ceiling" in _names(CL.triggers(record, ctx))
    assert "at_ceiling" not in _names(CL.triggers(replace(record, ceiling=3), ctx))


def _per_start(monkeypatch, values):
    """Pin each start's Laplace log-evidence: ``values`` maps (label, source) to lnZ, others None."""
    def fake(model, ctx):
        return [values.get((model.label, s.source)) for s in model.starts]
    monkeypatch.setattr(CL, "_start_log_evidence", fake)


def test_split_trigger_is_the_split_margin(doublet, monkeypatch):
    """A split of the best model's parent within SPLIT_MARGIN of the parent contends; further off
    it does not."""
    ctx, record = doublet
    clean = replace(_with_evidence_only(record), ceiling=3)
    parent = clean.by_label()["stokes+bg"]
    parent_ln_z = parent.log_evidence
    _per_start(monkeypatch, {("stokes2+bg", "split"): parent_ln_z - 0.9 * CL.SPLIT_MARGIN})
    assert "split_competitive" in _names(CL.triggers(clean, ctx))
    _per_start(monkeypatch, {("stokes2+bg", "split"): parent_ln_z - 1.1 * CL.SPLIT_MARGIN})
    assert "split_competitive" not in _names(CL.triggers(clean, ctx))


def test_start_families_that_disagree_on_the_count_trigger(doublet, monkeypatch):
    ctx, record = doublet
    clean = replace(_with_evidence_only(record), ceiling=3)
    top = CL._best(clean.models).log_evidence
    near = {("stokes2+bg", "split"): top, ("stokes+bg", "descriptor"): top - 0.5 * CL.START_DISAGREEMENT_MARGIN}
    _per_start(monkeypatch, near)
    fired = CL.triggers(clean, ctx)
    assert "starts_disagree" in _names(fired)
    far = {("stokes2+bg", "split"): top, ("stokes+bg", "descriptor"): top - 2.0 * CL.START_DISAGREEMENT_MARGIN}
    _per_start(monkeypatch, far)
    assert "starts_disagree" not in _names(CL.triggers(clean, ctx))


def _best_with(record, **changes):
    """The record with its best model's representative changed (``changes`` on the fit result)."""
    best = CL._best(record.models)
    starts = list(best.starts)
    i = best.representative_start
    starts[i] = replace(starts[i], result=replace(starts[i].result, **changes))
    return replace(record, models=[replace(m, starts=starts) if m is best else m
                                   for m in record.models])


def test_merged_modes_trigger_below_the_fraction_of_the_resolution_width(doublet):
    ctx, record = doublet
    clean = replace(_with_evidence_only(record), ceiling=3)
    fit = CL._best(clean.models).representative
    width = CL.resolution_fwhm(ctx)
    for gap, fires in ((0.9 * CL.MERGED_FWHM_FRACTION, True), (1.1 * CL.MERGED_FWHM_FRACTION, False)):
        modes = [dict(fit.modes[0], omega0=5.0), dict(fit.modes[1], omega0=5.0 + gap * width)]
        fired = _names(CL.triggers(_best_with(clean, modes=modes), ctx))
        assert ("modes_merged" in fired) is fires


def test_amplitude_at_zero_or_within_one_error_triggers(doublet):
    ctx, record = doublet
    clean = replace(_with_evidence_only(record), ceiling=3)
    fit = CL._best(clean.models).representative
    assert "amplitude_zero" not in _names(CL.triggers(clean, ctx))
    weak = dict(fit.params, **{"phonon2.A": 2.0})
    errs = dict(fit.param_errors, **{"phonon2.A": 3.0})
    assert "amplitude_zero" in _names(CL.triggers(_best_with(clean, params=weak, param_errors=errs), ctx))
    railed = dict(fit.at_limit, **{"phonon2.A": True})
    assert "amplitude_zero" in _names(CL.triggers(_best_with(clean, at_limit=railed), ctx))


def test_structured_residual_triggers_and_flags_the_central_region():
    """One mode cannot carry a broad central component: the runs test finds the structure, and the
    flag says the quasi-elastic component was not tested, never that it is absent."""
    broad = 900.0 / (1.0 + (X / 1.5) ** 2)
    ctx = _ctx(_dho(8.0, 0.4, 500.0) + broad + 8.0)
    record = _with_evidence_only(_rung1(ctx, 1))
    fired = CL.triggers(record, ctx)
    assert "structured_residual" in _names(fired)
    best = CL._best(record.models)
    assert CL.FLAG_CENTRAL in CL._flags(best, best.representative, ctx)
    clean_ctx = _ctx(_dho(5.0, 0.4, 500.0) + 8.0)
    clean = _with_evidence_only(_rung1(clean_ctx, 1))
    good = CL._best(clean.models)
    assert CL._flags(good, good.representative, clean_ctx) == []


# --- the states -----------------------------------------------------------------------------------
def _stub(record, label, ln_z, *, reason=None):
    m = record.by_label()[label]
    return CL.Rung2Model(label=label, count=m.count, log_evidence=ln_z, log_evidence_err=0.5,
                         method="nested" if ln_z is not None else "failed", reason=reason,
                         result=m.representative, result_source="rung1", seconds=0.1)


def _escalated(record):
    return CL.Rung2Plan(triggers=[CL.Trigger("laplace_invalid", "test")],
                        contending=CL.contending(record))


def test_no_trigger_is_settled_on_rung_one(one_peak):
    ctx, record = one_peak
    clean = _with_evidence_only(record)
    res = CL.conclude(clean, ctx, CL.plan(clean, ctx), {})
    assert res.state == CL.SETTLED and res.shown == "rung1"
    assert res.best_label == "stokes+bg" and res.ambiguity is None
    assert res.reasons == ["no escalation trigger fired; rung 1 decides"]


def test_rung_two_running_is_provisional_and_never_shown(one_peak):
    ctx, record = one_peak
    plan = _escalated(record)
    first = plan.contending[0]
    res = CL.conclude(record, ctx, plan, {first: _stub(record, first, -50.0)}, elapsed_s=1.0,
                      budget_s=100.0)
    assert res.state == CL.PROVISIONAL and res.shown == "rung1"
    assert res.shown_result is None
    assert "1 of" in res.reasons[0]


def test_a_finished_rung_two_settles_and_is_shown(one_peak):
    ctx, record = one_peak
    plan = _escalated(record)
    sampled = {label: _stub(record, label, -50.0 if label == "stokes+bg" else -70.0)
               for label in plan.contending}
    res = CL.conclude(record, ctx, plan, sampled, elapsed_s=5.0, budget_s=100.0)
    assert res.state == CL.SETTLED and res.shown == "rung2" and res.best_label == "stokes+bg"
    assert res.shown_result is sampled["stokes+bg"].result
    assert res.count_probabilities[1] == pytest.approx(1.0)
    assert res.ambiguity is None


def test_the_budget_limits_the_search_and_keeps_rung_one_shown(one_peak, monkeypatch):
    """Out of budget with contenders left: search limited, the boundary named, the partial rung 2
    never shown. The budget comes from the environment when not passed."""
    ctx, record = one_peak
    monkeypatch.setenv(CL.RUNG2_BUDGET_ENV, "0")
    res = CL.run(_unpriced(record, "bg"), ctx)                 # nothing can start at a zero budget
    assert res.state == CL.SEARCH_LIMITED and res.shown == "rung1" and res.rung2 == {}
    assert "rung-2 budget of 0 s" in res.reasons[0]
    monkeypatch.setenv(CL.RUNG2_BUDGET_ENV, "soon")
    with pytest.raises(ValueError, match=CL.RUNG2_BUDGET_ENV):
        CL.rung2_budget_s()


def test_the_ceiling_limits_the_search(doublet):
    ctx, record = doublet
    plan = _escalated(record)
    sampled = {label: _stub(record, label, -70.0 if label == "stokes2+bg" else -90.0)
               for label in plan.contending}
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.SEARCH_LIMITED and res.shown == "rung1"
    assert any("mode ceiling 2" in r for r in res.reasons)


def test_a_failed_calculation_leaves_the_spectrum_unresolved_not_lower(one_peak):
    """A contending model whose evidence could not be computed makes the spectrum unresolved; it
    is never scored as a low evidence and the rung-1 result stays shown. Unresolved outranks a
    search limit, and both reasons are kept."""
    ctx, record = one_peak
    plan = _escalated(record)
    sampled = {label: _stub(record, label, -50.0) for label in plan.contending}
    sampled["stokes2+bg"] = _stub(record, "stokes2+bg", None, reason="logz not finite")
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.UNRESOLVED and res.shown == "rung1"
    assert res.best_label == "stokes+bg"
    assert sampled["stokes2+bg"].probability is None
    assert "stokes2+bg could not be computed (logz not finite)" in res.reasons[0]
    capped = CL.conclude(replace(record, ceiling=1), ctx, plan, sampled, budget_s=100.0)
    assert capped.state == CL.UNRESOLVED
    assert any("mode ceiling 1" in r for r in capped.reasons)


def test_close_counts_carry_the_ambiguity_marker(one_peak):
    ctx, record = one_peak
    plan = _escalated(record)
    sampled = {label: _stub(record, label, -200.0) for label in plan.contending}
    sampled["stokes+bg"] = _stub(record, "stokes+bg", -50.0)
    sampled["stokes2+bg"] = _stub(record, "stokes2+bg", -50.0 + np.log(0.3 / 0.7))
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.SETTLED
    amb = res.ambiguity
    assert (amb["count"], amb["runner_up_count"]) == (1, 2)
    assert amb["probability"] == pytest.approx(0.7) and amb["runner_up_probability"] == pytest.approx(0.3)


def test_held_names_its_reason():
    res = CL.held("no energy zero")
    assert res.state == CL.HELD and res.reasons == ["no energy zero"] and res.shown == "none"


# --- rung 2 sampling ------------------------------------------------------------------------------
def test_an_unconverged_model_is_still_sampled_in_rung_two(one_peak):
    ctx, record = one_peak
    model = record.by_label()["stokes+bg"]
    starts = [replace(s, result=replace(s.result, optimizer_converged=False)) for s in model.starts]
    stuck = replace(model, starts=starts)
    assert not stuck.converged
    out = CL.sample_model(stuck, ctx, n_live=16)
    assert out.method == "nested" and np.isfinite(out.log_evidence)


def test_the_published_fit_is_the_refit_from_the_nested_maximum_likelihood_sample(one_peak):
    """A representative stuck at a poor point: rung 2's refit from the sampler's best sample is
    what is published, not the optimizer's old answer."""
    ctx, record = one_peak
    model = record.by_label()["stokes+bg"]
    prob = loop.build_problem(model.hypothesis, ctx)
    names = prob.composite.param_names()
    theta = np.array([{"phonon.Omega_d": 11.0, "phonon.Gamma": 0.4}.get(n, model.representative.params[n])
                      for n in names])
    br = BackendResult(theta, True, "stub", 1, None, False, {}, None)
    poor = loop.result_from_backend(model.label, prob.composite, prob.costv, prob.n_used, prob.k, br)
    stuck = replace(model, starts=[replace(model.starts[0], result=poor)], representative_start=0)
    out = CL.sample_model(stuck, ctx, n_live=16)
    assert out.result_source == "nested_refit"
    assert out.result.cost < poor.cost - 100.0
    assert abs(out.result.params["phonon.Omega_d"] - 5.0) < 0.5


def test_rung_two_samples_at_its_own_live_points_unless_the_environment_caps_them(monkeypatch):
    """Count decisions need more live points than the old escalation lane's 32 (measured: 30-40
    nats off on two- and three-mode models); the suite's environment cap still wins."""
    monkeypatch.delenv("ISAR_ESCALATION_N_LIVE", raising=False)
    assert CL.rung2_n_live() == CL.RUNG2_N_LIVE == 128
    monkeypatch.setenv("ISAR_ESCALATION_N_LIVE", "16")
    assert CL.rung2_n_live() == 16

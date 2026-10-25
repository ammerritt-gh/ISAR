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
               if not CL._has_evidence(m) and m.label not in CL._resolved(unpriced.models))


def test_the_count_above_contends_with_and_without_the_elastic_line(one_peak):
    """The count one above the best is sampled in both elastic variants: the variant the best
    model does not use may be the one whose local rung-1 fit was poor, and nested sampling is what
    would find the mode it missed. Neither is near the best here, so only the rule puts them in."""
    ctx, record = one_peak
    best = CL._best(record.models)
    assert (best.label, best.count, best.elastic) == ("stokes+bg", 1, False)
    other = record.by_label()["elastic+stokes2+bg"]
    assert not (CL._has_evidence(other) and other.log_evidence >= best.log_evidence - CL.CONTENDING_MARGIN)
    contenders = CL.contending(record)
    assert "stokes2+bg" in contenders and "elastic+stokes2+bg" in contenders
    assert not [m for m in record.models if m.label in contenders and m.count > best.count + 1]


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


def test_a_railed_model_whose_twin_did_not_converge_is_a_model_of_its_own(one_peak):
    """Selection prices a railed model as itself when its twin did not converge (select.resolved_twins),
    so the ladder must read 'resolved' the same way: the railed model can be the best and it contends.
    Its evidence is set directly here, as a selection would give it."""
    ctx, record = one_peak
    rail = record.by_label()["elastic+bg"]
    assert rail.resolved_to == "bg"
    bg = record.by_label()["bg"]
    stuck_bg = replace(bg, starts=[replace(s, result=replace(s.result, optimizer_converged=False))
                                   for s in bg.starts])
    lead = CL._best(record.models).log_evidence + 1.0
    alone = replace(record, models=[replace(m, log_evidence=lead) if m is rail
                                    else stuck_bg if m is bg else m for m in record.models])
    assert CL._best(alone.models).label == "elastic+bg"
    assert "elastic+bg" in CL.contending(alone)


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


def test_a_contender_that_cannot_be_sampled_leaves_the_spectrum_unresolved(one_peak, monkeypatch):
    """Rung 2 could not start for a contender that rung 1 did price: a failed calculation, never a
    comparison made on its rung-1 Laplace value. The spectrum is unresolved, rung 1 stays shown."""
    from isar.analyze.errors import bayes

    ctx, record = one_peak

    def refuse(*args, **kwargs):
        raise ValueError("the sampler cannot start (test)")

    monkeypatch.setattr(bayes, "bayesian_posterior", refuse)
    model = record.by_label()["stokes2+bg"]
    assert CL._has_evidence(model)                                       # rung 1 priced it
    out = CL.sample_model(model, ctx, n_live=16)
    assert out.log_evidence is None and out.method == "failed"
    assert "cannot start" in out.reason
    plan = _escalated(record)
    sampled = {label: _stub(record, label, -50.0) for label in plan.contending}
    sampled["stokes2+bg"] = out
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.UNRESOLVED and res.shown == "rung1"
    assert any("stokes2+bg could not be computed" in r for r in res.reasons)


def test_a_failed_contender_is_unresolved_at_once_even_with_others_pending(one_peak):
    """A failed calculation is unresolved as soon as it happens, never provisional while the other
    contenders wait; with every sampled model fine and some pending, the state is provisional."""
    ctx, record = one_peak
    plan = _escalated(record)
    first = plan.contending[0]
    failed = {first: _stub(record, first, None, reason="logz not finite")}
    res = CL.conclude(record, ctx, plan, failed, elapsed_s=1.0, budget_s=100.0)
    assert res.state == CL.UNRESOLVED and res.shown == "rung1"
    assert any(f"{first} could not be computed" in r for r in res.reasons)
    fine = CL.conclude(record, ctx, plan, {first: _stub(record, first, -50.0)}, elapsed_s=1.0,
                       budget_s=100.0)
    assert fine.state == CL.PROVISIONAL
    assert fine.reasons == [f"{CL.RUNG2_PENDING}: 1 of {len(plan.contending)} contending models "
                            f"sampled"]


def _escalating(monkeypatch, outcome):
    """``CL.run`` with a trigger forced and ``sample_model`` replaced by ``outcome(model)``;
    returns the labels sampled, in order."""
    calls = []
    monkeypatch.setattr(CL, "triggers", lambda record, ctx, contenders=None: [
        CL.Trigger("forced", "test")])

    def sample(model, ctx, **kw):
        calls.append(model.label)
        return outcome(model)

    monkeypatch.setattr(CL, "sample_model", sample)
    return calls


def test_the_loop_stops_at_a_failed_model_and_a_raising_sampler_is_a_failure(one_peak,
                                                                             monkeypatch):
    """The one rung-2 loop: a sampler that raises is a failed calculation, and no further contender
    is sampled for that spectrum, whose state is unresolved."""
    ctx, record = one_peak

    def boom(model):
        raise RuntimeError("sampler exploded (test)")

    calls = _escalating(monkeypatch, boom)
    res = CL.run(record, ctx, budget_s=100.0)
    assert len(res.contending) >= 2 and calls == res.contending[:1]
    assert res.state == CL.UNRESOLVED and res.shown == "rung1"
    assert res.rung2[calls[0]].method == "failed"
    assert "RuntimeError: sampler exploded" in res.rung2[calls[0]].reason


def test_the_loop_resumes_without_resampling_and_honours_the_slice(one_peak, monkeypatch):
    """A resumed model is kept, never sampled again; a zero slice samples nothing and leaves the
    spectrum provisional, and any positive slice samples at least one model."""
    import time

    ctx, record = one_peak
    calls = _escalating(monkeypatch, lambda model: time.sleep(0.05) or _stub(record, model.label,
                                                                              -50.0))
    contending = CL.contending(record)
    late = CL.run(record, ctx, slice_s=0.0, budget_s=100.0)
    assert calls == [] and late.state == CL.PROVISIONAL
    short = CL.run(record, ctx, slice_s=0.01, budget_s=100.0)        # shorter than one model
    assert calls == contending[:1] and short.state == CL.PROVISIONAL
    calls.clear()
    kept = {contending[0]: _stub(record, contending[0], -50.0)}
    done = CL.run(record, ctx, resume=kept, budget_s=100.0)
    assert calls == contending[1:] and set(done.rung2) == set(contending)
    assert done.rung2[contending[0]] is kept[contending[0]]


def test_a_rung_two_winner_without_a_converged_fit_is_unresolved(one_peak):
    """Rung 2 is shown only with a converged fit of its winner; without one, rung 1 stays shown and
    the state is unresolved. A loser without a converged fit leaves the count settled."""
    ctx, record = one_peak
    plan = _escalated(record)

    def unconverged(stub):
        return replace(stub, result=replace(stub.result, optimizer_converged=False))

    def sampled_with(winner_ok, loser_ok):
        out = {label: _stub(record, label, -90.0) for label in plan.contending}
        out["stokes+bg"] = _stub(record, "stokes+bg", -50.0)
        out["stokes2+bg"] = _stub(record, "stokes2+bg", -70.0)
        if not winner_ok:
            out["stokes+bg"] = unconverged(out["stokes+bg"])
        if not loser_ok:
            out["stokes2+bg"] = unconverged(out["stokes2+bg"])
        return out

    bad = CL.conclude(record, ctx, plan, sampled_with(False, True), budget_s=100.0)
    assert bad.state == CL.UNRESOLVED and bad.shown == "rung1"
    assert "rung-2 winner stokes+bg has no converged fit" in bad.reasons
    fine = CL.conclude(record, ctx, plan, sampled_with(True, False), budget_s=100.0)
    assert fine.state == CL.SETTLED and fine.shown == "rung2" and fine.best_label == "stokes+bg"


# --- the count is the most probable count, its variants summed -----------------------------------
def test_the_most_probable_count_sums_its_variants():
    """The reviewer's case: two zero-mode variants at 0.30 each outweigh a one-mode model at 0.40,
    so the count is 0 and the shown model the more probable zero-mode variant."""
    probability = {"bg": 0.30, "elastic+bg": 0.30, "stokes+bg": 0.40}
    count = {"bg": 0, "elastic+bg": 0, "stokes+bg": 1}
    assert select.most_probable_count(probability, count)[0] == 0
    assert select.most_probable_count({**probability, "bg": 0.31, "elastic+bg": 0.29}, count) \
        == (0, "bg")
    assert select.most_probable_count({"bg": 0.2, "elastic+bg": 0.2, "stokes+bg": 0.6}, count) \
        == (1, "stokes+bg")


def test_rung_two_decides_the_count_by_its_summed_probability(one_peak):
    ctx, record = one_peak
    plan = CL.Rung2Plan(triggers=[CL.Trigger("forced", "test")],
                        contending=["bg", "elastic+bg", "stokes+bg"])
    sampled = {"bg": _stub(record, "bg", np.log(0.30)),
               "elastic+bg": _stub(record, "elastic+bg", np.log(0.30)),
               "stokes+bg": _stub(record, "stokes+bg", np.log(0.40))}
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.SETTLED and res.shown == "rung2"
    assert res.best_label in ("bg", "elastic+bg")
    assert res.count_probabilities[0] == pytest.approx(0.60)
    amb = res.ambiguity
    assert (amb["count"], amb["runner_up_count"]) == (0, 1)
    assert amb["probability"] == pytest.approx(0.60)


def test_a_count_that_turns_on_an_unconverged_contender_is_unresolved(one_peak):
    """Without the unconverged zero-mode variant the published selection would pick one mode, so
    the count cannot be shown settled at 0."""
    ctx, record = one_peak
    plan = CL.Rung2Plan(triggers=[CL.Trigger("forced", "test")],
                        contending=["bg", "elastic+bg", "stokes+bg"])
    stuck = _stub(record, "elastic+bg", np.log(0.29))
    sampled = {"bg": _stub(record, "bg", np.log(0.31)),
               "elastic+bg": replace(stuck, result=replace(stuck.result, optimizer_converged=False)),
               "stokes+bg": _stub(record, "stokes+bg", np.log(0.40))}
    res = CL.conclude(record, ctx, plan, sampled, budget_s=100.0)
    assert res.state == CL.UNRESOLVED and res.shown == "rung1"
    assert any("turns on elastic+bg" in r for r in res.reasons)


def _priced(record, ln_p):
    """The record with the given models' rung-1 evidence and probability set (label -> ln P), every
    other model far below."""
    out = []
    for m in record.models:
        lnz = ln_p.get(m.label, -1e4)
        out.append(replace(m, log_evidence=lnz, probability=float(np.exp(lnz))))
    return replace(record, models=out)


def test_rung_one_shows_the_most_probable_count(one_peak):
    """Rung 1 decides the same way: two one-mode variants at 0.30 each outweigh a two-mode model
    at 0.40, and the count above the decided count is the one that contends."""
    ctx, record = one_peak
    priced = _priced(record, {"stokes+bg": np.log(0.30), "elastic+stokes+bg": np.log(0.30),
                              "stokes2+bg": np.log(0.40)})
    res = CL.conclude(priced, ctx, CL.Rung2Plan(triggers=[], contending=[]), {})
    assert res.state == CL.SETTLED and res.shown == "rung1"
    assert res.best_label in ("stokes+bg", "elastic+stokes+bg")
    assert (res.ambiguity["count"], res.ambiguity["runner_up_count"]) == (1, 2)
    assert "stokes2+bg" in CL.contending(priced)


def test_selection_picks_the_most_probable_count(one_peak):
    """select_models' winner, which the delivery shows and exports, follows the same rule."""
    from isar.analyze import evidence

    _ctx_, record = one_peak
    results = [m.representative for m in record.models
               if m.label in ("stokes+bg", "elastic+stokes+bg", "stokes2+bg")]
    ev = evidence.EvidenceSet("arena")
    for r, p in zip(results, (0.30, 0.30, 0.40)):
        ev.add(evidence.EvidenceRecord(label=r.label, log_evidence=float(np.log(p)),
                                       log_evidence_err=0.1, evidence_method="dynesty",
                                       k_free=int(r.k), arena_fingerprint="arena",
                                       prior_spec=(), validity={}))
    sel = select.select_models(results, stamp=False, evidence=ev)
    assert {r.label: r.n_modes for r in results}[sel.best] == 1
    assert [s.label for s in sel.scores if s.label == sel.best] == sel.survivors


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


def test_a_converged_refit_beats_a_lower_cost_unconverged_representative(one_peak):
    """Rung 2 publishes the fit nested sampling recovered: a converged refit is never displaced by
    an unconverged rung-1 fit, however low its cost."""
    ctx, record = one_peak
    model = record.by_label()["stokes+bg"]
    low = replace(model.representative, optimizer_converged=False,
                  cost=model.representative.cost - 10.0, posterior_cost=None)
    stuck = replace(model, starts=[replace(model.starts[0], result=low)], representative_start=0)
    out = CL.sample_model(stuck, ctx, n_live=16)
    assert out.result_source == "nested_refit" and out.result.optimizer_converged


def test_the_published_fit_prefers_converged_then_the_lower_objective(one_peak):
    """Both directions of the rule: converged wins whichever side it is on; between two of the
    same status the lower objective wins."""
    _ctx_, record = one_peak
    rep = record.by_label()["stokes+bg"].representative
    lower_unconverged = replace(rep, optimizer_converged=False, cost=rep.cost - 5.0,
                                posterior_cost=None)
    higher = replace(rep, cost=rep.cost + 5.0, posterior_cost=None)
    assert CL.published_fit(rep, lower_unconverged) == (rep, "rung1")
    assert CL.published_fit(lower_unconverged, higher) == (higher, "nested_refit")
    lower = replace(rep, cost=rep.cost - 5.0, posterior_cost=None)
    assert CL.published_fit(higher, lower) == (lower, "nested_refit")
    assert CL.published_fit(lower, higher) == (lower, "rung1")


def test_rung_two_samples_at_its_own_live_points_unless_the_environment_caps_them(monkeypatch):
    """Count decisions need more live points than the old escalation lane's 32 (measured: 30-40
    nats off on two- and three-mode models); the suite's environment cap still wins."""
    monkeypatch.delenv("ISAR_ESCALATION_N_LIVE", raising=False)
    assert CL.rung2_n_live() == CL.RUNG2_N_LIVE == 128
    monkeypatch.setenv("ISAR_ESCALATION_N_LIVE", "16")
    assert CL.rung2_n_live() == 16

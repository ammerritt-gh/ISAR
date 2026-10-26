"""The deep pass samples the fast pass's posterior, not a neighbour of it (C-deep-inputs example 6).

On a real fast-pass record (a synthetic spectrum on the committed a06 resolution curve, ceiling 1)
every rebuilt model's sampled loglike equals the record's ``-cost/2`` at its best basin, and its
prior bundle equals the fast pass's ``SpectrumPrior`` at the same point, at recipe v5 and under a
configured prior (plan revision F7). Also: a mixed delta mask keeps ONE ordered energy group under
either width density, and ``bayesian_posterior`` is unchanged by routing through ``EvidenceProblem``
(tests/analyze/test_bayes.py and test_evidence.py are the unchanged pins).
"""
import math

import numpy as np
import pytest

from isar.analyze import deep_pass as dp
from isar.analyze import evidence, fast_pass, loop, prior
from isar.analyze.errors.bayes import EvidenceProblem
from isar.analyze.fast_pass import FastPassSettings
from isar.session import scan_delivery
from isar.synth import bl43_curves, count_bench


def _synthetic_ctx():
    truth = count_bench.CaseTruth(
        stream=0, index=0, kind="single", modes=(count_bench.ModeTruth(5.0, 0.4, 500.0),),
        elastic_height=300.0, background=8.0, T=50.0, exposure=1.0,
        gauss_fwhm=bl43_curves.curve().fwhm, lorentz_fwhm=0.0, ceiling=1)
    return count_bench.context_of(count_bench.draw_observation(truth, np.random.default_rng(3)))


def _point(record, label, names):
    """The record params of the label's best basin (lowest cost), in bundle order, and its cost."""
    basins = [m for m in record["models"] if m["label"] == label and m.get("cost") is not None]
    best = min(basins, key=lambda m: m["cost"])
    return [best["params"][n] for n in names], best["cost"]


def _check_parity(ctx, ps, width_flat):
    res = fast_pass.run_fast_pass(ctx, settings=FastPassSettings(mode_ceiling=1, prior=ps))
    record = scan_delivery._fast_pass_record(res)
    entries = dp.models_from_record(ctx, record, dp.plain_hypotheses(ctx, 1))
    sp = prior.resolve_prior_for(ctx, ps)
    assert {e.label for e in entries} == {m["label"] for m in record["models"]}
    n_checked = 0
    for e in entries:
        if e.fastpass_lnl_opt is None:
            continue
        bundle = e.problem.bundle
        names = [p.name for p in bundle.params]
        theta, cost = _point(record, e.label, names)
        loglike, _ptform, ndim = e.problem()
        assert ndim == len(names) == e.ndim
        assert abs(loglike(np.array(theta)) - (-cost / 2)) <= 1e-6 * max(1.0, cost), e.label
        # the same prior: bounds and the density at the point
        for p in bundle.params:
            kind = ("omega0" if p.name.endswith(".omega0") else "Gamma" if p.name.endswith(".Gamma")
                    else "elastic" if p.name.startswith("elastic") else "bg" if p.name.endswith(".BG")
                    else "h")
            if kind != "Gamma":
                assert (p.lo, p.hi) == pytest.approx(sp.bounds(kind)), (e.label, p.name)
        values = dict(zip(names, theta))
        full = e.problem.full_params(theta)
        peaks = []
        for pre in sorted({n.split(".")[0] for n in full if n.endswith(".omega0")},
                          key=lambda s: full[s + ".omega0"]):
            peaks.append((full[pre + ".omega0"], values.get(pre + ".Gamma"), full[pre + ".h"]))
        want = sp.ln_prior(peaks, full.get("elastic.h"), full.get("bg.BG"))
        got = bundle.logpdf_ordered(values)
        # a flat width is normalised on [lo, cap] by the bundle and [0, cap] by SpectrumPrior: the
        # same unrenormalised mass below the floor as the softlog width's, ln(cap / (cap - lo)) each
        extra = sum(math.log(sp.bounds("Gamma")[1] / (p.hi - p.lo)) for p in bundle.params
                    if p.origin == "default_flat_width")
        assert got - want == pytest.approx(extra, abs=1e-9), e.label
        assert extra < 1e-3
        assert (extra > 0) == (width_flat and any(p.name.endswith(".Gamma") for p in bundle.params))
        n_checked += 1
    assert n_checked >= 4        # bg, elastic+bg, stokes+bg, elastic+stokes+bg (delta twins add more)


def test_example6_likelihood_and_prior_parity_at_recipe_v5():
    _check_parity(_synthetic_ctx(), None, width_flat=False)


def test_example6_likelihood_and_prior_parity_under_a_configured_prior():
    ps = prior.PriorSettings(amplitude_margin=3.0, width_kind="flat")
    _check_parity(_synthetic_ctx(), ps, width_flat=True)


@pytest.mark.parametrize("ps", [None, prior.PriorSettings(width_kind="flat")])
def test_a_mixed_delta_mask_keeps_one_ordered_energy_group(ps):
    # elastic+stokes2+bg with mode 2 a delta: the finite mode's width prior must not split the modes
    # into two groups (a group of one is not ordered, so the k! and the transform would be lost)
    ctx = _synthetic_ctx()
    hyps = dp.plain_hypotheses(ctx, 2)
    plain = hyps["elastic+stokes2+bg"]
    twin = loop.delta_twin_hypothesis(plain, ["phonon2"], ctx)
    for hyp in (plain, twin):
        bundle = evidence.candidate_bundle(hyp, ctx, prior_settings=ps)
        names = [p.name for p in bundle.params]
        assert bundle._energy_groups() == [[names.index("phonon.omega0"), names.index("phonon2.omega0")]]


def test_the_problem_refuses_a_bundle_that_does_not_match():
    ctx = _synthetic_ctx()
    hyps = dp.plain_hypotheses(ctx, 1)
    bundle = evidence.candidate_bundle(hyps["stokes+bg"], ctx)
    with pytest.raises(ValueError, match="no prior for sampled parameter"):
        EvidenceProblem(hyps["elastic+stokes+bg"], ctx, bundle).setup()
    hyp = loop.delta_twin_hypothesis(hyps["stokes+bg"], ["phonon"], ctx)
    with pytest.raises(ValueError, match="hard-fixed"):
        EvidenceProblem(hyp, loop.candidate_context(hyp, ctx), bundle).setup()


def test_full_params_embeds_the_pinned_width():
    ctx = _synthetic_ctx()
    hyp = loop.delta_twin_hypothesis(dp.plain_hypotheses(ctx, 1)["stokes+bg"], ["phonon"], ctx)
    cctx = loop.candidate_context(hyp, ctx)
    problem = EvidenceProblem(hyp, cctx, evidence.candidate_bundle(hyp, ctx))
    assert [p.name for p in problem.bundle.params] == ["phonon.omega0", "phonon.h", "bg.BG"]
    full = problem.full_params([5.0, 100.0, 8.0])
    assert full == {"phonon.omega0": 5.0, "phonon.Gamma": loop.GAMMA_FLOOR, "phonon.h": 100.0,
                    "bg.BG": 8.0}

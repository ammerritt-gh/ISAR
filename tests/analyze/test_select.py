"""Step-2 model selection (§4) under evidence (SCENE-1 PR-b, isar.model_selection/2).

Selection is driven by the posterior over structures P(s) ∝ π_s·Z_s (lnZ from the evidence
ladder); the information criteria stay as the AIC-labeled diagnostics lane (D6/H4), still
computed by ``score_models`` from the Baker-Cousins -2lnL: AIC = cost + 2k exactly. The
nested "F-test" against a Poisson likelihood remains the likelihood-ratio diagnostic:
cost_reduced - cost_full ~ chi2(dk). Selection without evidence inputs is the RECORDED AIC
fallback, never a silent one.
"""
import math

import numpy as np
import pytest

from isar.analyze import loop, select
from isar.analyze.context import ScanContext
from isar.analyze.evidence import EvidenceRecord, EvidenceSet
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures

pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor")


def _result(label, k, ndof, cost, *, params=None, within_model=None, converged=True):
    """A minimal StructureFitResult carrying just what selection reads (label/k/ndof/cost/
    params/converged); the rest are filled with innocuous values."""
    params = params if params is not None else {f"{label}.p{i}": 0.0 for i in range(k)}
    return loop.StructureFitResult(
        label=label, k=k, ndof=ndof, cost=cost,
        cost_per_dof=cost / ndof if ndof > 0 else float("nan"),
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=True, analysis_usable=converged,
        params=params, param_errors={n: None for n in params},
        gamma=0.4, gamma_err=0.05, omega0=5.0, omega0_err_hesse=0.05,
        message="ok", within_model=within_model,
    )


def _by_label(results, **kw):
    return {s.label: s for s in select.score_models(results, **kw)}


def _toy_evidence(lnz_by_label, arena="toy-arena"):
    es = EvidenceSet(arena_fingerprint=arena)
    for label, lnz in lnz_by_label.items():
        es.add(EvidenceRecord(
            label=label, log_evidence=float(lnz), log_evidence_err=None,
            evidence_method="laplace" if np.isfinite(lnz) else "invalid",
            k_free=3, arena_fingerprint=arena, prior_spec=(), validity={}))
    return es


# --------------------------------------------------------- diagnostics lane (D6: unchanged)
def test_aic_bic_aicc_from_baker_cousins_cost():
    r = _result("m", k=5, ndof=19, cost=90.0)          # n = 24
    sc = _by_label([r])["m"]
    assert sc.n == 24
    assert sc.cost == 90.0
    assert sc.aic == 90.0 + 2 * 5                       # cost + 2k = 100
    assert sc.bic == 90.0 + 5 * math.log(24)
    assert sc.aicc == 100.0 + 2 * 5 * 6 / (24 - 5 - 1)  # aic + 2k(k+1)/(n-k-1)


def test_akaike_weights_sum_to_one_and_track_aic():
    a = _result("a", k=4, ndof=20, cost=100.0)         # aic 108
    b = _result("b", k=5, ndof=19, cost=90.0)          # aic 100 (better)
    scores = _by_label([a, b])
    assert abs(sum(s.akaike_weight for s in scores.values()) - 1.0) < 1e-12
    assert scores["b"].akaike_weight > scores["a"].akaike_weight
    assert scores["b"].delta_aic == 0.0
    assert scores["a"].delta_aic == 8.0
    # the diagnostics scorer leaves the slot on the Akaike weight (shadow lane contract)
    assert all(s.weight == s.akaike_weight for s in scores.values())


def test_nested_lrt_reports_for_subset_and_skips_non_nested():
    reduced = _result("stokes+bg", k=4, ndof=20, cost=120.0,
                      params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0, "bg.BG": 0})
    full = _result("elastic+stokes+bg", k=5, ndof=19, cost=90.0,
                   params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0,
                           "elastic.E": 0, "bg.BG": 0})
    scores = _by_label([reduced, full])
    nt = scores["elastic+stokes+bg"].nested
    assert nt is not None
    assert nt["reduced"] == "stokes+bg"
    assert nt["dof"] == 1
    assert nt["delta_deviance"] == 30.0                # cost_reduced - cost_full
    assert 0.0 <= nt["p_value"] <= 1.0
    assert nt["p_value"] < 1e-3                        # the extra elastic param is warranted
    assert scores["stokes+bg"].nested is None          # nothing is nested inside the reduced model


def test_non_converged_results_excluded_from_selection():
    good = _result("good", k=5, ndof=19, cost=90.0)
    bad = _result("bad", k=4, ndof=20, cost=10.0, converged=False)  # spuriously low cost
    assert {s.label for s in select.score_models([good, bad])} == {"good"}


def test_all_non_converged_yields_empty_scores():
    bad = _result("bad", k=5, ndof=19, cost=10.0, converged=False)
    assert select.score_models([bad]) == []


def test_nested_lrt_pairs_one_mode_subset_two_mode():
    """The 1-mode stokes+bg params are a strict subset of the 2-mode stokes2+bg params, so the
    unchanged nested LRT pairs them (mode 1 keeps the ``phonon`` prefix — that is what makes
    the subset relation hold)."""
    one = _result("stokes+bg", k=4, ndof=20, cost=140.0,
                  params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0, "bg.BG": 0})
    two = _result("stokes2+bg", k=7, ndof=17, cost=95.0,
                  params={"phonon.Omega_d": 0, "phonon.Gamma": 0, "phonon.A": 0,
                          "phonon2.Omega_d": 0, "phonon2.Gamma": 0, "phonon2.A": 0, "bg.BG": 0})
    scores = _by_label([one, two])
    nt = scores["stokes2+bg"].nested
    assert nt is not None and nt["reduced"] == "stokes+bg"
    assert nt["dof"] == 3                                 # a whole extra phonon (Ω_d, Γ, A)
    assert nt["delta_deviance"] == 45.0
    assert scores["stokes+bg"].nested is None


# --------------------------------------------------------- evidence posterior (D3)
def test_supplied_evidence_from_another_arena_is_refused():
    """A reused EvidenceSet is internally arena-consistent by construction — that says nothing
    about whether it is THIS scan's. Applying another scan's lnZ by label is exactly the
    comparison EvidenceSet.add refuses, so the wholesale handoff must refuse it too."""
    from isar.analyze.evidence import ArenaMismatchError

    a = _result("a", k=4, ndof=20, cost=100.0)
    foreign = _toy_evidence({"a": -10.0}, arena="a-different-scan")

    with pytest.raises(ArenaMismatchError, match="cross-arena"):
        select.select_models([a], {}, _one_peak_ctx(), evidence=foreign, stamp=False)


def test_toy_evidence_without_a_context_stays_usable():
    """The guard is conditional on ctx for a reason: hand-built diagnostics pass an evidence set
    with no context, and must keep working rather than being refused by a check they cannot
    satisfy."""
    a = _result("a", k=4, ndof=20, cost=100.0)
    sel = select.select_models([a], evidence=_toy_evidence({"a": -10.0}), stamp=False)
    assert sel.best == "a" and sel.selection_fallback is None


def test_posterior_from_toy_evidence_matches_softmax():
    a = _result("a", k=4, ndof=20, cost=100.0)
    b = _result("b", k=5, ndof=19, cost=90.0)             # AIC prefers b…
    es = _toy_evidence({"a": -10.0, "b": -12.0})          # …evidence prefers a
    sel = select.select_models([a, b], evidence=es, stamp=False)
    pa = 1.0 / (1.0 + math.exp(-2.0))                     # softmax of lnZ, uniform prior
    scores = {s.label: s for s in sel.scores}
    assert sel.selection_fallback is None
    assert sel.best == "a"                                # argmax posterior, NOT min AIC
    assert scores["a"].posterior_probability == pytest.approx(pa, abs=1e-12)
    assert scores["b"].posterior_probability == pytest.approx(1.0 - pa, abs=1e-12)
    # the weight slot IS the posterior; the Akaike weight survives as the diagnostic
    assert all(s.weight == s.posterior_probability for s in sel.scores)
    assert abs(sum(s.akaike_weight for s in sel.scores) - 1.0) < 1e-12
    assert all(s.candidate_prior == 0.5 for s in sel.scores)
    assert all(s.evidence_method == "laplace" for s in sel.scores)


def test_invalid_evidence_candidate_gets_zero_posterior():
    a = _result("a", k=4, ndof=20, cost=100.0)
    b = _result("b", k=5, ndof=19, cost=90.0)
    bad = _result("bad", k=4, ndof=20, cost=80.0)          # best AIC of the three…
    es = _toy_evidence({"a": -10.0, "b": -12.0, "bad": float("nan")})
    sel = select.select_models([a, b, bad], evidence=es, stamp=False)
    scores = {s.label: s for s in sel.scores}
    assert sel.best == "a"                                 # …but excluded from the posterior
    assert scores["bad"].posterior_probability == 0.0
    assert scores["bad"].candidate_prior == 0.0
    assert scores["bad"].survives is False
    assert scores["a"].candidate_prior == 0.5              # π uniform over the ADMISSIBLE two
    assert abs(sum(s.weight for s in sel.scores) - 1.0) < 1e-12


def test_survivor_floor_applies_to_posterior():
    win = _result("win", k=5, ndof=19, cost=80.0)
    junk = _result("junk", k=4, ndof=20, cost=112.0)
    es = _toy_evidence({"win": 0.0, "junk": -20.0})        # posterior(junk) ~ 2e-9
    sel = select.select_models([win, junk], evidence=es, stamp=False, weight_floor=0.01)
    scores = {s.label: s for s in sel.scores}
    assert scores["win"].survives is True
    assert scores["junk"].survives is False
    assert sel.survivors == ["win"]


def test_no_evidence_inputs_is_the_recorded_aic_fallback():
    a = _result("a", k=4, ndof=20, cost=100.0)
    b = _result("b", k=5, ndof=19, cost=90.0)
    sel = select.select_models([a, b], stamp=False)        # no hyps/ctx/evidence supplied
    assert sel.selection_fallback == "aic_no_evidence_inputs"
    assert sel.best == "b"                                 # min AIC
    scores = {s.label: s for s in sel.scores}
    assert all(s.posterior_probability is None for s in sel.scores)
    assert all(s.weight == s.akaike_weight for s in sel.scores)
    assert scores["b"].survives is True


def test_all_invalid_evidence_is_the_recorded_aic_fallback():
    a = _result("a", k=4, ndof=20, cost=100.0)
    b = _result("b", k=5, ndof=19, cost=90.0)
    es = _toy_evidence({"a": float("nan"), "b": float("nan")})
    sel = select.select_models([a, b], evidence=es, stamp=False)
    assert sel.selection_fallback == "aic_all_evidence_invalid"
    assert sel.best == "b"
    assert all(s.weight == s.akaike_weight for s in sel.scores)


def test_missing_hypothesis_for_scored_label_raises():
    ctx = _spectrum_ctx()
    results = loop.fit_admissible_structures(ctx, errors=False)
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    victim = next(iter(r.label for r in results if r.optimizer_converged))
    del hyps[victim]
    with pytest.raises(ValueError, match="no hypothesis supplied"):
        select.select_models(results, hyps, ctx, stamp=False)


# --------------------------------------------------------- end-to-end under evidence
def _hyps(ctx):
    by_hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}
    by_hyp.update({h.label: h for h in enumerate_phonon_multimode(ctx)})
    return by_hyp


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121, seed=7):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = np.random.default_rng(seed).poisson(np.clip(p1 + p2 + bg, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _one_peak_ctx(o1=5.0, a1=500.0, g=0.4, bg=8.0, n=81, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    lam = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes") + bg
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _spectrum_ctx(seed=0, omega0=5.0, n=81):
    x = np.linspace(-10.0, 10.0, n)
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_two_stage_select_accepts_second_mode_on_true_double_peak():
    """End-to-end: a wide two-peak spectrum → the two-stage loop offers 2-mode, the evidence
    posterior selects it, and the PRIMARY (largest-amplitude, acoustic-like ~4 meV) ω₀
    interval brackets the truth."""
    ctx = _two_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, with_errors=True, M=40, seed=1)
    sel = select.select_models(results, _hyps(ctx), ctx)
    assert sel.selection_fallback is None
    assert sel.best == "stokes2+bg"
    win = {r.label: r for r in results}["stokes2+bg"]
    assert win.n_modes == 2
    assert abs(win.omega0 - 4.0) < 0.6                    # primary = the stronger (a1>a2) mode
    lo, med, hi = win.within_model["freq_interval"]
    assert lo <= win.omega0 <= hi                         # interval brackets the fitted primary
    assert abs(med - 4.0) < 0.3                           # …sits on the true acoustic peak (~4 meV)
    assert hi < 7.0                                        # …cleanly separated from the 10 meV mode


def test_two_stage_select_rejects_second_mode_on_true_single_peak():
    """When ≥2 peaks are declared the 2-mode set is fit even on a true single peak, but the
    evidence posterior rejects the extra mode: the 1-mode stokes+bg wins (the Occam factor
    does what ΔAIC=2k did, from priors instead of a count)."""
    ctx = _one_peak_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]
    results = loop.fit_structures_two_stage(ctx, with_errors=False, M=40, seed=1, peaks=peaks)
    assert "stokes2+bg" in {r.label for r in results}     # it WAS offered
    sel = select.select_models(results, _hyps(ctx), ctx)
    assert sel.selection_fallback is None
    assert sel.best == "stokes+bg"                        # …and rejected on evidence


def test_select_end_to_end_real_spectrum_folds():
    """Wiring: fit + bootstrap the admissible structures, then select under evidence. The
    elastic+stokes+bg model wins, evidence provenance is populated per candidate, the lone
    survivor's bootstrap is unimodal -> fold, and the scan decision is stamped onto
    across_model."""
    ctx = _spectrum_ctx()
    results = loop.fit_admissible_structures(ctx, errors=True, M=40, seed=1)
    sel = select.select_models(results, _hyps(ctx), ctx)
    assert sel.selection_fallback is None
    assert sel.best == "elastic+stokes+bg"
    assert "elastic+stokes+bg" in sel.survivors
    assert sel.modality == "fold"
    lo, med, hi = sel.pooled.interval
    assert lo <= 5.0 <= hi
    scores = {s.label: s for s in sel.scores}
    assert all(s.evidence_method in ("laplace", "laplace_map", "dynesty", "dominated",
                                     "invalid")
               for s in sel.scores)
    # the decisive scan's railed null is pruned by its evidence ceiling, not sampled
    assert scores["elastic+bg"].evidence_method == "dominated"
    assert scores["elastic+bg"].posterior_probability == 0.0
    assert scores["elastic+stokes+bg"].posterior_probability > 0.5
    win = {r.label: r for r in results}["elastic+stokes+bg"]
    assert win.across_model["scan"]["modality"] == "fold"
    assert win.across_model["scan"]["selection_fallback"] is None
    assert win.across_model["score"]["survives"] is True
    assert win.across_model["score"]["weight"] == \
        scores["elastic+stokes+bg"].posterior_probability
    assert win.across_model["score"]["akaike_weight"] > 0.99
    # evidence is attached to the Selection for reuse (prelim/final pair of one fit)
    assert sel.evidence is not None
    assert sel.evidence.by_label().keys() >= {"elastic+stokes+bg"}

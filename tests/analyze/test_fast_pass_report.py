"""The fast pass's reporting half (plan step 10 and 13): peak values, the post-search thresholds in
``_classify``, ``reclassify`` and ``reclassify_record``.

The numbers come from the engine's own point on small synthetic spectra (Voigt resolution, FWHM
1.5 meV); the threshold logic is driven on plain dicts, as ``reclassify_record`` sees it. Every test
names its ceiling: the repo conftest's ``ISAR_MODE_CEILING`` is never read here.
"""
import json
import math
from dataclasses import asdict, replace

import numpy as np
import pytest
from scipy.optimize import least_squares
from scipy.special import gammaln, xlogy

from isar.analyze import catalogue, fast_pass
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings

FWHM = 1.5
X = np.linspace(-8.0, 20.0, 113)


def _ctx(counts, **kw):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float), mask=np.ones(X.size, bool), T=300.0,
                       center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute", **kw)


def _expected(modes, elastic=150.0, bg=4.0):
    ctx = _ctx(np.ones(X.size))
    label = "elastic+stokes{}+bg".format(len(modes) if len(modes) > 1 else "")
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == label)
    return hyp.build().evaluate(X, [v for m in modes for v in m] + [elastic, bg], ctx)


def _hand(counts, theta, cov_free=None, **kw):
    """(engine, basin) for the one-peak elastic+bg model, with ``theta`` as its posterior mode and
    ``cov_free`` its covariance: step 10 reads exactly these."""
    eng = fast_pass._Engine(_ctx(counts, **kw), FastPassSettings(mode_ceiling=1))
    model = eng.model(eng.variant(1, True, True), ())
    theta = np.asarray(theta, float)
    b = fast_pass._Basin(model, 0, theta, eng.cost(model, theta), 0.0, 0.0, "hand", status="ok",
                         theta_map=theta, ln_z=0.0, cov_free=cov_free)
    return eng, b


def _classified(eng, b, settings=None):
    peaks, extras = eng._peaks(b)
    fs = {"max_count": 100.0, "fwhm": eng.fwhm, "count_ln_z": {1: 0.0}, "unpriced_contenders": [],
          "evidence_spread_nats": 0.0, "alternatives": [], **extras}
    c = fast_pass._classify(settings or FastPassSettings(mode_ceiling=1), [asdict(p) for p in peaks],
                            fs, {1: 1.0}, 1, "")
    return tuple(replace(p, **u) for p, u in zip(peaks, c["peaks"])), c


# --------------------------------------------------------------------------- peak values
def test_sigma_area_agrees_with_refitting_poisson_draws():
    truth = np.array([8.0, 0.55, 400.0, 150.0, 4.0])
    lam = _expected([tuple(truth[:3])], elastic=150.0, bg=4.0)
    eng = fast_pass._Engine(_ctx(np.rint(lam)), FastPassSettings(mode_ceiling=1))
    b, _ = eng.fit(eng.variant(1, True, True), [(8.0, 0.55, 400.0, False)], "ladder")
    eng.price_all()
    peak = eng._peaks(b)[0][0]
    assert peak.area_status == "usable" and peak.sigma_area and peak.sigma_area_both

    model, free = b.model, b.model.free
    lo, hi = model.lo[free], model.hi[free]
    rng = np.random.default_rng(6420294)
    area, area_both = [], []
    for _ in range(200):
        draw = rng.poisson(lam).astype(float)

        def resid(z):
            t = truth.copy()
            t[free] = z
            return fast_pass.poisson_residuals(draw, eng.mean(model, t))

        fit = least_squares(resid, truth[free], bounds=(lo, hi), method="trf", x_scale=np.maximum(truth, 1.0),
                            max_nfev=80, ftol=1e-9, xtol=1e-9, gtol=1e-8)
        per_s, per_b = eng._area_per_h(model.variant.peaks[0], fit.x[0], fit.x[1])
        area.append(fit.x[2] * per_s)
        area_both.append(fit.x[2] * per_b)
    # The seed is fixed. Counting only the height's error (no shape gradient) gives a ratio of 1.18;
    # the full delta method gives 1.00 (the sd of a ratio of 200-draw sds is ~5%).
    assert peak.sigma_area / np.std(area, ddof=1) == pytest.approx(1.0, abs=0.1)
    assert peak.sigma_area_both / np.std(area_both, ddof=1) == pytest.approx(1.0, abs=0.1)


def test_deletion_impact_removes_the_peak_under_the_exposure_scale():
    rng = np.random.default_rng(1)
    expo = rng.uniform(0.6, 1.4, X.size)
    theta = np.array([8.0, 0.55, 300.0, 150.0, 4.0])
    cov = np.diag([0.01, 0.001, 25.0, 25.0, 0.1])
    eng, b = _hand(np.full(X.size, 30.0), theta, cov, exposure_scale=expo, exposure_reference=1.0e4,
                   exposure_source="monitor")
    peak = eng._peaks(b)[0][0]
    mean = eng.mean(b.model, theta)
    gone = theta.copy()
    gone[2] = 0.0                          # the same model with the peak's height zeroed
    reduced = eng.mean(b.model, gone)

    def ll(y, mu):
        return float(np.sum(xlogy(y, mu) - mu - gammaln(y + 1.0)))

    assert peak.deletion_impact == pytest.approx(2 * (ll(mean, mean) - ll(mean, reduced)), rel=1e-9)


def test_regime_is_recorded_apart_from_status():
    flat = np.full(X.size, 30.0)
    cov = np.diag([0.04, 0.04, 25.0, 25.0, 0.1])
    near, c = _classified(*_hand(flat, [0.8, 0.15, 300.0, 150.0, 4.0], cov))
    assert near[0].regime == "near_elastic" and near[0].manual_fit and near[0].status["omega0"] == "constrained"
    over, c = _classified(*_hand(flat, [4.0, 12.0, 100.0, 150.0, 4.0], cov))
    assert over[0].regime == "overdamped" and over[0].manual_fit
    assert over[0].status["omega0"] != "undetermined"          # a good covariance still measures it
    fine, c = _classified(*_hand(flat, [8.0, 0.5, 100.0, 150.0, 4.0], cov))
    assert fine[0].regime == "underdamped" and not fine[0].manual_fit


def test_a_weak_peak_is_not_confident_and_a_missing_covariance_is_undetermined():
    flat = np.full(X.size, 30.0)
    cov = np.diag([0.04, 0.04, 0.25, 25.0, 0.1])
    s = FastPassSettings(mode_ceiling=1, confident_anchor=((0.0, 50.0),), flag_residual_threshold=0.0)
    weak, _ = _classified(*_hand(flat, [8.0, 0.55, 2.0, 150.0, 4.0], cov), s)
    assert weak[0].deletion_impact < 50.0 and not weak[0].confident
    strong, _ = _classified(*_hand(flat, [8.0, 0.55, 300.0, 150.0, 4.0], cov), s)
    assert strong[0].deletion_impact > 50.0 and strong[0].confident
    none, c = _classified(*_hand(flat, [8.0, 0.55, 300.0, 150.0, 4.0], None))
    assert none[0].area_status == "unavailable" and none[0].sigma_area is None
    assert set(none[0].status.values()) == {"undetermined"}


# --------------------------------------------------------------------------- thresholds on plain data
def _fs(peaks, **kw):
    """flag_statistics for peaks given as (omega0, Gamma, deletion_impact, corr_omega0_gamma)."""
    out = {"max_count": 100.0, "fwhm": 1.5, "residual_scan": [], "residual_scan_max": 0.0,
           "height_correlations": [], "area_covariance": None, "alternatives": [],
           "count_ln_z": {len(peaks): 0.0}, "unpriced_contenders": [], "evidence_spread_nats": 0.0,
           "peaks": [dict(zip(("omega0", "Gamma", "deletion_impact", "corr_omega0_gamma"), p))
                     for p in peaks]}
    out.update(kw)
    return out


def _peaks(fs, area=10.0):
    return [{"height": 1.0, "area_stokes": area, "area_status": "usable",
             "status": {k: "constrained" for k in ("omega0", "Gamma", "height", "area")}}
            for _ in fs["peaks"]]


def _alt(omega0, p, gap=1.0, family="ladder"):
    return {"model_id": "m", "family": family, "probability": p, "evidence_gap": gap, "omega0": omega0}


def _run(fs, preferred=None, probs=None, reason="", **settings):
    n = len(fs["peaks"])
    s = FastPassSettings(mode_ceiling=6, **settings)
    return fast_pass._classify(s, _peaks(fs), fs, probs or {n: 1.0}, n if preferred is None else preferred,
                               reason)


def test_confident_needs_the_anchor():
    fs = _fs([(5.0, 0.4, 100.0, 0.1), (9.0, 0.4, 10.0, 0.1)])
    assert [p["confident"] for p in _run(fs, confident_anchor=((0.0, 50.0),))["peaks"]] == [True, False]
    # log-linear in the spectrum's max count between pairs, clamped at the ends
    pairs = ((100.0, 10.0), (10000.0, 1000.0))
    assert fast_pass._anchor_at(pairs, 1000.0) == pytest.approx(505.0)
    assert fast_pass._anchor_at(pairs, 1.0) == 10.0 and fast_pass._anchor_at(pairs, 1e9) == 1000.0


def test_a_peak_must_have_a_match_in_every_credible_alternative():
    peaks = [(5.0, 0.4, 100.0, 0.1), (9.0, 0.4, 100.0, 0.1)]
    only_first = [5.1]
    both = [5.1, 9.2]
    cases = [
        ([_alt(only_first, 0.30)], [True, False]),            # a material alternative without it
        ([_alt(only_first, 0.01)], [True, True]),             # below material_p: not credible
        ([_alt(only_first, 0.0, gap=None)], [True, False]),   # an unpriced contender always counts
        ([_alt(both, 0.30), _alt([4.9, 8.0], 0.30)], [True, True]),
        ([_alt([5.0, 5.8], 0.30)], [True, False]),            # one match each, never two peaks to one
    ]
    for alts, want in cases:
        got = [p["confident"] for p in _run(_fs(peaks, alternatives=alts))["peaks"]]
        assert got == want, (alts, got)


def test_a_flag_demotes_the_peak_a_neighbour_may_be_absorbing():
    scan = [[6.2, 7.8, 40.0], [7.0, 8.6, 5.0]]
    fs = _fs([(6.0, 0.4, 100.0, 0.1), (14.0, 0.4, 100.0, 0.1)], residual_scan=scan)
    off = _run(fs, flag_residual_threshold=0.0)                     # that flag kind is off
    assert off["flags"] == [] and [p["confident"] for p in off["peaks"]] == [True, True]
    on = _run(fs, flag_residual_threshold=25.0)
    assert [(f["kind"], f["e_lo"], f["e_hi"], f["statistic"]) for f in on["flags"]] == [
        ("structured_residual", 6.2, 7.8, 40.0)]
    assert [p["confident"] for p in on["peaks"]] == [False, True]


def test_correlated_heights_make_the_areas_conditional_and_keep_the_anticorrelation():
    cov = [[100.0, -95.0], [-95.0, 100.0]]
    fs = _fs([(8.0, 0.5, 100.0, 0.1), (8.8, 0.6, 100.0, 0.1)], height_correlations=[[0, 1, -0.95]],
             area_covariance=cov)
    out = _run(fs)
    assert [p["area_status"] for p in out["peaks"]] == ["decomposition_conditional"] * 2
    assert all(p["status"]["height"] == p["status"]["area"] == "model_dependent" and not p["confident"]
               for p in out["peaks"])
    assert [(f["kind"], f["statistic"]) for f in out["flags"]] == [("ambiguous_decomposition", None)]
    assert (out["flags"][0]["e_lo"], out["flags"][0]["e_hi"]) == (7.25, 9.55)
    (lo, hi, area, sigma), = out["grouped_intensity"]
    assert (lo, hi, area) == (7.25, 9.55, 20.0) and sigma == pytest.approx(math.sqrt(10.0))
    # a threshold above the correlation: no pair, no flag; the two peaks still form a group by distance
    loose = _run(fs, overdamped_correlation=0.99)
    assert loose["flags"] == [] and loose["peaks"][0]["area_status"] == "usable"
    assert loose["peaks"][0]["confident"] and len(loose["grouped_intensity"]) == 1


def test_a_forced_broad_or_shoulder_alternative_elsewhere_flags_an_ambiguous_decomposition():
    peaks = [(5.0, 0.4, 100.0, 0.1)]
    far = [5.1, 12.0]
    for family, near in (("broad", False), ("shoulder", False), ("split", False), ("broad", True)):
        omega0 = [5.1, 5.9] if near else far
        flags = _run(_fs(peaks, alternatives=[_alt(omega0, 0.3, gap=0.7, family=family)]))["flags"]
        if family != "split" and not near:
            assert [(f["kind"], f["e_lo"], f["e_hi"], f["statistic"]) for f in flags] == [
                ("ambiguous_decomposition", 11.25, 12.75, 0.7)]
            assert "near 12.0 meV" in flags[0]["detail"]
        else:
            assert flags == []             # a split, or within one FWHM of a selected peak


def test_the_count_state_follows_the_stated_precedence():
    fs = _fs([(5.0, 0.4, 100.0, 0.1)], count_ln_z={1: 0.0, 2: -1.0})
    probs = {1: 0.7, 2: 0.3}
    close = _run(fs, 1, probs)
    assert close["state"] == "close_call" and close["close_call_alternatives"] == (2,)
    assert close["deep_pass_candidate"] and not close["probabilities_conditional"]
    assert _run(fs, 1, {1: 0.97, 2: 0.03})["state"] == "resolved"
    assert _run(fs, 1, probs, close_call_p=0.6)["state"] == "resolved"        # threshold, no refit
    assert _run(fs, 1, probs, reason="ceiling")["state"] == "search_limited"
    unpriced = _run(dict(fs, unpriced_contenders=[2]), 1, probs, reason="ceiling")
    assert unpriced["state"] == "unresolved" and unpriced["probabilities_conditional"]
    empty = _fs([], count_ln_z={})                                  # nothing has evidence
    nothing = fast_pass._classify(FastPassSettings(mode_ceiling=6), [], empty, {}, None, "safety_budget")
    assert nothing["state"] == "unresolved" and nothing["peaks"] == [] and nothing["close_call_alternatives"] == ()
    # an evidence uncertainty that can move the probability across the threshold says so
    wobbly = dict(fs, count_ln_z={1: 0.0, 2: -1.0}, evidence_spread_nats=1.0)
    assert _run(wobbly, 1, {1: 0.74, 2: 0.26})["numerically_marginal"]
    assert not _run(fs, 1, {1: 0.74, 2: 0.26})["numerically_marginal"]


# --------------------------------------------------------------------------- engine result, both entries
@pytest.fixture(scope="module")
def two_peak():
    lam = _expected([(4.0, 0.4, 90.0), (9.0, 0.6, 60.0)])
    counts = np.random.default_rng(11).poisson(lam).astype(float)
    return fast_pass.run_fast_pass(_ctx(counts), settings=FastPassSettings(
        mode_ceiling=2, audit_rounds=0, confident_anchor=((0.0, 50.0),)))


def _record(r):
    """The C-fastpass-record fields reclassify_record reads, after a JSON round trip."""
    keys = ("e_lo", "e_hi", "intensity", "sigma")
    return json.loads(json.dumps({
        "state": r.state, "preferred_count": r.preferred_count,
        "count_probabilities": r.count_probabilities, "probabilities_conditional": r.probabilities_conditional,
        "close_call_alternatives": r.close_call_alternatives, "numerically_marginal": r.numerically_marginal,
        "search_limit_reason": r.search_limit_reason, "deep_pass_candidate": r.deep_pass_candidate,
        "peaks": [asdict(p) for p in r.peaks], "flags": [asdict(f) for f in r.flags],
        "grouped_intensity": [dict(zip(keys, g)) for g in r.grouped_intensity],
        "flag_statistics": r.flag_statistics, "settings": asdict(r.settings)}))


def test_the_engine_reports_what_classify_says(two_peak):
    r = two_peak
    assert len(r.peaks) == 2 and all(p.confident for p in r.peaks) and r.flags == ()
    again = fast_pass.reclassify(r, r.settings)
    assert again.peaks == r.peaks and again.state == r.state and again.grouped_intensity == r.grouped_intensity
    fs = r.flag_statistics
    assert fs["anchor"] == 50.0 and len(fs["residual_scan"]) == X.size and len(fs["peaks"]) == 2
    assert np.asarray(fs["area_covariance"]).shape == (2, 2)
    json.dumps(fs)                                                  # JSON-safe as it stands


def test_reclassify_changes_the_verdict_without_refitting_and_refuses_a_search_setting(two_peak):
    r = two_peak
    stricter = fast_pass.reclassify(r, replace(r.settings, confident_anchor=((0.0, 1e9),)))
    assert not any(p.confident for p in stricter.peaks) and all(p.confident for p in r.peaks)
    assert stricter.n_fits == r.n_fits and stricter.models is r.models and stricter.flag_statistics["anchor"] == 1e9
    with pytest.raises(ValueError, match="trf_max_nfev"):
        fast_pass.reclassify(r, replace(r.settings, trf_max_nfev=r.settings.trf_max_nfev + 1))
    # any nonzero correlation now counts: regimes, statuses and flags follow
    tight = fast_pass.reclassify(r, replace(r.settings, overdamped_correlation=1e-6))
    assert all(p.regime == "overdamped" and p.manual_fit for p in tight.peaks)
    assert all(p.area_status == "decomposition_conditional" and not p.confident for p in tight.peaks)
    assert [f.kind for f in tight.flags] == ["ambiguous_decomposition"] and len(tight.grouped_intensity) == 1


@pytest.mark.parametrize("change", [
    {"confident_anchor": ((0.0, 3000.0),)},
    {"overdamped_correlation": 0.01},
    {"flag_residual_threshold": 1.0},
    {"material_p": 1e-12, "flag_residual_threshold": 2.0, "overdamped_correlation": 0.3},
])
def test_reclassify_and_reclassify_record_agree(two_peak, change):
    r, settings = two_peak, replace(two_peak.settings, **change)
    rec = _record(r)
    before = json.dumps(rec, sort_keys=True)
    want, got = fast_pass.reclassify(r, settings), fast_pass.reclassify_record(rec, settings)
    assert json.dumps(rec, sort_keys=True) == before                # the input is not mutated
    assert (got["state"], got["numerically_marginal"], got["deep_pass_candidate"],
            got["probabilities_conditional"]) == (want.state, want.numerically_marginal,
                                                  want.deep_pass_candidate, want.probabilities_conditional)
    assert got["close_call_alternatives"] == list(want.close_call_alternatives)
    assert got["flags"] == [json.loads(json.dumps(asdict(f))) for f in want.flags]
    assert got["grouped_intensity"] == [dict(zip(("e_lo", "e_hi", "intensity", "sigma"), g))
                                        for g in want.grouped_intensity]
    for g, w in zip(got["peaks"], want.peaks):
        assert (g["confident"], g["regime"], g["manual_fit"], g["status"], g["area_status"]) == (
            w.confident, w.regime, w.manual_fit, w.status, w.area_status)
    base = fast_pass.reclassify(r, r.settings)               # the change did something
    assert (want.peaks, want.flags, want.grouped_intensity) != (base.peaks, base.flags, base.grouped_intensity)
    with pytest.raises(ValueError, match="trf_max_nfev"):
        fast_pass.reclassify_record(rec, replace(r.settings, trf_max_nfev=1))

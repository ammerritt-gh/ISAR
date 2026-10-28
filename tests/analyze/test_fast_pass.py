"""The fast-pass engine (plan C-fastpass, worked examples 1-7, and the engine's own checks).

Small synthetic spectra on a Voigt resolution (FWHM 1.5 meV): the internal entry points
(``_Engine.fit``, ``resolve``, ``price``, ``_tally``) are driven directly where a whole run would
cost more than the case needs. Twin cases use noiseless counts (the expected counts themselves),
so a width whose truth is zero rails at its floor deterministically. Every test names its ceiling:
the repo conftest's ``ISAR_MODE_CEILING`` is never read here.
"""
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.stats import truncnorm

from isar.analyze import catalogue, fast_pass, loop, prior
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.analyze.phonon import GAMMA_FLOOR
from isar.schema.steering import ParamConstraints

FWHM = 1.5
X = np.linspace(-8.0, 20.0, 113)


def _ctx(counts, x=X, **kw):
    return ScanContext(x=x, counts=np.asarray(counts, dtype=float), mask=kw.pop("mask", np.ones(x.size, bool)),
                       T=300.0, center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute", **kw)


def _plain(ctx, label):
    n = int(label.split("stokes")[1].split("+")[0] or 1) if "stokes" in label else 0
    return next(h for h in catalogue.enumerate_count_structures(ctx, n) if h.label == label)


def _expected(modes, elastic=150.0, bg=4.0, x=X):
    """Expected counts of elastic + len(modes) DHO modes + bg; a mode width 0 renders its narrow
    limit (C-dho clips it at 1e-9 meV)."""
    ctx = _ctx(np.ones(x.size), x)
    label = "elastic+stokes{}+bg".format(len(modes) if len(modes) > 1 else "")
    theta = [v for m in modes for v in m] + [elastic, bg]
    return _plain(ctx, label).build().evaluate(x, theta, ctx)


def _engine(counts, ceiling, **settings):
    return fast_pass._Engine(_ctx(counts), FastPassSettings(mode_ceiling=ceiling, **settings))


def _by_id(result):
    return {m.model_id: m for m in result.models}


FOUR = [(3.0, 0.5, 80.0), (7.0, 0.6, 60.0), (11.0, 0.5, 50.0), (15.0, 0.0, 70.0)]


# --------------------------------------------------------------------------- example 1
def test_ex1_count_prior_is_normalised_over_variants_and_masks():
    # Count 1 with V = 4 and both masks, count 2 with V = 4 and all four masks, every ln_z = 0:
    # Z_1 = Z_2 = 1 and P = 0.5 / 0.5. The unnormalised tally (prototype) gives 2 and 4.
    entries = []
    for k, masks in ((1, 2), (2, 4)):
        for _variant in range(4):
            for _mask in range(masks):
                entries.append(SimpleNamespace(count=k, status="ok", ln_z=0.0, ln_like=0.0, k_free=3 * k,
                                               ln_model_prior=fast_pass._ln_model_prior(4, k)))
    tally = fast_pass._tally(entries, 10.0)
    assert tally.count_ln_z[1] == pytest.approx(0.0, abs=1e-12)
    assert tally.count_ln_z[2] == pytest.approx(0.0, abs=1e-12)
    assert tally.probabilities == pytest.approx({1: 0.5, 2: 0.5})


def test_a_count_with_a_priced_basin_never_contends_unpriced():
    # Count 1 is priced; count 2 has a priced basin and a higher-ln L unpriced one (not a contender);
    # count 3 has only unpriced basins reaching the selected ln L (a contender); count 4's only
    # unpriced basin fits worse than the selected model (not a contender).
    def entry(count, status, ln_like, ln_z=None):
        return SimpleNamespace(count=count, status=status, ln_z=ln_z, ln_like=ln_like, k_free=3 * count,
                               ln_model_prior=0.0)
    entries = [entry(1, "ok", -10.0, 0.0), entry(2, "ok", -11.0, -3.0), entry(2, "no_evidence", -5.0),
               entry(3, "no_evidence", -9.0), entry(4, "no_evidence", -20.0)]
    tally = fast_pass._tally(entries, 10.0)
    assert tally.preferred == 1 and tally.unpriced_contenders == [3]


@pytest.mark.parametrize("delta", [(), (1,)])
def test_failed_variant_remains_in_deep_input_when_same_count_sibling_succeeds(monkeypatch, delta):
    from isar.analyze import deep_pass
    from isar.session.scan_delivery import _fast_pass_record

    eng = _engine(_expected([(4.0, 0.5, 60.0), (10.0, 0.5, 40.0)]), 2)
    eng.reached.add(2)
    successful = eng.variant(2, True, True)
    failed = eng.variant(2, True, False)
    failed_model = eng.model(failed, delta)

    # Isolate one optimiser failure; the sibling's start is a valid fitted point. Neither
    # failure handling nor deep reconstruction requires an optimisation or evidence run.
    def fit_or_fail(model, theta0, family):
        if model is failed_model:
            return None
        return SimpleNamespace(x=theta0[model.free], nfev=1, message="synthetic success")

    monkeypatch.setattr(eng, "_trf", fit_or_fail)
    monkeypatch.setattr(eng, "resolve", lambda basin, point: None)
    modes = [(4.0, 0.5, 60.0, False), (10.0, 0.5, 40.0, False)]
    assert eng.fit(successful, modes, "ladder") is not None
    failed_modes = [(e, g, h, i in delta) for i, (e, g, h, _) in enumerate(modes)]
    assert eng.fit(failed, failed_modes, "ladder") is None

    record = _fast_pass_record(eng.conclude())
    rows = {m["label"]: m for m in record["models"]}
    assert failed_model.hyp.label in rows
    row = rows[failed_model.hyp.label]
    assert row["evidence_status"] == "not_fitted"
    assert row["reason"] and row["cost"] is None and row["ln_z"] is None
    assert row["ln_model_prior"] == pytest.approx(fast_pass._ln_model_prior(len(eng.variants[2]), 2))
    assert rows[successful.hyp.label]["cost"] is not None
    # Keep base variants and the attempted mask; do not expand the other 2**k masks.
    expected_labels = {v.label for v in eng.variants[2]} | {failed_model.hyp.label}
    assert set(rows) == expected_labels and len(rows) == len(record["models"])

    entries = {e.label: e for e in deep_pass.models_from_record(
        eng.ctx, record, deep_pass.plain_hypotheses(eng.ctx, 2))}
    assert set(entries) == expected_labels
    entry = entries[failed_model.hyp.label]
    assert entry.fastpass_ln_z is None and entry.fastpass_lnl_opt is None
    assert entry.ndim == row["k_free"] and entry.log_model_prior == row["ln_model_prior"]


def test_a_soft_constraint_replaces_only_its_own_coordinates_recipe_factor():
    # Flat-width arm: the unconstrained width must keep the flat factor, not the catalogue's default.
    arm = prior.PriorSettings(width_kind="flat")
    soft = ParamConstraints(soft={"phonon2.Gamma": (0.5, 0.2)})
    eng = fast_pass._Engine(_ctx(np.full(X.size, 200.0), constraints=soft),
                            FastPassSettings(mode_ceiling=2, prior=arm))
    model = eng.model(eng.variant(2, True, True), ())
    theta = np.array([4.0, 0.3, 50.0, 9.0, 0.6, 40.0, 100.0, 4.0])
    sp = eng.sp
    lo, hi = model.lo[4], model.hi[4]
    gauss = truncnorm.logpdf(0.6, (lo - 0.5) / 0.2, (hi - 0.5) / 0.2, loc=0.5, scale=0.2)
    # the constraint singles mode 2 out, so the energies are two plain factors, not one ordered k = 2
    others = sp.ln_prior([(4.0, 0.3, 50.0), (9.0, None, 40.0)], 100.0, 4.0) - math.log(2)
    assert math.isfinite(others + gauss)
    assert eng.ln_prior(model, theta) == pytest.approx(others + gauss, abs=1e-9)
    # a pinned (delta) width carries no factor either: the delta twin of the same count
    twin = eng.model(eng.variant(2, True, True), (0,))
    assert twin.delta == (0,) and twin.omit >= {twin.modes[0][1], 4}


def test_a_constrained_mode_leaves_the_ordered_energy_group():
    # Unconstrained: both energies share the k! factor. A soft width on mode 2 singles it out: two
    # flat single-energy factors (catalogue.PriorBundle._energy_groups). A delta twin's pinned
    # width does not split the group.
    theta = np.array([4.0, 0.3, 50.0, 9.0, 0.6, 40.0, 100.0, 4.0])
    plain = fast_pass._Engine(_ctx(np.full(X.size, 200.0)), FastPassSettings(mode_ceiling=2))
    free = plain.model(plain.variant(2, True, True), ())
    sp = plain.sp
    assert [len(g) for g in free.groups] == [2]
    assert plain.ln_prior(free, theta) == pytest.approx(sp.ln_prior([(4.0, 0.3, 50.0), (9.0, 0.6, 40.0)],
                                                                     100.0, 4.0), abs=1e-9)
    soft = ParamConstraints(soft={"phonon2.Gamma": (0.5, 0.2)})
    eng = fast_pass._Engine(_ctx(np.full(X.size, 200.0), constraints=soft), FastPassSettings(mode_ceiling=2))
    model = eng.model(eng.variant(2, True, True), ())
    assert [len(g) for g in model.groups] == [1, 1]
    flat = sp.ln_energies([4.0]) + sp.ln_energies([9.0])
    assert flat == pytest.approx(sp.ln_energies([4.0, 9.0]) - math.log(2))
    rest = (sp.ln_width(0.3) + sp.ln_height(50.0) + sp.ln_height(40.0) + sp.ln_height(100.0)
            + sp.ln_background(4.0) + model.soft_priors[0][1].logpdf(0.6))
    assert eng.ln_prior(model, theta) == pytest.approx(flat + rest, abs=1e-9)
    twin = plain.model(plain.variant(2, True, True), (0,))
    assert [len(g) for g in twin.groups] == [2]


# --------------------------------------------------------------------------- example 2
def test_ex2_a_railed_width_resolves_to_its_delta_twin():
    eng = _engine(_expected(FOUR), 4)
    modes = [(e, g, h, False) for e, g, h in FOUR]
    assert eng.fit(eng.variant(4, True, True), modes, "ladder") is not None
    eng.price_all()
    res = _by_id(eng.conclude())
    parent, twin = res["elastic+stokes4+bg#0"], res["elastic+stokes4+bg_delta4#0"]
    assert parent.evidence_status == "degenerate_twin"
    assert parent.twin_of == "elastic+stokes4+bg_delta4#0" and parent.probability == 0.0
    assert twin.delta_modes == (3,) and twin.k_free == parent.k_free - 1 == 13
    assert twin.params["phonon4.Gamma"] == GAMMA_FLOOR and "phonon4.Gamma" not in twin.free
    assert twin.evidence_status in ("ok", "ok_boundary") and twin.cov.shape == (14, 14)
    i = list(twin.params).index("phonon4.Gamma")
    assert not twin.cov[i].any() and not twin.cov[:, i].any()
    assert twin.ln_model_prior == pytest.approx(-math.log(4) - 4 * math.log(2))
    plain = _plain(eng.ctx, "elastic+stokes4+bg")
    assert twin.label == loop.delta_twin_hypothesis(plain, ["phonon4"], eng.ctx).label


# --------------------------------------------------------------------------- example 3
def test_ex3_twins_resolve_recursively_to_one_terminal_identity():
    truth = [(3.0, 0.5, 80.0), (7.0, 0.0, 60.0), (11.0, 0.5, 50.0), (15.0, 0.0, 70.0)]
    eng = _engine(_expected(truth), 4)
    assert eng.fit(eng.variant(4, True, True), [(e, g, h, False) for e, g, h in truth], "ladder")
    res = _by_id(eng.conclude())
    assert res["elastic+stokes4+bg#0"].twin_of == "elastic+stokes4+bg_delta2-4#0"
    middle = [m for m in res.values() if len(m.delta_modes) == 1]
    assert middle and all(m.evidence_status == "degenerate_twin"
                          and m.twin_of == "elastic+stokes4+bg_delta2-4#0" for m in middle)
    assert res["elastic+stokes4+bg_delta2-4#0"].twin_of is None


# --------------------------------------------------------------------------- example 4
def test_ex4_coincident_deltas_merge_one_pair_at_a_time():
    # Truth: a broad mode at 9.0 and a delta mode at 9.1. The planted count-3 point splits the delta
    # into two delta modes 4e-5 meV apart: the single-pair merge refits count 2 within 2 deviance
    # units; the all-pairs chained merge (which also swallows the broad mode) does not.
    ctx = _ctx(np.ones(X.size))
    plain = _plain(ctx, "elastic+stokes2+bg")
    twin_hyp = loop.delta_twin_hypothesis(plain, ["phonon2"], ctx)
    theta = [9.0, 0.7, 80.0, 9.1, GAMMA_FLOOR, 100.0, 150.0, 4.0]
    eng = _engine(twin_hyp.build().evaluate(X, theta, loop.candidate_context(twin_hyp, ctx)), 3)
    model = eng.model(eng.variant(3, True, True), (1, 2))
    point = np.array([9.0, 0.7, 80.0, 9.1, GAMMA_FLOOR, 60.0, 9.10004, GAMMA_FLOOR, 40.0, 150.0, 4.0])
    cost_p = eng.cost(model, point)
    b = eng._place(model, point, cost_p, "planted", 0, "")
    assert b.status == "degenerate_twin" and b.reason.startswith("coincident")
    assert b.twin.model.variant.k == 2 and b.twin.model.delta == (1,)
    chained = fast_pass._merge_chain(eng.modes_of(model, point), eng.s.twin_coincide_fwhm * FWHM)
    assert len(chained) == 1
    out = eng.fit(eng.variant(1, True, True), chained, "twin", eng.linear_of(model, point))
    assert out is None or out[1] > cost_p + eng.s.twin_cost_tolerance


# --------------------------------------------------------------------------- spectra for whole runs
def _one_peak(seed=5, gamma=0.5, h=120.0):
    lam = _expected([(6.0, gamma, h)], elastic=200.0, bg=5.0)
    return np.random.default_rng(seed).poisson(lam).astype(float)


@pytest.fixture(scope="module")
def three_peak_ceiling_two():
    lam = _expected([(4.0, 0.4, 90.0), (9.0, 0.6, 60.0), (14.0, 0.5, 50.0)])
    counts = np.random.default_rng(11).poisson(lam).astype(float)
    return fast_pass.run_fast_pass(_ctx(counts), settings=FastPassSettings(mode_ceiling=2))


# --------------------------------------------------------------------------- example 5
def test_ex5_a_contending_count_without_evidence_leaves_the_result_unresolved(monkeypatch):
    real = fast_pass._Engine.laplace

    def no_count_two(self, b, f, z, lo, hi, rel_step=1e-4):
        if b.count == 2:
            return prior.LaplaceResult(math.nan, "no_evidence", "Hessian not positive definite",
                                       math.nan, None, "", 0.0)
        return real(self, b, f, z, lo, hi, rel_step=rel_step)

    monkeypatch.setattr(fast_pass._Engine, "laplace", no_count_two)
    res = fast_pass.run_fast_pass(_ctx(_one_peak()),
                                  settings=FastPassSettings(mode_ceiling=2, audit_rounds=0))
    assert res.state == "unresolved" and res.probabilities_conditional and res.deep_pass_candidate
    two = [m for m in res.models if m.n_peaks == 2 and m.evidence_status == "no_evidence"]
    assert two and all(m.ln_z is None and m.reason == "Hessian not positive definite" for m in two)
    assert 2 not in res.count_probabilities
    assert max(m.ln_like for m in two) >= res.selected.ln_like


# --------------------------------------------------------------------------- example 6
def test_ex6_a_hit_posterior_iteration_limit_is_no_evidence_never_a_failed_model():
    eng = _engine(_one_peak(), 1, posterior_maxiter=2)
    b, _cost = eng.fit(eng.variant(1, True, True), [(6.0, 0.5, 120.0, False)], "ladder")
    eng.price(b)
    m = _by_id(eng.conclude())[b.model_id]
    assert m.evidence_status == "no_evidence" and m.ln_z is None
    assert "iteration limit (2)" in m.reason and m.posterior_iterations == 2


# --------------------------------------------------------------------------- states
def test_a_flat_spectrum_is_resolved_at_count_zero():
    counts = np.random.default_rng(2).poisson(np.full(X.size, 20.0)).astype(float)
    res = fast_pass.run_fast_pass(_ctx(counts), settings=FastPassSettings(mode_ceiling=2))
    assert res.state == "resolved" and res.preferred_count == 0 and res.peaks == ()
    assert res.selected.n_peaks == 0 and not res.deep_pass_candidate


def test_a_best_count_at_the_ceiling_is_search_limited(three_peak_ceiling_two):
    res = three_peak_ceiling_two
    assert res.state == "search_limited" and res.search_limit_reason == "ceiling"
    assert res.preferred_count == 2 and res.deep_pass_candidate


def test_no_clock_stops_the_fast_pass_search(monkeypatch):
    # No analysis stops because a clock ran out: a clock a million seconds per read gives the answer
    # of a run that has no clock reads at all (the old 300 s per-spectrum budget would cut it short).
    free = fast_pass.run_fast_pass(_ctx(_one_peak()), settings=FastPassSettings(mode_ceiling=2))
    ticks = iter(range(10 ** 6))
    monkeypatch.setattr(fast_pass, "time", SimpleNamespace(perf_counter=lambda: 1e6 * next(ticks)))
    res = fast_pass.run_fast_pass(_ctx(_one_peak()), settings=FastPassSettings(mode_ceiling=2))
    assert res.search_limit_reason != "safety_budget" and res.selected is not None
    assert res.state == free.state and res.preferred_count == free.preferred_count
    assert res.count_probabilities == free.count_probabilities
    assert set(fast_pass.STAGES) | {"total", "families", "seed_count"} == set(res.timing)


# --------------------------------------------------------------------------- the adapter
def test_engine_mean_is_the_composite_mean_with_mask_and_exposure():
    rng = np.random.default_rng(4)
    mask = np.ones(X.size, bool)
    mask[[5, 40, 41, 90]] = False
    ctx = _ctx(np.full(X.size, 30.0), mask=mask, exposure_scale=rng.uniform(0.5, 1.5, X.size),
               exposure_reference=1.0e4, exposure_source="monitor")
    eng = fast_pass._Engine(ctx, FastPassSettings(mode_ceiling=2))
    model = eng.model(eng.variant(2, True, True), (1,))
    theta = np.array([4.0, 0.4, 50.0, 9.0, GAMMA_FLOOR, 30.0, 80.0, 3.0])
    want = model.hyp.build().evaluate(ctx.x_used, theta, model.cctx)
    np.testing.assert_allclose(eng.mean(model, theta), want, rtol=1e-10, atol=0.0)


def test_model_results_carry_their_fit_at_their_own_point(three_peak_ceiling_two):
    res = three_peak_ceiling_two
    keys = [(m.n_peaks, m.elastic, m.background, m.delta_modes, int(m.model_id.rsplit("#", 1)[1]))
            for m in res.models]
    assert keys == sorted(keys)
    for m in res.models:
        if m.fit is None:
            assert m.evidence_status == "not_fitted"
            continue
        assert m.fit.params == (m.map_params or m.params)
        if m.cov is None:
            assert m.fit.parameter_covariance is None
        else:
            np.testing.assert_array_equal(np.asarray(m.fit.parameter_covariance), m.cov)
        assert m.k_free == len(m.free) == m.fit.k
    assert res.selected.evidence_status in ("ok", "ok_boundary")
    assert len(res.peaks) == res.preferred_count == res.selected.n_peaks


# --------------------------------------------------------------------------- smoothness
@pytest.mark.parametrize("gamma", [2e-3, 1e-2])
def test_posterior_curvature_in_a_narrow_width_does_not_depend_on_the_step(gamma):
    eng = _engine(_one_peak(gamma=0.2), 1)
    model = eng.model(eng.variant(1, True, True), ())
    theta = np.array([6.0, gamma, 120.0, 200.0, 5.0])
    f, lo, hi = eng.objective(model, theta)
    span = hi[1] - lo[1]

    def along(g):
        z = theta.copy()
        z[1] = g
        return f(z)

    curv = []
    for rel in (1e-4, 3e-4):                 # Laplace's own step rule, at its two rel_steps
        step = rel * max(gamma, 1e-2 * span)
        curv.append(prior.hessian_fd(lambda v: along(v[0]), np.array([gamma]), [step])[0, 0])
    # A branch switch or a blend inside the stencil moves the curvature by O(1). The narrow-pole
    # rendering's round-off (~1e-8 in f, measured) alone moves it ~2% at the 2e-5 meV step.
    assert curv[1] == pytest.approx(curv[0], rel=0.03)


# --------------------------------------------------------------------------- example 7 and basins
def _priced_one_peak(counts, start):
    eng = _engine(counts, 1)
    b, _ = eng.fit(eng.variant(1, True, True), [start], "ladder")
    eng.price_all()
    return eng, b


def test_ex7_a_width_within_four_and_a_half_sd_of_zero_is_priced_as_its_delta_mask():
    eng, b = _priced_one_peak(_one_peak(seed=3, gamma=0.15, h=40.0), (6.0, 0.15, 40.0, False))
    assert b.status in ("ok", "ok_boundary")
    g = b.theta_map[1]
    sd = math.sqrt(b.cov_free[1, 1])
    assert 2e-3 < g < fast_pass.NEAR_ZERO_SIGMAS * sd           # near zero, never railed
    masked = eng.model(b.model.variant, (0,))
    assert any(m.family == "near_zero" and m.status in ("ok", "ok_boundary") for m in masked.basins)

    eng, b = _priced_one_peak(_one_peak(seed=3, gamma=1.0, h=400.0), (6.0, 1.0, 400.0, False))
    assert b.theta_map[1] > fast_pass.NEAR_ZERO_SIGMAS * math.sqrt(b.cov_free[1, 1])
    assert "near_zero" not in eng.families


def test_basins_separate_on_width_as_well_as_energy():
    eng = _engine(_expected([(5.0, 0.5, 80.0), (10.0, 0.5, 60.0)]), 2)
    model = eng.model(eng.variant(2, True, True), ())
    a = np.array([5.0, 0.3, 80.0, 10.0, 0.5, 60.0, 150.0, 4.0])
    wider = a.copy()
    wider[1] = 1.0                     # |d Gamma| 0.7 > 0.3 x max(FWHM, Gamma) = 0.45
    close = a.copy()
    close[1] = 0.6                            # |d Gamma| 0.3 <= 0.45
    assert not eng.same_basin(model, a, wider)
    assert eng.same_basin(model, a, close)

"""Step-4 verdict buckets (§6) + detection power (§0.4). Helpers first: the excursion-above-
background signal test (reused from the QC layer's flatness idea) and the residual-structure
test (a runs test that keeps a structured quasi-elastic residual out of the junk bucket)."""
import types

import numpy as np

from isar.analyze import loop, select, verdict
from isar.analyze.context import ScanContext
from isar.analyze.prior import PhononPrior
from isar.analyze.phonon import enumerate_phonon_structures


def _ctx(counts, *, energy_status="needs_zero", prior_omega=None, x=None):
    counts = np.asarray(counts, dtype=float)
    if x is None:
        x = np.linspace(-10.0, 10.0, counts.size)
    return ScanContext(x=x, counts=counts, mask=np.ones(counts.size, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status=energy_status, prior_omega=prior_omega)


def test_phonon_prior_fields():
    p = PhononPrior(omega0=6.0, amplitude=300.0, omega0_sigma=1.0)
    assert p.omega0 == 6.0 and p.amplitude == 300.0 and p.omega0_sigma == 1.0
    assert PhononPrior(omega0=5.0).amplitude is None      # strength optional (detection abstains)


def test_excursion_above_bg_detects_peak_and_flat():
    rng = np.random.default_rng(0)
    flat = rng.poisson(20.0, 81).astype(float)
    peak = flat.copy(); peak[40] += 500.0
    assert verdict._excursion_above_bg(_ctx(peak)) is True
    assert verdict._excursion_above_bg(_ctx(flat)) is False


def test_residual_structured_vs_noise():
    rng = np.random.default_rng(1)
    noise = rng.normal(0.0, 1.0, 60)
    assert verdict._residual_structured(noise) is False           # random signs -> ~expected runs
    bump = np.r_[np.full(20, -1.5), np.full(20, 1.8), np.full(20, -1.2)]
    assert verdict._residual_structured(bump) is True             # 3 long runs -> too few


# --- classify (prior-free buckets) ----------------------------------------------------------

def _fit_result(label, params, *, cost_per_dof, converged=True, within_model=None, omega0=None):
    if omega0 is None:
        omega0 = float(params["phonon.omega0"])
    return loop.StructureFitResult(
        label=label, k=len(params), ndof=40, cost=cost_per_dof * 40, cost_per_dof=cost_per_dof,
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=converged, analysis_usable=converged, params=dict(params),
        param_errors={n: None for n in params}, gamma=params["phonon.Gamma"], gamma_err=0.05,
        omega0=omega0, omega0_err_hesse=0.05, message="ok", within_model=within_model)


def _sel(modality="fold", *, best="stokes+bg", pooled=None):
    return select.Selection(scores=[], best=best, survivors=[best] if best else [],
                            pooled=pooled, modality=modality)


_SB = {"phonon.omega0": 5.0, "phonon.Gamma": 0.4, "phonon.h": 300.0, "bg.BG": 20.0}


def test_held_for_calibration_when_uncalibrated():
    ctx = _ctx(np.full(61, 50.0), energy_status="needs_conversion_and_zero")
    v = verdict.classify([_fit_result("stokes+bg", _SB, cost_per_dof=2.0)], ctx, _sel())
    assert v.verdict == "held_for_calibration"


def test_held_for_calibration_helper():
    v = verdict.held_for_calibration("no energy zero available")
    assert v.verdict == "held_for_calibration"
    assert v.confidence == 0.0
    assert "no energy zero" in v.reasons[0]


def test_unconstrained_when_nothing_converges():
    ctx = _ctx(np.full(61, 50.0))
    sel = select.Selection(scores=[], best=None, survivors=[], pooled=None, modality="none")
    assert verdict.classify([], ctx, sel).verdict == "unconstrained"


def test_fork_without_prior_is_unconstrained_but_prior_breaks_it():
    rng = np.random.default_rng(3)
    counts = rng.poisson(40.0, 81).astype(float); counts[40] += 600.0   # signal present
    ctx = _ctx(counts, x=np.linspace(-10, 10, 81))
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=2.0)
    pooled = select.Pooled("fork", np.array([]), np.array([]), None,
                           [select.Mode(5.0, 0.5, (4.8, 5.0, 5.2)),
                            select.Mode(8.0, 0.5, (7.8, 8.0, 8.2))], "either 5 or 8")
    sel = _sel("fork", best="elastic+stokes+bg", pooled=pooled)
    assert verdict.classify([res], ctx, sel, prior=None).verdict == "unconstrained"
    broken = verdict.classify([res], ctx, sel, prior=PhononPrior(8.0, 300.0, 0.5))
    assert broken.verdict == "resolved"                      # prior selects the 8 meV mode


def test_sparse_and_wide_is_unconstrained():
    rng = np.random.default_rng(4)
    counts = rng.poisson(40.0, 81).astype(float); counts[40] += 600.0
    ctx = _ctx(counts, x=np.linspace(-10, 10, 81))
    wm = {"freq_interval": (2.0, 5.0, 9.0),                   # hw 3.5 / omega0 5 = 0.7 > 0.5
          "agreement": types.SimpleNamespace(regime="sparse")}
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=2.0,
                      within_model=wm, omega0=5.0)
    assert verdict.classify([res], ctx, _sel("fold", best="elastic+stokes+bg")).verdict == "unconstrained"


def test_junk_vs_model_incomplete_split():
    x = np.linspace(-10.0, 10.0, 81)
    base = _ctx(np.zeros_like(x), x=x)
    comp = {h.label: h for h in enumerate_phonon_structures(base)}["stokes+bg"].build()
    theta = np.array([_SB["phonon.omega0"], _SB["phonon.Gamma"], _SB["phonon.h"], _SB["bg.BG"]])
    model = comp.evaluate(x, theta, base)
    rng = np.random.default_rng(0)
    junk_counts = np.maximum(model + rng.normal(0.0, 8.0, x.size), 0.0)   # structureless residual
    inc_counts = model + 60.0 * np.exp(-0.5 * (x / 3.0) ** 2)             # coherent quasi-elastic tail
    res = _fit_result("stokes+bg", _SB, cost_per_dof=8.0)                 # catastrophic GoF
    vj = verdict.classify([res], _ctx(junk_counts, x=x), _sel())
    vi = verdict.classify([res], _ctx(inc_counts, x=x), _sel())
    assert vj.verdict == "junk"
    assert vi.verdict == "model_incomplete"


def test_no_signal_when_flat_and_no_prior():
    ctx = _ctx(np.full(61, 50.0))
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=1.0)
    assert verdict.classify([res], ctx, _sel(best="elastic+stokes+bg"), prior=None).verdict == "no_signal"


def test_resolved_clean_scan_has_confidence():
    rng = np.random.default_rng(5)
    counts = rng.poisson(40.0, 81).astype(float); counts[40] += 600.0
    ctx = _ctx(counts, x=np.linspace(-10, 10, 81), energy_status="absolute")
    wm = {"freq_interval": (5.95, 6.0, 6.05), "agreement": types.SimpleNamespace(regime="asymptotic")}
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=2.0,
                      within_model=wm, omega0=6.0)
    v = verdict.classify([res], ctx, _sel("fold", best="elastic+stokes+bg"))
    assert v.verdict == "resolved"
    assert 0.0 < v.confidence <= 1.0
    assert "s_interval" in v.components


# --- detection power + underpowered/no-signal split (the circularity guard) ------------------

_EL = {"phonon.omega0": 6.0, "phonon.Gamma": 0.4, "phonon.h": 300.0,
       "elastic.h": 50.0, "bg.BG": 50.0}


def test_detection_power_detectable_vs_underpowered():
    ctx = _ctx(np.full(81, 50.0), x=np.linspace(-10, 10, 81), prior_omega=6.0)
    res = _fit_result("elastic+stokes+bg", _EL, cost_per_dof=1.0, omega0=6.0)
    weak = verdict.detection_power(ctx, PhononPrior(6.0, 15.0, 1.0), res)
    strong = verdict.detection_power(ctx, PhononPrior(6.0, 500.0, 1.0), res)
    assert weak["detectable"] is False and strong["detectable"] is True
    assert weak["expected_significance"] < strong["expected_significance"]


def test_detection_power_abstains_without_expected_amplitude():
    ctx = _ctx(np.full(81, 50.0), x=np.linspace(-10, 10, 81))
    res = _fit_result("elastic+stokes+bg", _EL, cost_per_dof=1.0, omega0=6.0)
    assert verdict.detection_power(ctx, PhononPrior(6.0, None, 1.0), res)["detectable"] is None


def test_underpowered_is_provisional_and_split_from_no_signal():
    x = np.linspace(-10.0, 10.0, 81)
    flat = _ctx(np.full(81, 50.0), x=x, prior_omega=6.0)
    res = _fit_result("elastic+stokes+bg", _EL, cost_per_dof=1.0, omega0=6.0)
    sel = _sel("fold", best="elastic+stokes+bg")
    up = verdict.classify([res], flat, sel, prior=PhononPrior(6.0, 15.0, 1.0))
    assert up.verdict == "underpowered" and up.provisional is True   # guard: provisional
    assert up.detection is not None and up.detection["detectable"] is False
    ns = verdict.classify([res], flat, sel, prior=PhononPrior(6.0, 500.0, 1.0))
    assert ns.verdict == "no_signal" and ns.provisional is False     # had power -> genuine absence


# --- phonon-less winners (SCENE-1 PR-a): elastic_only / no_signal ----------------------------

def _sel_null(best="elastic+bg", *, p=1e-4, weight=0.9):
    """A Selection whose elastic+bg score carries a real nested LRT vs bg — the significance
    gate fails closed, so elastic_only expectations must supply the evidence (PR review)."""
    sc = types.SimpleNamespace(
        label="elastic+bg", weight=weight,
        nested={"reduced": "bg", "delta_deviance": 15.0, "dof": 1, "p_value": p})
    return select.Selection(scores=[sc], best=best, survivors=[best] if best else [],
                            pooled=None, modality="unpooled")


def _null_result(label, params, *, cost_per_dof=1.0, converged=True):
    """A converged phonon-less fit (elastic+bg / bg): NaN omega/gamma, zero modes."""
    return loop.StructureFitResult(
        label=label, k=len(params), ndof=40, cost=cost_per_dof * 40, cost_per_dof=cost_per_dof,
        optimizer_converged=converged, covariance_ok=True, profile_ok=False,
        well_constrained=converged, analysis_usable=converged, params=dict(params),
        param_errors={n: None for n in params}, gamma=float("nan"), gamma_err=float("nan"),
        omega0=float("nan"), omega0_err_hesse=float("nan"), message="ok",
        modes=[], n_modes=0)


def _elastic_counts(x, *, area=400.0, bg=20.0, seed=11):
    """Seeded Poisson counts from a resolution-shaped elastic line on flat background —
    honest data, so AIC differences are noise-scaled, not inflated by a noiseless curve."""
    from isar.analyze.lineshapes import Voigt

    lam = bg + Voigt(np.asarray(x, dtype=float), area, 1.0, 0.0, 0.0)
    return np.random.default_rng(seed).poisson(lam).astype(float)


def test_elastic_only_when_elastic_bg_wins():
    x = np.linspace(-10.0, 10.0, 81)
    ctx = _ctx(_elastic_counts(x), x=x)
    res = _null_result("elastic+bg", {"elastic.h": 200.0, "bg.BG": 20.0})
    v = verdict.classify([res], ctx, _sel_null())
    assert v.verdict == "elastic_only"
    assert 0.0 < v.confidence <= 0.5 + 1e-12             # the categorical-humility cap
    assert v.components["s_scope_elastic"] == 0.5
    assert v.provisional is False and v.detection is None  # no prior expected anything here


def test_elastic_only_fails_closed_without_the_bg_comparison():
    """The positive claim requires its evidence: a missing/insignificant nested LRT vs bg
    (bg not converged, diagnostic absent, p > alpha) must fall to the null convention,
    never claim elastic_only (PR #13 review)."""
    x = np.linspace(-10.0, 10.0, 81)
    ctx = _ctx(_elastic_counts(x), x=x)
    res = _null_result("elastic+bg", {"elastic.h": 200.0, "bg.BG": 20.0})
    # no scores at all -> no LRT -> fail closed
    bare = verdict.classify([res], ctx, _sel("fold", best="elastic+bg"))
    assert bare.verdict == "no_signal" and bare.confidence == 0.0
    # LRT present but insignificant -> same
    weak = verdict.classify([res], ctx, _sel_null(p=0.2))
    assert weak.verdict == "no_signal"
    assert any("significance" in r for r in weak.reasons)


def test_elastic_only_is_provisional_when_power_is_unconfirmed():
    x = np.linspace(-10.0, 10.0, 81)
    ctx = _ctx(_elastic_counts(x), x=x, prior_omega=6.0)
    res = _null_result("elastic+bg", {"elastic.h": 200.0, "bg.BG": 20.0})
    sel = _sel_null()
    # prior expects a mode but declares no strength -> power abstains -> provisional (S0.4 guard)
    v = verdict.classify([res], ctx, sel, prior=PhononPrior(6.0, None, 1.0))
    assert v.verdict == "elastic_only" and v.provisional is True
    assert v.detection is not None and v.detection["detectable"] is None
    # prior expects a mode the scan HAD power to see -> a confident (still capped) claim
    w = verdict.classify([res], ctx, sel, prior=PhononPrior(6.0, 500.0, 1.0))
    assert w.verdict == "elastic_only" and w.provisional is False
    assert w.confidence > v.confidence                   # confirmed power scores higher


def test_bg_winner_is_no_signal_and_keeps_the_underpowered_split():
    x = np.linspace(-10.0, 10.0, 81)
    ctx = _ctx(np.full(81, 20.0), x=x)
    res = _null_result("bg", {"bg.BG": 20.0})
    sel = _sel("fold", best="bg")
    plain = verdict.classify([res], ctx, sel)
    assert plain.verdict == "no_signal" and plain.confidence == 0.0
    ctx_p = _ctx(np.full(81, 20.0), x=x, prior_omega=6.0)
    up = verdict.classify([res], ctx_p, sel, prior=PhononPrior(6.0, 5.0, 1.0))
    assert up.verdict == "underpowered" and up.provisional is True
    ns = verdict.classify([res], ctx_p, sel, prior=PhononPrior(6.0, 500.0, 1.0))
    assert ns.verdict == "no_signal" and ns.detection["detectable"] is True


def test_gof_bad_null_winner_is_not_elastic_only():
    """A badly-fitting elastic+bg must route through the GoF branch, never claim elastic_only."""
    x = np.linspace(-10.0, 10.0, 81)
    rng = np.random.default_rng(7)
    counts = np.maximum(_elastic_counts(x) + rng.normal(0.0, 40.0, x.size), 0.0)
    ctx = _ctx(counts, x=x)
    res = _null_result("elastic+bg", {"elastic.h": 200.0, "bg.BG": 20.0}, cost_per_dof=9.0)
    v = verdict.classify([res], ctx, _sel("fold", best="elastic+bg"))
    assert v.verdict in ("junk", "model_incomplete", "signal_unmodeled")


def test_null_structures_fit_end_to_end():
    """The enumerated nulls survive the real fit path: IC, minimize, result extraction."""
    x = np.linspace(-10.0, 10.0, 81)
    ctx = _ctx(_elastic_counts(x), x=x)
    by_label = {h.label: h for h in enumerate_phonon_structures(ctx)}
    assert "elastic+bg" in by_label and "bg" in by_label
    for label in ("elastic+bg", "bg"):
        r = loop.fit_hypothesis(by_label[label], ctx)
        assert r.optimizer_converged
        assert r.n_modes == 0 and r.modes == []
        assert np.isnan(r.omega0) and np.isnan(r.gamma)   # NaN, never a fake 0.0
    full = loop.fit_structures_two_stage(ctx, mode_ceiling=2)
    sel = select.select_models(full)
    assert sel.best == "elastic+bg"                       # the null wins on pure-elastic data
    v = verdict.classify(full, ctx, sel)
    assert v.verdict == "elastic_only"


# --- an ambiguous peak count is never a clean fold (hard rule 5) ----------------------------------
def _scored(label, weight):
    return select.ModelScore(label=label, k=4, n=81, cost=80.0, aic=88.0, aicc=88.5, bic=96.0,
                             delta_aic=0.0, weight=weight, nested=None, survives=True,
                             posterior_probability=weight)


def _clean_two_counts(p_two):
    rng = np.random.default_rng(5)
    counts = rng.poisson(40.0, 81).astype(float); counts[40] += 600.0
    ctx = _ctx(counts, x=np.linspace(-10, 10, 81), energy_status="absolute")
    wm = {"freq_interval": (5.95, 6.0, 6.05), "agreement": types.SimpleNamespace(regime="asymptotic")}
    one = _fit_result("elastic+stokes+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=2.0,
                      within_model=wm, omega0=6.0)
    two = _fit_result("elastic+stokes2+bg", {**_SB, "elastic.h": 50.0}, cost_per_dof=2.0,
                      omega0=6.0)
    two.n_modes = 2
    sel = select.Selection(scores=[_scored(one.label, 1.0 - p_two), _scored(two.label, p_two)],
                           best=one.label, survivors=[one.label], pooled=None, modality="fold")
    return [one, two], ctx, sel


def test_an_ambiguous_peak_count_is_not_resolved():
    """Only the winner is bootstrapped, so two close counts no longer pool into a fork; the count
    ladder's ambiguity marker takes its place and keeps the scan out of 'resolved' (and so out of
    the dispersion prior). A clear count stays resolved."""
    results, ctx, sel = _clean_two_counts(0.35)
    v = verdict.classify(results, ctx, sel)
    assert v.verdict == "unconstrained"
    assert "peak count ambiguous: 1 modes (P=0.65) or 2 (P=0.35)" in v.reasons[-1]
    results, ctx, sel = _clean_two_counts(0.02)
    assert verdict.classify(results, ctx, sel).verdict == "resolved"


def test_the_deciding_rungs_count_probabilities_override_the_selection():
    """When rung 2 decided the count, its probabilities are the ones the verdict reads."""
    results, ctx, sel = _clean_two_counts(0.35)
    assert verdict.classify(results, ctx, sel,
                            count_probabilities={1: 0.99, 2: 0.01}).verdict == "resolved"
    results, ctx, sel = _clean_two_counts(0.02)
    assert verdict.classify(results, ctx, sel,
                            count_probabilities={1: 0.6, 2: 0.4}).verdict == "unconstrained"

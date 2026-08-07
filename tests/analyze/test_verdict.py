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
        omega0 = float(np.hypot(params["phonon.Omega_d"], params["phonon.Gamma"]))
    return loop.StructureFitResult(
        label=label, k=len(params), ndof=40, cost=cost_per_dof * 40, cost_per_dof=cost_per_dof,
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=converged, analysis_usable=converged, params=dict(params),
        param_errors={n: None for n in params}, gamma=params["phonon.Gamma"], gamma_err=0.05,
        omega0=omega0, omega0_err_hesse=0.05, message="ok", within_model=within_model)


def _sel(modality="fold", *, best="stokes+bg", pooled=None):
    return select.Selection(scores=[], best=best, survivors=[best] if best else [],
                            pooled=pooled, modality=modality)


_SB = {"phonon.Omega_d": 5.0, "phonon.Gamma": 0.4, "phonon.A": 300.0, "bg.BG": 20.0}


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
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.E": 50.0}, cost_per_dof=2.0)
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
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.E": 50.0}, cost_per_dof=2.0,
                      within_model=wm, omega0=5.0)
    assert verdict.classify([res], ctx, _sel("fold", best="elastic+stokes+bg")).verdict == "unconstrained"


def test_junk_vs_model_incomplete_split():
    x = np.linspace(-10.0, 10.0, 81)
    base = _ctx(np.zeros_like(x), x=x)
    comp = {h.label: h for h in enumerate_phonon_structures(base)}["stokes+bg"].build()
    theta = np.array([_SB["phonon.Omega_d"], _SB["phonon.Gamma"], _SB["phonon.A"], _SB["bg.BG"]])
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
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.E": 50.0}, cost_per_dof=1.0)
    assert verdict.classify([res], ctx, _sel(best="elastic+stokes+bg"), prior=None).verdict == "no_signal"


def test_resolved_clean_scan_has_confidence():
    rng = np.random.default_rng(5)
    counts = rng.poisson(40.0, 81).astype(float); counts[40] += 600.0
    ctx = _ctx(counts, x=np.linspace(-10, 10, 81), energy_status="absolute")
    wm = {"freq_interval": (5.95, 6.0, 6.05), "agreement": types.SimpleNamespace(regime="asymptotic")}
    res = _fit_result("elastic+stokes+bg", {**_SB, "elastic.E": 50.0}, cost_per_dof=2.0,
                      within_model=wm, omega0=6.0)
    v = verdict.classify([res], ctx, _sel("fold", best="elastic+stokes+bg"))
    assert v.verdict == "resolved"
    assert 0.0 < v.confidence <= 1.0
    assert "s_interval" in v.components


# --- detection power + underpowered/no-signal split (the circularity guard) ------------------

_EL = {"phonon.Omega_d": 6.0, "phonon.Gamma": 0.4, "phonon.A": 300.0,
       "elastic.E": 50.0, "bg.BG": 50.0}


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

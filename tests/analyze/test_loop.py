"""The structure loop: per-hypothesis problem builder + fit + StructureFitResult.

Key guarantees: each hypothesis is a real model variant (its own k/ndof, review F1); the
full elastic+stokes+bg fit reproduces the frozen synth.fit(); and the result separates
optimizer convergence from error availability (review F2) so step-3 bootstrap is not
starved of converged-but-covariance-less fits.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_structures
from isar.synth import fit
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.analyze.backends import BackendResult


def _spectrum_ctx(n=61, status="needs_zero", center=-0.4):
    x = np.linspace(-10.0, 10.0, n)
    counts = convolute_voigt_dho(x, 5.0, center, 1.1, 0.0, 0.45, 400.0, 70.0, 12.0, 150.0,
                                 peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=150.0,
                       center=center, gauss_fwhm=1.1, lorentz_fwhm=0.0, instrument="S30",
                       energy_status=status)


def _by_label(ctx):
    return {h.label: h for h in enumerate_phonon_structures(ctx)}


def test_build_problem_per_hypothesis_k_and_ndof():
    ctx = _spectrum_ctx(n=41)
    hyps = _by_label(ctx)
    p_so = loop.build_problem(hyps["stokes+bg"], ctx)
    p_full = loop.build_problem(hyps["elastic+stokes+bg"], ctx)
    assert p_so.k == 4 and p_so.ndof == 41 - 4
    assert p_full.k == 5 and p_full.ndof == 41 - 5


def test_fit_hypothesis_matches_synth_fit_full_model():
    ctx = _spectrum_ctx()
    hyp = _by_label(ctx)["elastic+stokes+bg"]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    ref = fit.fit(ctx.x, ctx.counts, gauss_fwhm=ctx.gauss_fwhm, lorentz_fwhm=ctx.lorentz_fwhm,
                  T=ctx.T, center=ctx.center, minos=False)
    assert np.isclose(res.omega0, ref.omega0, rtol=1e-5)
    assert np.isclose(res.gamma, ref.gamma, rtol=1e-5)
    assert np.isclose(res.cost, ref.cost, rtol=1e-5)
    assert res.k == 5 and res.ndof == ref.ndof


def test_fit_hypothesis_recovers_truth_on_clean_spectrum():
    ctx = _spectrum_ctx()
    hyp = _by_label(ctx)["elastic+stokes+bg"]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    assert abs(res.omega0 - 5.0) < 0.1
    assert res.optimizer_converged


def test_fit_hypothesis_recovers_truth_with_unequal_exposure():
    base = _spectrum_ctx()
    scale = np.linspace(0.35, 2.4, base.x.size)
    exposed = ScanContext(
        x=base.x, counts=base.counts * scale, mask=base.mask, T=base.T, center=base.center,
        gauss_fwhm=base.gauss_fwhm, lorentz_fwhm=base.lorentz_fwhm,
        instrument=base.instrument, energy_status=base.energy_status, exposure_scale=scale)
    hyp = _by_label(exposed)["elastic+stokes+bg"]
    result = loop.fit_hypothesis(hyp, exposed, minos=False)
    assert result.optimizer_converged
    assert abs(result.omega0 - 5.0) < 0.1
    assert abs(result.gamma - 0.45) < 0.1
    assert np.array_equal(exposed.counts, base.counts * scale)


def test_result_separates_convergence_from_covariance():
    """A converged fit whose HESSE covariance failed must stay usable (review F2)."""
    comp = CompositeModel([PhononDHO(), ConstantBackground()])  # k = 4
    theta = np.array([4.0, 0.4, 300.0, 10.0])
    br = BackendResult(x=theta, success=True, message="converged", n_eval=10,
                       covariance=None, covariance_ok=False, at_limit={}, merrors=None)
    res = loop.result_from_backend("stokes+bg", comp, lambda th: 5.0, n_used=20, k=4, br=br)
    assert res.optimizer_converged is True
    assert res.covariance_ok is False
    assert res.profile_ok is False
    assert res.well_constrained is False        # strict Phase-1 rule needs covariance
    assert res.analysis_usable is True          # but it is NOT discarded
    assert np.isnan(res.omega0_err_hesse)
    assert np.isclose(res.omega0, np.sqrt(4.0**2 + 0.4**2))
    assert res.ndof == 16


def test_fit_admissible_structures_emits_both_then_offwindow_one():
    both = loop.fit_admissible_structures(_spectrum_ctx(), minos=False)
    assert {r.label for r in both} == {"stokes+bg", "elastic+stokes+bg"}

    x = np.arange(1.0, 21.5, 0.5)
    counts = convolute_voigt_dho(x, 4.0, 0.0, 0.6, 0.0, 0.3, 300.0, 0.0, 20.0, 290.0, peak="all")
    off = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=290.0,
                      center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0, instrument="IN8",
                      energy_status="absolute")
    res = loop.fit_admissible_structures(off, minos=False)
    assert [r.label for r in res] == ["stokes+bg"]


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121, seed=None):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = p1 + p2 + bg
    if seed is not None:
        counts = np.random.default_rng(seed).poisson(np.clip(counts, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _one_peak_ctx(o1=5.0, a1=500.0, g=0.4, bg=8.0, n=81, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    counts = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + bg
    if seed is not None:
        counts = np.random.default_rng(seed).poisson(np.clip(counts, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_gate_fires_on_two_peak_residual_not_on_clean_single():
    two = _two_peak_ctx()
    one_results = loop.fit_admissible_structures(two, minos=False)
    assert loop._offer_multimode(one_results, two) is True     # one DHO can't fit two peaks

    single = _one_peak_ctx()
    ok_results = loop.fit_admissible_structures(single, minos=False)
    assert loop._offer_multimode(ok_results, single) is False  # a single DHO fits fine


def test_two_stage_adds_multimode_when_gate_fires():
    ctx = _two_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False)
    labels = {r.label for r in results}
    assert "stokes+bg" in labels                                # 1-mode set always present
    assert "stokes2+bg" in labels                               # escalated on the gate
    two = {r.label: r for r in results}["stokes2+bg"]
    assert two.n_modes == 2 and len(two.modes) == 2
    # modes canonicalized ascending by ω₀ and bracket the two true peaks (~4 and ~10 meV)
    o = [m["omega0"] for m in two.modes]
    assert o == sorted(o)
    assert abs(o[0] - 4.0) < 1.0 and abs(o[1] - 10.0) < 1.0


def test_two_stage_single_peak_stays_one_mode():
    ctx = _one_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False)
    # gate did not fire → no 2-mode ("stokes2…") hypothesis was offered
    assert not any("stokes2" in r.label for r in results)
    assert all(r.n_modes == 1 for r in results)


def test_two_stage_offers_multimode_when_two_peaks_declared():
    """Even a clean single-peak scan gets the 2-mode set offered when ≥2 peaks are declared
    (the driver-supplied ``peaks`` hint); AIC is then left to reject the extra mode."""
    ctx = _one_peak_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]
    results = loop.fit_structures_two_stage(ctx, minos=False, peaks=peaks)
    assert "stokes2+bg" in {r.label for r in results}


def _scientific_signature(results):
    """Fields whose order/value must not depend on candidate scheduling."""
    return [(r.label, r.optimizer_converged, r.cost, r.k, r.params, r.modes)
            for r in results]


def test_two_stage_parallel_candidates_are_ordered_and_identical():
    from concurrent.futures import ProcessPoolExecutor

    ctx = _one_peak_ctx()
    serial = loop.fit_structures_two_stage(ctx, minos=False)
    with ProcessPoolExecutor(max_workers=2) as executor:
        parallel = loop.fit_structures_two_stage(ctx, minos=False, executor=executor)
    assert _scientific_signature(parallel) == _scientific_signature(serial)


def test_candidate_pool_failure_logs_and_retries_serially(caplog):
    class BrokenExecutor:
        def map(self, *_args, **_kwargs):
            raise RuntimeError("worker vanished")

    ctx = _one_peak_ctx()
    expected = loop.fit_structures_two_stage(ctx, minos=False)
    actual = loop.fit_structures_two_stage(ctx, minos=False, executor=BrokenExecutor())
    assert _scientific_signature(actual) == _scientific_signature(expected)
    assert "retrying serially" in caplog.text


def test_error_paths_reuse_candidate_executor_and_preserve_pairing(monkeypatch):
    from isar.analyze import errors

    class InlineExecutor:
        def map(self, fn, items):
            return map(fn, items)

    executor = InlineExecutor()
    calls = []

    def _capture(items, **kwargs):
        calls.append((list(items), kwargs))

    monkeypatch.setattr(errors, "attach_batch", _capture)
    admissible = loop.fit_admissible_structures(
        _one_peak_ctx(), errors=True, M=12, seed=4, executor=executor)
    two_stage = loop.fit_structures_two_stage(
        _one_peak_ctx(), with_errors=True, M=12, seed=4, executor=executor)

    assert len(calls) == 2
    for results, (items, kwargs) in zip((admissible, two_stage), calls):
        assert [result for result, _hyp, _ctx in items] == results
        assert [result.label for result, hyp, _ctx in items] == [hyp.label for _, hyp, _ in items]
        assert kwargs["executor"] is executor
        assert kwargs["boot_workers"] >= 2


# --- FIX 1: greedy-IC "seed robustness" was really a MIGRAD call-budget starvation ----------

def _sparse_two_peak_ctx(seed=208, o1=5.7, o2=7.9, a1=12.0, a2=8.0, g=0.5, bg=0.05, n=44):
    """A sparse (many-zero-count) wide-window two-peak scan: a strong 'optic' + a weaker
    'acoustic', mirroring the closed-loop regime where the k=7 two-mode fit is near-degenerate
    and MIGRAD needs more than iminuit's default call budget to certify a minimum."""
    x = np.linspace(-4.0, 11.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.37, 0.0, g, a1, 0.0, 0.0, 40.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.37, 0.0, g, a2, 0.0, 0.0, 40.0, peak="stokes+astokes")
    lam = np.clip(p1 + p2 + bg, 0.0, None)
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=40.0,
                       center=0.0, gauss_fwhm=1.37, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_migrad_budget_exceeds_iminuit_default():
    """The raised budget is strictly greater than iminuit's own default (200+100·npar+5·npar²)
    for every parameter count, so a fit that already converges inside the default is untouched
    while the k≥7 fits get the head-room they were being starved of."""
    from isar.analyze import backends
    for npar in (4, 5, 7, 8, 10):
        default = 200 + 100 * npar + 5 * npar * npar
        assert backends._migrad_ncall(npar) > default


def test_call_budget_starvation_drops_two_mode_that_raised_budget_certifies(monkeypatch):
    """Reproduce-then-fix: on the SAME data and SAME greedy seed, a starved MIGRAD budget leaves
    the k=7 two-mode fit uncertified (has_reached_call_limit → not valid → dropped by selection);
    the raised budget lets the identical fit converge to the identical minimum and recover the
    hard (weaker) secondary peak. The greedy seed is fine either way — the failure was the fit
    being cut short, not the seed."""
    from isar.analyze import phonon, select
    from isar.analyze.phonon import enumerate_phonon_multimode
    from isar.analyze import backends

    ctx = _sparse_two_peak_ctx()
    hyp2 = {h.label: h for h in enumerate_phonon_multimode(ctx)}["stokes2+bg"]
    one = loop.fit_hypothesis(_by_label(ctx)["stokes+bg"], ctx)
    x0 = phonon.greedy_multimode_ic(hyp2.build(), ctx, one)

    # starved budget → the fit hits the call limit and is NOT certified as converged.
    monkeypatch.setattr(backends, "_migrad_ncall", lambda npar: 40)
    starved = loop.fit_hypothesis(hyp2, ctx, x0=x0)
    assert starved.optimizer_converged is False
    monkeypatch.undo()

    # raised (real) budget → the identical fit certifies and recovers both peaks.
    fixed = loop.fit_hypothesis(hyp2, ctx, x0=x0)
    assert fixed.optimizer_converged is True
    o = sorted(m["omega0"] for m in fixed.modes)
    assert abs(o[0] - 5.7) < 0.2 and abs(o[1] - 7.9) < 0.3
    assert fixed.cost < starved.cost                       # the certified minimum is the better one

    # end-to-end: selection now picks a two-mode structure.
    sel = select.select_models(loop.fit_structures_two_stage(ctx, minos=False))
    assert sel.best is not None and "stokes2" in sel.best


def test_sparse_two_peak_selects_two_mode_and_single_peak_stays_one_mode():
    """No overfitting regression: the fix does not turn a genuine single peak into two modes."""
    from isar.analyze import select
    two = select.select_models(loop.fit_structures_two_stage(_sparse_two_peak_ctx(), minos=False))
    assert "stokes2" in (two.best or "")
    single = select.select_models(loop.fit_structures_two_stage(_one_peak_ctx(), minos=False))
    assert single.best is not None and "stokes2" not in single.best


# --- FIX 2: a phonon Γ railed out to its bound → the ω₀ interval is flagged unreliable -------

def test_gamma_at_bound_helper():
    assert loop._gamma_at_bound([0.0011, 0.5]) is True       # near the lower floor
    assert loop._gamma_at_bound([loop.GAMMA_MAX * 0.9999]) is True   # runaway broad
    assert loop._gamma_at_bound([0.05, 0.3]) is False        # genuinely fitted widths


def test_result_flags_gamma_at_bound():
    """A converged fit with a phonon Γ pinned at the floor carries ``gamma_at_bound`` so the
    driver can flag its ω₀ interval unreliable (the degenerate-bootstrap case)."""
    comp = CompositeModel([PhononDHO(), ConstantBackground()])  # (Omega_d, Gamma, A, BG)
    theta = np.array([5.0, loop.GAMMA_FLOOR, 300.0, 10.0])
    br = BackendResult(x=theta, success=True, message="ok", n_eval=10,
                       covariance=None, covariance_ok=False, at_limit={}, merrors=None)
    res = loop.result_from_backend("stokes+bg", comp, lambda th: 5.0, n_used=20, k=4, br=br)
    assert res.gamma_at_bound is True

    theta2 = np.array([5.0, 0.4, 300.0, 10.0])                 # a real width
    br2 = BackendResult(x=theta2, success=True, message="ok", n_eval=10,
                        covariance=None, covariance_ok=False, at_limit={}, merrors=None)
    res2 = loop.result_from_backend("stokes+bg", comp, lambda th: 5.0, n_used=20, k=4, br=br2)
    assert res2.gamma_at_bound is False

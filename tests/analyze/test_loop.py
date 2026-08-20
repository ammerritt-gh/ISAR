"""The structure loop: per-hypothesis problem builder + fit + StructureFitResult.

Key guarantees: each hypothesis is a real model variant (its own k/ndof, review F1); the
full elastic+stokes+bg fit reproduces the frozen synth.fit(); and the result separates
optimizer convergence from error availability (review F2) so step-3 bootstrap is not
starved of converged-but-covariance-less fits.
"""
import logging
from dataclasses import replace

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
    # H4: same model family, different amplitude parameterization (intrinsic vs measured), so
    # the optima coincide physically but the optimizer path differs — tolerances reflect that.
    assert np.isclose(res.omega0, ref.omega0, rtol=1e-4)
    assert np.isclose(res.gamma, ref.gamma, rtol=1e-3)
    assert res.cost < 1e-3 and ref.cost < 1e-3          # both essentially perfect on clean data
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
    assert {r.label for r in both} == {"stokes+bg", "elastic+stokes+bg", "elastic+bg", "bg"}

    x = np.arange(1.0, 21.5, 0.5)
    counts = convolute_voigt_dho(x, 4.0, 0.0, 0.6, 0.0, 0.3, 300.0, 0.0, 20.0, 290.0, peak="all")
    off = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=290.0,
                      center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0, instrument="IN8",
                      energy_status="absolute")
    res = loop.fit_admissible_structures(off, minos=False)
    assert [r.label for r in res] == ["stokes+bg", "bg"]


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
    assert all(r.n_modes <= 1 for r in results)      # phonon-bearing: 1 mode; nulls: 0


def _between_threshold_ctx(a2=2.0, seed=2):
    """A weak second peak whose residual structure sits strictly BETWEEN the relaxed
    (``GATE_COST_DECLARED``/``RUNS_ALPHA_DECLARED``) and global (``GATE_COST``/``RUNS_Z``)
    thresholds: cost/dof ≈ 1.37 clears the relaxed 1.3 but not the global 2.0. Picked by search
    against the fixed seed; not physically special."""
    x = np.linspace(-6.0, 20.0, 121)
    p1 = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                             peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, 11.0, 0.0, 1.0, 0.0, 0.4, a2, 0.0, 0.0, 50.0,
                             peak="stokes+astokes")
    counts = np.random.default_rng(seed).poisson(np.clip(p1 + p2 + 8.0, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_declared_scan_between_thresholds_is_offered_identical_undeclared_is_not():
    """Ruling H8(a), 2026-08-09: a declaration RELAXES the offering diagnostic, it
    never bypasses it. A scan whose one-mode residual structure clears the relaxed thresholds but
    not the global ones is offered the 2-mode set only when a ≥2-peak count is declared."""
    ctx = _between_threshold_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]

    undeclared = loop.fit_structures_two_stage(ctx, minos=False)
    assert "stokes2+bg" not in {r.label for r in undeclared}

    route_out = {}
    declared = loop.fit_structures_two_stage(
        ctx, minos=False, peaks=peaks, route_out=route_out)
    assert "stokes2+bg" in {r.label for r in declared}
    assert route_out["multimode_route"] == "declaration_relaxed"


def test_declared_scan_offered_by_relaxation_still_loses_to_one_mode_on_evidence():
    """The relaxed offer is not a forced win: a single-peak scan whose noise happens to clear the
    relaxed cost/dof threshold (but not the global one) still has its offered 2-mode candidate
    lose to the 1-mode structure on AIC — evidence, not the declaration, decides."""
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    counts = np.random.default_rng(51).poisson(np.clip(counts, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]

    undeclared = loop.fit_structures_two_stage(ctx, minos=False)
    assert "stokes2+bg" not in {r.label for r in undeclared}   # global gate does not fire

    route_out = {}
    results = loop.fit_structures_two_stage(
        ctx, minos=False, peaks=peaks, route_out=route_out)
    assert route_out["multimode_route"] == "declaration_relaxed"
    by_label = {r.label: r for r in results}
    one = by_label["stokes+bg"]
    two = by_label["stokes2+bg"]
    assert one.optimizer_converged and two.optimizer_converged
    assert (one.cost + 2.0 * one.k) < (two.cost + 2.0 * two.k)   # one-mode AIC-preferred


def test_declared_clean_one_mode_scan_is_not_offered():
    """Natural abstention: a declared scan whose one-mode fit is clean under BOTH relaxed
    diagnostics is still not offered — the declaration permits, it does not force."""
    ctx = _one_peak_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]
    route_out = {}
    results = loop.fit_structures_two_stage(
        ctx, minos=False, peaks=peaks, route_out=route_out)
    assert "stokes2+bg" not in {r.label for r in results}
    assert route_out["multimode_route"] == "not_offered"


def test_declared_scan_offered_by_global_triggers_keeps_ordinary_route():
    """A declared scan the GLOBAL thresholds would already have offered persists the ordinary
    ``residual_triggered`` route, not ``declaration_relaxed`` — the route is re-derived from the
    gate outcome, never from ``peaks`` presence."""
    ctx = _two_peak_ctx()
    peaks = [{"omega0": 4.0}, {"omega0": 10.0}]
    route_out = {}
    results = loop.fit_structures_two_stage(
        ctx, minos=False, peaks=peaks, route_out=route_out)
    assert "stokes2+bg" in {r.label for r in results}
    assert route_out["multimode_route"] == "residual_triggered"


def test_fit_shadow_candidates_reports_same_route_vocabulary():
    """`fit_shadow_candidates` (the STAT-2a shadow route) applies the identical declared-count
    relaxation and route provenance as the production two-stage path — the diagnostic is one
    function regardless of which caller runs it."""
    ctx = _between_threshold_ctx()
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]

    undeclared_bundle = loop.fit_shadow_candidates(ctx)
    assert undeclared_bundle.multimode_route == "not_offered"
    assert "stokes2+bg" not in set(undeclared_bundle.offered_labels)

    declared_bundle = loop.fit_shadow_candidates(ctx, peaks=peaks)
    assert declared_bundle.multimode_route == "declaration_relaxed"
    assert "stokes2+bg" in set(declared_bundle.offered_labels)


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


# --- catalogue constrained variants as additional candidates (SCENE-1 PR-c) -------------------
def _near_degenerate_ctx(n=81):
    """A phonon whose ω₀ sits below the resolution width — the near-degenerate case."""
    x = np.linspace(-6.0, 6.0, n)
    counts = convolute_voigt_dho(x, 0.6, 0.0, 0.9, 0.0, 0.3, 300.0, 60.0, 12.0, 150.0,
                                 peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=150.0,
                       center=0.0, gauss_fwhm=0.9, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_near_degenerate_variant_is_offered_as_an_additional_twin():
    """A converged one-mode fit with ω̂ ≤ the resolution width earns a pinned twin candidate: same
    components, ``_neardeg`` label, Γ frozen at the floor, one free parameter fewer. The
    unconstrained parent stays in the set — the variant is an ADDITIONAL candidate, not a
    replacement (2a ruling 2)."""
    from isar.analyze.phonon import GAMMA_FLOOR

    ctx = _near_degenerate_ctx()
    results = {r.label: r for r in loop.fit_structures_two_stage(ctx, minos=False)}
    assert "stokes+bg" in results
    twin = results["stokes+bg_neardeg"]

    assert twin.variant == "near_degenerate"
    assert twin.fit_kind == "branch_conditional"           # the existing hard-fixed provenance tag
    assert twin.optimizer_converged
    assert twin.k == results["stokes+bg"].k - 1
    assert twin.params["phonon.Gamma"] == GAMMA_FLOOR
    assert results["stokes+bg"].variant is None


def test_no_variant_offered_when_the_mode_is_resolved():
    """ω₀ well above the resolution width: the near-degenerate special case is not a rival, so
    nothing is offered and the candidate set is unchanged."""
    ctx = _spectrum_ctx()                                   # ω₀ = 5 meV, gauss_fwhm = 1.1
    results = loop.fit_structures_two_stage(ctx, minos=False)
    assert not any(r.label.endswith("_neardeg") for r in results)
    assert all(r.variant is None for r in results)


def test_variant_semantics_are_ineligible_and_writable():
    """The pin is recorded as an effective HARD constraint of origin ``catalogue_variant``, which
    makes the candidate selection-ineligible — and the record must survive the write-time
    invariant (eligible ⇒ all-soft-prespecified) rather than crash in it."""
    from isar.schema.statistics import FIT_SELECTION_SCHEMA, validate_for_write

    ctx = _near_degenerate_ctx()
    results = {r.label: r for r in loop.fit_structures_two_stage(ctx, minos=False)}
    semantics = results["stokes+bg_neardeg"].fit_semantics

    assert semantics is not None
    assert [(c["parameter"], c["constraint_kind"], c["origin"])
            for c in semantics["effective_constraints"]] == [
        ("phonon.Gamma", "hard", "catalogue_variant")]
    assert semantics["selection_eligibility"] == "ineligible_unapproved_conditioning"
    assert semantics["estimator_kind"] == "hard_conditioned_likelihood"
    assert validate_for_write(semantics, FIT_SELECTION_SCHEMA) == semantics


def test_variant_skipped_when_the_context_already_constrains_the_parameter(caplog):
    """The scan's own constraints win: the catalogue never overwrites a Γ the steering layer,
    a rescue, or the scientist already pinned. The variant is dropped, and says so."""
    from isar.schema.steering import ParamConstraints

    base = _near_degenerate_ctx()
    ctx = replace(base, constraints=ParamConstraints(
        hard={"phonon.Gamma": 0.25}, origins={"phonon.Gamma": "user_declared"}))
    with caplog.at_level(logging.INFO, logger="isar.analyze.loop"):
        results = loop.fit_structures_two_stage(ctx, minos=False)

    assert not any(r.label.endswith("_neardeg") for r in results)
    assert "already constrains phonon.Gamma" in caplog.text
    # …and the ordinary candidates still fit under their own pin, untouched.
    assert results[0].params["phonon.Gamma"] == 0.25

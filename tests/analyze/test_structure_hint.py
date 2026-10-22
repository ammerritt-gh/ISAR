"""Stage 6 (B2): the opt-in structure-skip shortcut in the analyze layer.

``fit_structures_two_stage_hinted`` fits only the hinted structure + a no-phonon null and accepts
the shortcut only when the hinted structure is still enough on its own. These tests pin the
accept path (fewer fits, ``structure_skipped=True``) and every fall-through (label mismatch, a
misfit, and the no-phonon-preferred case), and confirm the ``structure_hint=None`` default is
bit-for-bit the full count ladder. The ladder runs to an explicit ceiling of 2 here: the count is
not what these tests are about.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures
from isar.analyze.lineshapes import convolute_voigt_dho


def _one_peak_ctx(o1=5.0, a1=500.0, g=0.4, bg=8.0, n=81, seed=3):
    x = np.linspace(-8.0, 16.0, n)
    counts = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + bg
    if seed is not None:
        counts = np.random.default_rng(seed).poisson(np.clip(counts, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121, seed=7):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = np.random.default_rng(seed).poisson(np.clip(p1 + p2 + bg, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _flat_ctx(bg=30.0, n=61, seed=11):
    x = np.linspace(-8.0, 16.0, n)
    counts = np.random.default_rng(seed).poisson(np.full(n, bg)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _count_build_problem(monkeypatch):
    """Count every hypothesis assembled — one per fit_hypothesis call plus one for the null."""
    calls = {"n": 0}
    orig = loop.build_problem

    def wrapped(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(loop, "build_problem", wrapped)
    return calls


def test_hint_none_is_bit_identical_full_enumeration():
    ctx = _two_peak_ctx()
    full = loop.fit_structures_two_stage(ctx, minos=False, mode_ceiling=2)
    results, skipped = loop.fit_structures_two_stage_hinted(ctx, minos=False, structure_hint=None,
                                                            mode_ceiling=2)
    assert skipped is False
    assert [r.label for r in results] == [r.label for r in full]
    for a, b in zip(results, full):
        assert a.label == b.label
        assert np.isclose(a.cost, b.cost) and a.k == b.k


def test_accepted_hint_fits_only_hinted_structure_plus_null(monkeypatch):
    ctx = _one_peak_ctx()
    calls = _count_build_problem(monkeypatch)
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg")
    assert skipped is True
    assert [r.label for r in results] == ["stokes+bg"]          # ONLY the hinted structure
    assert calls["n"] == 2                                      # hinted structure + one null
    assert results[0].optimizer_converged


def test_declared_hint_between_thresholds_rejects_shortcut_for_the_full_ladder():
    """The declared-count relaxation still applies at the structure-hint shortcut (2026-08-09
    ruling): a declared scan whose hinted one-mode fit clears the RELAXED thresholds (but not the
    global ones) rejects the shortcut and runs the full count ladder. The identical undeclared
    hint accepts the shortcut."""
    x = np.linspace(-6.0, 20.0, 121)
    p1 = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                             peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, 11.0, 0.0, 1.0, 0.0, 0.4, 2.0, 0.0, 0.0, 50.0,
                             peak="stokes+astokes")
    counts = np.random.default_rng(2).poisson(np.clip(p1 + p2 + 8.0, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    peaks = [{"omega0": 5.0}, {"omega0": 11.0}]

    route_out = {}
    declared_results, declared_skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, peaks=peaks, structure_hint="stokes+bg", route_out=route_out,
        mode_ceiling=2)
    assert declared_skipped is False                          # shortcut rejected
    assert "stokes2+bg" in {r.label for r in declared_results}  # the full ladder ran
    assert route_out["multimode_route"] == "count_ladder"

    route_out_undeclared = {}
    undeclared_results, undeclared_skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg", route_out=route_out_undeclared)
    assert undeclared_skipped is True                         # shortcut accepted, no declaration
    assert [r.label for r in undeclared_results] == ["stokes+bg"]
    assert route_out_undeclared["multimode_route"] == "not_evaluated_structure_hint"


def test_gate_failure_falls_through_to_full(monkeypatch):
    # A two-peak spectrum hinted with the 1-mode "stokes+bg": one DHO leaves a structured residual
    # → _hint_insufficient fires → the hint is discarded and the full count ladder runs.
    ctx = _two_peak_ctx()
    calls = _count_build_problem(monkeypatch)
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg", mode_ceiling=2)
    assert skipped is False
    labels = {r.label for r in results}
    assert "stokes2+bg" in labels                              # escalated to the 2-mode set
    assert calls["n"] > 2                                      # full enumeration, not the shortcut


def test_label_mismatch_falls_through_to_full():
    ctx = _one_peak_ctx()
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+qe+bg",       # not admissible (no QE width)
        mode_ceiling=2)
    assert skipped is False
    assert {r.label for r in results} == {
        h.label for h in (*enumerate_phonon_structures(ctx), *enumerate_phonon_multimode(ctx, 2))}


def test_null_preferred_falls_through_to_full():
    # Flat, signal-free data: the phonon buys no explanatory power over the no-phonon null, so the
    # null is AIC-preferred and the shortcut is refused (the "master no longer has a phonon" guard).
    ctx = _flat_ctx()
    r = loop.fit_hypothesis({h.label: h for h in enumerate_phonon_structures(ctx)}["stokes+bg"], ctx)
    assert not loop._hint_insufficient(r, ctx)                 # GoF check itself would PASS on flat data
    _results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg", mode_ceiling=2)
    assert skipped is False                                    # …but the null gate still refuses it


def test_complete_shadow_bundle_aliases_existing_results():
    ctx = _one_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False, mode_ceiling=2)

    bundle = loop.shadow_bundle_from_complete_results(results)

    assert bundle.results is results
    assert all(a is b for a, b in zip(bundle.results, results))
    assert bundle.reused_labels == [result.label for result in results]
    assert bundle.freshly_fitted_labels == []


def test_accepted_hint_is_reused_but_cannot_suppress_shadow_alternatives():
    ctx = _one_peak_ctx()
    hinted, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg")
    assert skipped is True

    bundle = loop.fit_shadow_candidates(ctx, reusable_results=hinted, mode_ceiling=2)

    assert set(bundle.offered_labels) == {
        hypothesis.label
        for hypothesis in (*enumerate_phonon_structures(ctx), *enumerate_phonon_multimode(ctx, 2))
    }
    assert "stokes+bg" in bundle.reused_labels
    assert set(bundle.freshly_fitted_labels) == (
        set(bundle.attempted_labels) - set(bundle.reused_labels)
    )
    assert next(
        result for result in bundle.results if result.label == "stokes+bg"
    ) is hinted[0]

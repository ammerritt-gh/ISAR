"""Stage 6 (B2): the opt-in structure-skip shortcut in the analyze layer.

``fit_structures_two_stage_hinted`` fits only the hinted structure + a no-phonon null and accepts
the shortcut only when the same gates the full enumeration relies on still pass. These tests pin the
accept path (fewer fits, ``structure_skipped=True``) and every fall-through (label mismatch, a
gate-failing misfit, and the no-phonon-preferred case), and confirm the ``structure_hint=None``
default is bit-for-bit the full enumeration.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_structures
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
    full = loop.fit_structures_two_stage(ctx, minos=False)
    results, skipped = loop.fit_structures_two_stage_hinted(ctx, minos=False, structure_hint=None)
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


def test_gate_failure_falls_through_to_full(monkeypatch):
    # A two-peak spectrum hinted with the 1-mode "stokes+bg": one DHO leaves a structured residual
    # → _offer_multimode fires → the hint is discarded and the full two-stage enumeration runs.
    ctx = _two_peak_ctx()
    calls = _count_build_problem(monkeypatch)
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg")
    assert skipped is False
    labels = {r.label for r in results}
    assert "stokes2+bg" in labels                              # escalated to the 2-mode set
    assert calls["n"] > 2                                      # full enumeration, not the shortcut


def test_label_mismatch_falls_through_to_full():
    ctx = _one_peak_ctx()
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+qe+bg")       # not admissible (no QE width)
    assert skipped is False
    assert {r.label for r in results} == {h.label for h in enumerate_phonon_structures(ctx)}


def test_null_preferred_falls_through_to_full():
    # Flat, signal-free data: the phonon buys no explanatory power over the no-phonon null, so the
    # null is AIC-preferred and the shortcut is refused (the "master no longer has a phonon" guard).
    ctx = _flat_ctx()
    r = loop.fit_hypothesis({h.label: h for h in enumerate_phonon_structures(ctx)}["stokes+bg"], ctx)
    assert not loop._offer_multimode([r], ctx)                 # GoF gate itself would PASS on flat data
    _results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg")
    assert skipped is False                                    # …but the null gate still refuses it


def test_complete_shadow_bundle_aliases_existing_results():
    ctx = _one_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False)

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

    bundle = loop.fit_shadow_candidates(ctx, reusable_results=hinted)

    assert set(bundle.offered_labels) == {
        hypothesis.label for hypothesis in enumerate_phonon_structures(ctx)
    }
    assert "stokes+bg" in bundle.reused_labels
    assert set(bundle.freshly_fitted_labels) == (
        set(bundle.attempted_labels) - set(bundle.reused_labels)
    )
    assert next(
        result for result in bundle.results if result.label == "stokes+bg"
    ) is hinted[0]

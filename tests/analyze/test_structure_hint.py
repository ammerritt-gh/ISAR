"""Stage 6 (B2): the opt-in structure hint in the analyze layer.

``fit_structures_two_stage_hinted`` takes a previous master's winning label as a *seed source*: the
hinted structure gets one extra cold start on its own count and variant, like a declared peak does,
and the count ladder runs every count regardless. Nothing the hint says can skip a count (a hint
that skipped the ladder missed a real extra mode in a refitted master). These tests pin that, and
that ``structure_hint=None`` is bit-for-bit the plain ladder. The ladder runs to an explicit
ceiling of 2 here: the count is not what these tests are about.
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


def _hint_sources(record):
    return {model.label: [s for s in model.starts if s.source == "hint"]
            for model in record.models}


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


def test_a_hint_the_old_shortcut_accepted_still_runs_every_count():
    # A clean one-peak spectrum: the old shortcut accepted "stokes+bg" (it passed the residual
    # checks and beat the null) and never tried a second mode.
    ctx = _one_peak_ctx()
    rung1_out, route_out = {}, {}
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg", rung1_out=rung1_out, route_out=route_out,
        mode_ceiling=2)
    assert skipped is False
    assert route_out["multimode_route"] == "count_ladder"
    assert {model.count for model in rung1_out["rung1"].models} == {0, 1, 2}
    assert {r.label for r in results} == {
        h.label for h in (*enumerate_phonon_structures(ctx), *enumerate_phonon_multimode(ctx, 2))}


def test_the_hinted_structure_is_a_seed_source_on_its_own_label_only():
    ctx = _two_peak_ctx()
    rung1_out = {}
    loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes2+bg", rung1_out=rung1_out, mode_ceiling=2)
    hinted = _hint_sources(rung1_out["rung1"])
    assert len(hinted["stokes2+bg"]) == 1
    assert [label for label, starts in hinted.items() if starts] == ["stokes2+bg"]


def test_a_declared_hinted_scan_gets_both_seed_sources_and_the_ladder():
    ctx = _two_peak_ctx()
    rung1_out, route_out = {}, {}
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, peaks=[{"omega0": 3.5}, {"omega0": 11.0}], structure_hint="stokes+bg",
        rung1_out=rung1_out, route_out=route_out, mode_ceiling=2)
    assert skipped is False
    assert route_out["multimode_route"] == "count_ladder"
    assert "stokes2+bg" in {r.label for r in results}
    two = rung1_out["rung1"].by_label()["stokes2+bg"]
    assert two.seeds_by_source.get("declared") == 1


def test_label_mismatch_adds_no_hint_start():
    ctx = _one_peak_ctx()
    rung1_out = {}
    results, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+qe+bg",       # not admissible (no QE width)
        rung1_out=rung1_out, mode_ceiling=2)
    assert skipped is False
    assert not any(_hint_sources(rung1_out["rung1"]).values())
    assert {r.label for r in results} == {
        h.label for h in (*enumerate_phonon_structures(ctx), *enumerate_phonon_multimode(ctx, 2))}


def test_complete_shadow_bundle_aliases_existing_results():
    ctx = _one_peak_ctx()
    results = loop.fit_structures_two_stage(ctx, minos=False, mode_ceiling=2)

    bundle = loop.shadow_bundle_from_complete_results(results)

    assert bundle.results is results
    assert all(a is b for a, b in zip(bundle.results, results))
    assert bundle.reused_labels == [result.label for result in results]
    assert bundle.freshly_fitted_labels == []


def test_a_hinted_result_is_reused_but_cannot_suppress_shadow_alternatives():
    ctx = _one_peak_ctx()
    hinted, skipped = loop.fit_structures_two_stage_hinted(
        ctx, minos=False, structure_hint="stokes+bg", mode_ceiling=2)
    assert skipped is False
    one = next(r for r in hinted if r.label == "stokes+bg")

    bundle = loop.fit_shadow_candidates(ctx, reusable_results=[one], mode_ceiling=2)

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
    ) is one

"""Structure-aware initial conditions: grid+NNLS generalized to a CompositeModel.

The load-bearing test is that, for the full elastic+stokes+bg composite, phonon_structure_ic
reproduces the frozen `fit.initial_guess` exactly — proving the generalization is faithful.
"""
import numpy as np

from isar.analyze import phonon
from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic,
                                     PhononDHO)
from isar.analyze.context import ScanContext
from isar.synth import fit
from isar.analyze.lineshapes import convolute_voigt_dho


def _spectrum_ctx():
    x = np.linspace(-10.0, 10.0, 41)
    counts = convolute_voigt_dho(x, 5.0, -0.5, 1.2, 0.0, 0.4, 400.0, 80.0, 15.0, 200.0,
                                 peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=200.0,
                       center=-0.5, gauss_fwhm=1.2, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_structure_ic_matches_initial_guess_for_full_model():
    ctx = _spectrum_ctx()
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    got = phonon.phonon_structure_ic(comp, ctx)
    expected = fit.initial_guess(ctx.x, ctx.counts, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                 ctx.T, center=ctx.center, x_max=float(np.max(ctx.x)))
    # H4: the production IC's phonon amplitude is INTRINSIC; the frozen synth initial_guess
    # stays measured. Identical up to the occupation factor at the IC's (Omega_d, GAMMA0).
    expected = np.asarray(expected, dtype=float).copy()
    expected[2] /= phonon.bose_occupation(phonon.omega0_from(expected[0], expected[1]), ctx.T)
    assert np.allclose(got, expected)


def test_structure_ic_stokes_only_has_no_elastic_param():
    ctx = _spectrum_ctx()
    comp = CompositeModel([PhononDHO(), ConstantBackground()])
    got = phonon.phonon_structure_ic(comp, ctx)
    assert got.shape == (4,)              # Omega_d, Gamma, A, BG — no elastic
    od, gamma, A, BG = got
    assert 0.0 < od <= float(np.max(ctx.x))
    assert A >= 0.0 and BG >= 0.0


def test_omega_d_grid_full_vs_prior_narrowed():
    x = np.linspace(-10.0, 10.0, 41)
    full = phonon.omega_d_grid(x, 10.0)
    assert full.min() < 1.0 and full.max() > 8.0
    narrowed = phonon.omega_d_grid(x, 10.0, prior_omega=5.0)
    assert narrowed.min() >= 3.4 and narrowed.max() <= 6.6


def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = p1 + p2 + bg
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_greedy_multimode_ic_recovers_two_seeds_on_double_peak():
    """On a synthetic two-peak spectrum, freezing mode-1 at the 1-mode fit and grid-scanning
    mode-2 places the two seeds near the two true peaks (ω₀ ≈ 4 and 10 meV)."""
    from isar.analyze import loop
    from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures
    ctx = _two_peak_ctx()
    one = loop.fit_hypothesis({h.label: h for h in enumerate_phonon_structures(ctx)}["stokes+bg"], ctx)
    comp = {h.label: h for h in enumerate_phonon_multimode(ctx)}["stokes2+bg"].build()
    seed = phonon.greedy_multimode_ic(comp, ctx, one)
    o1 = phonon.omega0_from(seed["phonon.Omega_d"], seed["phonon.Gamma"])
    o2 = phonon.omega0_from(seed["phonon2.Omega_d"], seed["phonon2.Gamma"])
    seeds = sorted([o1, o2])
    assert abs(seeds[0] - 4.0) < 1.5
    assert abs(seeds[1] - 10.0) < 1.5
    # identifiability: the two seeds are separated by more than a resolution FWHM
    assert abs(seeds[1] - seeds[0]) > ctx.gauss_fwhm


def test_greedy_multimode_ic_seeds_mode1_when_one_mode_result_is_none():
    """With no 1-mode result supplied, mode-1 is seeded internally (phonon_structure_ic) and mode-2
    still lands on the second peak — the seed is a full dict over all params."""
    ctx = _two_peak_ctx()
    from isar.analyze.phonon import enumerate_phonon_multimode
    comp = {h.label: h for h in enumerate_phonon_multimode(ctx)}["stokes2+bg"].build()
    seed = phonon.greedy_multimode_ic(comp, ctx, None)
    assert set(seed) == set(comp.param_names())
    seeds = sorted([phonon.omega0_from(seed["phonon.Omega_d"], seed["phonon.Gamma"]),
                    phonon.omega0_from(seed["phonon2.Omega_d"], seed["phonon2.Gamma"])])
    assert abs(seeds[0] - 4.0) < 1.6 and abs(seeds[1] - 10.0) < 1.6

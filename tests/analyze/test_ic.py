"""Structure-aware initial conditions: grid+NNLS generalized to a CompositeModel.

The load-bearing test is that, for the full elastic+stokes+bg composite, structure_ic
reproduces the frozen `fit.initial_guess` exactly — proving the generalization is faithful.
"""
import numpy as np

from isar.analyze import ic
from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic,
                                     PhononDHO)
from isar.analyze.context import ScanContext
from isar.synth import fit
from isar.synth._ff import convolute_voigt_dho


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
    got = ic.structure_ic(comp, ctx)
    expected = fit.initial_guess(ctx.x, ctx.counts, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                 ctx.T, center=ctx.center, x_max=float(np.max(ctx.x)))
    assert np.allclose(got, expected)


def test_structure_ic_stokes_only_has_no_elastic_param():
    ctx = _spectrum_ctx()
    comp = CompositeModel([PhononDHO(), ConstantBackground()])
    got = ic.structure_ic(comp, ctx)
    assert got.shape == (4,)              # Omega_d, Gamma, A, BG — no elastic
    od, gamma, A, BG = got
    assert 0.0 < od <= float(np.max(ctx.x))
    assert A >= 0.0 and BG >= 0.0


def test_omega_d_grid_full_vs_prior_narrowed():
    x = np.linspace(-10.0, 10.0, 41)
    full = ic.omega_d_grid(x, 10.0)
    assert full.min() < 1.0 and full.max() > 8.0
    narrowed = ic.omega_d_grid(x, 10.0, prior_omega=5.0)
    assert narrowed.min() >= 3.4 and narrowed.max() <= 6.6

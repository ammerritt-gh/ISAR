"""Step-4 computed confidence (§9): a [0,1] number built from exposed sub-scores — ω agreement
across structures, interval tightness, cross-paradigm regime, distance from the prior, and
provenance. Tested by its monotonicity in each input (the spec gives inputs, not a formula)."""
import math
import types

import numpy as np

from isar.analyze import confidence, loop, select
from isar.analyze.context import ScanContext
from isar.analyze.prior import PhononPrior


def _ctx(energy_status="absolute"):
    x = np.linspace(-10.0, 10.0, 61)
    return ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status=energy_status)


def _result(omega0=6.0, *, freq_hw=0.05, regime="asymptotic", cost_per_dof=2.0):
    lo, hi = omega0 - freq_hw, omega0 + freq_hw
    wm = {"freq_interval": (lo, omega0, hi), "agreement": types.SimpleNamespace(regime=regime)}
    params = {"phonon.Omega_d": omega0, "phonon.Gamma": 0.3, "phonon.A": 300.0,
              "elastic.E": 50.0, "bg.BG": 10.0}
    return loop.StructureFitResult(
        label="elastic+stokes+bg", k=5, ndof=36, cost=cost_per_dof * 36,
        cost_per_dof=cost_per_dof, optimizer_converged=True, covariance_ok=True, profile_ok=True,
        well_constrained=True, analysis_usable=True, params=params,
        param_errors={n: None for n in params}, gamma=0.3, gamma_err=0.05, omega0=omega0,
        omega0_err_hesse=freq_hw, message="ok", within_model=wm)


def _sel(modality="fold"):
    return select.Selection(scores=[], best="elastic+stokes+bg",
                            survivors=["elastic+stokes+bg"], pooled=None, modality=modality)


def test_confidence_in_unit_range_and_exposes_components():
    s, comp = confidence.compute_confidence(_result(), _ctx(), _sel(),
                                            prior=PhononPrior(6.0, 300.0, 1.0))
    assert 0.0 <= s <= 1.0
    assert set(comp) >= {"s_structure", "s_interval", "s_paradigm", "s_prior", "s_provenance"}


def test_tighter_interval_higher_confidence():
    tight = confidence.compute_confidence(_result(freq_hw=0.02), _ctx(), _sel())[0]
    loose = confidence.compute_confidence(_result(freq_hw=0.40), _ctx(), _sel())[0]
    assert tight > loose


def test_fork_lower_than_fold():
    fold = confidence.compute_confidence(_result(), _ctx(), _sel("fold"))[0]
    fork = confidence.compute_confidence(_result(), _ctx(), _sel("fork"))[0]
    assert fork < fold


def test_sparse_regime_lower_than_asymptotic():
    asy = confidence.compute_confidence(_result(regime="asymptotic"), _ctx(), _sel())[0]
    spa = confidence.compute_confidence(_result(regime="sparse"), _ctx(), _sel())[0]
    assert spa < asy


def test_prior_distance_lowers_and_absent_is_neutral():
    near = confidence.compute_confidence(_result(6.0), _ctx(), _sel(),
                                         prior=PhononPrior(6.0, 300.0, 1.0))[0]
    far = confidence.compute_confidence(_result(6.0), _ctx(), _sel(),
                                        prior=PhononPrior(9.0, 300.0, 1.0))[0]
    none = confidence.compute_confidence(_result(6.0), _ctx(), _sel(), prior=None)[0]
    assert far < near
    assert math.isclose(none, near, rel_tol=1e-9)        # absent prior is neutral, not penalized


def test_provenance_penalizes_uncalibrated():
    cal = confidence.compute_confidence(_result(), _ctx("absolute"), _sel())[0]
    unc = confidence.compute_confidence(_result(), _ctx("needs_zero"), _sel())[0]
    assert unc < cal

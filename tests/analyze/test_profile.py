"""MINOS (Tier-1 inline) + the exact ω₀ profile interval (§5.3).

ω₀ is a derived quantity (√(Ω_d²+Γ²)); iminuit MINOS only profiles the fit parameters, so
the ω₀ interval comes from a 1-D profile scan. In the well-counted regime it should match the
bootstrap (both → the asymptotic interval)."""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bootstrap as B
from isar.analyze.errors import profile as P
from isar.analyze.structures import enumerate_structures
from isar.synth._ff import convolute_voigt_dho


def _fitted(seed=0):
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 400.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp = {h.label: h for h in enumerate_structures(ctx)}["elastic+stokes+bg"]
    return hyp, ctx, loop.fit_hypothesis(hyp, ctx, minos=False)


def test_minos_intervals_present_and_ordered():
    hyp, ctx, fit = _fitted()
    mi = P.minos_intervals(hyp, ctx, fit)
    assert "phonon.Omega_d" in mi and "phonon.Gamma" in mi
    for lo, hi, valid in mi.values():
        assert lo <= hi


def test_omega0_profile_brackets_fit():
    hyp, ctx, fit = _fitted()
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit)
    assert np.isfinite(lo) and np.isfinite(hi)
    assert lo < fit.omega0 < hi


def test_omega0_profile_near_bootstrap_when_well_counted():
    hyp, ctx, fit = _fitted()
    lo, hi = P.omega0_profile_interval(hyp, ctx, fit)
    blo, _bmed, bhi = B.parametric_bootstrap(hyp, ctx, fit, M=80, seed=1).omega0_interval
    pw, bw = (hi - lo) / 2.0, (bhi - blo) / 2.0
    assert 0.4 < pw / bw < 2.5            # profile ≈ bootstrap in the asymptotic regime

"""Wiring: the error layer is opt-in per call and lands in StructureFitResult.within_model
(the slot step 1 reserved), leaving step 1's fast path unchanged."""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.synth._ff import convolute_voigt_dho


def _ctx(seed=0):
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_errors_flag_populates_within_model():
    results = loop.fit_admissible_structures(_ctx(), errors=True, M=40, seed=2)
    es = {r.label: r for r in results}["elastic+stokes+bg"]
    assert es.within_model is not None
    lo, med, hi = es.within_model["freq_interval"]
    assert lo <= es.omega0 <= hi
    assert es.within_model["n_converged"] > 0


def test_fast_path_leaves_within_model_none():
    for r in loop.fit_admissible_structures(_ctx(), errors=False):
        assert r.within_model is None

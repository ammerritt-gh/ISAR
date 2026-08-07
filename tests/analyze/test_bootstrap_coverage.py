"""§10 gate: per-scan parametric-bootstrap interval calibration on synthetic truth.

Promote the §0.1 claim from assertion to evidence — does the 68 % ω₀ bootstrap interval
contain the true ω₀ ≈68 % of the time? Run on a well-counted midzone cell (the asymptotic
regime, where calibration is expected). A gross miss is itself the finding. Marked slow
(R refits × M bootstrap refits)."""
import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bootstrap as B
from isar.analyze.phonon import enumerate_phonon_structures
from isar.synth import config, generate, noise


def context_from_ground_truth(gt, counts) -> ScanContext:
    """Bridge a synth GroundTruth + one Poisson draw → an analyze ScanContext."""
    x = np.asarray(noise.energy_grid(gt.x_min, gt.x_max, gt.x_step), dtype=float)
    return ScanContext(x=x, counts=np.asarray(counts, dtype=float),
                       mask=np.ones(x.size, dtype=bool), T=gt.T, center=gt.center,
                       gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
                       instrument="synthetic", energy_status="needs_zero")


@pytest.mark.slow
def test_bootstrap_omega0_coverage_on_synthetic_truth():
    cfg = config.default_run_config()
    H, target, R, M = 2.5, 200.0, 40, 60
    used = hit = 0
    for i in range(R):
        gt = generate.make_ground_truth(cfg, H, target, seed=1000 + i)
        ctx = context_from_ground_truth(gt, noise.draw_counts(gt))
        hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}["elastic+stokes+bg"]
        fit = loop.fit_hypothesis(hyp, ctx, minos=False)
        if not fit.optimizer_converged:
            continue
        used += 1
        lo, _med, hi = B.parametric_bootstrap(hyp, ctx, fit, M=M, seed=2000 + i).omega0_interval
        if lo <= gt.omega0 <= hi:
            hit += 1
    assert used >= 0.9 * R, f"only {used}/{R} synthetic fits converged"
    coverage = hit / used
    assert 0.50 <= coverage <= 0.85, \
        f"68% bootstrap ω₀ coverage={coverage:.2f} over {used} cells (expected ~0.68)"

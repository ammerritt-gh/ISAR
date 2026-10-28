"""§10 gate — per-scan error-extraction coverage on synthetic truth, all three roads compared.

*How the error is pulled out of a fit* is the object under test. On synthetic truth we score the
ω₀ interval coverage of the three roads — Jacobian/HESSE, parametric bootstrap, Bayesian posterior
— against the known ω₀. The Step-7 product is the mutual cross-check of the two roads that replace
the fragile Jacobian: **bootstrap ≈ posterior, and both calibrated**, in both a well-counted and a
sparse regime. That is the trust signal a single error estimate cannot give.

Honest scope note: this clean single-peak generator keeps ω₀ (a *location* parameter, robust even
at low counts) and Γ (here ≈0.5, far from its floor) well-behaved, so all three roads *coincide*
here — HESSE included. The Jacobian's documented failure (§5.1, ``TRUST_SIGMA_FACTOR``) lives in the
Γ→0 / parameter-at-boundary / structural-ambiguity corner of real messy data, which this generator
does not manufacture; so the gate does not claim a synthetic HESSE breakdown. What it *does* prove
is that bootstrap and posterior are calibrated and agree — the cross-check that carries real scans
where the Jacobian cannot be trusted.

Slow: R replicas × (bootstrap refits + one nested-sampling run) per regime; reduced sampler settings
keep it affordable. Deterministic (``draw_counts`` is seeded from the ground truth)."""
import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bayes as BZ
from isar.analyze.errors import bootstrap as B
from isar.analyze.phonon import enumerate_phonon_structures
from isar.synth import config, generate, noise

pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor")

_FAST = dict(n_live=100, dlogz=1.0, sample="unif")


def _context_from_ground_truth(gt, counts) -> ScanContext:
    x = np.asarray(noise.energy_grid(gt.x_min, gt.x_max, gt.x_step), dtype=float)
    return ScanContext(x=x, counts=np.asarray(counts, dtype=float),
                       mask=np.ones(x.size, dtype=bool), T=gt.T, center=gt.center,
                       gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
                       instrument="synthetic", energy_status="needs_zero")


def _coverages(target, R, seed0):
    """Score HESSE / bootstrap / posterior ω₀-interval coverage against truth over R replicas."""
    cfg = config.default_run_config()
    used = hesse_valid = hesse = boot = bayes = 0
    for i in range(R):
        gt = generate.make_ground_truth(cfg, 2.5, target, seed=seed0 + i)
        ctx = _context_from_ground_truth(gt, noise.draw_counts(gt))
        hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}["elastic+stokes+bg"]
        fit = loop.fit_hypothesis(hyp, ctx, minos=False)
        if not fit.optimizer_converged:
            continue
        used += 1
        s = fit.omega0_err_hesse
        if np.isfinite(s) and s > 0:                          # HESSE only where the Jacobian exists
            hesse_valid += 1
            if fit.omega0 - s <= gt.omega0 <= fit.omega0 + s:
                hesse += 1
        blo, _bm, bhi = B.parametric_bootstrap(hyp, ctx, fit, M=40, seed=7000 + i).omega0_interval
        boot += int(blo <= gt.omega0 <= bhi)
        zlo, _zm, zhi = BZ.bayesian_posterior(hyp, ctx, fit, seed=9000 + i, **_FAST).omega0_interval
        bayes += int(zlo <= gt.omega0 <= zhi)
    return dict(used=used, hesse=hesse, hesse_valid=hesse_valid, boot=boot, bayes=bayes)


@pytest.mark.statistical
@pytest.mark.slow
def test_three_methods_agree_and_calibrated_when_well_counted():
    """Well-counted regime: HESSE, bootstrap, posterior all ≈calibrated on ω₀, and bootstrap≈
    posterior (Bernstein–von Mises — the two paradigms coincide where the Jacobian is valid)."""
    R = 12
    c = _coverages(200.0, R, seed0=1000)
    used = c["used"]
    assert used >= 0.8 * R, f"only {used}/{R} synthetic fits converged"
    boot_cov, bayes_cov = c["boot"] / used, c["bayes"] / used
    hesse_cov = c["hesse"] / c["hesse_valid"] if c["hesse_valid"] else 0.0
    assert 0.45 <= boot_cov <= 0.95, f"bootstrap coverage={boot_cov:.2f}"
    assert 0.45 <= bayes_cov <= 0.95, f"posterior coverage={bayes_cov:.2f}"
    assert 0.40 <= hesse_cov <= 0.95, f"HESSE coverage={hesse_cov:.2f}"
    assert abs(boot_cov - bayes_cov) <= 0.25, f"boot={boot_cov:.2f} vs bayes={bayes_cov:.2f}"


@pytest.mark.statistical
@pytest.mark.slow
def test_bootstrap_and_posterior_agree_and_hold_at_low_counts():
    """Sparse regime: the two roads that replace the Jacobian stay calibrated on ω₀ and keep
    agreeing — mutual confirmation exactly where a single error estimate is least trustworthy."""
    R = 12
    c = _coverages(10.0, R, seed0=5000)
    used = c["used"]
    assert used >= 0.75 * R, f"only {used}/{R} sparse fits converged"
    boot_cov, bayes_cov = c["boot"] / used, c["bayes"] / used
    assert 0.40 <= boot_cov <= 0.90, f"bootstrap coverage collapsed: {boot_cov:.2f}"
    assert 0.40 <= bayes_cov <= 0.90, f"posterior coverage collapsed: {bayes_cov:.2f}"
    assert abs(boot_cov - bayes_cov) <= 0.30, f"boot={boot_cov:.2f} vs bayes={bayes_cov:.2f}"

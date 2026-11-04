"""The tie-breaker penalty in the solo fast pass: the design's three assumption checks, the penalty's place in the
prior, and a pre-penalty record's refusal. Synthetic spectra generated at test time (seeded Poisson counts); each run
names its ceiling, so the repo's ISAR_MODE_CEILING is never read here.
"""
import numpy as np
import pytest

from isar.analyze import catalogue, fast_pass
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings

FWHM = 1.5
X = np.linspace(-8.0, 20.0, 113)


def _ctx(counts):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float), mask=np.ones(X.size, bool), T=300.0,
                       center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute")


def _spectrum(modes, seed, elastic=150.0, bg=4.0):
    """Poisson counts of an elastic line, one DHO per (omega0, Gamma, height) and a flat background."""
    ctx = _ctx(np.ones(X.size))
    label = "elastic+stokes{}+bg".format(len(modes) if len(modes) > 1 else "")
    struct = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == label)
    theta = [v for m in modes for v in m] + [elastic, bg]
    mu = struct.build().evaluate(X, theta, ctx)
    return np.random.default_rng(seed).poisson(mu).astype(float)


def _count(counts):
    return fast_pass.run_fast_pass(_ctx(counts), settings=FastPassSettings(mode_ceiling=2)).preferred_count


def _theta(model, modes, bg=5.0):
    theta = np.zeros(len(model.names))
    for (a, g, c), (e, gam, h) in zip(model.modes, modes):
        theta[a], theta[g], theta[c] = e, gam, h
    theta[model.el], theta[model.bg] = 0.0, bg
    return theta


def test_two_separated_modes_keep_two_modes():                       # assumption check 1
    assert _count(_spectrum([(5.0, 0.3, 120.0), (5.0 + 2 * FWHM, 0.3, 120.0)], seed=1)) == 2


def test_a_resolvable_shoulder_keeps_two_modes():                    # assumption check 2
    assert _count(_spectrum([(6.0, 0.3, 400.0), (6.0 + 0.8 * FWHM, 0.3, 100.0)], seed=2)) == 2


def test_an_indistinguishable_pair_is_one_mode_and_pays_the_penalty():   # assumption check 3
    modes = [(8.0, 0.3, 150.0), (8.0 + 0.1 * FWHM, 0.3, 150.0)]
    counts = _spectrum(modes, seed=3)
    assert _count(counts) == 1
    eng = fast_pass._Engine(_ctx(counts), FastPassSettings(mode_ceiling=2))
    off = fast_pass._Engine(_ctx(counts), FastPassSettings(mode_ceiling=2, pair_penalty_cap=0.0))
    model = eng.model(eng.variant(2, True, True), ())
    theta = _theta(model, modes)
    pen = eng.pair_penalty(model, theta)
    assert pen > 0.5                                                  # about the cap: an equal, unresolved pair
    assert eng.ln_prior(model, theta) == pytest.approx(off.ln_prior(model, theta) - pen, abs=1e-9)


def test_a_record_saved_before_the_penalty_refuses_reclassification():
    # Its settings lack the two penalty keys: the refusal names a rerun, it does not fail on a missing key.
    old = {k: v for k, v in fast_pass._settings_dict(FastPassSettings()).items()
           if not k.startswith("pair_penalty")}
    with pytest.raises(ValueError, match="rerun the fast pass"):
        fast_pass.reclassify_record({"settings": old}, FastPassSettings())

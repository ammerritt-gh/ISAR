"""A near-zero mode must earn its place against the elastic line (recipe /6 energy penalty).

Three synthetic spectra, T = 97 K, fit resolution FWHM 1.5 meV, data drawn at test time with a fixed
seed (hard rule 10). Every full ``run_fast_pass`` here takes about 1-3 s; every test names its
``mode_ceiling`` (never the conftest's ``ISAR_MODE_CEILING``).

1. A central line a little broader than the fit's resolution, with no real near-zero mode. With the
   penalty off (``energy_ramp_ceiling=0.0``, the /5 energy prior up to its renormalisation) the fast
   pass prefers a pair below 1/2 FWHM and no elastic line; the default recipe prefers an elastic line.
   The off arm is the test's own proof that it can fail. The same case on the parent commit
   (recipe /5, no ramp fields) selects the pair, as the off arm does.
2. A genuine mode above 1 FWHM is still selected under the default recipe.
3. A strong mode below 1/4 FWHM is still selected: its evidence, measured with the penalty off, beats
   every model without such a mode by far more than the 7.5-nat ceiling.

The case 1 margins are small by nature (a penalty of at most 7.5 nats can only tip a near tie): the
measured ln Z gaps are asserted at >= 2 nats on each side.
"""
import numpy as np

from isar.analyze import catalogue, fast_pass
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.analyze.prior import PriorSettings

FWHM = 1.5
X = np.linspace(-8.0, 20.0, 113)
PRICED = ("ok", "ok_boundary")
OFF = PriorSettings(energy_ramp_ceiling=0.0)


def _ctx(counts, gauss_fwhm=FWHM):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float), mask=np.ones(X.size, bool), T=97.0,
                       center=0.0, gauss_fwhm=gauss_fwhm, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute")


def _counts(modes, elastic, bg, seed, gauss_fwhm=FWHM):
    """Poisson counts of elastic + len(modes) DHO modes (omega0, Gamma, height) + bg, rendered on a
    resolution of FWHM ``gauss_fwhm`` (wider than the fit's for a broadened central line)."""
    ctx = _ctx(np.ones(X.size), gauss_fwhm)
    n = len(modes)
    label = "elastic+stokes{}+bg".format(n if n > 1 else "") if n else "elastic+bg"
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, n) if h.label == label)
    lam = hyp.build().evaluate(X, [v for m in modes for v in m] + [elastic, bg], ctx)
    return np.random.default_rng(seed).poisson(lam).astype(float)


def _fit(counts, ceiling, off):
    settings = FastPassSettings(mode_ceiling=ceiling, prior=OFF if off else None)
    return fast_pass.run_fast_pass(_ctx(counts), settings=settings)


def _energies(model):
    return [v for k, v in (model.map_params or {}).items() if k.endswith("omega0")]


def _best(res, keep):
    """Highest ln Z among the priced models ``keep`` accepts."""
    return max(m.ln_z for m in res.models if m.evidence_status in PRICED and keep(m))


def test_a_broadened_central_line_is_an_elastic_line_not_a_near_zero_pair():
    # Central line 1.6 x the fit's resolution, one real mode at 7 meV, ceiling 2. Measured (seed 5):
    # off -> "stokes2+bg_delta1" (mode at 0.55 meV, no elastic) ahead by +3.4 nats of ln Z over the
    # best elastic model; default -> "elastic+stokes+bg", the best no-elastic model 2.8 nats behind.
    counts = _counts([(7.0, 0.5, 60.0)], 25.0, 4.0, seed=5, gauss_fwhm=1.6 * FWHM)

    off = _fit(counts, 2, off=True)
    assert not off.selected.elastic and min(p.omega0 for p in off.peaks) < 0.5 * FWHM
    assert _best(off, lambda m: not m.elastic) - _best(off, lambda m: m.elastic) >= 2.0

    on = _fit(counts, 2, off=False)
    assert on.selected.elastic and all(p.omega0 >= 0.5 * FWHM for p in on.peaks)
    assert _best(on, lambda m: m.elastic) - _best(on, lambda m: not m.elastic) >= 2.0


def test_a_genuine_mode_above_one_fwhm_is_still_selected():
    # Resolution-limited elastic line plus a mode at 1.3 FWHM (1.95 meV); ceiling 2.
    counts = _counts([(1.95, 0.4, 60.0)], 150.0, 4.0, seed=1)
    res = _fit(counts, 2, off=False)
    assert res.selected.elastic and len(res.peaks) == 1
    peak = res.peaks[0]
    assert abs(peak.omega0 - 1.95) < max(3 * peak.sigma_omega0, 0.1)


def test_a_strong_mode_below_a_quarter_fwhm_is_still_selected():
    # A mode at 0.2 FWHM (0.3 meV, width 2 meV, no elastic line), height 3000; ceiling 1. Measured
    # (seed 1): ln Z of the best model with a mode below 1/4 FWHM leads every model without one by
    # +96 nats with the penalty off and +89 under the default recipe.
    counts = _counts([(0.3, 2.0, 3000.0)], 0.0, 4.0, seed=1)
    quarter = 0.25 * FWHM

    def has_near_zero(m):
        return any(e < quarter for e in _energies(m))

    off = _fit(counts, 1, off=True)
    assert _best(off, has_near_zero) - _best(off, lambda m: not has_near_zero(m)) >= 15.0

    on = _fit(counts, 1, off=False)
    assert _best(on, has_near_zero) - _best(on, lambda m: not has_near_zero(m)) >= 15.0
    assert on.peaks and on.peaks[0].omega0 < quarter and abs(on.peaks[0].omega0 - 0.3) < 0.15

"""BL43LXU model selection with the central QE (design: "A quasi-elastic central component").

Four synthetic spectra drawn at test time (hard rule 10) on the measured a06 resolution curve
(``isar/synth/bl43_curves.py``), T = 97 K, window -10 to 15 meV, fixed seeds, ``mode_ceiling`` 2.
Every test asserts on the fast pass's public result (``selected``, ``peaks``, ``preferred_count``,
``qe``). The a06 resolution FWHM is about 1.13 meV.

1. A sharp elastic line, one phonon at 7 meV, no central excess: no QE is selected. The parent's
   selection is the same (elastic+stokes+bg); this case pins that the always-offered QE does not
   survive where it is not needed. Measured: the selected model leads the best QE-carrying one by 10 nats.
2. An elastic line plus a broad central excess (QE, intrinsic FWHM 1.5 x the resolution) and no
   phonon: explained with zero phonons (QE fitted 92.6 high, width 1.50 meV; truth 80, 1.70). Red on
   the parent, whose fast pass selects one phonon at 1.8 meV for the same data.
3. A genuine phonon at 1.5 meV (1.3 FWHM), no central excess: the phonon is kept, no QE. Measured: the
   best QE-carrying model trails by only 0.7 nats, so the assertions pin the phonon, not the QE's
   absence. The parent selects the same model.
4. Elastic line plus QE plus the same phonon: the phonon is kept, the central intensity is carried by
   the QE (the fit prefers a QE of width 1.12 meV alone, ln Z -277.3, to elastic plus QE: the two
   central components are nearly degenerate) and there is no extra phonon (one peak). Red on the
   parent, which has no QE.
Cases 1 and 3 also read ``res.qe``, which the parent lacks, so they raise there; their selections are
unchanged on the parent.
"""
import numpy as np

from isar.analyze import catalogue, fast_pass
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
FWHM = float(CURVE.fwhm)
X = np.linspace(-10.0, 15.0, 101)


def _ctx(counts):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float), mask=np.ones(X.size, bool), T=97.0,
                       center=0.0, gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="BL43LXU",
                       energy_status="absolute", resolution_curve=CURVE)


def _counts(modes, elastic, qe, bg, seed):
    """Poisson counts of elastic + QE (height, intrinsic FWHM) + DHO modes (omega0, Gamma, h) + bg."""
    ctx = _ctx(np.ones(X.size))
    n = len(modes)
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, n) if h.label.endswith("+bg"))
    comp = hyp.build()
    theta = {"elastic.h": elastic, "bg.BG": bg, "qe.h": qe[0] if qe else 0.0, "qe.width": qe[1] if qe else 1.0}
    for i, (w0, gamma, h) in enumerate(modes):
        pre = "phonon" if i == 0 else f"phonon{i + 1}"
        theta.update({pre + ".omega0": w0, pre + ".Gamma": gamma, pre + ".h": h})
    lam = comp.evaluate(X, [theta[name] for name in comp.param_names()], ctx)
    return np.random.default_rng(seed).poisson(lam).astype(float)


def _fit(counts):
    return fast_pass.run_fast_pass(_ctx(counts), settings=FastPassSettings(mode_ceiling=2))


def test_a_sharp_elastic_line_needs_no_qe():
    res = _fit(_counts([(7.0, 0.5, 60.0)], 300.0, None, 3.0, seed=1))
    assert res.preferred_count == 1 and len(res.peaks) == 1
    assert abs(res.peaks[0].omega0 - 7.0) < 0.3
    assert res.qe is None or res.qe.height < 0.05 * 300.0


def test_an_elastic_line_with_a_broad_central_excess_is_explained_without_a_phonon():
    res = _fit(_counts([], 100.0, (80.0, 1.5 * FWHM), 2.0, seed=2))
    assert res.preferred_count == 0 and not res.peaks
    assert res.qe is not None and res.qe.height > 20.0 and res.qe.width > FWHM


def test_a_genuine_low_energy_phonon_is_kept_without_a_central_excess():
    res = _fit(_counts([(1.5, 0.4, 70.0)], 200.0, None, 3.0, seed=3))
    assert res.preferred_count == 1 and len(res.peaks) == 1
    peak = res.peaks[0]
    assert abs(peak.omega0 - 1.5) < max(3 * peak.sigma_omega0, 0.15)


def test_a_low_energy_phonon_and_a_central_excess_take_one_phonon_and_the_qe():
    res = _fit(_counts([(1.5, 0.4, 70.0)], 100.0, (80.0, 1.5 * FWHM), 2.0, seed=4))
    assert res.preferred_count == 1 and len(res.peaks) == 1
    peak = res.peaks[0]
    assert abs(peak.omega0 - 1.5) < max(3 * peak.sigma_omega0, 0.15)
    assert res.qe is not None and res.qe.height > 20.0

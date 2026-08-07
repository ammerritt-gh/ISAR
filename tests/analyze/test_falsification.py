"""Phase 4a — under-model falsification: the L0 descriptor as a model-free check on the fit.

A descriptor peak is "unmatched" when the fitted model leaves a family-wise-significant positive
residual there (real signal the model can't fit) → verdict `signal_unmodeled`, demoted to
`descriptor_only`, disposition `"unmodeled"` (flagged for a human but NOT an ω₀ measurement). A
clean single-phonon must stay `resolved` (no new false flags). Matching is against the model
*curve*, so it is damping- and side-aware (a stokes-only model that misses a real anti-Stokes peak
leaves a residual → unmatched).
"""
import numpy as np

from isar.analyze import gold, loop, select
from isar.analyze import verdict as V
from isar.analyze.context import ScanContext
from isar.analyze.phonon import enumerate_phonon_structures
from isar.schema.steering import DescriptorPeak, ScanDescriptor
from isar.analyze.lineshapes import convolute_voigt_dho


def _ctx(counts, center=0.0):
    x = np.linspace(-8.0, 18.0, 131)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, bool), T=50.0, center=center,
                       gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30", energy_status="needs_zero")


def _clean_counts():
    x = np.linspace(-8.0, 18.0, 131)
    return x, convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0, peak="all")


def _fit(ctx, label="elastic+stokes+bg"):
    hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}[label]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    return res, select.select_models([res])


# --------------------------------------------------------------------------- significance filter
def test_significant_peaks_family_wise():
    d = ScanDescriptor(peaks=(DescriptorPeak(5.0, 1.0, 100.0, 5.0, 1e-5),
                              DescriptorPeak(8.0, 1.0, 100.0, 2.0, 5e-3)), n_res=10.0, n_points=60)
    sig = V._significant_peaks(d)                    # p*n_res: 1e-4 vs 5e-2; FLAG_ALPHA=0.01
    assert [p.energy for p in sig] == [5.0]          # only the strong one clears family-wise


# --------------------------------------------------------------------------- residual matching
def test_clean_fit_leaves_no_unmatched_peak():
    x, counts = _clean_counts()
    ctx = _ctx(counts)
    res, _ = _fit(ctx)
    d = V._safe_descriptor(ctx)
    m = V._model_curve(res, ctx)
    assert V._unmatched_descriptor_peaks(d, res, ctx, m) == []  # model explains every detected peak
    assert V._has_matched_descriptor_peak(d, res, ctx, m)       # …and confirms the real one


def test_planted_second_peak_is_unmatched():
    x, clean = _clean_counts()
    counts = clean + convolute_voigt_dho(x, 12.0, 0.0, 1.0, 0.0, 0.4, 300.0, 0.0, 0.0, 50.0,
                                         peak="stokes")
    ctx = _ctx(counts)
    res, _ = _fit(ctx)
    d = V._safe_descriptor(ctx)
    unm = V._unmatched_descriptor_peaks(d, res, ctx, V._model_curve(res, ctx))
    assert unm and any(abs(float(p.energy) - 12.0) < 2.0 for p in unm)   # the 2nd peak is caught


# (side-awareness — a stokes-only model that omits a real anti-Stokes lobe leaves a gain-side
# residual → unmatched — is inherent to matching against the model *curve*; the planted-peak test
# above already exercises "model omits a real peak → residual → unmatched" via _model_curve.)


# --------------------------------------------------------------------------- end-to-end classify
def test_clean_single_phonon_stays_resolved():
    x, counts = _clean_counts()
    ctx = _ctx(counts)
    res, sel = _fit(ctx)
    assert V.classify([res], ctx, sel, descriptor=V._safe_descriptor(ctx)).verdict == "resolved"


def test_clean_single_phonon_stays_resolved_under_poisson():
    x, clean = _clean_counts()
    counts = np.random.default_rng(1).poisson(np.clip(clean, 0, None)).astype(float)
    ctx = _ctx(counts)
    res, sel = _fit(ctx)
    assert V.classify([res], ctx, sel, descriptor=V._safe_descriptor(ctx)).verdict == "resolved"


def test_planted_second_peak_makes_signal_unmodeled():
    x, clean = _clean_counts()
    counts = clean + convolute_voigt_dho(x, 12.0, 0.0, 1.0, 0.0, 0.4, 300.0, 0.0, 0.0, 50.0,
                                         peak="stokes")
    ctx = _ctx(counts)
    res, sel = _fit(ctx)
    sv = V.classify([res], ctx, sel, descriptor=V._safe_descriptor(ctx))
    assert sv.verdict == "signal_unmodeled"
    assert sv.confidence == 0.0 and sv.components["unmatched_peaks"]


# --------------------------------------------------------------------------- disposition contract
def test_disposition_unmodeled_not_delivered_not_usable():
    from isar.drive.supervisor import stopping
    disp = gold.verdict_disposition("signal_unmodeled")
    assert disp == "unmodeled"
    assert not gold.delivered(disp)                            # not an ω₀ measurement
    assert disp not in stopping.USABLE_DISPOSITIONS           # stop rule never reads its ω₀

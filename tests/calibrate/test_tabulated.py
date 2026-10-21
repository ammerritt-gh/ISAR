"""The elastic-zero fit and the slit ladder (isar.calibrate.tabulated).

Everything is generated here from fixed seeds: three settings whose curves differ in width and tail
shape (narrow Gaussian, medium with an exponential right tail, broad and skewed), one scaled copy
per detector. A Poisson draw of each curve stands in for a measured elastic line.
"""
import numpy as np
import pytest

import isar
from isar.analyze.estimators import baker_cousins_cost
from isar.analyze.resolution import ResolutionLibrary, TabulatedResolution
from isar.calibrate import elastic, tabulated as T
from isar.schema import scan

DETS = ("d1", "d2", "d3")
X = np.round(np.arange(-8.0, 8.0 + 1e-9, 0.2), 10)      # a bin every 0.2 meV, as a binned analyzer
GRID = np.round(np.arange(-12.0, 12.0 + 1e-9, 0.05), 10)
AREA = 2500.0        # counts*meV: ~ 250 counts in the peak bin, a clear line
BG = 4.0             # counts per bin


def _shape(setting, e):
    if setting == "narrow":
        return np.exp(-0.5 * (e / 0.4) ** 2)
    if setting == "tail":                                # medium core, long right tail
        return np.exp(-0.5 * (e / 0.6) ** 2) + 0.05 * np.where(e > 0, np.exp(-e / 2.5), 0.0)
    if setting == "broad":                               # broad, skewed
        return np.where(e < 0, np.exp(-0.5 * (e / 0.9) ** 2), np.exp(-0.5 * (e / 1.4) ** 2))
    if setting == "tail2":                               # indistinguishable from "tail" at this
        return np.exp(-0.5 * (e / 0.6) ** 2) + 0.05 * np.where(e > 0, np.exp(-e / 2.55), 0.0)
    raise KeyError(setting)


def _library(settings=("narrow", "tail", "broad"), skip=()):
    curves = []
    for k, det in enumerate(DETS):
        for s in settings:
            if (det, s) not in skip:
                scale = 1.0 + 0.04 * k                   # detectors differ a little
                curves.append(TabulatedResolution(GRID * 1.0, _shape(s, GRID / scale), det, s, "gen"))
    return ResolutionLibrary(curves)


def _spectrum(det, counts, monitor=None, time=None, flags=()):
    n = counts.size
    return scan.Spectrum(
        detector_id=det, q=None, q_source="none", intensity=np.asarray(counts, dtype=float),
        variance=np.asarray(counts, dtype=float), mask=np.ones(n, dtype=bool), point_flags=[],
        spectrum_flags=list(flags), monitor=monitor, time=time)


def _draw(lib, det, setting, seed, shift=0.0, area=AREA, bg=BG, exposure=None):
    """Poisson counts of detector ``det``'s true curve at ``setting``."""
    rng = np.random.default_rng(seed)
    w = np.ones_like(X) if exposure is None else exposure / exposure.mean()
    mu = w * (area * lib.get(det, setting).profile(X - shift) + bg)
    return rng.poisson(mu).astype(float)


def _scan(lib, setting, seed=0, dets=DETS, **kw):
    return {d: _spectrum(d, _draw(lib, d, setting, seed + i, **kw)) for i, d in enumerate(dets)}


def test_tests_run_against_this_checkout():
    # Guard for the job worktree: the import root must be where this file's repo is.
    assert str(__import__("pathlib").Path(isar.__file__).resolve().parents[1]) == str(
        __import__("pathlib").Path(__file__).resolve().parents[2])


# -- fit_elastic_tabulated / zero_for -------------------------------------------------------------
@pytest.mark.parametrize("setting", ["narrow", "tail", "broad"])
def test_shifted_line_returns_its_shift(setting):
    lib = _library()
    shift = 0.35
    c = _draw(lib, "d2", setting, seed=3, shift=shift)
    fit = T.fit_elastic_tabulated(X, c, None, lib.get("d2", setting))
    assert fit.significant
    # Measured over 40 seeds (tail): centre scatter 0.006 meV, amplitude 0.9%; tolerances ~5 sigma.
    assert abs(fit.centre - shift) < 0.03
    assert abs(fit.amplitude / AREA - 1.0) < 0.05
    assert fit.deviance_per_point < 2.0                  # the true curve fits


def test_zero_for_uses_fit_when_significant_and_pipeline_zero_when_weak():
    lib = _library()
    strong = _spectrum("d1", _draw(lib, "d1", "tail", 1, shift=-0.2))
    z = T.zero_for(X, strong, lib.get("d1", "tail"))
    assert z.source == "elastic_fit" and abs(z.centre + 0.2) < 0.06 and z.flags == []

    weak = _spectrum("d1", _draw(lib, "d1", "tail", 2, shift=-0.2, area=2.0, bg=20.0))
    z = T.zero_for(X, weak, lib.get("d1", "tail"))
    assert z.source == "pipeline_zero" and z.centre == 0.0
    assert [f.type for f in z.flags] == ["pipeline_zero"] and "d1" in z.flags[0].reason
    assert np.isfinite(z.fit.deviance) and not z.fit.significant   # fit numbers still returned


def test_zero_count_bins_do_not_break_the_deviance():
    lib = _library()
    curve = lib.get("d1", "narrow")
    c = _draw(lib, "d1", "narrow", 5, bg=0.0)
    assert (c == 0).sum() > 20                           # sigma = 0 bins are really present
    fit = T.fit_elastic_tabulated(X, c, None, curve)
    assert np.isfinite(fit.deviance) and fit.significant and abs(fit.centre) < 0.06
    # The deviance is the exact Baker-Cousins sum on the raw counts (a y = 0 bin contributes 2 f).
    win = np.abs(X - X[np.argmax(c)]) <= T.FIT_HALF_WINDOW
    f = curve.profile(X - fit.centre) * fit.amplitude + fit.background
    assert fit.deviance == pytest.approx(baker_cousins_cost(c[win], f[win]), rel=1e-6)


def test_varying_exposure_is_honoured():
    lib = _library()
    e = np.tile([0.5, 2.0, 1.0, 1.5], X.size // 4 + 1)[:X.size]
    c = _draw(lib, "d3", "broad", 7, shift=0.25, exposure=e)
    fit = T.fit_elastic_tabulated(X, c, e, lib.get("d3", "broad"))
    assert abs(fit.centre - 0.25) < 0.08 and abs(fit.amplitude / AREA - 1.0) < 0.08
    assert fit.deviance_per_point < 2.0
    # Ignoring the exposure fits the same counts worse: the weights matter.
    blind = T.fit_elastic_tabulated(X, c, None, lib.get("d3", "broad"))
    assert blind.deviance > fit.deviance + 20


def test_flat_line_with_varying_exposure_is_not_significant():
    rng = np.random.default_rng(11)
    n_sig = 0
    for _ in range(60):
        e = rng.uniform(0.2, 3.0, X.size)               # short bins are noisy in rate
        c = rng.poisson(20.0 * e / e.mean()).astype(float)
        n_sig += T.elastic_significant(X, c, e)
    assert n_sig == 0
    # With a line on top, the same exposure pattern passes.
    lib = _library()
    e = rng.uniform(0.2, 3.0, X.size)
    assert T.elastic_significant(X, _draw(lib, "d1", "tail", 4, exposure=e), e)


def test_elastic_significant_matches_fit_elastic_at_uniform_exposure():
    rng = np.random.default_rng(3)
    lib = _library()
    cases = [rng.poisson(20.0, X.size).astype(float), _draw(lib, "d1", "tail", 9),
             _draw(lib, "d1", "tail", 10, area=40.0, bg=8.0)]
    for c in cases:
        assert T.elastic_significant(X, c, None) == elastic.fit_elastic(X, c)[3]


def test_wholly_missing_exposure_is_uniform_and_fits_its_zero():
    lib = _library()
    c = _draw(lib, "d2", "tail", 12, shift=0.3)
    nan = np.full(X.size, np.nan)
    sp = _spectrum("d2", c, monitor=nan, time=nan.copy())      # Ba122-style: exposure is missing
    z = T.zero_for(X, sp, lib.get("d2", "tail"))
    assert z.source == "elastic_fit" and abs(z.centre - 0.3) < 0.06
    uniform = T.fit_elastic_tabulated(X, c, None, lib.get("d2", "tail"))
    assert z.fit.centre == pytest.approx(uniform.centre) and z.fit.deviance == pytest.approx(uniform.deviance)


# -- choose_setting ---------------------------------------------------------------------------------
def _flags(choice):
    return [f.type for f in choice.flags]


@pytest.mark.parametrize("truth", ["narrow", "tail", "broad"])
def test_true_setting_is_inferred_when_nothing_is_declared(truth):
    lib = _library()
    choice = T.choose_setting(X, _scan(lib, truth, seed=20), lib, None)
    assert choice.setting == truth and choice.source == "inferred"
    assert _flags(choice) == ["slit_inferred"]           # clearly best: not ambiguous
    assert choice.comparison_set == list(DETS) and set(choice.deviances) == {"narrow", "tail", "broad"}
    assert min(choice.deviances, key=choice.deviances.get) == truth


def test_declared_wrong_setting_is_kept_with_slit_mismatch():
    lib = _library()
    choice = T.choose_setting(X, _scan(lib, "tail", seed=30), lib, "narrow")
    assert choice.setting == "narrow" and choice.source == "declared"
    assert _flags(choice) == ["slit_mismatch"]
    reason = choice.flags[0].reason
    assert "narrow" in reason and "tail" in reason         # names both the declared and the better
    # The declared true setting raises nothing.
    quiet = T.choose_setting(X, _scan(lib, "tail", seed=30), lib, "tail")
    assert quiet.setting == "tail" and quiet.flags == []


def test_near_identical_candidates_are_ambiguous():
    lib = _library(settings=("tail", "tail2", "broad"))
    choice = T.choose_setting(X, _scan(lib, "tail", seed=40), lib, None)
    assert choice.setting in ("tail", "tail2") and _flags(choice) == ["slit_inferred", "slit_ambiguous"]
    spread = abs(choice.deviances["tail"] - choice.deviances["tail2"])
    assert spread < T.CLEARLY_BETTER
    # A declared one of the pair is not contested by its twin: no mismatch within the threshold.
    assert T.choose_setting(X, _scan(lib, "tail", seed=40), lib, "tail2").flags == []


def test_analyzer_without_a_curve_at_every_candidate_is_left_out_of_every_score():
    lib_full = _library()
    lib_short = _library(skip={("d3", "broad")})
    spectra = _scan(lib_full, "tail", seed=50)
    short = T.choose_setting(X, spectra, lib_short, None)
    assert short.comparison_set == ["d1", "d2"]
    reference = T.choose_setting(X, {d: spectra[d] for d in ("d1", "d2")}, lib_full, None)
    assert short.deviances == pytest.approx(reference.deviances)    # d3 scored in no candidate


def test_non_integer_counts_never_enter_the_comparison_set():
    lib = _library()
    spectra = _scan(lib, "tail", seed=60)
    spectra["d2"].spectrum_flags.append(scan.Flag("counts_not_integer", "warning", "d2: _lr"))
    choice = T.choose_setting(X, spectra, lib, None)
    assert choice.comparison_set == ["d1", "d3"] and choice.setting == "tail"


def test_empty_comparison_set_never_refuses():
    lib = _library()
    weak = _scan(lib, "tail", seed=70, area=2.0, bg=20.0)
    declared = T.choose_setting(X, weak, lib, "narrow")
    assert (declared.setting, declared.source, declared.flags) == ("narrow", "declared", [])
    ruled = T.choose_setting(X, weak, lib, None)
    assert ruled.setting == "broad" and ruled.source == "inferred"      # largest median FWHM
    assert _flags(ruled) == ["slit_ambiguous"]
    assert "no significant elastic line; chosen by rule, not by evidence" in ruled.flags[0].reason
    empty = T.choose_setting(X, weak, ResolutionLibrary(), None)         # nothing to choose from
    assert empty.setting is None and _flags(empty) == ["slit_ambiguous"]


# -- an analyzer whose every bin is masked (invalid counts, dead analyzer) ------------------------
def _all_masked(lib, det, setting, seed):
    sp = _spectrum(det, _draw(lib, det, setting, seed))
    sp.mask[:] = False
    return sp


def test_all_masked_analyzer_gets_the_labelled_pipeline_zero():
    lib = _library()
    curve = lib.get("d1", "tail")
    sp = _all_masked(lib, "d1", "tail", 80)
    z = T.zero_for(X, sp, curve)
    assert z.source == "pipeline_zero" and z.centre == 0.0
    assert [f.type for f in z.flags] == ["pipeline_zero"] and "d1" in z.flags[0].reason
    assert np.isnan(z.fit.centre) and np.isnan(z.fit.deviance) and not z.fit.significant
    fit = T.fit_elastic_tabulated(X, np.full(X.size, np.nan), None, curve)
    assert np.isnan(fit.centre) and not fit.significant


def test_all_nan_counts_are_not_significant():
    assert not T.elastic_significant(X, np.full(X.size, np.nan), None)


def test_all_masked_analyzer_is_left_out_of_the_comparison_set():
    lib = _library()
    spectra = _scan(lib, "tail", seed=90)
    spectra["d2"].mask[:] = False
    choice = T.choose_setting(X, spectra, lib, None)
    assert choice.comparison_set == ["d1", "d3"] and choice.setting == "tail"
    assert _flags(choice) == ["slit_inferred"]

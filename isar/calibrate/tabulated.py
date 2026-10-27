"""The elastic-zero fit and the slit ladder for tabulated resolution curves.

Shared and instrument-agnostic (a card switches it on). Each detector's elastic line is fitted on
RAW counts with that detector's tabulated curve (:mod:`isar.analyze.resolution`) by the Poisson
(Baker-Cousins) deviance, giving the detector's energy zero. The scan's slit setting comes from a
ladder over the generic ``slit`` fact: a declared slit is always kept (a clearly better candidate
only raises ``slit_mismatch``); with none declared the best candidate is used and flagged
(``slit_inferred``, plus ``slit_ambiguous`` when no candidate is clearly best). Neither function
ever refuses: the fallbacks are labelled flags (``pipeline_zero``, ``slit_ambiguous``).

Exposure: the monitor, else the time, of the spectrum itself (BL43LXU spectra carry their own);
wholly missing exposure is uniform relative exposure. A spectrum without its own exposure is
therefore treated as uniform here. ``isar/calibrate/elastic.py`` (S30) is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Protocol

import numpy as np

from isar.analyze.context import select_exposure
from isar.analyze.estimators import baker_cousins_cost
from isar.schema.scan import Flag

# Summed-deviance gap that makes one slit "clearly better": a likelihood ratio of about e^5 = 150
# with the same number of free parameters per candidate.
CLEARLY_BETTER = 10.0
# Fit half-window around the located elastic peak: wide enough to pin the background and the
# tabulated tail, tight enough that an off-zero phonon is not fitted as elastic.
FIT_HALF_WINDOW = 3.0
# Significance half-window and k, as in isar.calibrate.elastic.fit_elastic.
SIGNIFICANCE_HALF_WINDOW = 1.5
SIGNIFICANCE_K = 5.0
# The peak is searched for within this of the file's own zero, narrower than fit_elastic's 2.5 meV: the
# axis is already the beamline's (R7 verifies it), and a wider search takes a low-energy inelastic line
# for the elastic one.
SEARCH_HALF_WIDTH = 1.5
CENTRE_BOUND = 1.5    # the fitted centre stays within this of the located peak
_MIN_POINTS = 5      # a 3-parameter fit needs more than 3 bins to mean anything


@dataclass
class ElasticFit:
    """One elastic-line fit. ``centre``/``amplitude``/``background`` are NaN when the window held
    too few bins to fit; ``amplitude`` is the line's area in counts*meV at the mean exposure,
    ``background`` the flat level in counts per bin at the mean exposure."""
    centre: float
    amplitude: float
    background: float
    deviance: float
    deviance_per_point: float
    n_points: int
    significant: bool            # the elastic_significant verdict


@dataclass
class ZeroResult:
    centre: float                # on the file's own energy axis
    source: str                  # "elastic_fit" | "pipeline_zero"
    fit: ElasticFit
    flags: list[Flag] = field(default_factory=list)


@dataclass
class SettingChoice:
    setting: str | None
    source: str                  # "declared" | "inferred"
    deviances: dict              # candidate setting -> summed deviance over the comparison set
    comparison_set: list[str]    # detector ids scored
    flags: list[Flag] = field(default_factory=list)


class CurveSource(Protocol):
    """What the coverage helpers read: a ResolutionLibrary, or the persisted curve check (intake.curves)."""

    def get(self, detector_id: str, setting: str | None): ...
    def settings(self) -> list[str | None]: ...


def curve_need(library: CurveSource, detector_id: str, setting: str | None, problem: str | None = None) -> str | None:
    """Why this analyzer cannot be calibrated at ``setting``, or None when its curve is there."""
    if setting is not None and library.get(detector_id, setting) is not None:
        return None
    if problem:
        return problem
    if setting is None:
        return "no slit setting is known for the scan"
    return f"no resolution curve for {detector_id} at slit {setting}"


def candidate_gaps(library: CurveSource, detector_id: str) -> list[str]:
    """The candidate slit settings at which this analyzer has no curve (it is left out of the slit comparison)."""
    return [s for s in library.settings() if s is not None and library.get(detector_id, s) is None]


def calibration_reusable(prev: dict | None, *, content_hash: str, library: str | None,
                         inputs_key: str | None = None) -> bool:
    """Whether a stored scan calibration still stands: same data and same resolution library, and with
    ``inputs_key`` (which holds both) the same calibration inputs and no error."""
    if inputs_key is not None:
        return prev is not None and "error" not in prev and prev.get("inputs_key") == inputs_key
    return prev is not None and prev.get("content_hash") == content_hash and prev.get("library") == library


def setting_basis(choice: SettingChoice) -> str:
    """How the slit was chosen, in the words the report uses: declared, inferred, the broadest-curve
    fallback (no elastic comparison was possible), or none (no curve at all)."""
    if choice.setting is None:
        return "none"
    if choice.source == "declared":
        return "declared"
    if choice.deviances:
        return "inferred"
    return "broadest_curve_fallback"


def _prepare(x, counts, exposure):
    """Finite points with positive exposure -> ``(x, counts, w)`` where ``w`` is the exposure
    relative to its mean (ones when exposure is wholly missing)."""
    x = np.asarray(x, dtype=float)
    c = np.asarray(counts, dtype=float)
    ok = np.isfinite(x) & np.isfinite(c)
    if exposure is None:
        w = np.ones_like(x)
    else:
        w = np.asarray(exposure, dtype=float)
        if not np.isfinite(w).any():                       # wholly missing: uniform (Ba122)
            w = np.ones_like(x)
        else:
            ok &= np.isfinite(w) & (w > 0)
            w = w / np.mean(w[ok]) if ok.any() else w
    return x[ok], c[ok], w[ok]


def _centre(x, c, w) -> float:
    """The elastic peak, located from the data alone (the highest rate near the nominal zero). NaN when no
    bin lies within the search: then there is no elastic line, and the whole spectrum is not searched."""
    rate = c / w
    pool = np.nonzero(np.abs(x) < SEARCH_HALF_WIDTH)[0]
    if pool.size == 0:
        return float("nan")
    return float(x[pool[int(np.argmax(rate[pool]))]])


def _not_raw(spectrum) -> bool:
    """A spectrum whose counts are not raw (the reader's ``counts_not_integer`` warning: every ``_lr``
    spectrum, or columns that are not counts). The ``info`` notice is print rounding of raw counts."""
    return any(f.type == "counts_not_integer" and f.severity == "warning" for f in spectrum.spectrum_flags)


NON_RAW_REASON = "non-raw counts (_lr) are not fitted"


def count_hold(spectrum, *, accepted: bool) -> str | None:
    """Why this spectrum's counts hold its fit, or None. Only the reader's warning-level
    ``counts_not_integer`` holds: always for a spectrum not recovered from the ``_i`` columns (``_lr``),
    and for an ``_i`` spectrum until its counts are accepted. The notice level (print rounding) holds nothing."""
    if not _not_raw(spectrum):
        return None
    rec = ((spectrum.facts or {}).get("count_recovery") or {}).get("value")
    if not isinstance(rec, dict) or rec.get("family") != "_i":
        return NON_RAW_REASON
    if accepted:
        return None
    return (f"counts may not be raw: {rec.get('bins_off')} of {rec.get('bins_tested')} bins are off an integer "
            f"after N = (y/e)^2 (largest deviation {float(rec.get('max_deviation') or 0.0):.3g}); "
            "not fitted until the counts are accepted")


def _at_search_edge(x, c0) -> bool:
    """Whether the located peak is the outermost bin searched: the highest rate there is a rising
    tail (an inelastic line beyond the search), not a line with the search on both sides of it."""
    near = np.abs(x) < SEARCH_HALF_WIDTH
    return bool(near.any() and (c0 <= x[near].min() or c0 >= x[near].max()))


def _significant(x, c, w, c0) -> bool:
    if _at_search_edge(x, c0):
        return False
    win = np.abs(x - c0) <= SIGNIFICANCE_HALF_WINDOW
    if win.sum() < 3:
        return False
    rate = c[win] / w[win]
    p = int(np.argmin(np.abs(x[win] - c0)))   # the located peak itself, not a stronger line beside it
    med = float(np.median(rate))
    # Poisson sigma of the peak bin's own rate if it held the background level: sqrt(med*w)/w,
    # floored at one count as in fit_elastic (w = 1 reproduces it). A short-exposure bin is
    # noisier; a long one cannot pass on exposure alone because its rate is normalised.
    wp = w[win][p]
    sigma = np.sqrt(max(med * wp, 1.0)) / wp
    return bool(rate[p] - med > SIGNIFICANCE_K * sigma)


def elastic_significant(x, counts, exposure) -> bool:
    """Whether a significant elastic line is present: peak rate minus median rate above k = 5
    Poisson sigma in the +/-1.5 meV window, the criterion of ``fit_elastic`` on exposure-normalised
    rates. ``exposure=None`` or all-NaN is uniform."""
    x, c, w = _prepare(x, counts, exposure)
    return x.size >= 3 and _significant(x, c, w, _centre(x, c, w))


def _fit_window(xw, cw, ww, curve, c0):
    """Minimise the Baker-Cousins deviance over centre, amplitude >= 0 and background >= 0 on
    the given bins. Returns ``(centre, amplitude, background, deviance)``."""
    from scipy.optimize import minimize

    dx = float(np.median(np.diff(np.sort(xw)))) if xw.size > 1 else 1.0
    rate = cw / ww
    bg0 = max(float(np.median(rate)), 1e-3)
    a0 = max(float(np.sum(np.clip(rate - bg0, 0, None))) * dx, 1.0)

    def dev(p):
        c, a, b = p[0], p[1] ** 2 * a0, p[2] ** 2 * bg0   # squares keep A, B >= 0
        if abs(c - c0) > CENTRE_BOUND:
            return 1e30
        return baker_cousins_cost(cw, ww * (a * curve.profile(xw - c) + b))

    p = np.array([c0, 1.0, 1.0])
    for _ in range(3):                                    # restart Nelder-Mead on its own result
        r = minimize(dev, p, method="Nelder-Mead",
                     options={"xatol": 1e-5, "fatol": 1e-7, "maxiter": 2000, "maxfev": 2000,
                              "initial_simplex": np.array(
                                  [p, p + [0.1, 0, 0], p + [0, 0.2, 0], p + [0, 0, 0.2]])})
        p = r.x
    return float(p[0]), float(p[1] ** 2 * a0), float(p[2] ** 2 * bg0), float(r.fun)


def _fit(x, c, w, curve) -> ElasticFit:
    if x.size < _MIN_POINTS:                             # no usable bins (all masked): nothing to locate
        return ElasticFit(np.nan, np.nan, np.nan, np.nan, np.nan, int(x.size), False)
    c0 = _centre(x, c, w)
    sig = _significant(x, c, w, c0)
    win = np.abs(x - c0) <= FIT_HALF_WINDOW
    if win.sum() < _MIN_POINTS:
        return ElasticFit(np.nan, np.nan, np.nan, np.nan, np.nan, int(win.sum()), False)
    centre, a, b, d = _fit_window(x[win], c[win], w[win], curve, c0)
    n = int(win.sum())
    if abs(centre - c0) >= CENTRE_BOUND - 1e-3:          # pinned at its bound: no line was found there
        sig = False
    return ElasticFit(centre, a, b, d, d / n, n, sig)


def fit_elastic_tabulated(x, counts, exposure, curve) -> ElasticFit:
    """Fit centre, amplitude and flat background of the elastic line with ``curve`` on raw counts:
    expected counts ``exposure_i/mean(exposure) * (A*curve.profile(x - c) + B)``, Poisson
    (Baker-Cousins) deviance, within +/-3 meV of the peak located from the data (so every
    candidate curve is scored on the same bins). sigma = 0 bins (zero counts) count in the
    deviance as the model value, with no floor."""
    return _fit(*_prepare(x, counts, exposure), curve)


def _inputs(x, spectrum):
    """``(x, counts, w)`` for one spectrum: its kept points, its own monitor else time."""
    keep = np.asarray(spectrum.mask, dtype=bool)
    shape = np.asarray(spectrum.intensity).shape
    sel = select_exposure(SimpleNamespace(monitor=spectrum.monitor, time=spectrum.time), shape, keep)
    exposure = None if sel.absolute is None else np.where(keep, sel.absolute, np.nan)
    x = np.asarray(x, dtype=float)
    counts = np.where(keep, np.asarray(spectrum.intensity, dtype=float), np.nan)
    return _prepare(x, counts, exposure)


def zero_for(x, spectrum, curve) -> ZeroResult:
    """The energy zero of one spectrum on the energy axis ``x`` (shared by the record's spectra).

    The fitted centre (source ``elastic_fit``) when the elastic line is significant. Otherwise
    centre 0.0 on the file's own calibrated axis (source ``pipeline_zero``: the beamline's zero,
    never an assumed zero for a raw axis) and a ``pipeline_zero`` flag naming the analyzer. The
    fit numbers come back either way."""
    fit = _fit(*_inputs(x, spectrum), curve)
    if fit.significant and np.isfinite(fit.centre):
        return ZeroResult(fit.centre, "elastic_fit", fit)
    flag = Flag("pipeline_zero", "warning",
                f"{spectrum.detector_id}: no significant elastic line; the energy axis is used "
                f"exactly as the file supplies it (pipeline zero)")
    return ZeroResult(0.0, "pipeline_zero", fit, [flag])


def choose_setting(x, spectra, library, declared) -> SettingChoice:
    """The slit setting of one scan: ``spectra`` is ``{detector_id: Spectrum}`` on the energy axis
    ``x``, ``declared`` the generic ``slit`` fact's value (header, else card) or None.

    The comparison set is fixed before any candidate is scored: detectors that pass
    ``elastic_significant``, carry no ``counts_not_integer`` warning (``_lr`` is never fitted) and have a
    curve at every candidate setting. Each candidate is scored by the summed deviance over that
    set, every detector on its own located window and bins. A declared slit is kept even when
    another candidate is better by ``CLEARLY_BETTER`` (``slit_mismatch``); with none declared the
    best is used (``slit_inferred``, and ``slit_ambiguous`` when the runner-up is within
    ``CLEARLY_BETTER``). A declared slit the library has no curve for is kept unscored. Never
    refuses."""
    candidates = [s for s in library.settings() if s is not None]
    prepared = {}
    for det, sp in spectra.items():
        if _not_raw(sp):
            continue
        if candidate_gaps(library, det):
            continue
        xs, c, w = _inputs(x, sp)
        if xs.size >= _MIN_POINTS and elastic_significant(xs, c, w):
            prepared[det] = (xs, c, w)

    deviances = {}
    if prepared and candidates:
        for s in candidates:
            deviances[s] = sum(_fit(*prepared[d], library.get(d, s)).deviance for d in prepared)
    # A detector whose window held too few bins gives NaN; drop such candidates' scores wholesale.
    if any(not np.isfinite(v) for v in deviances.values()):
        deviances = {}
    comparison = sorted(prepared) if deviances else []
    flags: list[Flag] = []

    if deviances:
        order = sorted(deviances, key=deviances.get)
        names = ", ".join(comparison)
        if declared:
            if declared in deviances:
                better = [s for s in order if s != declared
                          and deviances[declared] - deviances[s] >= CLEARLY_BETTER]
                if better:
                    flags.append(Flag(
                        "slit_mismatch", "warning",
                        f"declared slit {declared} is kept, but slit {better[0]} fits the elastic "
                        f"lines clearly better (summed deviance {deviances[better[0]]:.1f} against "
                        f"{deviances[declared]:.1f} over {names}); switch it if that is right"))
            return SettingChoice(declared, "declared", deviances, comparison, flags)
        flags.append(Flag("slit_inferred", "warning",
                          f"no slit declared; slit {order[0]} fits the elastic lines of {names} "
                          f"best (summed deviance {deviances[order[0]]:.1f})"))
        if len(order) > 1 and deviances[order[1]] - deviances[order[0]] < CLEARLY_BETTER:
            flags.append(Flag("slit_ambiguous", "warning",
                              f"slit {order[1]} fits within {CLEARLY_BETTER:g} in summed deviance "
                              f"of the chosen {order[0]} ({deviances[order[1]]:.1f} against "
                              f"{deviances[order[0]]:.1f} over {names}); no slit is clearly best"))
        return SettingChoice(order[0], "inferred", deviances, comparison, flags)

    if declared:
        return SettingChoice(declared, "declared", {}, [], [])
    return _broadest(spectra, library, candidates)


def _broadest(spectra, library, candidates) -> SettingChoice:
    """No significant elastic line and no declared slit: the setting with the broadest curves."""
    why = "no significant elastic line; chosen by rule, not by evidence"
    if not candidates:
        return SettingChoice(None, "inferred", {}, [], [Flag(
            "slit_ambiguous", "warning", f"{why}; the resolution library holds no slit setting")])

    def width(s):
        mine = [library.get(d, s).fwhm for d in spectra if library.get(d, s) is not None]
        return float(np.median(mine)) if mine else -np.inf
    best = max(candidates, key=width)
    if width(best) == -np.inf:                           # no analyzer here has any curve: use any
        best = candidates[0]
    return SettingChoice(best, "inferred", {}, [], [Flag(
        "slit_ambiguous", "warning", f"{why}; slit {best} has the broadest curves")])

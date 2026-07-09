"""L0 — the model-free spectral descriptor (docs/MEASUREMENT_STEERING.md §4.3).

Turns a raw spectrum into "signal at E=X, width W, intensity I", **without any physical model**
(no DHO, no dispersion). This is the foundation the confidence ladder rests on: it lets ISAR triage
a region it has no model for, gives the falsification gate a model-free peak count to check a fit
against, and seeds fits.

It is a **formal statistical contract, not a boolean in a lab coat** (contract 3):

- significance is the **exact Poisson tail** of the integrated excess over the local background, so
  it stays correct at the 30–50-count regime where a Gaussian/√N approximation breaks;
- multiple testing across energy bins is controlled by a **Bonferroni threshold** ``alpha / n_res``
  over the ``n_res`` independent resolution elements in the searched window, so the **per-scan
  false-positive rate is ≈ alpha** under a flat Poisson background (the null-FPR contract test);
- the **elastic line is excluded** (a window around ``center``) and only kept/unmasked points are
  searched.

Limitations (documented, not hidden): the background is estimated as a single flat level (robust
median), so a strongly sloped/curved background can bias significance — acceptable for the phonon
windows ISAR fits; a rolling-background refinement is a later extension. Peak width is a half-max
estimate on the background-subtracted excess, floored at the resolution.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm, poisson

from isar.schema.steering import DescriptorPeak, ScanDescriptor

MIN_POINTS = 5          # too few kept points to describe
_MAX_SIGMA = 40.0       # cap the Gaussian-equivalent σ so p→0 doesn't return inf


def _sigma_from_p(p: float) -> float:
    """One-sided Gaussian-equivalent σ of a tail probability, capped (p→0 ⇒ σ→_MAX_SIGMA)."""
    if p <= 0.0:
        return _MAX_SIGMA
    if p >= 1.0:
        return 0.0
    return float(min(_MAX_SIGMA, norm.isf(p)))


def describe_spectrum(x, counts, *, center, gauss_fwhm, lorentz_fwhm=0.0, mask=None,
                      alpha: float = 0.05, elastic_excl_fwhm: float = 1.5) -> ScanDescriptor:
    """Model-free peak descriptor for one spectrum (raw Poisson counts).

    ``center`` is the elastic-line position and ``gauss_fwhm``/``lorentz_fwhm`` the resolution (as on
    :class:`~isar.analyze.context.ScanContext`). ``alpha`` is the target per-scan false-positive rate;
    ``elastic_excl_fwhm`` sets the ±(k·FWHM) elastic exclusion. Returns a :class:`ScanDescriptor`.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(counts, dtype=float)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        x, y = x[m], y[m]
    order = np.argsort(x)
    x, y = x[order], y[order]
    n = x.size
    if n < MIN_POINTS:
        return ScanDescriptor(peaks=(), background=0.0, n_res=0.0, alpha=alpha, n_points=n)

    res = max(float(gauss_fwhm) + float(lorentz_fwhm), 1e-6)
    dx = float(np.median(np.diff(x))) if n > 1 else res
    dx = max(dx, 1e-9)
    hw_bins = max(1, int(round(0.5 * res / dx)))          # half-window in bins (~one resolution FWHM)

    # Robust flat background (peaks are a minority of the window).
    background = float(np.median(y))
    lam_win = max(background, 1e-9) * (2 * hw_bins + 1)   # expected background counts in a window

    # Searched region: exclude the elastic line + edge windows without full support.
    excl = elastic_excl_fwhm * res
    searchable = (np.abs(x - float(center)) > excl)
    searchable[:hw_bins] = False
    searchable[n - hw_bins:] = False
    span = float(x[searchable].max() - x[searchable].min()) if searchable.any() else 0.0
    # Independent tests for the Bonferroni correction = span / (decorrelation scale of the shifted
    # window statistic). Consecutive windows shift by one bin but overlap heavily; the statistic
    # decorrelates over ~the window half-width (≈ 0.5·resolution), NOT the full resolution — using
    # the resolution here would leave the per-scan FPR ~2× the target (measured).
    n_res = max(span / (0.5 * res), 1.0)
    p_thresh = alpha / n_res

    # Per-bin window-sum p-value (exact Poisson tail of the integrated counts vs background).
    idx = np.where(searchable)[0]
    cand = []
    for i in idx:
        w = y[i - hw_bins:i + hw_bins + 1]
        n_obs = float(w.sum())
        p = float(poisson.sf(n_obs - 1, lam_win)) if lam_win > 0 else (0.0 if n_obs > 0 else 1.0)
        if p < p_thresh:
            cand.append((i, p, n_obs))
    if not cand:
        return ScanDescriptor(peaks=(), background=background, n_res=n_res, alpha=alpha, n_points=n)

    # Non-maximum suppression: keep the most-significant bin, drop others within the peak's influence
    # radius (resolution + window half-width) — one physical feature integrates into many overlapping
    # windows, so a smaller radius would split it. Two features closer than this are unresolvable
    # model-free; the descriptor reports one and the fit / falsification layer disentangles.
    sup_radius = res + hw_bins * dx
    cand.sort(key=lambda c: (c[1], -c[2]))                 # smallest p, then highest window-sum
    #                                                        (break p→0 saturation ties toward the
    #                                                         true centre, not a plateau shoulder)
    chosen: list[int] = []
    for i, _p, _n in cand:
        if all(abs(x[i] - x[j]) > sup_radius for j in chosen):
            chosen.append(i)
    chosen.sort(key=lambda i: x[i])

    peaks = []
    for i in chosen:
        lo, hi = i - hw_bins, i + hw_bins + 1
        excess = float(y[lo:hi].sum() - lam_win)
        w = y[lo:hi]
        p = float(poisson.sf(float(w.sum()) - 1, lam_win)) if lam_win > 0 else 0.0
        peaks.append(DescriptorPeak(
            energy=float(x[i]), width=_estimate_fwhm(x, y, i, background, res, dx),
            integrated_intensity=excess, significance=_sigma_from_p(p), p_value=p))
    return ScanDescriptor(peaks=tuple(peaks), background=background, n_res=n_res,
                          alpha=alpha, n_points=n)


def _estimate_fwhm(x, y, i, background, res, dx) -> float:
    """Half-max FWHM of the background-subtracted excess around bin ``i``, floored at the resolution."""
    reach = max(1, int(round(2.0 * res / dx)))
    lo, hi = max(0, i - reach), min(x.size, i + reach + 1)
    exc = y[lo:hi] - background
    peak = float(exc[i - lo]) if 0 <= i - lo < exc.size else 0.0
    if peak <= 0:
        return res
    half = 0.5 * peak
    xs = x[lo:hi]
    above = xs[exc >= half]
    w = float(above.max() - above.min()) if above.size >= 2 else res
    return max(w, res)

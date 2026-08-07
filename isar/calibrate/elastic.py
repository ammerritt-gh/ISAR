"""The shared elastic-line fit — the one primitive both the energy zero (§7) and the IXS
resolution measurement need.

A resolution-limited elastic line on a reduced spectrum gives the instrument resolution (its
width) and the energy zero (its position). This fits a Voigt + flat background on a tight window
near the nominal zero (so an off-zero phonon is not mistaken for it), falling back to a half-max
width if the fit fails. ``ok`` reports whether a *significant* elastic was actually present
(peak vs Poisson noise) — a flat/absent elastic must not yield a bogus zero.

Promoted verbatim (modulo the ``ok`` flag) from the duplicated ``measure_resolution`` in
``docs/phase3_step1_check.py`` / ``docs/in8_phase1_check.py`` so the zero/resolution inputs stay
identical to the Phase-1 baseline.
"""
from __future__ import annotations

import numpy as np

from isar.analyze.lineshapes import Voigt


def fit_elastic(x, counts, *, k: float = 5.0):
    """Fit the elastic peak near E=0 → ``(center, gauss_fwhm, lorentz_fwhm, ok)``.

    ``ok`` is True iff a significant elastic is present (``max - median > k*sqrt(median)`` in the
    fit window), matching the QC-layer flatness convention. The returned center/widths are the
    Voigt fit when it converges with physical parameters, else an argmax + half-max fallback."""
    from scipy.optimize import curve_fit

    x = np.asarray(x, dtype="float64")
    counts = np.asarray(counts, dtype="float64")
    near = np.abs(x) < 2.5
    c0 = (float(x[near][int(np.argmax(counts[near]))]) if near.any()
          else float(x[int(np.argmax(counts))]))
    win = np.abs(x - c0) <= 1.5
    xs, ys = x[win], counts[win]
    bg0 = float(np.min(ys)) if ys.size else 0.0
    height = float(np.max(ys) - bg0) if ys.size else 0.0
    med = float(np.median(ys)) if ys.size else 0.0
    ok = bool(ys.size >= 3 and (float(np.max(ys)) - med) > k * np.sqrt(max(med, 1.0)))

    def model(xx, area, gfw, lfw, cen, bg):
        return Voigt(xx, area, gfw, lfw, cen) + bg

    try:
        popt, _ = curve_fit(model, xs, ys, p0=[max(height, 1.0) * 1.5, 1.3, 0.3, c0, bg0],
                            bounds=([0, 0.5, 0.0, c0 - 1.0, 0.0],
                                    [np.inf, 2.5, 1.0, c0 + 1.0, max(bg0 * 3 + 1, 1)]),
                            maxfev=20000)
        _area, gfw, lfw, cen, _bg = popt
        if 0.5 <= gfw <= 2.5 and 0.0 <= lfw <= 1.0:
            return float(cen), float(gfw), float(lfw), ok
    except Exception:                                      # noqa: BLE001
        pass
    half = bg0 + 0.5 * height
    above = xs[ys >= half]
    fwhm = float(above.max() - above.min()) if above.size >= 2 else 1.3
    return c0, min(max(fwhm, 0.5), 2.5), 0.0, ok

"""Energy-zero calibration (§7, D8) — the running, time-keyed zero series.

Fit the elastic line of calibration/elastic scans to get E0, key the points by **acquisition
time** (mono drift is temporal, not thermal — D8), and interpolate to a scan's timestamp. A scan
with no usable elastic borrows an interpolated zero; a scan with no zero available at all resolves
to ``None`` — a flagged non-result (held-for-calibration, §6), **never E0 = 0**.

The temporal key is the parsed wall-clock time from the raw header (SPEC ``#D`` line / IN8
``DATE_`` keyword), falling back to ``scan_number`` (≈ time) when no date is present/parseable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from isar.calibrate.elastic import fit_elastic

_DATE_FORMATS = ("%a %b %d %H:%M:%S %Y",    # SPEC  "Fri Mar 07 16:40:15 2025"
                 "%d-%b-%y %H:%M:%S",       # IN8   "09-Sep-25 00:00:00"
                 "%d-%b-%Y %H:%M:%S")


def fit_zero(x, counts) -> float | None:
    """The energy zero = the elastic-line center, or ``None`` if no significant elastic."""
    center, _g, _l, ok = fit_elastic(x, counts)
    return float(center) if ok else None


def _parse_dt(s):
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def scan_time(record) -> float:
    """Acquisition time (seconds) for keying the zero series: parsed from ``raw_header["D"]``
    (SPEC) or ``raw_header["keywords"]["DATE_"]`` (IN8); falls back to ``scan_number``."""
    rh = getattr(record, "raw_header", None) or {}
    s = rh.get("D")
    if not s:
        s = (rh.get("keywords") or {}).get("DATE_")
    dt = _parse_dt(s)
    if dt is not None:
        return dt.timestamp()
    return float(getattr(record, "scan_number", 0))


@dataclass(frozen=True)
class ZeroResolution:
    """How a scan's zero was obtained. ``source`` is ``"own"`` (its own elastic),
    ``"interpolated"`` (the series), or ``None`` (no zero → held-for-calibration)."""

    value: float | None
    source: str | None


class ZeroSeries:
    """Sorted (time-key, E0) points with linear interpolation. Outside the covered range — or
    on an empty series — ``interpolate`` returns ``None`` (no zero, never an extrapolated guess)."""

    def __init__(self, points):
        pts = sorted((float(k), float(v)) for k, v in points)
        self.keys = np.array([k for k, _ in pts], dtype=float)
        self.vals = np.array([v for _, v in pts], dtype=float)

    @classmethod
    def build(cls, points) -> "ZeroSeries":
        return cls(points)

    def __len__(self) -> int:
        return int(self.keys.size)

    def interpolate(self, key) -> float | None:
        if self.keys.size == 0:
            return None
        key = float(key)
        if key < self.keys[0] or key > self.keys[-1]:
            return None
        return float(np.interp(key, self.keys, self.vals))


def resolve_zero(x, counts, record, series) -> ZeroResolution:
    """Resolve a scan's energy zero: its own elastic if significant, else the interpolated
    series at its acquisition time, else ``None`` (held-for-calibration)."""
    own = fit_zero(x, counts)
    if own is not None:
        return ZeroResolution(own, "own")
    if series is not None:
        interp = series.interpolate(scan_time(record))
        if interp is not None:
            return ZeroResolution(interp, "interpolated")
    return ZeroResolution(None, None)


# --- calibration from dedicated elastic (Bragg) scans -----------------------------------------

_DEFAULT_RESOLUTION = (0.96, 0.60)   # (gauss_fwhm, lorentz_fwhm) meV — fallback for an unseen slit


@dataclass(frozen=True)
class EnergyCalibration:
    """The energy-zero + resolution an analysis needs, built once from the elastic/Bragg scans.

    ``zeros`` is a scan-keyed :class:`ZeroSeries` (interpolated to a phonon scan's number — the
    elastic scans bracket the phonon scans in scan order, so this tracks the temporal drift, D8).
    ``resolution_by_slit`` maps a slit width to the median Voigt (gauss, lorentz) FWHM measured on
    the elastic scans at that slit. A phonon scan with no zero in coverage → held-for-calibration
    (``zero`` returns ``None``, never E0 = 0)."""

    zeros: "ZeroSeries"
    resolution_by_slit: dict
    default_resolution: tuple = _DEFAULT_RESOLUTION

    def zero(self, key) -> float | None:
        return self.zeros.interpolate(key)

    def resolution(self, slit) -> tuple:
        try:
            return self.resolution_by_slit.get(int(slit), self.default_resolution)
        except (TypeError, ValueError):
            return self.default_resolution


def build_energy_calibration(elastic_scans, *, key: str = "scan_number",
                             default_resolution: tuple = _DEFAULT_RESOLUTION) -> EnergyCalibration:
    """Fit each elastic scan's line (``fit_elastic``) and assemble the zero series + per-slit
    resolution. Only significant elastics (``ok``) contribute; ``key`` chooses the series key
    (``"scan_number"`` — robust and monotonic — or ``"time"``)."""
    points: list[tuple[float, float]] = []
    by_slit: dict[int, list[tuple[float, float]]] = {}
    for s in elastic_scans:
        center, gfw, lfw, ok = fit_elastic(np.asarray(s.energy, float), np.asarray(s.counts, float))
        if not ok:
            continue
        points.append((float(getattr(s, key)), float(center)))
        by_slit.setdefault(int(s.slit), []).append((float(gfw), float(lfw)))
    resolution_by_slit = {
        slit: (float(np.median([g for g, _ in vals])), float(np.median([l for _, l in vals])))
        for slit, vals in by_slit.items()
    }
    return EnergyCalibration(ZeroSeries.build(points), resolution_by_slit, default_resolution)

"""Injectable defects (§4) — each maps to a real failure we've hit, and each is a *recording hook*:
it mutates a `RunScan` and logs what it did into `scan.truth["quirks"]`. The QC layer (next push)
reads that log to score masking exactly (did we mask the injected points, and only those?).

This push builds them; the round-trip gate (Gate A) runs **clean** (no quirks). Exercising them is
the QC step's job (§10 step 4). Mutations are in place; each returns the scan for chaining.
"""
from __future__ import annotations

import numpy as np


def _record(scan, **kw) -> None:
    scan.truth.setdefault("quirks", []).append(kw)


def _det(scan, detector_id):
    for d in scan.detectors:
        if d.detector_id == detector_id:
            return d
    raise KeyError(f"no detector {detector_id!r} in scan {scan.scan_number}")


def inject_spike(scan, detector_id: str, index: int, magnitude: float | None = None):
    """A single-channel cosmic-ray spike on one analyzer."""
    d = _det(scan, detector_id)
    if magnitude is None:
        magnitude = 20.0 * float(np.median(d.counts)) + 100.0
    d.counts[index] = d.counts[index] + magnitude
    _record(scan, type="spike", detector=detector_id, index=int(index), magnitude=float(magnitude))
    return scan


def inject_miniscan_cosmic_ray(scan, detector_id: str, point: int, mini: int,
                               magnitude: float | None = None):
    """A cosmic ray in a single raw miniscan pass: one (point, pass) of one analyzer is spiked.

    Adds to `miniscan_counts[point, mini]` (keeping the legit counts that were there), so the
    tracked rejection must reject exactly this pass and recover the clean combined value. Records the
    truth so a round-trip can assert it rejected this pass and only this pass.
    """
    d = _det(scan, detector_id)
    if d.miniscan_counts is None:
        raise ValueError(f"detector {detector_id!r} has no miniscan ensemble (not a raw run)")
    if magnitude is None:
        magnitude = 50.0 * float(np.median(d.miniscan_counts)) + 5000.0
    d.miniscan_counts[point, mini] += magnitude
    d.counts = d.miniscan_counts.sum(axis=1)
    _record(scan, type="miniscan_cosmic_ray", detector=detector_id,
            point=int(point), mini=int(mini), magnitude=float(magnitude))
    return scan


def inject_stale_head(scan, detector_id: str, n: int = 3, level: float | None = None):
    """A leading run of equal, high, low-scatter points (the HERIX stale-first-points bug)."""
    d = _det(scan, detector_id)
    if level is None:
        level = float(np.max(d.counts))
    d.counts[:n] = level
    _record(scan, type="stale_head", detector=detector_id, indices=list(range(n)), level=float(level))
    return scan


def inject_truncation(scan, n_keep: int):
    """An early-truncated scan: every shared/per-detector array is shortened to n_keep points."""
    n0 = len(scan.energy)
    scan.energy = scan.energy[:n_keep]
    scan.monitor = scan.monitor[:n_keep]
    scan.time = scan.time[:n_keep]
    for d in scan.detectors:
        d.counts = d.counts[:n_keep]
    _record(scan, type="truncated", kept=int(n_keep), original=int(n0))
    return scan


def inject_flat(scan, detector_id: str, level: float | None = None):
    """A flat-within-noise spectrum (beam down / wrong sample): no peak on one analyzer."""
    d = _det(scan, detector_id)
    if level is None:
        level = float(np.median(d.counts))
    d.counts[:] = level
    _record(scan, type="empty", detector=detector_id, level=float(level))
    return scan


def inject_monitor_dropout(scan, index: int):
    """A monitor==0 region: the shared monitor drops out at one point (affects all analyzers)."""
    scan.monitor[index] = 0.0
    _record(scan, type="monitor_dropout", index=int(index))
    return scan


def inject_sensor_divergence(scan, value: float = 298.0):
    """A disconnected/divergent temperature sensor (IN8 TT≈298 vs RT setpoint)."""
    scan.temperature_sensor = float(value)
    _record(scan, type="sensor_divergence", sensor=float(value), setpoint=float(scan.temperature_setpoint))
    return scan


def inject_dead_detector(scan, detector_id: str, level: float | None = None):
    """One analyzer flat/noise while the others carry signal (a dead/off analyzer)."""
    d = _det(scan, detector_id)
    if level is None:
        level = float(np.median(d.counts)) * 0.1
    d.counts[:] = level
    _record(scan, type="dead_detector", detector=detector_id, level=float(level))
    return scan

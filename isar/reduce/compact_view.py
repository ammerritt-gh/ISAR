"""compact_view — the downsampled, LLM-facing picture of a flagged scan (PHASE_2_DESIGN §6).

The adjudicator never sees the raw store. It needs the *shape* of a flagged scan: a downsampled
trace, summary stats, and — crucially — the flagged indices with their immediate neighbours, so the
model can *see* the spike or window edge it is judging. This is a pure formatter call, so storage
verbosity is fully decoupled from LLM token cost. ``_flagged_records`` selects which records are
worth a call at all (measurements that carry at least one flag).
"""
from __future__ import annotations

import numpy as np

from isar.schema import axes

MAX_TRACE_POINTS = 32      # cap the downsampled trace handed to the model
_NEIGHBORHOOD = 3          # points kept either side of a flagged index (full resolution)


def compact_view(record) -> dict:
    """A compact, JSON-able view of one (flagged) scan for the adjudicator."""
    energy = _energy(record)
    n_points = len(energy) if energy is not None else _first_len(record)
    return {
        "scan_number": int(record.scan_number),
        "scan_type": record.scan_type,
        "temperature": (float(record.temperature.setpoint[0])
                        if len(record.temperature.setpoint) else None),
        "energy_range": ([float(np.min(energy)), float(np.max(energy))]
                         if energy is not None and len(energy) else None),
        "n_points": int(n_points),
        "scan_flags": [_flag(f) for f in record.scan_flags],
        "detectors": {det: _detector_view(sp) for det, sp in record.spectra.items()},
    }


def _detector_view(sp) -> dict:
    inten = np.asarray(sp.intensity, dtype=float)
    return {
        "q": [round(float(v), 5) for v in sp.q],
        "intensity": _downsample(inten),
        "summary": {
            "max": float(np.max(inten)) if inten.size else None,
            "mean": round(float(np.mean(inten)), 3) if inten.size else None,
            "argmax": int(np.argmax(inten)) if inten.size else None,
            "edges": [float(inten[0]), float(inten[-1])] if inten.size else None,
        },
        "flagged_points": [_flagged_point(inten, pf) for pf in sp.point_flags],
        "spectrum_flags": [_flag(f) for f in sp.spectrum_flags],
    }


def _flagged_point(inten: np.ndarray, pf) -> dict:
    i = int(pf.index)
    lo, hi = max(0, i - _NEIGHBORHOOD), min(len(inten), i + _NEIGHBORHOOD + 1)
    return {
        "index": i,
        "type": pf.type,
        "reason": pf.reason,
        "neighbors": [float(v) for v in inten[lo:hi]],
    }


def _downsample(inten: np.ndarray) -> list:
    if inten.size <= MAX_TRACE_POINTS:
        return [float(v) for v in inten]
    idx = np.linspace(0, inten.size - 1, MAX_TRACE_POINTS).round().astype(int)
    return [float(inten[i]) for i in idx]


def _flag(f) -> dict:
    return {"type": f.type, "severity": f.severity, "reason": f.reason}


def _energy(record):
    sc = axes.scanned_axes(record)
    return sc[0].values if sc else None


def _first_len(record) -> int:
    if not record.spectra:
        return 0
    return len(next(iter(record.spectra.values())).intensity)


def _flagged_records(records):
    """Yield measurement records carrying at least one point/spectrum/scan flag (skip clean scans)."""
    for r in records:
        if r.scan_type != "measurement":
            continue
        if _has_flag(r):
            yield r


def _has_flag(record) -> bool:
    if record.scan_flags:
        return True
    return any(sp.point_flags or sp.spectrum_flags for sp in record.spectra.values())

"""Deterministic scan/spectrum QC (§7.2) — flag, never delete (§0.4); survive mess (§0.5).

`qc_scans(record, card)` appends scan-/spectrum-level Flags for the conditions a human must see but the
reduction step doesn't surface:

  - **truncated** — fewer points than expected (a `np <N>` count parsed from the scan command when
    present — forward-compat for IN8's `np 41` — else the card `min_points` floor).
  - **empty** — every measurement spectrum flat within Poisson noise: "no real data" (beam down / wrong
    sample). Scan-level.
  - **dead_detector** — one spectrum flat while sibling analyzers carry signal (a *candidate*, since a
    flat analyzer can be physics). Spectrum-level. Single-detector instruments never trip this (no
    siblings) — they get `empty` instead.
  - **sensor_divergence** — a temperature sensor disagreeing with the setpoint beyond the card tolerance
    (the IN8 `TT≈298` disconnect). Flags the scan and marks that sensor's `valid_mask`; the setpoint
    stays the trusted provenance (§5.4) — we never overwrite it with a wrong number.

Nothing here touches the signal; the only array written is a divergent sensor's `valid_mask`. Each
check is wrapped so a malformed spectrum yields a "couldn't check" note, never a raised exception.
"""
from __future__ import annotations

import re

import numpy as np

from isar.schema import axes, scan

_DEFAULT_EMPTY_K = 5.0
_NP_RE = re.compile(r"\bnp\s+(\d+)\b", re.IGNORECASE)


def _is_flat(intensity, k) -> bool | None:
    """True if the spectrum shows no peak above its Poisson noise; None if undeterminable."""
    y = np.asarray(intensity, dtype="float64")
    y = y[np.isfinite(y)]
    if y.size < 3:
        return None
    med = float(np.median(y))
    excursion = float(np.max(y)) - med
    noise = k * np.sqrt(max(med, 1.0))
    return excursion <= noise


def _flag(record, type_, severity, reason):
    record.scan_flags.append(scan.Flag(type_, severity, reason))


def _check_truncated(record, pol):
    scanned = axes.scanned_axes(record)
    if not scanned:
        return
    nP = int(len(scanned[0].values))
    command = str(record.raw_header.get("S", "")) if isinstance(record.raw_header, dict) else ""
    m = _NP_RE.search(command)
    commanded = int(m.group(1)) if m else None
    floor = int(pol.min_points)
    if commanded is not None and nP < commanded:
        severity = "error" if nP < floor else "warning"
        _flag(record, "truncated", severity, f"kept {nP} of {commanded} commanded points")
    elif nP < floor:
        _flag(record, "truncated", "error", f"only {nP} points (below min_points={floor})")


def _check_empty_and_dead(record, pol):
    if record.scan_type != "measurement":
        return
    k = float((pol.qc or {}).get("empty_k", _DEFAULT_EMPTY_K))
    flat: dict = {}
    for det_id, sp in record.spectra.items():
        try:
            flat[det_id] = _is_flat(sp.intensity, k)
        except Exception:
            flat[det_id] = None
    decided = {d: v for d, v in flat.items() if v is not None}
    if not decided:
        return
    if all(decided.values()):
        # every spectrum we could judge is flat → the whole scan is empty (no real data)
        _flag(record, "empty", "error", "all spectra flat within Poisson noise — no real data")
        return
    # otherwise: a flat spectrum with a live sibling is a dead-detector candidate
    for det_id, is_flat in decided.items():
        if is_flat:
            record.spectra[det_id].spectrum_flags.append(
                scan.Flag("dead_detector", "warning",
                          "flat within noise while sibling analyzers carry signal (candidate)"))


def _check_sensor_divergence(record, pol):
    tol = float((pol.temperature_rule or {}).get("divergence_tol", 5.0))
    temp = record.temperature
    setpoint = np.asarray(temp.setpoint, dtype="float64")
    for name, sensor in temp.sensors.items():
        try:
            vals = np.asarray(sensor.values, dtype="float64")
            n = min(len(vals), len(setpoint))
            if n == 0:
                continue
            diff = np.abs(vals[:n] - setpoint[:n])
            diverge = np.isfinite(diff) & (diff > tol)
            if diverge.any():
                sensor.valid_mask[:n] = sensor.valid_mask[:n] & ~diverge
                gap = float(np.max(diff[diverge]))
                _flag(record, "sensor_divergence", "warning",
                      f"sensor {name!r} diverges from setpoint by up to {gap:.1f} "
                      f"(tol {tol:.1f}); provenance = setpoint")
        except Exception:
            _flag(record, "sensor_divergence", "info",
                  f"could not check sensor {name!r} divergence")


def qc_scans(record, card):
    """Append scan/spectrum-level QC flags in place; never deletes, never raises. Returns record."""
    pol = card.policy
    for check in (_check_truncated, _check_empty_and_dead, _check_sensor_divergence):
        try:
            check(record, pol)
        except Exception:  # §0.5: a QC failure is a flag-or-skip, never a crashed batch
            record.scan_flags.append(scan.Flag("qc_error", "info", f"{check.__name__} failed"))
    return record

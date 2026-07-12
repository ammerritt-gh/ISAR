"""Per-unit (scan × detector) data files for the GUI inspector (Pass 2b Phase 2).

The atomic GUI unit is one *scan × analyzer* (== a ledger entry ``exp:scan:det``). To let the
read-only viewer draw a unit's raw counts without re-parsing raw instrument files, ``ingest_batch``
writes one small JSON per ledger entry under ``data/ingest/``. The subdir keeps these clear of the
drive loop's ``data/j-*.json`` (whose non-recursive ``glob("*.json")`` in ``workspace_api.evaluate``
never sees the subdir) and cannot collide with a job id.

Schema ``isar.unit_data/1`` (all arrays verbatim, honest — D3: σ²=0 stays 0; hard rule 4: ``x`` is
the RAW abscissa, never energy-zero-shifted; the axis block records the 3-state calibration so a
consumer can label honestly and never assumes ``E0=0``).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SCHEMA = "isar.unit_data/1"
INGEST_SUBDIR = "ingest"


def _sanitize(part) -> str:
    """One path component from an id, same idiom as drive ``scan_writer`` (never trust an id into a
    path): keep ``[A-Za-z0-9-_.]``, replace the rest with ``_``; empty degrades to ``x``."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(part))
    return s or "x"


def unit_data_path(data_dir, exp, scan, det) -> Path:
    """``<data_dir>/ingest/ingest-<exp>-<scan:04d>-<det>.json`` (each component sanitized)."""
    fname = f"ingest-{_sanitize(exp)}-{int(scan):04d}-{_sanitize(det)}.json"
    return Path(data_dir) / INGEST_SUBDIR / fname


def _tolist(arr) -> list:
    return np.asarray(arr).tolist()


def write_unit_data(rec, det, data_dir) -> Path:
    """Write the ``isar.unit_data/1`` file for one ``(rec, detector)`` unit; returns the path.

    ``rec`` is a (reduced + QC'd) :class:`~isar.schema.scan.ScanRecord`; ``det`` a key into
    ``rec.spectra``. Arrays are stored verbatim from the record — counts and variance honest (σ²=0
    kept), mask never applied, ``x`` the raw scanned abscissa (never shifted). Creates ``data/ingest/``
    as needed. The caller isolates write failures (ingest never aborts a batch on one bad unit).
    """
    from isar.schema.axes import scanned_axes

    sp = rec.spectra[det]
    sc = scanned_axes(rec) if getattr(rec, "scan_axes", None) else []
    ax = sc[0] if sc else None
    x = ax.values if ax is not None else np.arange(np.asarray(sp.intensity).size)
    ec = rec.energy_calibration
    payload = {
        "schema": SCHEMA,
        "scan_id": f"{rec.experiment_id}:{rec.scan_number}:{det}",
        "experiment_id": rec.experiment_id,
        "scan_number": rec.scan_number,
        "detector_id": det,
        "x": _tolist(x),                       # RAW abscissa — never energy-zero-shifted (hard rule 4)
        "counts": _tolist(sp.intensity),       # verbatim raw counts (combined, post-rejection)
        "variance": _tolist(sp.variance),      # honest Poisson variance; σ²=0 stays 0 (D3)
        "mask": _tolist(np.asarray(sp.mask, dtype=bool)),
        "axis": {
            "name": getattr(ax, "name", None),
            "kind": getattr(ax, "kind", None),
            "units": getattr(ax, "units", None),
            "raw_abscissa_kind": getattr(ec, "raw_abscissa_kind", None),
            "calibration_status": getattr(ec, "status", None),
        },
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = unit_data_path(data_dir, rec.experiment_id, rec.scan_number, det)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_unit_data(path) -> dict | None:
    """Tolerant read of a unit-data JSON: the parsed dict, or ``None`` on any missing/parse problem
    (a read surface must never raise; a torn or pre-Phase-2 workspace simply has no unit file)."""
    p = Path(path)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _provenance_extras(rec) -> dict:
    """Additive, strictly-tolerant provenance keys stamped onto a ledger entry at ingest.

    Every key is best-effort: a missing/oddly-shaped source silently omits that key (never raises,
    never a placeholder). Existing ingest provenance (source_file, T, q, ingested_at) is unaffected.
    """
    extras: dict = {}
    rh = getattr(rec, "raw_header", None)
    rh = rh if isinstance(rh, dict) else {}

    # q_target — the scan's TARGET #Q (S30 only), distinct from a spectrum's per-analyzer q. Only a
    # finite 3-list qualifies (group/inspector metadata; never a per-unit coordinate).
    q_t = rh.get("Q")
    if isinstance(q_t, (list, tuple)) and len(q_t) == 3:
        try:
            vals = [float(v) for v in q_t]
            if all(math.isfinite(v) for v in vals):
                extras["q_target"] = vals
        except (TypeError, ValueError):
            pass

    # command — SPEC-family #S; tolerant per-plugin fallbacks (IN8 keyword COMND, generic).
    cmd = rh.get("S")
    if cmd is None:
        kw = rh.get("keywords")
        if isinstance(kw, dict):
            cmd = kw.get("COMND") or kw.get("command")
    if cmd is None:
        cmd = rh.get("command")
    if cmd:
        extras["command"] = str(cmd)

    norm = getattr(rec, "normalization", None)
    if norm is not None:
        cm = getattr(norm, "counting_mode", None)
        if cm is not None:
            extras["counting_mode"] = cm
        preset = getattr(norm, "preset", None)
        if preset is not None:
            try:
                extras["preset"] = float(preset)
            except (TypeError, ValueError):
                pass

    # scan_axis descriptor + n_points from the single scanned axis.
    from isar.schema.axes import scanned_axes
    sc = scanned_axes(rec) if getattr(rec, "scan_axes", None) else []
    if sc:
        ax = sc[0]
        extras["scan_axis"] = {"name": ax.name, "kind": ax.kind, "units": ax.units}
        try:
            extras["n_points"] = int(np.asarray(ax.values).size)
        except (TypeError, ValueError):
            pass

    # 3-state energy calibration, verbatim (never collapse to a 2-state absolute-or-not world).
    ec = getattr(rec, "energy_calibration", None)
    if ec is not None:
        if getattr(ec, "status", None) is not None:
            extras["energy_status"] = ec.status
        if getattr(ec, "raw_abscissa_kind", None) is not None:
            extras["raw_abscissa_kind"] = ec.raw_abscissa_kind

    # B — median applied-field setpoint, only when field data is present (schema/scan.py EnvField).
    env = getattr(rec, "environment", None)
    field = getattr(env, "field", None) if env is not None else None
    setpoint = getattr(field, "setpoint", None) if field is not None else None
    if setpoint is not None:
        try:
            arr = np.asarray(setpoint, dtype=float)
            if arr.size and np.any(np.isfinite(arr)):
                extras["B"] = float(np.nanmedian(arr))
        except (TypeError, ValueError):
            pass

    return extras

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

import base64
import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from isar.schema.scan import Q_SOURCE_RUNG

log = logging.getLogger(__name__)

SCHEMA = "isar.unit_data/1"
INGEST_SUBDIR = "ingest"


def _sanitize(part) -> str:
    """One path component from an id, same idiom as drive ``scan_writer`` (never trust an id into a
    path): keep ``[A-Za-z0-9-_.]``, replace the rest with ``_``; empty degrades to ``x``."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(part))
    return s or "x"


def legacy_unit_data_path(data_dir, exp, scan, det) -> Path:
    """Return the original lossy filename used by early Phase-2 workspaces.

    This is deliberately retained only for reads/backfills.  Sanitising identity components is not
    injective (``a/b`` and ``a:b`` both become ``a_b``), so new writes must never use this path.
    """
    fname = f"ingest-{_sanitize(exp)}-{int(scan):04d}-{_sanitize(det)}.json"
    return Path(data_dir) / INGEST_SUBDIR / fname


def _encode_component(value) -> str:
    """Injective, Windows-safe encoding for one persisted identity component."""
    raw = str(value).encode("utf-8")
    # URL-safe base64 has no path separators or Windows-reserved characters.  A non-empty source
    # produces a non-empty encoding; represent the unlikely empty identity explicitly as ``x``.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") or "x"


def unit_data_path(data_dir, exp, scan, det) -> Path:
    """Return the canonical v2 per-unit path with lossless complete identity encoding.

    ``isar.unit_data/1`` remains the payload schema; only its filename is upgraded.  Legacy
    sanitized paths are still read through :func:`unit_data_paths_for_read`.
    """
    fname = f"ingest-v2-e{_encode_component(exp)}-s{int(scan)}-d{_encode_component(det)}.json"
    return Path(data_dir) / INGEST_SUBDIR / fname


def unit_data_paths_for_read(data_dir, exp, scan, det) -> tuple[Path, ...]:
    """Canonical then legacy candidate paths for one atomic unit, without duplicate paths."""
    canonical = unit_data_path(data_dir, exp, scan, det)
    legacy = legacy_unit_data_path(data_dir, exp, scan, det)
    return (canonical,) if legacy == canonical else (canonical, legacy)


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
    # A unit file is a small immutable snapshot.  Write a sibling temporary file and replace it so
    # an interrupted ingest cannot turn a valid previous file into a truncated JSON document.
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("unit data: could not remove failed temporary write %s (%s)", tmp_name, exc)
    return path


def _valid_identity(d: dict, expected_identity: tuple | None) -> bool:
    """Validate the additive identity fields, optionally against the requested atomic unit."""
    exp = d.get("experiment_id")
    scan = d.get("scan_number")
    det = d.get("detector_id")
    if not isinstance(exp, str) or not exp or isinstance(scan, bool) or not isinstance(scan, int):
        return False
    if not isinstance(det, str) or not det:
        return False
    if d.get("scan_id") != f"{exp}:{scan}:{det}":
        return False
    return expected_identity is None or (exp, scan, det) == expected_identity


def _valid_payload(d: dict, expected_identity: tuple | None = None) -> bool:
    """Conservative validation for a read-only surface; invalid data degrades to missing.

    The validator intentionally does not adjust values: zero variance stays zero, masks are kept
    verbatim, and raw axes are never shifted.  It establishes only the schema and aligned-vector
    invariants needed for a reliable plot.
    """
    if not isinstance(d, dict) or d.get("schema") != SCHEMA or not _valid_identity(d, expected_identity):
        return False
    if not isinstance(d.get("axis"), dict):
        return False
    arrays = []
    for name in ("x", "counts", "variance"):
        value = d.get(name)
        if not isinstance(value, list):
            return False
        try:
            arr = np.asarray(value, dtype=float)
        except (TypeError, ValueError):
            return False
        if arr.ndim != 1:
            return False
        arrays.append(arr)
    mask = d.get("mask")
    if not isinstance(mask, list):
        return False
    try:
        mask_array = np.asarray(mask)
    except (TypeError, ValueError):
        return False
    if (mask_array.ndim != 1
            or not all(isinstance(value, (bool, np.bool_)) for value in mask)):
        return False
    size = arrays[0].size
    return all(arr.size == size for arr in arrays[1:]) and len(mask) == size


def read_unit_data(path, *, expected_identity: tuple | None = None) -> dict | None:
    """Tolerant read of a unit-data JSON: the parsed dict, or ``None`` on any missing/parse problem
    (a read surface must never raise; a torn or pre-Phase-2 workspace simply has no unit file).
    When ``expected_identity`` is supplied, a payload that belongs to another unit is also treated
    as missing.  This closes collisions from the legacy lossy filename scheme without making the
    viewer raise on a damaged workspace.
    """
    p = Path(path)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if _valid_payload(d, expected_identity) else None


def read_unit_data_for_unit(data_dir, exp, scan, det) -> dict | None:
    """Read the canonical file, then an older sanitized path, for exactly one requested unit."""
    identity = (str(exp), int(scan), str(det))
    for path in unit_data_paths_for_read(data_dir, *identity):
        rec = read_unit_data(path, expected_identity=identity)
        if rec is not None:
            return rec
    return None


def canonical_unit_data_is_valid(data_dir, exp, scan, det) -> bool:
    """Whether the current lossless path holds valid data for this exact identity."""
    identity = (str(exp), int(scan), str(det))
    return read_unit_data(unit_data_path(data_dir, *identity), expected_identity=identity) is not None


def unit_data_artifact_exists(data_dir, exp, scan, det) -> bool:
    """Whether either supported per-unit filename exists, even if its contents are invalid.

    Callers use this to distinguish an old drive-only workspace (where scan-level fallback is safe)
    from a damaged atomic-unit artifact (which must remain visibly missing rather than borrowing a
    potentially different detector's legacy scan-level file).
    """
    return any(path.is_file() for path in unit_data_paths_for_read(data_dir, exp, scan, det))


def scan_temperature(rec) -> float | None:
    """The scan's median setpoint temperature, or ``None`` when it is not a finite number (a scan
    whose temperature is missing carries NaN; it must not be stamped, matched or pooled as a value)."""
    arr = np.asarray(rec.temperature.setpoint, dtype=float)
    if not arr.size:
        return None
    t = float(np.median(arr))
    return t if math.isfinite(t) else None


def _flag_rows(flags) -> list:
    return [{"type": f.type, "severity": f.severity, "reason": f.reason} for f in flags]


def _provenance_extras(rec, det=None) -> dict:
    """Additive, strictly-tolerant provenance keys stamped onto a ledger entry at ingest.

    Every key is best-effort: a missing/oddly-shaped source silently omits that key (never raises,
    never a placeholder). Existing ingest provenance (source_file, T, q, ingested_at) is unaffected.
    With ``det`` the analyzer's own keys are added too: its facts, its Q source and rung, its flags.
    """
    extras: dict = {}
    # Facts are {"value", "source"[, "conflicts"]}: the inspector shows each value with its source.
    if getattr(rec, "facts", None):
        extras["scan_facts"] = rec.facts
    if getattr(rec, "scan_flags", None):
        extras["scan_flags"] = _flag_rows(rec.scan_flags)
    sp = rec.spectra.get(det) if det is not None else None
    if sp is not None:
        if sp.facts:
            extras["analyzer_facts"] = sp.facts
        extras["q_source"] = sp.q_source
        extras["q_rung"] = Q_SOURCE_RUNG.get(sp.q_source)
        if sp.spectrum_flags:
            extras["spectrum_flags"] = _flag_rows(sp.spectrum_flags)
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

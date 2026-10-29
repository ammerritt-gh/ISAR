"""The per-scan import record ``report/intake_scans.json``: the file facts of every BL43 scan read.

Machine-written by ``isar ingest`` and the BL43 ``isar run``; the GUI never writes it. One row per
measurement scan, keyed ``"<experiment_id>:<scan_number>"``: the file values with their sources (never a
user value), each analyzer's Q, count record and exposure source, the scan flags, and what the reader left
out (the ``import`` block). A top-level ``files`` section lists the files of a batch that were not read,
and why. Rows merge by scan key, a scan absent from a batch keeps its row, and the file is atomically
replaced only when a row or a file entry changed, so an idempotent re-ingest leaves it byte-identical.
Imports only stdlib and ``isar.schema`` at module level.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from isar.schema import _json, axes
from isar.schema.scan import exposure_for

INTAKE_SCANS = "report/intake_scans.json"
SCHEMA = "isar.intake-scans/1"


def applies(card) -> bool:
    """The intake records are BL43's alone."""
    return card.policy.plugin == "sp8_bl43"


def _finite(obj):
    """A JSON-able tree with every non-finite float as None (the record is strict JSON)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _counts(sp) -> dict | None:
    fact = (sp.facts or {}).get("count_recovery")
    if not fact or not isinstance(fact.get("value"), dict):
        return None
    flag = next((f for f in sp.spectrum_flags if f.type == "counts_not_integer"), None)
    level = None if flag is None else ("warning" if flag.severity == "warning" else "notice")
    return {**fact["value"], "level": level}


def _exposure(record, det: str, energy) -> str:
    """The exposure source the fit stage will use for one analyzer: "monitor", "time" or "missing"."""
    # imported here: the intake read side stays free of scipy, which isar.analyze pulls in
    from isar.analyze.context import select_exposure
    sp = record.spectra[det]
    norm = exposure_for(record, det) if sp.monitor is not None or sp.time is not None else record.normalization
    return select_exposure(norm, energy.shape, sp.mask).source or "missing"


def _import_block(raw: dict) -> dict | None:
    """What the reader recorded as left out (None for a record that carries none, e.g. another format)."""
    if "sidecar_state" not in raw:
        return None
    return {"sidecar": {"state": raw["sidecar_state"], "reason": raw.get("sidecar_problem")},
            "left_out": dict(raw.get("left_out") or {}),
            "paths": dict(raw.get("paths") or {}),
            "unrecognised_lines": list(raw.get("unrecognised_lines") or []),
            "unparsed_lines": list(raw.get("unparsed_lines") or [])}


def scan_row(record, data_revision: str) -> dict:
    """The import row of one parsed, reduced and QC'd record; ``data_revision`` is its ``content_hash``."""
    raw = record.raw_header or {}
    label = ((record.facts or {}).get("scan_label") or {}).get("value")
    energy = axes.scanned_axes(record)[0].values
    analyzers = {det: {"q": None if sp.q is None else [float(v) for v in sp.q], "q_source": sp.q_source,
                       "counts": _counts(sp), "exposure": _exposure(record, det, energy),
                       # the file's own |Q| (the `#Q=` trailer, 1/nm): what the gpi check compares against
                       "Q_nm": ((sp.facts or {}).get("Q_nm") or {}).get("value")}
                 for det, sp in sorted(record.spectra.items())}
    row = {
        "scan": f"{record.experiment_id}:{record.scan_number}",
        "experiment_id": record.experiment_id,
        "scan_number": record.scan_number,
        "set": label if label is not None else record.source.file,
        "data_revision": data_revision,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "files": dict(raw.get("files") or {}),
        "sidecar": raw.get("sidecar"),
        "facts": _json.to_jsonable(record.facts or {}),
        "analyzers": _json.to_jsonable(analyzers),
        "flags": [{"type": f.type, "severity": f.severity, "reason": f.reason} for f in record.scan_flags],
    }
    if (gaps := _import_block(raw)) is not None:
        row["import"] = gaps
    return _finite(json.loads(json.dumps(row)))   # plain JSON types, as a re-read gives them


def _load(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("scans"), dict):
        raise ValueError(f"{path} is not an {SCHEMA} record")
    return doc


def read_scans(root) -> dict[str, dict]:
    """Scan key -> row; ``{}`` when nothing was imported. ValueError when the file is malformed."""
    return dict(_load(Path(root) / INTAKE_SCANS).get("scans", {}))


def _same(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k != "imported_at"} == {k: v for k, v in b.items() if k != "imported_at"}


def _merge_files(old: dict, seen: Mapping[str, dict | None], now: str) -> dict:
    """The ``files`` section after a batch: a path mapped to None loses its entry, any other gets
    ``seen`` (``class``, ``reason``, ``set``) and keeps its ``seen_at`` when nothing else changed."""
    files = dict(old)
    for path, entry in seen.items():
        if entry is None:
            files.pop(path, None)
            continue
        prev = files.get(path)
        keep = prev is not None and {k: v for k, v in prev.items() if k != "seen_at"} == entry
        files[path] = prev if keep else {**entry, "seen_at": now}
    return files


def record_import(root, rows: Iterable[dict], *, seen_files: Mapping[str, dict | None] | None = None) -> bool:
    """Merge ``rows`` into the record; True when the file was rewritten.

    A re-imported scan's row is replaced (keys this writer does not make are kept) and keeps its
    ``imported_at`` when nothing else changed. ``seen_files`` (resolved path -> entry, or None to drop it)
    updates the ``files`` section of files not read; a path it does not name keeps its entry. A batch
    with no row but a changed ``files`` entry still writes the record. Other top-level keys are kept.
    shortcut: read-modify-write with no lock; two importers at once could drop a row. The GUI runs one
    ``isar ingest``/``isar run`` at a time; take the intake lock if two can ever overlap.
    """
    root = Path(root)
    path = root / INTAKE_SCANS
    doc = _load(path)
    old = doc.get("scans", {})
    merged = dict(old)
    changed = False
    for row in rows:
        prev = old.get(row["scan"])
        new = row if prev is None else {**prev, **row}
        if prev is not None and _same(new, prev):
            continue
        merged[row["scan"]] = new
        changed = True
    out = {**doc, "schema": SCHEMA, "scans": merged}
    if seen_files is not None:
        files = _merge_files(doc.get("files") or {}, seen_files,
                             datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        if files != (doc.get("files") or {}):
            out["files"], changed = files, True
    if not changed:
        return False
    from isar.session.atomic import replace_retrying   # imported here: the read side never loads isar.session
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8", newline="\n")
    replace_retrying(tmp, path)
    return True

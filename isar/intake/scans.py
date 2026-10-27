"""The per-scan import record ``report/intake_scans.json``: the file facts of every BL43 scan read.

Machine-written by ``isar ingest`` and the BL43 ``isar run``; the GUI never writes it. One row per
measurement scan, keyed ``"<experiment_id>:<scan_number>"``: the file values with their sources (never a
user value), each analyzer's Q and count record, and the scan flags. Rows merge by scan key, a scan absent
from a batch keeps its row, and the file is atomically replaced only when a row changed, so an idempotent
re-ingest leaves it byte-identical. Imports only stdlib and ``isar.schema`` at module level.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from isar.schema import _json

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


def scan_row(record, data_revision: str) -> dict:
    """The import row of one parsed, reduced and QC'd record; ``data_revision`` is its ``content_hash``."""
    raw = record.raw_header or {}
    label = ((record.facts or {}).get("scan_label") or {}).get("value")
    analyzers = {det: {"q": None if sp.q is None else [float(v) for v in sp.q], "q_source": sp.q_source,
                       "counts": _counts(sp)}
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


def record_import(root, rows: Iterable[dict]) -> bool:
    """Merge ``rows`` into the record; True when the file was rewritten.

    A re-imported scan's row is replaced (keys this writer does not make are kept) and keeps its
    ``imported_at`` when nothing else changed. Other top-level keys are kept.
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
    if not changed:
        return False
    from isar.session.atomic import replace_retrying   # imported here: the read side never loads isar.session
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**doc, "schema": SCHEMA, "scans": merged}, indent=2, sort_keys=True,
                              ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    replace_retrying(tmp, path)
    return True

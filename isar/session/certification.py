"""Deterministic, append-only checkpoints for a final certification run.

The drive process creates the immutable manifest and passes only its path through the runner
contract.  This module lives on the session side of that boundary: it validates the manifest and
stores pure fitted units before the parent applies any ledger or dispersion-prior mutations.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .state import ScanEntry


SCHEMA = "isar.certification/1"
UNIT_SCHEMA = "isar.certification.unit/1"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _entry_from_dict(row: dict) -> ScanEntry:
    return ScanEntry(
        scan_id=str(row["scan_id"]), scan_number=int(row.get("scan_number", 0)),
        scan_type=str(row.get("scan_type", "")), status=str(row.get("status", "")),
        content_hash=str(row.get("content_hash", "")),
        guidelines_hash=str(row.get("guidelines_hash", "")),
        detectors=dict(row.get("detectors") or {}), reasons=list(row.get("reasons") or []),
        provenance=dict(row.get("provenance") or {}))


class CheckpointStore:
    """Validated checkpoint revision addressed by its immutable manifest fingerprint."""

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path).resolve()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != SCHEMA:
            raise ValueError(f"unsupported certification manifest schema: {manifest.get('schema')!r}")
        fingerprint = str(manifest.get("fingerprint") or "")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("certification manifest has an invalid fingerprint")
        canonical = dict(manifest)
        canonical.pop("fingerprint", None)
        actual = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if actual != fingerprint:
            raise ValueError("certification manifest fingerprint does not match its content")
        if self.manifest_path.parent.name != fingerprint:
            raise ValueError("certification manifest is not in its fingerprinted revision")
        self.manifest = manifest
        self.root = self.manifest_path.parent

    def _unit_path(self, unit_key: str) -> Path:
        digest = hashlib.sha256(unit_key.encode("utf-8")).hexdigest()
        return self.root / "units" / f"{digest}.json"

    def load(self, unit_key: str, *, content_hash: str, guidelines_hash: str = "") -> ScanEntry | None:
        path = self._unit_path(unit_key)
        if not path.exists():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("schema") != UNIT_SCHEMA or row.get("unit_key") != unit_key:
            raise ValueError(f"invalid certification checkpoint {path}")
        entry = _entry_from_dict(row.get("entry") or {})
        if entry.content_hash != content_hash or entry.guidelines_hash != guidelines_hash:
            return None
        return entry

    def save(self, unit_key: str, entry: ScanEntry) -> Path:
        path = self._unit_path(unit_key)
        if path.exists():
            existing = self.load(unit_key, content_hash=entry.content_hash,
                                 guidelines_hash=entry.guidelines_hash)
            if existing is not None:
                return path
            raise ValueError(
                f"checkpoint revision contains a different payload for {unit_key!r}; "
                "changed inputs require a new manifest fingerprint")
        _atomic_json(path, {"schema": UNIT_SCHEMA, "unit_key": unit_key,
                            "entry": asdict(entry)})
        return path

    def mark_complete(self, *, unit_keys: list[str]) -> None:
        _atomic_json(self.root / "complete.json", {
            "schema": "isar.certification.complete/1",
            "fingerprint": self.manifest["fingerprint"],
            "unit_keys": list(unit_keys),
        })

    def report_progress(self, completed: int) -> None:
        """Emit advisory point progress when the drive process supplied live runtime metadata."""
        runtime_path = self.root / "runtime.json"
        if not runtime_path.exists():
            return
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        activity_id = runtime.get("activity_id")
        log_path = runtime.get("log_path")
        total = int(runtime.get("total") or 0)
        if not activity_id or not log_path or total < 1:
            return
        record = {
            "ts": time.time(), "kind": "activity_progress",
            "payload": {"activity_id": activity_id, "completed": min(int(completed), total),
                        "total": total, "progress_unit": "points"},
        }
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()  # advisory progress intentionally remains non-fsynced

"""Cross-stage durable contract for master error-tier promotions.

This module is deliberately analysis-free so both the session publisher and decision-only driver
can reconcile the same atomic records without crossing the Stage II/III import boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromotionKey:
    master_id: str
    source_revision: str
    spectrum_digest: str
    point_fingerprint: str
    tier: str
    M: int
    seed: int

    @property
    def digest(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_record(cls, record: dict) -> "PromotionKey":
        value = record.get("key")
        if not isinstance(value, dict):
            raise ValueError("promotion record has no key")
        return cls(**value)


class PromotionStore:
    """Atomic manifest/result storage rooted below a workspace's machine-owned data directory."""

    def __init__(self, workspace_root):
        self.root = Path(workspace_root) / "data" / "promotions"

    def path_for(self, key: PromotionKey) -> Path:
        return self.root / f"{key.digest}.json"

    def load(self, key: PromotionKey) -> dict | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def begin(self, key: PromotionKey, *, escalation_reasons) -> dict:
        existing = self.load(key)
        if existing is not None and existing.get("status") in (
                "completed", "published", "superseded"):
            return existing
        record = {
            "schema": "isar.master_promotion/1",
            "key": asdict(key),
            "promotion_id": key.digest,
            "status": "pending",
            "escalation_reasons": list(escalation_reasons),
            "result": None,
            "publication_outcome": None,
        }
        self._write(key, record)
        return record

    def complete(self, key: PromotionKey, result: dict) -> dict:
        record = self.load(key)
        if record is None:
            record = self.begin(key, escalation_reasons=())
        record = {**record, "status": "completed", "result": result}
        self._write(key, record)
        return record

    def resolve_publication(self, key: PromotionKey, *, current_revision: str) -> dict:
        """Mark a completed result publishable only while its frozen source is still current."""
        record = self.load(key)
        if record is None or record.get("status") not in (
                "completed", "published", "superseded"):
            raise ValueError("promotion has no completed result")
        publish = str(current_revision) == key.source_revision
        outcome = "published" if publish else "superseded"
        record = {**record, "status": outcome, "publication_outcome": outcome}
        self._write(key, record)
        return record

    def supersede_other_attempts(self, published_key: PromotionKey) -> list[dict]:
        """Retire unfinished attempts covered by a published result for the same source revision."""
        changed = []
        for record in self.records():
            key = PromotionKey.from_record(record)
            if (key.digest == published_key.digest or key.master_id != published_key.master_id
                    or key.source_revision != published_key.source_revision
                    or record.get("status") not in ("pending", "completed")):
                continue
            updated = {**record, "status": "superseded", "publication_outcome": "superseded",
                       "superseded_by": published_key.digest}
            self._write(key, updated)
            changed.append(updated)
        return changed

    def reconcile(self, current_revisions: dict[str, str]) -> list[dict]:
        """Retire stale unfinished work and return unresolved work for current master revisions."""
        unresolved = []
        for record in self.records(strict=True):
            if record.get("status") not in ("pending", "completed"):
                continue
            key = PromotionKey.from_record(record)
            current = current_revisions.get(key.master_id)
            if current != key.source_revision:
                updated = {**record, "status": "superseded",
                           "publication_outcome": "superseded"}
                self._write(key, updated)
            else:
                unresolved.append(record)
        return unresolved

    def records(self, *, strict: bool = False) -> list[dict]:
        if not self.root.exists():
            return []
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                PromotionKey.from_record(record)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                log.warning("could not read promotion record %s: %s: %s",
                            path, type(error).__name__, error)
                if strict:
                    raise RuntimeError(f"unreadable promotion record: {path}") from error
                continue
            records.append(record)
        return records

    def pending(self) -> list[dict]:
        return [record for record in self.records() if record.get("status") == "pending"]

    def _write(self, key: PromotionKey, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(key)
        fd, temporary = tempfile.mkstemp(prefix=f".{key.digest}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                log.warning("could not remove temporary promotion file %s: %s: %s",
                            temporary, type(cleanup_error).__name__, cleanup_error)
            raise

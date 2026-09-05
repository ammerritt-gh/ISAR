"""Session bookkeeping (the retired unification plan §2.2-2.3, git history; current contract: docs/WORKSPACE_AND_CLI.md) — the ingest manifest + per-scan ledger.

`SessionState` lives in `session.json` in the workspace. It is the *machine-owned, deletable*
half of persistence (U3): the ledger of what was seen and what happened to it. It can be deleted
and rebuilt from the data files plus a re-run — unlike `card.state.json` (the experiment-owned
accumulated state: calibration, prior, exclusions), which must survive.

Two hashes drive the idempotency model (U4, §0.5):
- `content_hash` — of the parsed record; changes ⇒ the scan re-fits (growing-file re-parse).
- `guidelines_hash` — of the `[fitting]` guidelines that applied to this scan when it was
  processed; changes ⇒ the scan re-fits. This is what makes a card edit an effective correction
  channel that touches exactly the affected scans (§6).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from isar.schema import _json

# Bumped to /2 for the GUI Phase 0 additive fields (FitOutcome.amplitudes + .candidates in each
# ScanEntry.detectors payload), then to /3 for the tracked-branch endpoint fields
# (FitOutcome.structural_endpoint_status + .is_reduced_q_origin). Purely a label — not fed into any
# content/drift hash — so a bump never forces a refit; from_dict still defaults it for older
# ledgers, which load unchanged.
SCHEMA_VERSION = "isar.session/3"

# Ledger status vocabulary (the retired unification plan §2.3, git history; current contract: docs/WORKSPACE_AND_CLI.md). "held_*" statuses always re-enter the fit
# queue on a later run (the bootstrap-gate re-examination).
STATUSES = (
    "ingested",             # parsed into the ledger, not yet routed/fitted — always re-enters the fit queue
    "fitted",               # produced a FitOutcome (any verdict, including declined non-results)
    "held_for_calibration",  # measurement arrived before a usable energy zero/resolution existed
    "held_for_sample",       # measurement with no established sample context (require_alignment)
    "excluded",              # exclusions_seed / card.state.exclusions / guideline exclude
    "context_applied",       # calibration/alignment/environment event consumed as a context update
    "non_result",            # undersampled/no-fit before the fit stage (e.g. too few points)
)


@dataclass
class ScanEntry:
    """One ledger row, keyed by `scan_id = f"{experiment_id}:{scan_number}:{detector_id}"` (the
    per-detector identity the driver and ingest both write). The scan-level `exp:scan` form appears
    only in reports (ingest's added/updated/unchanged lists), never as a ledger key."""
    scan_id: str
    scan_number: int
    scan_type: str
    status: str
    content_hash: str = ""
    guidelines_hash: str = ""
    detectors: dict = field(default_factory=dict)   # detector_id -> FitOutcome.to_dict()
    reasons: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)   # resolution_version, zero_source, prior_*, M, seed
    # The counts-authoritative measurement-arena record for this scan (SCENE-4 PR 4.0, #26), as
    # isar.schema.measurement.MeasurementArena.to_dict() — carried on the entry so a pool worker
    # can hand it back to the parent process, which appends it to report/measurements.jsonl as
    # entries are collected (isar/session/driver.py). None only for an entry this WP never
    # touches (a pooled master, or a pre-4.0 checkpoint replayed from an older ledger).
    measurement_record: dict | None = None


@dataclass
class SessionState:
    schema_version: str = SCHEMA_VERSION
    files: dict = field(default_factory=dict)        # resolved-path str -> {sha256, size, mtime, n_scans, format}
    scans: dict = field(default_factory=dict)        # scan_id -> ScanEntry
    data_paths: list = field(default_factory=list)   # data locations, remembered for status/report reparse
    last_run: dict = field(default_factory=dict)     # summary counts, gaps snapshot, timestamps
    # Additive operator-facing evidence for the most recent ingest. This remains machine-owned.
    last_ingest: dict = field(default_factory=dict)

    # ---- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return _json.to_jsonable(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        # Reconstruct ScanEntry objects by hand (robust to older/newer ledger keys — extra keys are
        # ignored, absent optional keys take dataclass defaults).
        scans = {sid: _scan_entry_from_dict(row) for sid, row in (d.get("scans") or {}).items()}
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            files=dict(d.get("files") or {}),
            scans=scans,
            data_paths=list(d.get("data_paths") or []),
            last_run=dict(d.get("last_run") or {}),
            last_ingest=(dict(d.get("last_ingest") or {})
                         if isinstance(d.get("last_ingest"), dict) else {}),
        )

    @classmethod
    def load(cls, path) -> "SessionState":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, path) -> Path:
        """Atomic JSON write (write-temp + os.replace) so a crash mid-write can't corrupt state."""
        return _atomic_write_json(path, self.to_dict())

    # ---- ledger helpers ----------------------------------------------------
    def get(self, scan_id: str) -> ScanEntry | None:
        return self.scans.get(scan_id)

    def put(self, entry: ScanEntry) -> None:
        self.scans[entry.scan_id] = entry


def _scan_entry_from_dict(row: dict) -> ScanEntry:
    return ScanEntry(
        scan_id=row["scan_id"],
        scan_number=int(row.get("scan_number", 0)),
        scan_type=row.get("scan_type", ""),
        status=row.get("status", ""),
        content_hash=row.get("content_hash", ""),
        guidelines_hash=row.get("guidelines_hash", ""),
        detectors=dict(row.get("detectors") or {}),
        reasons=list(row.get("reasons") or []),
        provenance=dict(row.get("provenance") or {}),
        measurement_record=(dict(row["measurement_record"])
                            if row.get("measurement_record") is not None else None),
    )


# ---------------------------------------------------------------------------
# Hashing helpers (the idempotency spine, U4)
# ---------------------------------------------------------------------------
def canonical_hash(obj) -> str:
    """sha256 of a canonical JSON serialization — stable across runs and machines."""
    payload = json.dumps(_json.to_jsonable(obj), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(record) -> str:
    """Hash a parsed ScanRecord's content (U4). Two resends of the same scan hash equal."""
    return canonical_hash(record)


# ---------------------------------------------------------------------------
def _atomic_write_json(path, data: dict) -> Path:
    from isar.session.atomic import replace_retrying
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    replace_retrying(tmp, p)
    return p

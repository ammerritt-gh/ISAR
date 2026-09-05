"""Deterministic, append-only checkpoints for a final certification run.

The drive process creates a Qt-free request and passes only its path through the runner contract.
This session-side module resolves that request against the workspace, creates the immutable
manifest, validates checkpoints, and stores pure fitted units before publication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from isar.predictive import PREDICTIVE_CONTRACT as _PREDICTIVE_CONTRACT

from .state import ScanEntry

log = logging.getLogger(__name__)


SCHEMA = "isar.certification/2"
LEGACY_SCHEMA = "isar.certification/1"
ANALYSIS_SCHEMA = "isar.analysis_work/1"
UNIT_SCHEMA = "isar.certification.unit/2"
LEGACY_UNIT_SCHEMA = "isar.certification.unit/1"
PROGRESS_SCHEMA = "isar.certification.progress/1"
MASTER_FIT_CONTRACT = "isar.master_fit/2"
# The predictive artifact a master fit publishes is part of that fit's output contract, so its
# version participates in the fit's cache fingerprint: bumping it invalidates every cached master
# fit, which is what makes an unchanged fit regenerate its predictive artifact under the new
# contract on the next run instead of carrying a stale one forever (ADR-006).
#
# Re-exported from the module that defines the artifact, never redeclared: a second literal here
# could be bumped independently of the driver's publisher, leaving the fingerprint (and therefore
# every checkpoint-store entry) keyed to a contract nobody publishes any more.
PREDICTIVE_CONTRACT = _PREDICTIVE_CONTRACT


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    from isar.session.atomic import replace_retrying
    replace_retrying(temporary, path)


def master_fit_fingerprint(*, spectrum_digest: str, policy, resolved_guidelines: dict,
                           resolved_calibration: dict, frozen_state: dict,
                           errors_tier: str, M: int, seed: int,
                           boot_adaptive: bool, bayes_opts) -> str:
    """Hash the complete, worker-count-independent numerical contract for one master fit."""
    payload = {
        "schema": MASTER_FIT_CONTRACT,
        "predictive_contract": PREDICTIVE_CONTRACT,
        "spectrum_digest": spectrum_digest,
        "policy": policy,
        "resolved_guidelines": resolved_guidelines,
        "resolved_calibration": resolved_calibration,
        "state": frozen_state,
        "errors_tier": errors_tier,
        "M": int(M),
        "seed": int(seed),
        "boot_adaptive": bool(boot_adaptive),
        "bayes_opts": bayes_opts,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_master_certification_manifest(workspace_root: str | Path,
                                         request_path: str | Path) -> Path:
    """Resolve a drive-owned request into the frozen session-owned v2 manifest."""
    from isar.schema.card import Card
    from isar.session import guidelines as guide_mod
    from isar.session.state import SessionState

    root = Path(workspace_root).resolve()
    request_path = Path(request_path).resolve()
    if not request_path.is_relative_to(root) or not request_path.is_file():
        raise ValueError("certification request must be a workspace-relative file")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != "isar.certification.request/1":
        raise ValueError("unsupported certification request schema")
    request_body = dict(request)
    supplied_request_fingerprint = str(request_body.pop("fingerprint", ""))
    request_body.pop("runtime", None)
    request_fingerprint = hashlib.sha256(json.dumps(
        request_body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if supplied_request_fingerprint != request_fingerprint:
        raise ValueError("certification request fingerprint does not match its content")

    policy_path = root / "card.policy.toml"
    state_path = root / "card.state.json"

    def file_sha(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    card = Card.load(policy_path, state_path)
    state = card.state
    session = SessionState.load(root / "session.json")
    guidelines = guide_mod.resolve(card)
    frozen_state = {
        "dispersion_prior": state.dispersion_prior,
        "branch_models": state.branch_models,
        "branch_belief": state.branch_belief,
        "branch_belief_models": state.branch_belief_models,
        "branch_tracks": state.branch_tracks,
        "hypothesis_snapshot": state.hypothesis_snapshot,
        "campaign_target": state.campaign_target,
    }
    fit_options = dict(request.get("fit_options") or {})
    tier = str(request.get("tier") or "")
    if tier not in {"full", "bayes"}:
        raise ValueError(f"unsupported certification tier: {tier!r}")
    M = int(fit_options.get("M", 200))
    boot_adaptive = bool(fit_options.get("boot_adaptive", tier in {"full", "bayes"}))
    bayes_opts = dict(fit_options.get("bayes_opts") or {})
    objective_path = request.get("objective_path") or []
    decision_points = request.get("per_q") or []
    if len(decision_points) != len(objective_path) or not objective_path:
        raise ValueError(
            "certification request needs exactly one decision state per objective-path point")

    masters = []
    for entry in session.scans.values():
        if not entry.scan_id.startswith("master:"):
            continue
        pooled_outcomes = [outcome for outcome in entry.detectors.values()
                           if outcome.get("pooled")]
        if len(pooled_outcomes) == 1:
            masters.append((entry, pooled_outcomes[0]))

    worklist = []
    excluded = []
    used_master_ids = set()
    for canonical_index, (objective_hkl, point) in enumerate(
            zip(objective_path, decision_points)):
        raw_hkl = point.get("hkl") if isinstance(point, dict) else None
        if (not isinstance(objective_hkl, (list, tuple)) or len(objective_hkl) != 3
                or not isinstance(raw_hkl, (list, tuple)) or len(raw_hkl) != 3):
            raise ValueError(f"invalid q at objective-path index {canonical_index}")
        hkl = [float(value) for value in raw_hkl]
        if any(abs(left - float(right)) > 1e-8
               for left, right in zip(hkl, objective_hkl)):
            raise ValueError(
                f"final decision q {hkl} does not match objective path index "
                f"{canonical_index}: {list(objective_hkl)}")
        matches = [(entry, outcome) for entry, outcome in masters
                   if all(abs(float(left) - float(right)) <= 1e-8
                          for left, right in zip(outcome.get("q") or (), hkl))]
        if not matches and _is_structurally_unresolvable(point):
            # A reduced-q origin the tracker inferred rather than measured has no pooled master by
            # construction, so demanding one aborts an otherwise complete campaign. Record the
            # exclusion explicitly: a manifest that silently omitted a grid point would be worse
            # than the crash it replaces.
            excluded.append({
                "canonical_index": canonical_index,
                "hkl": hkl,
                "structural_endpoint_status": point.get("structural_endpoint_status"),
                "is_reduced_q_origin": bool(point.get("is_reduced_q_origin", False)),
                "reason": str(point.get("reason") or "structurally unresolvable; not certifiable"),
            })
            continue
        if len(matches) != 1:
            raise ValueError(
                f"requested q {hkl} maps to {len(matches)} pooled masters; expected exactly one")
        entry, outcome = matches[0]
        if entry.scan_id in used_master_ids:
            raise ValueError(f"pooled master {entry.scan_id} maps to multiple objective points")
        used_master_ids.add(entry.scan_id)
        members = [list(member) for member in (entry.provenance.get("members") or ())]
        spectrum_ref = str(entry.provenance.get("spectrum_ref")
                           or outcome.get("spectrum_ref") or "")
        spectrum_path = (root / spectrum_ref).resolve()
        if (not members or not spectrum_ref or not spectrum_path.is_relative_to(root)
                or not spectrum_path.is_file()):
            raise ValueError(f"pooled master {entry.scan_id} lacks frozen members or spectrum")
        spectrum_digest = hashlib.sha256(spectrum_path.read_bytes()).hexdigest()
        group_index = int(entry.scan_number) - 9_000_000
        if group_index < 0:
            raise ValueError(f"pooled master {entry.scan_id} has an invalid canonical scan number")
        seed = 6000 + int(entry.scan_number)
        fit_fingerprint = master_fit_fingerprint(
            spectrum_digest=spectrum_digest, policy=card.policy,
            resolved_guidelines={
                "min_fit_points": guidelines.min_fit_points,
                "monitor_frac": guidelines.monitor_frac,
            },
            resolved_calibration=dict(outcome.get("calib") or {}),
            frozen_state=frozen_state, errors_tier=tier, M=M, seed=seed,
            boot_adaptive=boot_adaptive, bayes_opts=bayes_opts)
        worklist.append({
            "canonical_index": canonical_index,
            "unit_key": f"certify:{entry.scan_id}",
            "master_id": entry.scan_id,
            "hkl": hkl,
            "T": outcome.get("T"),
            "group_index": group_index,
            "ordered_members": members,
            "spectrum_ref": spectrum_ref,
            "spectrum_digest": spectrum_digest,
            "resolved_calibration": dict(outcome.get("calib") or {}),
            "requested_tier": tier,
            "seed": seed,
            "master_fit_fingerprint": fit_fingerprint,
        })
    payload = {
        "schema": SCHEMA,
        "request_fingerprint": request_fingerprint,
        "tier": tier,
        "algorithm_contract": MASTER_FIT_CONTRACT,
        "fit_options": {"M": M, "boot_adaptive": boot_adaptive,
                        "bayes_opts": bayes_opts},
        "inputs": {
            "policy_sha256": file_sha(policy_path),
            "state_sha256": file_sha(state_path),
            "prior": state.dispersion_prior or [],
            "frozen_state": frozen_state,
            "calibration": dict(request.get("calibration") or {}),
            "pooled_membership": [
                {"master_id": unit["master_id"], "members": unit["ordered_members"],
                 "spectrum_digest": unit["spectrum_digest"]}
                for unit in worklist
            ],
        },
        "seeds": dict(request.get("seeds") or {}),
        "worklist": worklist,
        # Grid points deliberately not certified, and why. Present (empty when nothing was skipped)
        # so a reader can always tell "nothing was excluded" from "this manifest predates the field".
        "excluded_points": excluded,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    payload["fingerprint"] = fingerprint
    manifest = root / "report" / "certification" / fingerprint / "manifest.json"
    if manifest.exists():
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(f"certification manifest collision at {manifest}")
    else:
        _atomic_json(manifest, payload)
    runtime = request.get("runtime") or {}
    if runtime:
        _atomic_json(manifest.parent / "runtime.json", dict(runtime))
    return manifest


def _is_structurally_unresolvable(point: dict) -> bool:
    """Whether a decision point represents a q that cannot carry a certified interval.

    Mirrors ``GridQ.precision_coverage_eligible`` (isar/drive/supervisor/stopping.py), which is the
    authority; ``evaluate_stop`` emits its verdict into each ``per_q`` row. Preferring that emitted
    verdict over re-deriving it here keeps one rule for "this q is not a measurable target" instead
    of two that can drift apart — the drift that aborted an otherwise complete campaign.
    """
    if not isinstance(point, dict):
        return False
    return not bool(point.get("precision_coverage_eligible", True))


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
        if manifest.get("schema") not in {SCHEMA, LEGACY_SCHEMA, ANALYSIS_SCHEMA}:
            raise ValueError(f"unsupported checkpoint manifest schema: {manifest.get('schema')!r}")
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

    @property
    def is_master_certification(self) -> bool:
        return self.manifest.get("schema") == SCHEMA

    def load(self, unit_key: str, *, content_hash: str, guidelines_hash: str = "",
             fit_fingerprint: str | None = None,
             requested_tier: str | None = None) -> ScanEntry | None:
        path = self._unit_path(unit_key)
        if not path.exists():
            return None
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("certification checkpoint %s is unreadable and will be rerun: %s", path, exc)
            return None
        if (row.get("schema") not in {UNIT_SCHEMA, LEGACY_UNIT_SCHEMA}
                or row.get("unit_key") != unit_key):
            log.warning("certification checkpoint %s failed integrity validation and will be rerun",
                        path)
            return None
        if row.get("schema") == UNIT_SCHEMA and row.get("manifest_fingerprint") != self.manifest["fingerprint"]:
            raise ValueError(f"certification checkpoint belongs to another manifest: {path}")
        try:
            entry = _entry_from_dict(row.get("entry") or {})
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("certification checkpoint %s has an invalid payload and will be rerun: %s",
                        path, exc)
            return None
        if entry.content_hash != content_hash or entry.guidelines_hash != guidelines_hash:
            return None
        if fit_fingerprint is not None and entry.provenance.get("master_fit_fingerprint") != fit_fingerprint:
            return None
        if requested_tier is not None and not tier_satisfies(
                entry.provenance.get("analysis_tier"), requested_tier):
            return None
        return entry

    def save(self, unit_key: str, entry: ScanEntry) -> Path:
        path = self._unit_path(unit_key)
        if path.exists():
            try:
                existing_row = json.loads(path.read_text(encoding="utf-8"))
                _entry_from_dict(existing_row.get("entry") or {})
                structurally_valid = (
                    existing_row.get("schema") in {UNIT_SCHEMA, LEGACY_UNIT_SCHEMA}
                    and existing_row.get("unit_key") == unit_key
                    and (existing_row.get("schema") != UNIT_SCHEMA
                         or existing_row.get("manifest_fingerprint")
                         == self.manifest["fingerprint"]))
            except (OSError, TypeError, ValueError, KeyError) as exc:
                log.warning("replacing corrupt certification checkpoint %s: %s", path, exc)
                structurally_valid = False
            existing = self.load(
                unit_key, content_hash=entry.content_hash,
                guidelines_hash=entry.guidelines_hash,
                fit_fingerprint=entry.provenance.get("master_fit_fingerprint"),
                requested_tier=entry.provenance.get("analysis_tier"))
            if existing is not None:
                return path
            if structurally_valid:
                raise ValueError(
                    f"checkpoint revision contains a different payload for {unit_key!r}; "
                    "changed inputs require a new manifest fingerprint")
        _atomic_json(path, {
            "schema": UNIT_SCHEMA, "unit_key": unit_key,
            "manifest_fingerprint": self.manifest["fingerprint"],
            "entry": asdict(entry),
        })
        return path

    def mark_complete(self, *, unit_keys: list[str], published_hashes: dict | None = None) -> None:
        _atomic_json(self.root / "complete.json", {
            "schema": "isar.certification.complete/2",
            "fingerprint": self.manifest["fingerprint"],
            "unit_keys": list(unit_keys),
            "published_hashes": dict(published_hashes or {}),
        })

    def mark_published(self, *, unit_keys: list[str], published_hashes: dict) -> None:
        """Commit the revision marker, then point readers at it as the final publication step."""
        self.mark_complete(unit_keys=unit_keys, published_hashes=published_hashes)
        publication_dir = self.root.parent
        _atomic_json(publication_dir / "current.json", {
            "schema": "isar.certification.current/1",
            "fingerprint": self.manifest["fingerprint"],
            "revision": self.root.name,
            "complete_ref": (self.root / "complete.json").relative_to(
                publication_dir.parent.parent).as_posix(),
            "published_hashes": dict(published_hashes),
            "published_ts": time.time(),
        })

    def begin_publication(self) -> None:
        """Invalidate any older current marker before canonical files begin changing."""
        _atomic_json(self.root.parent / "current.json", {
            "schema": "isar.certification.current/1",
            "fingerprint": self.manifest["fingerprint"],
            "revision": self.root.name,
            "published": False,
            "phase": "publishing",
            "updated_ts": time.time(),
        })

    def report_progress(self, completed: int, *, reused_workspace: int = 0,
                        reused_checkpoint: int = 0, newly_fitted: int = 0,
                        active: int = 0, last_hkl=None, phase: str = "fitting",
                        remaining_analysis_seconds: float | None = None,
                        published: bool = False) -> None:
        """Persist durable status and emit advisory activity progress."""
        runtime_path = self.root / "runtime.json"
        runtime = (json.loads(runtime_path.read_text(encoding="utf-8"))
                   if runtime_path.exists() else {})
        activity_id = runtime.get("activity_id")
        log_path = runtime.get("log_path")
        total = int(runtime.get("total") or len(self.manifest.get("worklist") or ()))
        payload = {
            "activity_id": activity_id,
            "completed": min(int(completed), total), "total": total,
            "progress_unit": "points", "reused_workspace": int(reused_workspace),
            "reused_checkpoint": int(reused_checkpoint), "newly_fitted": int(newly_fitted),
            "active": max(int(active), 0), "last_hkl": last_hkl, "phase": str(phase),
            "remaining_analysis_seconds": (None if remaining_analysis_seconds is None else
                                           max(float(remaining_analysis_seconds), 0.0)),
            "published": bool(published), "fingerprint": self.manifest["fingerprint"],
            "updated_ts": time.time(),
        }
        _atomic_json(self.root / "progress.json", {"schema": PROGRESS_SCHEMA, **payload})
        if not activity_id or not log_path or total < 1:
            return
        record = {
            "ts": time.time(), "kind": "activity_progress",
            "payload": {key: value for key, value in payload.items()
                        if key not in {"fingerprint", "updated_ts"}},
        }
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()  # advisory progress intentionally remains non-fsynced


def tier_satisfies(actual: str | None, requested: str) -> bool:
    """Return whether a completed fit tier contains every method requested."""
    if actual == requested:
        return True
    return actual == "bayes" and requested == "full"


def create_analysis_manifest(workspace_root: str | Path, payload: dict) -> Path:
    """Create or validate an immutable ordinary-analysis checkpoint revision.

    The caller supplies only JSON-compatible, already-canonical work/input metadata. Worker count,
    dispatch wave size, and deadline are intentionally absent: they may change across resume without
    changing the frozen scientific work. Completed units are reduced only after the whole canonical
    worklist is available.
    """
    body = {"schema": ANALYSIS_SCHEMA, **dict(payload)}
    body.pop("fingerprint", None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    body["fingerprint"] = fingerprint
    revision = Path(workspace_root) / "report" / "analysis" / fingerprint
    manifest_path = revision / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != body:
            raise ValueError(f"analysis manifest collision at {manifest_path}")
        return manifest_path
    _atomic_json(manifest_path, body)
    return manifest_path

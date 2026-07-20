"""Canonical comparisons for speedup shadow campaigns.

Timing, process identifiers, and free-form explanatory prose are deliberately excluded.  The
remaining stream is the scientific contract: what was proposed, how fit promotions and branch
state changed, how the supervisor decided, which points entered the prior, and the final pooled
results.  Keeping the projection here gives every speedup phase one comparison rule.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from isar.drive.session_log import read_log


_DECISION_KINDS = {
    "propose", "escalate_fit", "decide", "stop", "branch_track_update",
    "branch_reassignment", "frontier_blocked", "certification_crossing",
}
_VOLATILE_KEYS = {
    "ts", "seconds", "elapsed_seconds", "estimated_seconds", "activity_id", "job_id",
    "run_id", "log_path", "timings", "method_seconds", "fit_seconds",
    "two_stage_seconds", "errors_attach_seconds", "bootstrap_seconds", "minos_seconds",
    "profile_seconds", "bayes_seconds",
}
_DECISION_FIELDS = {
    "propose": ("hkl", "e_lo", "e_hi", "e_step", "ncount", "kind", "n_points", "meta"),
    "escalate_fit": ("phase", "iter", "hkl", "from_tier", "to_tier", "reason",
                     "revision_key", "outcome", "dispositions"),
    "decide": ("event", "action", "iter", "hkl", "per_q", "converged", "n_grid"),
    "stop": ("state", "per_q", "grid_state"),
    "branch_track_update": ("iter", "revision", "assignments", "confirmed"),
    "branch_reassignment": ("iter", "revision", "hkl", "from_branch", "to_branch"),
    "frontier_blocked": ("iter", "hkl", "branch", "state"),
    "certification_crossing": ("iter", "hkl", "production_batch_fingerprint", "state"),
}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in sorted(value.items())
                if key not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, tuple):
        return [_stable(item) for item in value]
    return value


def canonical_decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a session log onto its ordered scientific-decision stream."""
    projected = []
    for event in events:
        kind = event.get("kind")
        if kind not in _DECISION_KINDS:
            continue
        payload = event.get("payload") or {}
        fields = _DECISION_FIELDS[kind]
        stable_payload = {key: _stable(payload[key]) for key in fields if key in payload}
        if kind == "propose" and isinstance(stable_payload.get("meta"), dict):
            # Explanatory strings may improve without changing the scored/proposed measurement.
            stable_payload["meta"].pop("reason", None)
            score = stable_payload["meta"].get("score")
            if isinstance(score, dict):
                score.pop("reason", None)
        projected.append({"kind": kind, "payload": stable_payload})
    return projected


def _workspace_log(workspace: Path) -> Path:
    return workspace / "report" / "session_log.jsonl"


def _prior_points(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "card.state.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    points = [_stable(point) for point in (payload.get("dispersion_prior") or [])]
    return sorted(points, key=lambda point: json.dumps(point, sort_keys=True, ensure_ascii=False))


def _certified_rows(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "report" / "results.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    # Every published pooled-result field is canonical unless explicitly timing-only. This avoids a
    # false pass when a new additive scientific/provenance column changes (linewidth, modality,
    # calibration, model robustness, and future Stage-II↔III additions included).
    selected = [
        _stable(dict(row))
        for row in rows if str(row.get("pooled", "")).strip().lower() in {"true", "1"}
    ]
    return sorted(selected, key=lambda row: (
        row.get("scan_id") or "", row.get("detector") or "", row.get("scan") or ""))


def canonical_workspace(workspace: str | Path) -> dict[str, Any]:
    """Return the canonical shadow-comparison payload for one campaign workspace."""
    root = Path(workspace)
    return {
        "decisions": canonical_decisions(read_log(_workspace_log(root))),
        "dispersion_prior": _prior_points(root),
        "certified_results": _certified_rows(root),
    }


@dataclass(frozen=True)
class ShadowComparison:
    identical: bool
    differences: tuple[str, ...]
    left: dict[str, Any]
    right: dict[str, Any]


def compare_workspaces(left: str | Path, right: str | Path) -> ShadowComparison:
    """Compare two campaign workspaces and identify each differing contract section."""
    left_payload = canonical_workspace(left)
    right_payload = canonical_workspace(right)
    differences = tuple(
        key for key in left_payload if left_payload[key] != right_payload.get(key))
    return ShadowComparison(not differences, differences, left_payload, right_payload)

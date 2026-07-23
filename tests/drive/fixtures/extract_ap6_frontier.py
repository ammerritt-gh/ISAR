"""Regenerate the compact AP6 forensic fixture from the frozen failed-campaign workspace.

PR0 of the action-inventory repair (see the plan): AP6 is the real failed acoustic campaign that
stopped ``incomplete — branch_ambiguity`` at path indices 18/19/20 with beam time remaining. The full
workspace lives at ``test_workspaces/AP6_Failed/`` (gitignored, local-only). This script distills the
few load-bearing facts into a small committed JSON so the reproduction test can run without the 11 MB
session log, and records SHA-256 hashes of the source files so the freeze is immutable and auditable.

Run from the repo root:  ``python tests/drive/fixtures/extract_ap6_frontier.py``

It never mutates the source workspace; it only reads it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE = REPO_ROOT / "test_workspaces" / "AP6_Failed"
OUT = Path(__file__).resolve().parent / "ap6_frontier.json"

# The failed campaign was inspected/frozen at this commit.
FROZEN_COMMIT = "7b469154e108566f3fdce844d909e218dbb9a9c2"
# The blocked frontier: the last three of 21 path points, H=2.9 / 2.95 / 3.0.
FRONTIER_INDICES = (18, 19, 20)
# Fields we lock from the terminal ``decide`` per-q rollup.  ``stuck`` is deliberately excluded:
# stopping.py serializes it as a hard-coded ``False`` literal (a logging defect), so the recorded
# value is meaningless — the real ``GridQ.stuck`` property is ``ambiguity_exhausted`` and is True at
# index 18.  The reproduction test drives the real property instead of trusting the log.
PER_Q_FIELDS = (
    "hkl", "q", "track_state", "assessment_state", "target_probability", "identity_entropy",
    "local_identity_clear", "identity_saturated", "exposure_saturated", "strategy_ceiling_reached",
    "interval_floored", "n_production", "n_master_updates", "diagnostic_halfwidth", "converged",
)
WINDOW_FIELDS = ("hkl", "e_lo", "e_hi", "e_step", "n_points")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_events(log_path: Path) -> list[dict]:
    """Parse the JSONL log, tolerating only a truncated *final* line.

    A malformed line anywhere but the end means the log is corrupt, and continuing would silently
    produce a partial fixture with misleading hashes — so raise with the line number instead.
    """
    raw = log_path.read_text(encoding="utf-8").splitlines()
    last_nonempty = max((i for i, line in enumerate(raw) if line.strip()), default=-1)
    events: list[dict] = []
    for lineno, line in enumerate(raw):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if lineno == last_nonempty:
                # A truncated trailing line is not fatal for a read-only extraction.
                continue
            raise ValueError(
                f"{log_path} is corrupt: malformed JSON at line {lineno + 1} (not the final line)")
    return events


def _near(value, target: float, tol: float = 1e-6) -> bool:
    try:
        return abs(float(value) - target) < tol
    except (TypeError, ValueError):
        return False


def _last_payload(events: list[dict], kind: str, log_path: Path) -> dict:
    for event in reversed(events):
        if event.get("kind") == kind:
            return event["payload"]
    raise RuntimeError(f"no '{kind}' event in {log_path} — the frozen workspace is not the AP6 run")


def build() -> dict:
    log_path = WORKSPACE / "report" / "session_log.jsonl"
    card_path = WORKSPACE / "card.state.json"
    events = _load_events(log_path)

    stop = _last_payload(events, "stop", log_path)
    decide = _last_payload(events, "decide", log_path)
    per_q = decide["per_q"]

    frontier = {}
    for index in FRONTIER_INDICES:
        row = per_q[index]
        frontier[str(index)] = {key: row.get(key) for key in PER_Q_FIELDS}

    # Every emitted window at a frontier q — proves the acoustic [-2, 7] ceiling clipped the rival.
    frontier_hkl0 = {2.9, 2.95, 3.0}
    windows = []
    for o in events:
        if o.get("kind") != "propose":
            continue
        pl = o["payload"]
        hkl = pl.get("hkl")
        if hkl and any(_near(hkl[0], value) for value in frontier_hkl0):
            windows.append({key: pl.get(key) for key in WINDOW_FIELDS})

    card = json.loads(card_path.read_text(encoding="utf-8"))
    target = card.get("campaign_target", {})

    return {
        "_comment": "Compact forensic extract of the AP6_Failed acoustic campaign. Regenerate with "
                    "tests/drive/fixtures/extract_ap6_frontier.py. Do not hand-edit.",
        "frozen_commit": FROZEN_COMMIT,
        "source_workspace": "test_workspaces/AP6_Failed",
        "source_hashes": {
            "report/session_log.jsonl": _sha256(log_path),
            "report/results.csv": _sha256(WORKSPACE / "report" / "results.csv"),
            "card.state.json": _sha256(card_path),
        },
        "objective": {
            "branch": target.get("branch"),
            "selector": target.get("selector"),
            "energy_search_meV": target.get("energy_search_meV"),
            "path_len": len(target.get("path", [])),
        },
        "terminal": {"state": stop["state"], "reasons": stop["reasons"]},
        "frontier_indices": list(FRONTIER_INDICES),
        "frontier_per_q": frontier,
        "emitted_windows_at_frontier": windows,
    }


def main() -> None:
    if not WORKSPACE.exists():
        raise SystemExit(f"AP6_Failed workspace not found at {WORKSPACE} — nothing to extract.")
    fixture = build()
    OUT.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} "
          f"({len(fixture['emitted_windows_at_frontier'])} frontier windows)")


if __name__ == "__main__":
    main()

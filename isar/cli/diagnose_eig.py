"""``isar diagnose --workspace DIR --eig`` — the scan-window EIG audit trail, as a table.

The driver already records *why* the scan-window EIG did or did not change a window: one
content-addressed decision per iteration under ``report/scan-design-authority/``, and the realized
legacy/target pair it judged under ``report/scan-design-realizations/``. Both are written for a
reconstruction months later, not for reading — a scientist asking "did EIG ever act, and if not,
what stopped it?" would otherwise have to open several dozen JSON files by hand.

This module answers that question and nothing else. It is strictly **read-only**: it opens the
artifacts through the same authenticating loaders the driver uses (so an unreadable or tampered
artifact is reported, never silently believed), reads the session log for the iteration numbers the
artifacts do not carry, and writes nothing into the workspace.
"""
from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from isar.drive.acquisition.authority import (
    AUTHORITY_DECISION_REF_KIND,
    load_authority_decision,
)
from isar.drive.acquisition.realized_comparison import (
    REALIZED_COMPARISON_REF_KIND,
    load_scan_design_realized_comparison,
)
from isar.drive.session_log import read_log
from isar.session.workspace import Workspace

log = logging.getLogger(__name__)

AUTHORITY_DIR = "scan-design-authority"
REALIZATION_DIR = "scan-design-realizations"


def _reference(kind: str, directory: str, digest: str) -> dict[str, str]:
    return {"kind": kind, "sha256": digest, "path": f"report/{directory}/{digest}.json"}


def _digests(root: Path, directory: str) -> list[str]:
    """Every content-addressed artifact name in one report directory, in a stable order."""
    folder = root / "report" / directory
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.json"))


def _q_text(hkl) -> str:
    """``H=2.15`` for a path varying in one index; the full triple otherwise."""
    try:
        coords = [float(value) for value in hkl]
    except (TypeError, ValueError):
        return "?"
    if len(coords) == 3:
        moving = [index for index, value in enumerate(coords) if value != 0.0]
        if len(moving) == 1:
            return f"{'HKL'[moving[0]]}={coords[moving[0]]:g}"
    return "(" + ", ".join(f"{value:g}" for value in coords) + ")"


def _iterations_by_fingerprint(root: Path) -> dict[str, int]:
    """Iteration numbers keyed by decision fingerprint / artifact sha, from the session log.

    The artifacts are deliberately free of loop bookkeeping, so the iteration a decision belongs to
    is only recoverable from the log. A missing or unreadable log costs the ``iter`` column and
    nothing else.
    """
    iterations: dict[str, int] = {}
    try:
        records = read_log(root / "report" / "session_log.jsonl")
    except OSError as error:
        log.warning("EIG diagnostic could not read the session log (%s) — iterations omitted",
                    error)
        return iterations
    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        iteration = payload.get("iter")
        if not isinstance(iteration, int):
            continue
        for key in ("decision_fingerprint", "decision_ref"):
            value = payload.get(key)
            if isinstance(value, str):
                iterations.setdefault(value, iteration)
        for key in ("comparison_ref", "scan_design_authority_decision_ref"):
            value = payload.get(key)
            if isinstance(value, dict) and isinstance(value.get("sha256"), str):
                iterations.setdefault(value["sha256"], iteration)
    return iterations


def _margin_text(row: dict[str, Any]) -> str:
    delta, error = row["delta"], row["standard_error"]
    if delta is None:
        return "—"
    if error is None:
        return f"{delta:+.4f}"
    return f"{delta:+.4f}±{error:.4f}"


def _decision_rows(root: Path, iterations: dict[str, int]) -> tuple[list[dict], list[str]]:
    """One row per authority decision, plus one message per artifact that would not authenticate."""
    rows: list[dict] = []
    problems: list[str] = []
    for digest in _digests(root, AUTHORITY_DIR):
        try:
            decision = load_authority_decision(
                _reference(AUTHORITY_DECISION_REF_KIND, AUTHORITY_DIR, digest), root)
        except (ValueError, OSError) as error:
            log.warning("authority decision %s did not authenticate: %s", digest[:12], error)
            problems.append(f"{digest[:12]}… authority decision: {error}")
            continue
        hkl = None
        selected = decision.selected_request
        if selected is not None:
            hkl = selected.get("hkl")
        if hkl is None:
            try:
                comparison = load_scan_design_realized_comparison(
                    decision.realized_comparison_ref, root)
                hkl = comparison.realized_pair.legacy.hkl
            except (ValueError, OSError) as error:
                log.warning("decision %s has no readable realized pair: %s", digest[:12], error)
                problems.append(f"{digest[:12]}… realized pair: {error}")
        rows.append({
            "source": "decision",
            "digest": digest,
            "hkl": hkl,
            "q": _q_text(hkl),
            "iter": iterations.get(decision.authority_decision_fingerprint,
                                   iterations.get(digest)),
            "outcome": decision.decision_outcome,
            "role": decision.physical_role,
            "legacy_points": decision.legacy_validation.get("executed_point_count"),
            "target_points": decision.target_core_validation.get("executed_point_count"),
            "delta": decision.delta_information_per_executed_point_nats,
            "standard_error": decision.paired_standard_error_nats,
            "reasons": list(decision.reason_codes),
        })
    return rows, problems


def _realization_rows(root: Path, iterations: dict[str, int]) -> tuple[list[dict], list[str]]:
    """One row per realized comparison — the only evidence a shadow-mode campaign leaves behind."""
    rows: list[dict] = []
    problems: list[str] = []
    for digest in _digests(root, REALIZATION_DIR):
        try:
            comparison = load_scan_design_realized_comparison(
                _reference(REALIZED_COMPARISON_REF_KIND, REALIZATION_DIR, digest), root)
        except (ValueError, OSError) as error:
            log.warning("realized comparison %s did not authenticate: %s", digest[:12], error)
            problems.append(f"{digest[:12]}… realized comparison: {error}")
            continue
        legacy = comparison.realized_pair.legacy
        target = comparison.realized_pair.target_core
        rows.append({
            "source": "realization",
            "digest": digest,
            "hkl": legacy.hkl,
            "q": _q_text(legacy.hkl),
            "iter": iterations.get(digest),
            "outcome": f"shadow/{comparison.score_disposition}",
            "role": "legacy",
            "legacy_points": legacy.executed_point_count,
            "target_points": getattr(target, "executed_point_count", None),
            "delta": None,
            "standard_error": None,
            "reasons": list(comparison.reason_codes),
        })
    return rows, problems


def _latest_per_q(rows: list[dict]) -> list[dict]:
    """The last decision reached at each Q — iteration order where known, artifact order otherwise."""
    latest: dict[str, tuple[tuple[int, int, int], dict]] = {}
    for index, row in enumerate(rows):
        iteration = row["iter"]
        key = (1 if iteration is not None else 0, iteration if iteration is not None else 0, index)
        current = latest.get(row["q"])
        if current is None or key > current[0]:
            latest[row["q"]] = (key, row)
    return [row for _key, row in sorted(latest.values(), key=lambda item: item[1]["q"])]


def _table(rows: list[dict]) -> list[str]:
    header = ("Q", "iter", "outcome", "role", "pts legacy/target", "margin nats/pt", "reasons")
    body = [(
        row["q"],
        "—" if row["iter"] is None else str(row["iter"]),
        str(row["outcome"]),
        str(row["role"]),
        f"{row['legacy_points'] if row['legacy_points'] is not None else '—'} / "
        f"{row['target_points'] if row['target_points'] is not None else '—'}",
        _margin_text(row),
        ", ".join(row["reasons"]) or "—",
    ) for row in rows]
    widths = [max(len(header[column]), *(len(line[column]) for line in body)) if body
              else len(header[column]) for column in range(len(header))]
    lines = ["  " + "  ".join(text.ljust(widths[column])
                              for column, text in enumerate(header)).rstrip()]
    lines.append("  " + "  ".join("-" * width for width in widths))
    lines.extend("  " + "  ".join(text.ljust(widths[column])
                                  for column, text in enumerate(line)).rstrip() for line in body)
    return lines


def _decision_events(root: Path) -> list[dict[str, Any]]:
    """Every ``eig_decision`` payload in iteration order; an unreadable log costs this section."""
    try:
        records = read_log(root / "report" / "session_log.jsonl")
    except OSError as error:
        log.warning("EIG diagnostic could not read the session log (%s) — rate section omitted",
                    error)
        return []
    events = [record["payload"] for record in records
              if isinstance(record, dict) and record.get("kind") == "eig_decision"
              and isinstance(record.get("payload"), dict)]
    return sorted(events, key=lambda payload: payload.get("iter") or 0)


def _first_pass_last_iteration(events: list[dict[str, Any]]) -> int | None:
    """The iteration that completed the campaign's first pass across Q.

    Before a q has ever been measured there is no confirmed target to centre a scan on, so every
    decision in the first pass is blocked for a reason that says nothing about whether EIG works.
    Folding those into one rate is what makes a working capability look broken.
    """
    seen: set[tuple] = set()
    last = None
    for payload in events:
        hkl = payload.get("hkl")
        # Accept any numeric triple, matching ``_q_text``: a log that serialized hkl as anything
        # other than a list would otherwise be treated as having no q at all, silently moving the
        # first-pass boundary that every rate below is measured against.
        key = (tuple(float(value) for value in hkl)
               if isinstance(hkl, (list, tuple)) and len(hkl) == 3
               and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       for value in hkl) else None)
        if key is not None and key not in seen:
            seen.add(key)
            iteration = payload.get("iter")
            if isinstance(iteration, int):
                last = iteration
    return last


def _rate_lines(events: list[dict[str, Any]]) -> list[str]:
    """Grants, scored decisions and production requests, split at the first pass across Q."""
    if not events:
        return []
    boundary = _first_pass_last_iteration(events)
    after = [payload for payload in events
             if boundary is None or (payload.get("iter") or 0) > boundary]
    before = len(events) - len(after)
    grants = sum(1 for payload in after
                 if payload.get("decision_outcome") == "select_target_core")
    scored = sum(1 for payload in after
                 if payload.get("decision_outcome") in {"select_target_core",
                                                        "retain_legacy_control"})
    # A campaign that ran before the event carried a request kind has none, which is not the same
    # fact as "no production requests" and must not print as zero.
    recorded = any("request_kind" in payload for payload in after)
    production = (str(sum(1 for payload in after if payload.get("request_kind") == "production"))
                  if recorded else "not recorded by this campaign")
    lines = ["", f"  After the first pass across Q (completed at iteration {boundary}):"]
    # All three, always. The gate is grants over scored decisions, but that denominator moves
    # whenever an eligibility gate changes, so a rate reported alone cannot distinguish a worse
    # selector from a bigger denominator.
    rate = f"{100.0 * grants / scored:.1f}%" if scored else "—"
    lines.append(f"    grants / scored decisions      {grants} / {scored}  ({rate})")
    lines.append(f"    production requests            {production}")
    lines.append(f"    decisions in total             {len(after)}")
    if before:
        lines.append(f"  First pass across Q: {before} decision(s), before any confirmed target")
    blockers = Counter(str(payload.get("blocker")) for payload in after
                       if payload.get("blocker"))
    if blockers:
        lines.append("")
        lines.append("  Blocker after the first pass:")
        width = max(len(code) for code in blockers)
        lines.extend(f"    {code.ljust(width)}  {count}"
                     for code, count in sorted(blockers.items(),
                                               key=lambda item: (-item[1], item[0])))
    return lines


def render(workspace: str | Path) -> list[str]:
    """The complete report as printable lines. Reads the workspace; never writes to it."""
    root = Path(workspace)
    iterations = _iterations_by_fingerprint(root)
    decisions, decision_problems = _decision_rows(root, iterations)
    realizations, realization_problems = _realization_rows(root, iterations)
    rows = decisions or realizations
    lines = [f"EIG scan-window decisions — {root}",
             f"  {len(decisions)} authority decision(s), "
             f"{len(realizations)} realized comparison(s)", ""]
    if not rows:
        lines.append("  no scan-window EIG artifacts in this workspace "
                     "(the campaign ran with scan-window EIG off)")
        return lines
    if not decisions:
        lines.append("  no authority decisions — showing the shadow realized comparisons")
    lines.extend(_table(_latest_per_q(rows)))
    blockers = Counter(row["reasons"][0] for row in rows if row["reasons"])
    lines.append("")
    if blockers:
        lines.append(f"  Primary reason code across all {len(rows)} record(s):")
        width = max(len(code) for code in blockers)
        lines.extend(f"    {code.ljust(width)}  {count}"
                     for code, count in sorted(blockers.items(),
                                               key=lambda item: (-item[1], item[0])))
    else:
        lines.append("  no reason codes recorded")
    lines.extend(_rate_lines(_decision_events(root)))
    problems = decision_problems + realization_problems
    if problems:
        lines.append("")
        lines.append(f"  {len(problems)} artifact(s) did not authenticate:")
        lines.extend(f"    {problem}" for problem in problems)
    return lines


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 1
    try:
        lines = render(ws.root)
    except Exception as exc:  # noqa: BLE001 — a diagnostic reports its failure, not a stack trace
        log.exception("isar diagnose --eig failed")
        print(f"isar diagnose --eig: failed: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0

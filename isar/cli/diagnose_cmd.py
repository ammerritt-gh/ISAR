"""`isar diagnose --workspace DIR` — render the architect's CAMPAIGN_DIAGNOSTIC.md (Part 2).

Deterministic re-presentation of a finished (or crashed) campaign from the file contract — no
refitting, no re-scoring, no LLM. Mirrors ``report_cmd`` in shape.
"""
from __future__ import annotations

import sys

from isar.session.workspace import Workspace


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 1
    try:
        from isar.report.campaign_diagnostic import render_campaign_diagnostic
        out = render_campaign_diagnostic(args.workspace)
    except Exception as exc:  # noqa: BLE001 — surface a clear message, not a stack trace
        print(f"isar diagnose: failed: {exc}", file=sys.stderr)
        return 1
    print(f"  diagnostic: {out}")
    return 0

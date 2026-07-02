"""`isar report --workspace DIR` — re-render the report from persisted state (UNIFICATION_DESIGN §5).

Reads the ledger only (no reparse, no refitting) and rebuilds report.md/.html/.json + the
dispersion figure. Add `--formats` to control outputs.
"""
from __future__ import annotations

import sys

from isar.report import run_report
from isar.session.workspace import Workspace


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2
    card, session = ws.load()
    formats = tuple(f.strip() for f in getattr(args, "formats", "html,markdown").split(",") if f.strip())
    written = run_report.write_run_report(session, card, ws, formats=formats)
    for kind, path in written.items():
        print(f"  {kind}: {path}")
    return 0

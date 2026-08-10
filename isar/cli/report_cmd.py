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

    if getattr(args, "scene", False):
        # Explicitly requested, so a refusal is VISIBLE (H1): a workspace the scene adapter cannot
        # read faithfully — no candidates column, an unqualified master, a row predating the
        # delivery contract — says re-deliver and exits nonzero, rather than writing an empty
        # scene that reads as "this campaign measured nothing". The report above is already
        # written and its own exit semantics are unchanged.
        try:
            # Imported inside the try so an ImportError is reported exactly like every other
            # scene failure, rather than escaping as a traceback from one lane of the envelope.
            from isar.session import scene_hook
            result = scene_hook.build_and_store(ws.root, source="report_cli")
        except Exception as exc:  # noqa: BLE001 — reported to the operator, not swallowed
            # 3, not 2: 2 already means "no workspace here", and a workspace that exists but
            # cannot be scored is a different thing for a script to branch on.
            print(f"error: scene: {exc}", file=sys.stderr)
            return 3
        print(f"  scene: {result.path} ({result.digest[:12]}, "
              f"{'unchanged' if result.unchanged else 'new'})")
    return 0

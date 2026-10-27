"""`isar ingest <data>... --workspace DIR` — parse data into the ledger WITHOUT fitting.

Populates the session ledger (status ``"ingested"``) so parsed scans are visible through
``workspace_api`` before any ``isar run``. Never fits, never deletes, never writes
``card.policy.toml``. The GUI's parse-preview shells this with ``--json`` (the retired unification plan §5, git history; current contract: docs/WORKSPACE_AND_CLI.md).
"""
from __future__ import annotations

import json
import sys
import traceback

from isar.intake import curves, inputs
from isar.schema import card as card_mod
from isar.session import ingest
from isar.session.workspace import Workspace


def _fail_no_workspace(ws, as_json: bool) -> int:
    msg = f"no workspace at {ws.root} (run `isar init` first)"
    if as_json:
        print(json.dumps({"error": {"kind": "no_workspace", "message": msg}}))
    print(f"error: {msg}", file=sys.stderr)
    return 2


def run(args) -> int:
    as_json = getattr(args, "json", False)
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        return _fail_no_workspace(ws, as_json)

    # U7: a typo'd card section silently disables guidelines — warn (never on stdout under --json).
    for unknown in card_mod.unknown_toml_tables(ws.policy_path):
        print(f"warning: unknown card section [{unknown}] — ignored (typo?)", file=sys.stderr)

    card, session = ws.load()
    try:
        report = ingest.ingest_batch(card, session, args.data, data_dir=ws.root / "data",
                                     workspace_root=ws.root)
    except Exception as exc:
        # Per-file isolation (ingest_batch) makes this a rare backstop, but the GUI's cause-extractor
        # must ALWAYS find a clean `error:` line rather than a raw traceback. Print the traceback for
        # diagnosis, then a machine-readable cause, and exit 2 (internal, distinct from exit-1 crashes).
        traceback.print_exc()
        msg = f"{type(exc).__name__}: {exc}"
        if as_json:
            print(json.dumps({"error": {"kind": "internal", "message": msg}}))
        print(f"error: {msg}", file=sys.stderr)
        return 2
    if report.dirty:
        session.save(ws.session_path)   # only the machine-owned ledger; never card.policy.toml
    damaged = inputs.damage_lines(ws.root)   # skipped lines of the intake files, never a stop

    if as_json:
        print(json.dumps({
            "workspace": str(ws.root),
            "files": report.to_jsonable()["files"],
            "scans": {"added": report.added, "updated": report.updated,
                      "unchanged": report.unchanged},
            "deduped": report.to_jsonable()["deduped"],
            "passed_over": report.to_jsonable()["passed_over"],
            "gaps": report.to_jsonable()["gaps"],
            "last_ingest": report.to_jsonable()["last_ingest"],
            "curves": report.curves,
            "intake_damaged": damaged,
            "import_record_error": report.import_record_error}))
        return 0

    # Human summary.
    by_class: dict[str, int] = {}
    for f in report.files:
        by_class[f["class"]] = by_class.get(f["class"], 0) + 1
    print(f"Workspace: {ws.root}")
    if by_class:
        print("  files: " + ", ".join(f"{k}={v}" for k, v in sorted(by_class.items())))
    else:
        print("  files: (none found under the given paths)")
    print(f"  scans: added={len(report.added)}, updated={len(report.updated)}, "
          f"unchanged={len(report.unchanged)}")
    for d in report.deduped:
        print(f"  dedup: {d['scan']} kept {d['kept']} "
              f"(superseded {', '.join(d['superseded'])})")
    for p in report.passed_over:
        print(f"  passed over: {p['reason']}")
    if report.gaps:
        print(f"  scan-number gaps: {len(report.gaps)} "
              f"({', '.join(f'{a}->{b}' for _e, a, b in report.gaps[:6])})")
    if report.curves is not None:
        if "error" in report.curves:
            print(f"  resolution curves: check failed ({report.curves['error']})")
        else:
            for line in curves.CurveCheck.from_dict(report.curves).lines():
                print(f"  {line}")
    for line in damaged:
        print(f"  intake: {line}")
    if report.import_record_error:
        print(f"  intake: {report.import_record_error}")
    if not report.dirty:
        print("  (no change — ledger already current)")
    return 0

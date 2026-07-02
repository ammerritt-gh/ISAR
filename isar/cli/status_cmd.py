"""`isar status --workspace DIR` — summarize the ledger without reprocessing (UNIFICATION_DESIGN §5).

Reads card + session only (no reparse): counts by status, held scans with reasons, scan-number and
coverage gaps from the last run, calibration versions, and prior population.
"""
from __future__ import annotations

import sys
from collections import Counter

from isar.schema import card as card_mod
from isar.session.workspace import Workspace


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2

    # U7: warn on unknown top-level TOML tables (a typo'd section silently disables guidelines).
    for unknown in card_mod.unknown_toml_tables(ws.policy_path):
        print(f"warning: unknown card section [{unknown}] — ignored (typo?)", file=sys.stderr)

    card, session = ws.load()
    pol = card.policy
    print(f"Workspace: {ws.root}")
    print(f"Instrument: {pol.facility}/{pol.instrument} (format {pol.format_version}), plugin '{pol.plugin}'")
    print(f"Data paths: {', '.join(session.data_paths) or '(none yet — run `isar run`)'}")
    print()

    entries = list(session.scans.values())
    print(f"Scans in ledger: {len(entries)}")
    by_status = Counter(e.status for e in entries)
    for status, n in sorted(by_status.items()):
        print(f"  {status:22s} {n}")

    # Held scans, with the reason that parked them.
    held = [e for e in entries if e.status.startswith("held")]
    if held:
        print()
        print("Held scans (re-examined automatically on the next run):")
        for e in sorted(held, key=lambda e: e.scan_number):
            reason = "; ".join(e.reasons) or e.status
            print(f"  scan {e.scan_number}: {reason}")

    last = session.last_run or {}
    gaps = last.get("scan_gaps") or []
    if gaps:
        print()
        print("Scan-number gaps (a scan that never arrived?):")
        for g in gaps:
            print(f"  {g}")

    if card.state.resolution_versions:
        print()
        print(f"Calibration versions: {len(card.state.resolution_versions)}")
        for v in card.state.resolution_versions:
            print(f"  {v.get('version', '?')}: method={v.get('method', '?')}")
    print()
    print(f"Dispersion prior points: {len(card.state.dispersion_prior)}")
    return 0

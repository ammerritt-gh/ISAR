"""`isar run <data>... --workspace DIR` — the full pipeline, idempotent (UNIFICATION_DESIGN §5)."""
from __future__ import annotations

import sys

from isar.schema import card as card_mod
from isar.session import driver
from isar.session.timing import StageTimer
from isar.session.workspace import Workspace


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2

    # U7: a typo'd card section silently disables guidelines — warn.
    for unknown in card_mod.unknown_toml_tables(ws.policy_path):
        print(f"warning: unknown card section [{unknown}] — ignored (typo?)", file=sys.stderr)

    errors_tier = getattr(args, "errors", "full")
    timer = StageTimer(errors_tier=errors_tier, M=args.M, argv=list(sys.argv))
    try:
        formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
        summary = driver.run_batch(
            ws, args.data, force=args.force, llm=args.llm, M=args.M,
            detectors=args.detectors, formats=formats,
            workers=getattr(args, "workers", None), errors_tier=errors_tier, timer=timer)

        # Try the rich report (build step 6); the CSV that run_batch always writes is the fallback.
        report_note = f"results: {ws.report_dir / 'results.csv'}"
        # --no-render (driver campaigns): skip report.md/.html/.json + figures entirely — nothing
        # mid-campaign consumes them (drive reads only results.csv / card.state.json) and the report
        # layer / matplotlib import dominates per-invocation cost. The campaign re-renders once at the
        # end. Don't emit a zero-length report_render timing stage when skipping.
        if getattr(args, "render", True):
            try:
                with timer.stage("report_render"):
                    from isar.report import run_report
                    card, session = ws.load()
                    run_report.write_run_report(session, card, ws, formats=formats)
                report_note = f"report: {ws.report_dir}"
            except Exception:  # noqa: BLE001 — report layer optional until step 6
                pass
    finally:
        # A timing-write failure must never fail the run (StageTimer.write is itself best-effort).
        timer.write(ws.report_dir / "run_timings.json")

    by_status = summary.counts.get("by_status", {})
    by_disp = summary.counts.get("by_disposition", {})
    print(f"Handled {summary.n_reprocessed} scan-detector(s) this run "
          f"({summary.n_fitted_this_run} reached the fitter); "
          f"{len(summary.outcomes)} outcomes in the ledger.")
    if by_status:
        print("  by status:      " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    if by_disp:
        print("  by disposition: " + ", ".join(f"{k}={v}" for k, v in sorted(by_disp.items())))
    if summary.gaps:
        print(f"  scan-number gaps: {len(summary.gaps)} "
              f"({', '.join(f'{a}->{b}' for _e, a, b in summary.gaps[:6])})")
    for line in summary.context_lines:
        print(f"  · {line}")
    print(report_note)
    return 0

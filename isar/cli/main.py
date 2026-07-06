"""`isar` CLI entry point (UNIFICATION_DESIGN §5).

Subcommands: init, run, status, report, score. Built incrementally; unimplemented commands raise a
clear message rather than a stack trace.
"""
from __future__ import annotations

import argparse
import sys


def _reconfigure_stdio() -> None:
    # Windows consoles default to cp1252; reports/figures use ω/Δ/≤. Match the check scripts.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="isar", description="ISAR — inelastic-scattering triage pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="detect the instrument and scaffold an experiment workspace")
    pi.add_argument("data", help="a data file, or a directory containing one")
    pi.add_argument("--out", default=None, help="workspace directory (default ./isar_<instr>_<date>)")

    pr = sub.add_parser("run", help="run the pipeline on a batch of data (idempotent)")
    pr.add_argument("data", nargs="+")
    pr.add_argument("--workspace", required=True)
    pr.add_argument("--force", action="store_true", help="refit every scan, ignoring the ledger")
    pr.add_argument("--llm", action="store_true", help="enable LLM adjudication of QC flags")
    pr.add_argument("--M", type=int, default=200, help="bootstrap draws per scan (default 200)")
    pr.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")
    pr.add_argument("--detectors", default=None, help="restrict fitted detectors (comma-separated)")

    ps = sub.add_parser("status", help="summarize a workspace's ledger without reprocessing")
    ps.add_argument("--workspace", required=True)

    prp = sub.add_parser("report", help="re-render the report from persisted state")
    prp.add_argument("--workspace", required=True)
    prp.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")

    psc = sub.add_parser("score", help="score a run against a human gold CSV or known analytic truth")
    psc.add_argument("--workspace", required=True)
    # Mutually exclusive: gold (human-fit CSV) vs truth (σ-calibration against known truth).
    src = psc.add_mutually_exclusive_group(required=True)
    src.add_argument("--gold", help="human gold-fit CSV (disposition + ω scoring, safety-gated)")
    src.add_argument("--truth-fn", dest="truth_fn",
                     help="analytic truth function name (e.g. phonon_dft) for interval-coverage scoring")
    src.add_argument("--truth-csv", dest="truth_csv",
                     help="truth table CSV (h,k,l,omega_true) or (q,omega_true) for coverage scoring")
    psc.add_argument("--bucket-by", dest="bucket_by", default=None,
                     help="results.csv column to bucket σ-calibration coverage by (truth mode)")
    psc.add_argument("--json", default=None,
                     help="write the full scoring dict to this path (truth mode)")
    return p


def main(argv=None) -> int:
    _reconfigure_stdio()
    import matplotlib
    matplotlib.use("Agg")  # headless before any pyplot import in the command modules

    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd == "init":
        from isar.cli import init_cmd
        return init_cmd.run(args)
    if cmd == "run":
        from isar.cli import run_cmd
        return run_cmd.run(args)
    if cmd == "status":
        from isar.cli import status_cmd
        return status_cmd.run(args)
    if cmd == "report":
        from isar.cli import report_cmd
        return report_cmd.run(args)
    if cmd == "score":
        from isar.cli import score_cmd
        return score_cmd.run(args)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

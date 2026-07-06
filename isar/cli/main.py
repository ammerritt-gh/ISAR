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

    pd = sub.add_parser("drive", help="run the measurement driver campaign (LLM_HARNESS_DESIGN §8)")
    src = pd.add_mutually_exclusive_group(required=True)
    src.add_argument("--goal", help="natural-language measurement goal (LLM supervisor; needs LLMM)")
    src.add_argument("--spec", help="objective spec JSON file (LLM-free path)")
    pd.add_argument("--workspace", required=True, help="ISAR workspace (init'd) the driver drives")
    pd.add_argument("--tier", default="deterministic", choices=["deterministic", "mcstas"],
                    help="fidelity tier (LLM_HARNESS_DESIGN §6)")
    pd.add_argument("--budget", default=None,
                    help="override budget as '<amount><unit>' e.g. '8h', '1e10n' (else from the spec/goal)")
    pd.add_argument("--calibration", default=None,
                    help="σ-calibration artifact path (default the repo phonon_dft_deterministic.json)")
    pd.add_argument("--tavi", default=None, help="TAVI API base URL (default http://127.0.0.1:8642/api/v1)")
    pd.add_argument("--seed", type=int, default=0, help="base seed for the scan seed sequence")
    return p


def _parse_budget(text: str) -> dict:
    """Parse a ``--budget`` override: ``'8h'`` → counting_seconds, ``'1e10n'`` → neutrons.

    A bare number is treated as counting-seconds. Accepts ``h``/``hour(s)``, ``s``/``sec``, ``n``.
    """
    s = str(text).strip().lower()
    if s.endswith(("h", "hour", "hours")):
        num = s.rstrip("hours").rstrip("h")
        return {"kind": "counting_seconds", "amount": float(num) * 3600.0}
    if s.endswith("n") or s.endswith("neutrons"):
        num = s.rstrip("neutrons").rstrip("n")
        return {"kind": "neutrons", "amount": float(num)}
    if s.endswith(("s", "sec", "secs")):
        num = s.rstrip("secs").rstrip("s")
        return {"kind": "counting_seconds", "amount": float(num)}
    return {"kind": "counting_seconds", "amount": float(s)}


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
    if cmd == "drive":
        from isar.drive.loop import run_campaign
        from isar.drive.supervisor.goal_to_spec import GoalParseError, goal_to_spec, spec_from_json
        try:
            if args.spec:
                objective = spec_from_json(args.spec, defaults={"tier": args.tier})
            else:
                objective = goal_to_spec(args.goal, defaults={"tier": args.tier})
            if args.budget:
                objective.budget = _parse_budget(args.budget)
                objective.__post_init__()
        except (GoalParseError, ValueError, OSError) as exc:
            print(f"isar drive: {exc}", file=sys.stderr)
            return 2
        try:
            summary = run_campaign(objective, {
                "workspace": args.workspace, "calibration": args.calibration,
                "tavi_url": args.tavi, "seed": args.seed})
        except Exception as exc:  # noqa: BLE001  (a campaign error is exit 2, not a stack trace)
            print(f"isar drive: campaign failed: {exc}", file=sys.stderr)
            return 2
        state = summary["stop"]["state"]
        print(f"isar drive: {state} — {'; '.join(summary['stop']['reasons'])}")
        return {"met": 0, "unmeetable": 3}.get(state, 2)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

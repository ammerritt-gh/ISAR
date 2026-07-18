"""`isar` CLI entry point (UNIFICATION_DESIGN §5).

Subcommands: init, detect, ingest, run, status, report, score, profile, diagnose, view, drive.
Built incrementally; unimplemented commands raise a clear message rather than a stack trace.
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


def _positive_int(text: str) -> int:
    """argparse ``type`` for ``--max-iters``: a strictly-positive integer (>= 1)."""
    value = int(text)   # a non-integer raises ValueError, which argparse reports as an invalid value
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _control_token(text: str) -> str:
    """argparse ``type`` for ``--control-token``: a bounded opaque-token alphabet.

    The token is interpolated directly into a stop-file name (``report/control/stop-<token>.json``),
    so path separators or other special filename characters would silently break stop detection or
    create unintended paths. The GUI's uuid4 tokens fit this alphabet."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text):
        raise argparse.ArgumentTypeError(
            "must be 1-64 characters from [A-Za-z0-9_-] (it names a control file)")
    return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="isar", description="ISAR — inelastic-scattering triage pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="detect the instrument and scaffold an experiment workspace")
    pi.add_argument("data", help="a data file, or a directory containing one")
    pi.add_argument("--out", default=None, help="workspace directory (default ./isar_<instr>_<date>)")
    pi.add_argument("--json", action="store_true", help="emit a machine-readable JSON object")

    pdt = sub.add_parser("detect", help="report the instrument detected from a data path (no writes)")
    pdt.add_argument("data", help="a data file, or a directory containing one")
    pdt.add_argument("--json", action="store_true", help="emit a machine-readable JSON object")

    pin = sub.add_parser("ingest", help="parse data into the ledger without fitting (for the GUI/preview)")
    pin.add_argument("data", nargs="+")
    pin.add_argument("--workspace", required=True)
    pin.add_argument("--json", action="store_true", help="emit a single machine-readable JSON object")

    pr = sub.add_parser("run", help="run the pipeline on a batch of data (idempotent)")
    pr.add_argument("data", nargs="+")
    pr.add_argument("--workspace", required=True)
    pr.add_argument("--force", action="store_true", help="refit every scan, ignoring the ledger")
    pr.add_argument("--llm", action="store_true", help="enable LLM adjudication of QC flags")
    pr.add_argument("--M", type=int, default=200, help="bootstrap draws per scan (default 200)")
    pr.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")
    pr.add_argument("--detectors", default=None, help="restrict fitted detectors (comma-separated)")
    pr.add_argument("--workers", type=int, default=None,
                    help="parallel fit workers across scans (default min(30, cpu-2); "
                         "env ISAR_WORKERS; 1 = serial)")
    pr.add_argument("--errors", default="full", choices=["full", "fast", "cheap", "bayes"],
                    help="uncertainty depth: 'full' (MINOS+bootstrap+profile, default), "
                         "'fast' (HESSE ω₀ interval + trust flag, no bootstrap), "
                         "'cheap' (HESSE only — quick-look verdict + rough ω₀, no interval), or "
                         "'bayes' = full + nested-sampling posterior interval (slow; three-method "
                         "cross-check)")
    pr.add_argument("--bayes-nlive", dest="bayes_nlive", type=int, default=None,
                    help="bayes tier: dynesty live-point budget (default 250; env ISAR_BAYES_NLIVE). "
                         "Benchmark caveat: on very-high-count scans 250 can give narrower credible "
                         "intervals than the step-7-validated 400 despite passing the ESS gate — "
                         "use 400 when the posterior width itself is the deliverable")
    pr.add_argument("--bayes-dlogz", dest="bayes_dlogz", type=float, default=None,
                    help="bayes tier: nested-sampling dlogz stop target (default 0.5; env ISAR_BAYES_DLOGZ)")
    pr.add_argument("--bayes-maxiter", dest="bayes_maxiter", type=int, default=None,
                    help="bayes tier: nested-sampling max iterations (default 30000; env ISAR_BAYES_MAXITER)")
    pr.add_argument("--no-render", dest="render", action="store_false", default=True,
                    help="skip the human report (report.md/.html/.json + figures); results.csv and "
                         "card.state.json are still written. Used by the driver, which re-renders once "
                         "at end of campaign — most run time savings come from not importing the report "
                         "layer / matplotlib per invocation")

    ps = sub.add_parser("status", help="summarize a workspace's ledger without reprocessing")
    ps.add_argument("--workspace", required=True)

    pp = sub.add_parser("profile", help="profile a campaign's session log — where time was spent")
    pp.add_argument("--workspace", required=True)
    pp.add_argument("--log", default=None,
                    help="session log path (default <workspace>/report/session_log.jsonl)")
    pp.add_argument("--top", type=int, default=None,
                    help="show only the N slowest stages (default: all)")
    pp.add_argument("--iterations", action="store_true",
                    help="also print the per-iteration breakdown table")

    prp = sub.add_parser("report", help="re-render the report from persisted state")
    prp.add_argument("--workspace", required=True)
    prp.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")

    pdg = sub.add_parser("diagnose", help="render the architect's CAMPAIGN_DIAGNOSTIC.md for a campaign")
    pdg.add_argument("--workspace", required=True)

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
    psc.add_argument("--interval", choices=("bootstrap", "bayes"), default="bootstrap",
                     help="which reported interval to coverage-score in truth mode: 'bootstrap' "
                          "(default, the 68%% bar) or 'bayes' (posterior credible interval)")
    psc.add_argument("--json", default=None,
                     help="write the full scoring dict to this path (truth mode)")

    pv = sub.add_parser("view", help="open the desktop GUI on a workspace (needs the [gui] extra)")
    pv.add_argument("workspace", nargs="?", default=None,
                    help="an ISAR workspace directory to open read-only (default: offer recovery "
                         "of unfinished temporary work, then open the most recent workspace, or "
                         "an empty window if neither is openable)")

    pd = sub.add_parser("drive", help="run the measurement driver campaign (LLM_HARNESS_DESIGN §8)")
    src = pd.add_mutually_exclusive_group(required=True)
    src.add_argument("--goal", help="natural-language measurement goal (LLM supervisor; needs LLMM)")
    src.add_argument("--spec", help="objective spec JSON file (LLM-free path)")
    src.add_argument("--envelope", help="frozen objective envelope JSON (LLM-free confirmed start)")
    src.add_argument("--resume-latest", dest="resume_latest", action="store_true",
                     help="resume the last campaign in --workspace using its frozen objective from the "
                          "session log (LLM-free — never re-compiles a goal); --budget may still override")
    pd.add_argument("--workspace", required=False,
                    help="ISAR workspace the driver drives (auto-init'd after the first scan)")
    pd.add_argument("--compile-only", dest="compile_only", metavar="OUTPUT",
                    help="compile --goal/--spec to a frozen envelope and exit without contacting TAVI")
    pd.add_argument("--max-iters", dest="max_iters", type=_positive_int, default=None,
                    help="cap the adaptive-loop iterations for THIS invocation (>= 1). Applies per "
                         "invocation: each run_campaign restarts iteration numbering at zero, so a "
                         "restart grants a fresh cap. Default: the built-in cap")
    pd.add_argument("--control-token", dest="control_token", default=None, type=_control_token,
                    help="opaque token ([A-Za-z0-9_-], max 64) enabling a cooperative operator stop: "
                         "writing <workspace>/report/control/stop-<token>.json makes the loop finish "
                         "the current iteration and exit with an operator_stopped state (exit code 4)")
    pd.add_argument("--tier", default=None, choices=["deterministic", "mcstas"],
                    help="fidelity tier (LLM_HARNESS_DESIGN §6; default deterministic). Ignored with "
                         "--resume-latest: the tier is frozen with the resumed objective")
    pd.add_argument("--budget", default=None,
                    help="override budget as '<amount><unit>' e.g. '8h', '1e10n' (else from the spec/goal)")
    pd.add_argument("--calibration", default=None,
                    help="σ-calibration artifact path (default the repo phonon_dft_deterministic.json)")
    pd.add_argument("--tavi", default=None, help="TAVI API base URL (default http://127.0.0.1:8642/api/v1)")
    pd.add_argument("--seed", type=int, default=0, help="base seed for the scan seed sequence")
    pd.add_argument("--no-worker", dest="worker", action="store_false", default=True,
                    help="disable the persistent analysis worker; run a fresh `isar run` subprocess "
                         "per iteration (the pre-worker cold-start path)")
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
    if cmd == "detect":
        from isar.cli import detect_cmd
        return detect_cmd.run(args)
    if cmd == "ingest":
        from isar.cli import ingest_cmd
        return ingest_cmd.run(args)
    if cmd == "run":
        from isar.cli import run_cmd
        return run_cmd.run(args)
    if cmd == "status":
        from isar.cli import status_cmd
        return status_cmd.run(args)
    if cmd == "profile":
        from isar.cli import profile_cmd
        return profile_cmd.run(args)
    if cmd == "report":
        from isar.cli import report_cmd
        return report_cmd.run(args)
    if cmd == "diagnose":
        from isar.cli import diagnose_cmd
        return diagnose_cmd.run(args)
    if cmd == "score":
        from isar.cli import score_cmd
        return score_cmd.run(args)
    if cmd == "view":
        from isar.cli import view_cmd
        return view_cmd.run(args)
    if cmd == "drive":
        import hashlib
        import json
        from pathlib import Path
        from isar.drive.loop import run_campaign
        from isar.drive.session_log import read_log
        from isar.drive.supervisor.goal_to_spec import (
            ENVELOPE_SCHEMA, GoalParseError, envelope_from_json, goal_to_spec, make_envelope, spec_from_dict,
            spec_from_json)
        try:
            if args.compile_only and (args.resume_latest or args.envelope):
                raise GoalParseError("--compile-only accepts --goal or --spec, not resume/envelope")
            if not args.compile_only and not args.workspace:
                raise GoalParseError("--workspace is required unless --compile-only is used")
            if args.resume_latest:
                # LLM-free resume: rebuild the FROZEN objective from the last campaign_start event; a
                # goal is NEVER re-compiled through the LLM (spec_from_dict is a pure dict path).
                log_path = Path(args.workspace) / "report" / "session_log.jsonl"
                starts = [e for e in read_log(log_path)
                          if e.get("kind") == "decide"
                          and (e.get("payload") or {}).get("action") == "campaign_start"]
                if not starts:
                    print(f"isar drive: no campaign to resume in {log_path} "
                          "(no campaign_start event in the session log)", file=sys.stderr)
                    return 2
                payload = starts[-1]["payload"]
                objective = spec_from_dict(payload["objective"],
                                           defaults={"tier": args.tier or "deterministic"},
                                           where="resume")
                # The frozen objective carries its own tier; an explicit --tier cannot override it
                # (a full ObjectiveSpec dict bypasses the defaults). Say so instead of silently ignoring.
                if args.tier is not None and objective.tier != args.tier:
                    print(f"isar drive: note — --tier is frozen with the resumed objective "
                          f"({objective.tier}); the requested '{args.tier}' is ignored",
                          file=sys.stderr)
                # Carry the resumed campaign's fingerprint over so it stays comparable across resumes.
                prev_source = payload.get("source") or {}
                source = {"kind": "resume", "sha256": prev_source.get("sha256")}
            elif args.envelope:
                objective, source, original_goal = envelope_from_json(args.envelope)
                source = dict(source)
                source["envelope_schema"] = ENVELOPE_SCHEMA
                if original_goal is not None:
                    source["original_goal"] = original_goal
            elif args.spec:
                objective = spec_from_json(args.spec, defaults={"tier": args.tier or "deterministic"})
                source = {"kind": "spec",
                          "sha256": hashlib.sha256(Path(args.spec).read_bytes()).hexdigest()}
            else:
                card_hypotheses = None
                if args.workspace:
                    policy_path = Path(args.workspace) / "card.policy.toml"
                    if policy_path.exists():
                        from isar.schema.card import load_policy
                        card_hypotheses = load_policy(policy_path).hypotheses
                objective = goal_to_spec(
                    args.goal, defaults={"tier": args.tier or "deterministic"},
                    card_hypotheses=card_hypotheses)
                source = {"kind": "goal",
                          "sha256": hashlib.sha256(args.goal.encode("utf-8")).hexdigest()}
            if args.budget and not args.envelope:
                objective.budget = _parse_budget(args.budget)
                objective.__post_init__()
            elif args.budget and args.envelope:
                print("isar drive: note — --budget is frozen with the confirmed objective envelope; "
                      "the override is ignored", file=sys.stderr)
            if args.compile_only:
                original_goal = args.goal if args.goal is not None else None
                envelope = make_envelope(objective, source=source, original_goal=original_goal)
                out = Path(args.compile_only)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(envelope))
                return 0
        except (GoalParseError, ValueError, OSError, KeyError) as exc:
            print(f"isar drive: {exc}", file=sys.stderr)
            return 2
        config = {"workspace": args.workspace, "calibration": args.calibration,
                  "tavi_url": args.tavi, "seed": args.seed, "worker": args.worker,
                  "control_token": args.control_token, "source": source}
        if args.max_iters is not None:
            config["max_iters"] = args.max_iters      # per-invocation cap; else the loop's built-in default
        try:
            summary = run_campaign(objective, config)
        except Exception as exc:  # noqa: BLE001  (a campaign error is exit 2, not a stack trace)
            print(f"isar drive: campaign failed: {exc}", file=sys.stderr)
            return 2
        state = summary["stop"]["state"]
        print(f"isar drive: {state} — {'; '.join(summary['stop']['reasons'])}")
        return {"met": 0, "unmeetable": 3, "operator_stopped": 4,
                "pending_recovery": 5}.get(state, 2)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

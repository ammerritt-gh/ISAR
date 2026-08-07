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
    pr.add_argument("--force-master", dest="force_masters", action="append", default=None,
                    metavar="MASTER_ID", help=argparse.SUPPRESS)
    pr.add_argument("--llm", action="store_true", help="enable LLM adjudication of QC flags")
    pr.add_argument("--M", type=int, default=200,
                    help="bootstrap draw cap for pooled masters and certification (default 200)")
    pr.add_argument("--scan-M", dest="scan_M", type=int, default=None,
                    help="bootstrap draw cap for provisional scan fits (default 50)")
    pr.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")
    pr.add_argument("--detectors", default=None, help="restrict fitted detectors (comma-separated)")
    pr.add_argument("--workers", type=int, default=None,
                    help="total fitting worker budget, assigned across scans or within a promoted "
                         "fit (default min(30, cpu-2); env ISAR_WORKERS; 1 = serial)")
    pr.add_argument("--errors", default="full", choices=["auto", "full", "fast", "cheap", "bayes"],
                    help="uncertainty depth: 'auto' (HESSE-first with selective full promotion), "
                         "'full' (MINOS+bootstrap+profile, default), "
                         "'fast' (HESSE ω₀ interval + trust flag, no bootstrap), "
                         "'cheap' (HESSE only — quick-look verdict + rough ω₀, no interval), or "
                         "'bayes' = full + nested-sampling posterior interval (slow; three-method "
                         "cross-check)")
    pr.add_argument("--scan-errors", dest="scan_errors_tier", default=None,
                    choices=["auto", "full", "fast", "cheap", "bayes"], help=argparse.SUPPRESS)
    pr.add_argument("--master-errors", dest="master_errors_tier", default=None,
                    choices=["auto", "full", "fast", "cheap", "bayes"], help=argparse.SUPPRESS)
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
    pr.add_argument("--certification-manifest", default=None, help=argparse.SUPPRESS)
    pr.add_argument("--certification-request", default=None, help=argparse.SUPPRESS)
    pr.add_argument("--analysis-allowance-seconds", type=float, default=None,
                    help=argparse.SUPPRESS)
    pr.add_argument("--finish-initial-wave", action="store_true", help=argparse.SUPPRESS)

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
    pp.add_argument("--compare-workspace", default=None,
                    help="compare scientific decisions and certified outputs with another workspace")

    prp = sub.add_parser("report", help="re-render the report from persisted state")
    prp.add_argument("--workspace", required=True)
    prp.add_argument("--formats", default="html,markdown", help="report formats (comma-separated)")

    pdg = sub.add_parser("diagnose", help="render the architect's CAMPAIGN_DIAGNOSTIC.md for a campaign")
    pdg.add_argument("--workspace", required=True)
    pdg.add_argument("--eig", action="store_true",
                     help="print the scan-window EIG decision table instead (read-only: says why "
                          "EIG did or did not change each scan window, and what blocked it)")

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
    src.add_argument("--program", help="isar.campaign-program/1 JSON file (LLM-free path; also "
                                       "what --compile-only writes for a confirmed start)")
    src.add_argument("--resume-latest", dest="resume_latest", action="store_true",
                     help="resume the last campaign in --workspace using its frozen objective from the "
                          "session log (LLM-free — never re-compiles a goal); --budget may still override")
    pd.add_argument("--workspace", required=False,
                    help="ISAR workspace the driver drives (auto-init'd after the first scan)")
    pd.add_argument("--compile-only", dest="compile_only", metavar="OUTPUT",
                    help="compile --goal/--program to a frozen campaign program and exit without "
                         "contacting TAVI")
    pd.add_argument("--control-token", dest="control_token", default=None, type=_control_token,
                    help="opaque token ([A-Za-z0-9_-], max 64) enabling a cooperative operator stop: "
                         "writing <workspace>/report/control/stop-<token>.json makes the loop finish "
                         "the current iteration and exit with an operator_stopped state (exit code 4)")
    pd.add_argument("--tier", default=None, choices=["deterministic", "mcstas"],
                    help="fidelity tier (LLM_HARNESS_DESIGN §6; default deterministic). Ignored with "
                         "--resume-latest: the tier is frozen with the resumed objective")
    pd.add_argument("--budget", default=None,
                    help="override budget as '<amount><unit>' e.g. '8h', '1e10n' (else from the spec/goal)")
    pd.add_argument("--analysis-limit", default=None,
                    help="absolute cumulative analysis ceiling, e.g. '1h', '10m', '3600s', or 'none'")
    pd.add_argument("--scan-window-eig-mode", choices=["off", "shadow", "authoritative"],
                    default=None,
                    help="operator authoring choice for scan-window EIG; authoritative is restricted "
                         "to the exact O41 profile frozen from a ready workspace")
    pd.add_argument("--calibration", default=None,
                    help="σ-calibration artifact path (default the repo phonon_dft_deterministic.json)")
    pd.add_argument("--tavi", default=None, help="TAVI API base URL (default http://127.0.0.1:8642/api/v1)")
    pd.add_argument("--seed", type=int, default=0, help="base seed for the scan seed sequence")
    pd.add_argument("--no-worker", dest="worker", action="store_false", default=True,
                    help="disable the persistent analysis worker; run a fresh `isar run` subprocess "
                         "per iteration (the pre-worker cold-start path)")
    return p


class _StepNotExecutable(Exception):
    """A campaign program parsed, validated and froze, but this build cannot execute it.

    Deliberately not a ``ValueError``: a refusal to run a well-formed program is a different
    outcome from a malformed one, and ``isar drive`` gives it its own exit code (7) so an operator
    script can tell "I asked for something that is not built yet" from "I wrote it wrong" (2) and
    from a crash.
    """


def _single_executable_step(program, *, where: str):
    """The one targeted step this build runs, or a refusal naming the slice that adds the rest."""
    if len(program.steps) > 1:
        raise _StepNotExecutable(
            f"{where}: this campaign program declares {len(program.steps)} steps and froze "
            "cleanly, but sequencing more than one step lands in SCENE-4. Run one step per "
            "program for now.")
    step = program.steps[0]
    if step.kind != "targeted":
        raise _StepNotExecutable(
            f"{where}: step {step.id!r} is a {step.kind!r} step. It parsed, validated and froze "
            "cleanly; executing discovery steps lands in SCENE-4. Declare a targeted step to "
            "measure now.")
    return step


_SPEC_SUFFIXES = {".json", ".toml", ".yaml", ".yml"}


def _looks_like_a_path(text: str) -> bool:
    """Whether ``text`` is a file path rather than a natural-language goal.

    Deliberately generous: an existing file is decisive, and so is a spec-shaped suffix even when
    the file is absent, because a mistyped path is the same mistake as a correct one. A real goal
    is a sentence and does not end in ``.json``. Path construction itself can raise on Windows for
    a sentence containing characters illegal in a filename, which is only more evidence it is prose.
    """
    from pathlib import Path  # local, matching this module's deferred-import style

    value = (text or "").strip()
    if not value:
        return False
    try:
        candidate = Path(value)
        return candidate.suffix.lower() in _SPEC_SUFFIXES or candidate.is_file()
    except (OSError, ValueError):
        return False


def _parse_budget(text: str) -> dict:
    """Parse a ``--budget`` override: ``'8h'`` → counting_seconds, ``'1e10n'`` → neutrons.

    A bare number is treated as counting-seconds. Accepts hours, minutes, seconds, and neutrons.

    The unit is matched as a whole word, never as a suffix: suffix matching across overlapping
    vocabularies resolved ``10min`` as "…n for neutrons" and crashed on ``float('10mi')``.
    """
    import math
    import re

    s = str(text).strip().lower()
    match = re.fullmatch(r"([0-9.eE+-]+)\s*([a-z]*)", s)
    if match is None:
        raise ValueError(f"could not parse a budget from {text!r}")
    amount = float(match.group(1))
    if not math.isfinite(amount):
        # A non-finite budget serializes as bare ``Infinity``/``NaN``, which is not JSON and which
        # a strict reader of the campaign objective rejects.
        raise ValueError(f"budget amount must be finite, not {match.group(1)!r}")
    units = {
        "": ("counting_seconds", 1.0),
        "n": ("neutrons", 1.0), "neutron": ("neutrons", 1.0), "neutrons": ("neutrons", 1.0),
        "h": ("counting_seconds", 3600.0), "hr": ("counting_seconds", 3600.0),
        "hrs": ("counting_seconds", 3600.0), "hour": ("counting_seconds", 3600.0),
        "hours": ("counting_seconds", 3600.0),
        "m": ("counting_seconds", 60.0), "min": ("counting_seconds", 60.0),
        "mins": ("counting_seconds", 60.0), "minute": ("counting_seconds", 60.0),
        "minutes": ("counting_seconds", 60.0),
        "s": ("counting_seconds", 1.0), "sec": ("counting_seconds", 1.0),
        "secs": ("counting_seconds", 1.0), "second": ("counting_seconds", 1.0),
        "seconds": ("counting_seconds", 1.0),
    }
    unit = match.group(2)
    if unit not in units:
        raise ValueError(f"unknown budget unit {unit!r} in {text!r}")
    kind, multiplier = units[unit]
    return {"kind": kind, "amount": amount * multiplier}


def _parse_analysis_limit(text: str) -> float | None:
    if str(text).strip().lower() == "none":
        return None
    parsed = _parse_budget(text)
    if parsed["kind"] != "counting_seconds":
        raise ValueError("--analysis-limit accepts a duration or 'none', not neutrons")
    return float(parsed["amount"])


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
        from dataclasses import replace
        from pathlib import Path
        from isar.drive.loop import run_campaign
        from isar.drive.session_log import read_log
        from isar.drive.supervisor.goal_to_spec import (
            GoalParseError, goal_to_program, make_program, program_from_json,
            rebind_scan_window_eig_policy, spec_from_dict,
        )
        from isar.drive.objective import OBJECTIVE_SCHEMA
        from isar.schema.campaign_program import PROGRAM_SCHEMA
        from isar.schema.hypotheses import content_fingerprint
        try:
            if args.compile_only and args.resume_latest:
                raise GoalParseError("--compile-only accepts --goal or --program, not resume")
            if not args.compile_only and not args.workspace:
                raise GoalParseError("--workspace is required unless --compile-only is used")

            def card_objective_authoring():
                if not args.workspace:
                    return None, None
                policy_path = Path(args.workspace) / "card.policy.toml"
                if not policy_path.exists():
                    return None, None
                from isar.schema.card import load_policy
                card_policy = load_policy(policy_path)
                return card_policy.hypotheses, card_policy.scan_window_eig_policy_set

            def apply_mode_override(objective, *, frozen: bool, declared_mode=None,
                                    card_eig_policy_set=None):
                requested = args.scan_window_eig_mode
                current = str((objective.steering or {}).get("scan_window_eig_mode", "off"))
                if requested is None:
                    return objective
                if frozen:
                    if requested != current:
                        raise GoalParseError(
                            "--scan-window-eig-mode conflicts with the frozen objective "
                            f"({current})")
                    return objective
                if declared_mode is not None and requested != declared_mode:
                    raise GoalParseError(
                        "--scan-window-eig-mode conflicts with steering.scan_window_eig_mode "
                        f"in the authoring input ({declared_mode})")
                if requested == "authoritative" and not args.workspace:
                    raise GoalParseError(
                        "authoritative scan-window EIG requires a ready --workspace with "
                        "card.policy.toml")
                steering = {**dict(objective.steering), "scan_window_eig_mode": requested}
                overridden = replace(
                    objective, steering=steering,
                    hypothesis_fingerprint=content_fingerprint(
                        objective.hypotheses, steering))
                # The recomputed hypothesis fingerprint is part of the objective comparison scope a
                # card EIG policy binds to, so the policy compiled before this override no longer
                # matches. Rebind it here, or scan-window scoring reports qoi_policy_missing for the
                # whole campaign.
                return rebind_scan_window_eig_policy(overridden, card_eig_policy_set)

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
                objective = apply_mode_override(objective, frozen=True)
                # The frozen objective carries its own tier; an explicit --tier cannot override it
                # (a full ObjectiveSpec dict bypasses the defaults). Say so instead of silently ignoring.
                if args.tier is not None and objective.tier != args.tier:
                    print(f"isar drive: note — --tier is frozen with the resumed objective "
                          f"({objective.tier}); the requested '{args.tier}' is ignored",
                          file=sys.stderr)
                # Carry the resumed campaign's fingerprint over so it stays comparable across resumes.
                prev_source = payload.get("source") or {}
                source = {"kind": "resume", "sha256": prev_source.get("sha256")}
                for field in ("campaign_authoring_fingerprint", "objective_fingerprint"):
                    if prev_source.get(field) is not None:
                        source[field] = prev_source[field]
            elif args.program:
                program = program_from_json(args.program)
                step = _single_executable_step(program, where=str(args.program))
                payload = step.payload
                # A payload carrying the frozen targeted schema is already compiled and must never
                # reread card policy; compact authoring input may inherit the human-owned card.
                frozen_payload = payload.get("schema") == OBJECTIVE_SCHEMA
                card_hypotheses = None
                card_eig_policy_set = None
                if not frozen_payload:
                    card_hypotheses, card_eig_policy_set = card_objective_authoring()
                objective = spec_from_dict(
                    payload,
                    defaults={"tier": args.tier or "deterministic"},
                    where=f"{args.program}:{step.id}",
                    card_hypotheses=card_hypotheses,
                    card_eig_policy_set=card_eig_policy_set,
                )
                declared_mode = None
                if isinstance(payload.get("steering"), dict):
                    declared_mode = payload["steering"].get("scan_window_eig_mode")
                objective = apply_mode_override(
                    objective, frozen=frozen_payload, declared_mode=declared_mode,
                    card_eig_policy_set=card_eig_policy_set)
                # A program written by --compile-only carries the fingerprint of the authoring
                # input it was compiled from; a hand-written one is identified by its own bytes.
                source = dict(program.source) or {
                    "kind": "program",
                    "sha256": hashlib.sha256(Path(args.program).read_bytes()).hexdigest()}
                source["program_schema"] = PROGRAM_SCHEMA
                source["program_step_id"] = step.id
                if program.source_text is not None:
                    source["source_text"] = program.source_text
            else:
                # ``--goal`` is natural language for the LLM supervisor; ``--program`` reads a file.
                # Handing a path to the supervisor does not fail — it compiles *something*, and the
                # campaign then runs a different objective than the file describes, silently. That
                # cost a real run: a spec for the acoustic branch along h 2.0→3.0 compiled instead
                # as an optic campaign along [hh0] from the zone origin over [-2, 12] meV, and the
                # only symptom was an immediately exhausted action inventory.
                if _looks_like_a_path(args.goal):
                    print(f"error: --goal takes a natural-language measurement goal, not a file "
                          f"path ({args.goal!r}).\n"
                          f"       Use --program {args.goal} to read a campaign program from a "
                          "file.", file=sys.stderr)
                    return 2
                card_hypotheses, card_eig_policy_set = card_objective_authoring()
                program = goal_to_program(
                    args.goal, defaults={"tier": args.tier or "deterministic"},
                    card_hypotheses=card_hypotheses,
                    card_eig_policy_set=card_eig_policy_set)
                step = _single_executable_step(program, where="--goal")
                objective = spec_from_dict(step.payload, where=f"--goal:{step.id}")
                raw = objective.meta.get("raw") if isinstance(objective.meta, dict) else None
                declared_mode = ((raw.get("steering") or {}).get("scan_window_eig_mode")
                                 if isinstance(raw, dict) else None)
                objective = apply_mode_override(
                    objective, frozen=False, declared_mode=declared_mode,
                    card_eig_policy_set=card_eig_policy_set)
                source = {"kind": "goal",
                          "sha256": hashlib.sha256(args.goal.encode("utf-8")).hexdigest(),
                          "program_schema": PROGRAM_SCHEMA, "program_step_id": step.id,
                          "source_text": args.goal}
            if args.budget:
                objective.budget = _parse_budget(args.budget)
                objective.__post_init__()
            if args.analysis_limit is not None:
                objective.analysis_limit_seconds = _parse_analysis_limit(args.analysis_limit)
                objective.__post_init__()
            if not args.resume_latest:
                objective_payload = objective.to_dict()
                objective_fingerprint = hashlib.sha256(json.dumps(
                    objective_payload, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True).encode("utf-8")).hexdigest()
                policy_set = objective.scan_window_eig.policy_set
                authoring_payload = {
                    "source_sha256": source.get("sha256"),
                    "scan_window_eig_mode": objective.steering.get(
                        "scan_window_eig_mode", "off"),
                    "policy_set_fingerprint": (
                        None if policy_set is None else policy_set.policy_set_fingerprint),
                    "tier": objective.tier,
                    "budget": objective.budget,
                    "analysis_limit_seconds": objective.analysis_limit_seconds,
                }
                source["objective_fingerprint"] = objective_fingerprint
                source["campaign_authoring_fingerprint"] = hashlib.sha256(json.dumps(
                    authoring_payload, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True).encode("utf-8")).hexdigest()
            if args.compile_only:
                compiled = make_program(objective, source=source,
                                        source_text=source.get("source_text")).to_dict()
                out = Path(args.compile_only)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(compiled, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(compiled))
                return 0
        except _StepNotExecutable as exc:
            print(f"isar drive: {exc}", file=sys.stderr)
            return 7
        except (GoalParseError, ValueError, OSError, KeyError) as exc:
            print(f"isar drive: {exc}", file=sys.stderr)
            return 2
        config = {"workspace": args.workspace, "calibration": args.calibration,
                  "tavi_url": args.tavi, "seed": args.seed, "worker": args.worker,
                  "control_token": args.control_token, "source": source}
        try:
            summary = run_campaign(objective, config)
        except Exception as exc:  # noqa: BLE001  (a campaign error is exit 2, not a stack trace)
            print(f"isar drive: campaign failed: {exc}", file=sys.stderr)
            return 2
        state = summary["stop"]["state"]
        print(f"isar drive: {state} — {'; '.join(summary['stop']['reasons'])}")
        return {"met": 0, "incomplete": 3, "operator_stopped": 4,
                "pending_recovery": 5, "infeasible": 6}.get(state, 2)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

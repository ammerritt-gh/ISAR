"""`isar run <data>... --workspace DIR` — the full pipeline, idempotent (UNIFICATION_DESIGN §5)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import uuid

from isar.schema import card as card_mod
from isar.session import driver
from isar.session.timing import StageTimer
from isar.session.workspace import Workspace


log = logging.getLogger(__name__)

_REPORT_STATUS = "report.complete.json"


def _build_scene(ws, summary) -> None:
    """Build and publish this workspace's scene revision. NEVER raises (SCENE-4.3 D1).

    Fail-soft is the composition's contract, inherited unchanged from the deleted driver hook:
    `isar run` delivers ANALYSIS, and a scene that cannot be built is a missing explanation, not a
    failed delivery — the fits, results.csv and session.json are already written and unaffected.
    The operator-visible surface is unchanged too: the same `scene revision …` context line the
    hook appended, printed by `run` below. The two callers that DO need a refusal ask for one by
    name: `isar report --scene` exits nonzero, and the campaign parent classifies its own failure
    as a controller fault rather than steering from an older scene.

    Runs on every delivery, including one that fitted nothing: the store's content-addressed
    unchanged path makes a rebuild of an unchanged workspace free and leaves the pointer where it
    was.
    """
    try:
        from isar.session import scene_hook
        result = scene_hook.build_and_store(ws.root)
    except ValueError as exc:
        # The adapter's own refusals (no candidates column, an unqualified master, a row predating
        # the delivery contract) and a corrupt scene pointer. Expected, and a workspace that
        # refuses once refuses on EVERY delivery — a traceback per scan would bury the log for a
        # condition whose message already says what to do.
        log.warning("scene revision skipped for %s: %s", ws.root, exc)
        summary.context_lines.append(f"scene revision skipped: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — see the docstring: a scene never fails a delivery
        log.exception("scene revision skipped for %s", ws.root)
        summary.context_lines.append(f"scene revision skipped: {exc}")
        return
    summary.context_lines.append(f"scene revision {result.digest[:12]}"
                                 + (" (unchanged)" if result.unchanged else ""))


def _atomic_write_text(path, text: str) -> None:
    """Replace one small report-status artifact without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _results_revision(report_dir) -> str | None:
    results = report_dir / "results.csv"
    return hashlib.sha256(results.read_bytes()).hexdigest() if results.exists() else None


def _publish_report_status(report_dir, *, status: str, run_id: str,
                           analysis_revision: str | None, replace_reports: bool) -> None:
    """Publish report availability, optionally replacing every canonical human-report artifact."""
    payload = {
        "schema": "isar.report_completion/1",
        "status": status,
        "run_id": run_id,
        "analysis_revision": analysis_revision,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if replace_reports:
        explanation = (
            f"ISAR report unavailable: {status.replace('_', ' ')}.\n"
            f"Analysis revision: {analysis_revision or 'not yet published'}.\n")
        _atomic_write_text(report_dir / "report.md", "# ISAR report unavailable\n\n" + explanation)
        _atomic_write_text(
            report_dir / "report.html",
            "<!doctype html><meta charset=\"utf-8\"><title>ISAR report unavailable</title>"
            f"<h1>ISAR report unavailable</h1><p>{explanation}</p>")
        _atomic_write_text(report_dir / "report.json", encoded)
    _atomic_write_text(report_dir / _REPORT_STATUS, encoded)


def run(args) -> int:
    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2

    # U7: a typo'd card section silently disables guidelines — warn.
    for unknown in card_mod.unknown_toml_tables(ws.policy_path):
        print(f"warning: unknown card section [{unknown}] — ignored (typo?)", file=sys.stderr)

    errors_tier = getattr(args, "errors", "full")
    scan_errors_tier = getattr(args, "scan_errors_tier", None)
    master_errors_tier = getattr(args, "master_errors_tier", None)
    # Bayes-tier sampler knobs: pass only the explicitly-set (non-None) CLI overrides; the driver
    # merges CLI > env > defaults. Empty dict on every other tier / when no override was given.
    bayes_cli = {k: v for k, v in (("n_live", getattr(args, "bayes_nlive", None)),
                                   ("dlogz", getattr(args, "bayes_dlogz", None)),
                                   ("max_iter", getattr(args, "bayes_maxiter", None)))
                 if v is not None}
    scan_M = getattr(args, "scan_M", None)
    timer_meta = {"errors_tier": errors_tier, "M": args.M,
                  "scan_M": (50 if scan_M is None else scan_M), "argv": list(sys.argv)}
    if scan_errors_tier is not None:
        timer_meta["scan_errors_tier"] = scan_errors_tier
    if master_errors_tier is not None:
        timer_meta["master_errors_tier"] = master_errors_tier
    timer = StageTimer(**timer_meta)
    run_id = uuid.uuid4().hex
    # Invalidate any prior canonical report before results can change.  Readers may see a compact
    # running-status document, but never a successful report describing an older results.csv.
    _publish_report_status(ws.report_dir, status="running", run_id=run_id,
                           analysis_revision=None, replace_reports=True)
    try:
        formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
        certification_manifest = getattr(args, "certification_manifest", None)
        certification_request = getattr(args, "certification_request", None)
        if certification_request is not None:
            if certification_manifest is not None:
                raise ValueError("certification request and manifest are mutually exclusive")
            from isar.session.certification import create_master_certification_manifest
            certification_manifest = create_master_certification_manifest(
                ws.root, certification_request)
        role_kwargs = {}
        if scan_errors_tier is not None:
            role_kwargs["scan_errors_tier"] = scan_errors_tier
        if master_errors_tier is not None:
            role_kwargs["master_errors_tier"] = master_errors_tier
        summary = driver.run_batch(
            ws, args.data, force=args.force,
            force_masters=getattr(args, "force_masters", None),
            llm=args.llm, M=args.M, scan_M=scan_M,
            detectors=args.detectors, formats=formats,
            workers=getattr(args, "workers", None), errors_tier=errors_tier,
            bayes_cli=bayes_cli, timer=timer,
            certification_manifest=certification_manifest,
            analysis_allowance_seconds=getattr(args, "analysis_allowance_seconds", None),
            finish_initial_wave=bool(getattr(args, "finish_initial_wave", False)),
            **role_kwargs)
        analysis_incomplete = bool(getattr(summary, "analysis_incomplete", False))
        analysis_revision = _results_revision(ws.report_dir)

        # SCENE-4.3 D1: `isar run` is an explicit COMPOSITION — the analysis above, then the ONE
        # canonical scene build. It used to run inside `driver.run_batch`; owning it here keeps
        # the analysis child analysis-only while preserving the behaviour every `isar run`
        # frontend already had (the GUI shells this command and has no scene call site of its
        # own). `--no-scene` is the campaign runners' opt-out, not a second build semantics: the
        # campaign parent invokes the same `build_and_store` itself and pins the digest.
        # Placed immediately after run_batch, where the hook used to sit — after results.csv and
        # card.state are on disk, before the human report — so the scene sees the delivery that
        # just landed. An INCOMPLETE analysis builds no scene, exactly like the old hook (it sat
        # after the writes AnalysisWorkIncomplete aborts): a revision built here would describe
        # the previous ledger while the run reports incomplete.
        if getattr(args, "scene", True) and not analysis_incomplete:
            with timer.stage("scene_observe"):
                _build_scene(ws, summary)

        # Try the rich report (build step 6); the CSV that run_batch always writes is the fallback.
        report_note = f"results: {ws.report_dir / 'results.csv'}"
        # --no-render (driver campaigns): skip report.md/.html/.json + figures entirely — nothing
        # mid-campaign consumes them (drive reads only results.csv / card.state.json) and the report
        # layer / matplotlib import dominates per-invocation cost. The campaign re-renders once at the
        # end. Don't emit a zero-length report_render timing stage when skipping.
        if analysis_incomplete:
            _publish_report_status(
                ws.report_dir, status="incomplete", run_id=run_id,
                analysis_revision=analysis_revision, replace_reports=True)
            report_note = f"report unavailable: incomplete analysis ({ws.report_dir / 'results.csv'})"
        elif not getattr(args, "render", True):
            _publish_report_status(
                ws.report_dir, status="not_rendered", run_id=run_id,
                analysis_revision=analysis_revision, replace_reports=True)
        else:
            try:
                with timer.stage("report_render"):
                    from isar.report import run_report
                    card, session = ws.load()
                    run_report.write_run_report(session, card, ws, formats=formats)
                _publish_report_status(
                    ws.report_dir, status="complete", run_id=run_id,
                    analysis_revision=analysis_revision, replace_reports=False)
                report_note = f"report: {ws.report_dir}"
            except Exception as exc:  # noqa: BLE001 — results.csv remains the canonical fallback
                _publish_report_status(
                    ws.report_dir, status="render_failed", run_id=run_id,
                    analysis_revision=analysis_revision, replace_reports=True)
                print(f"warning: report rendering failed: {exc}", file=sys.stderr)
                report_note = f"report unavailable: render failed ({ws.report_dir / 'results.csv'})"
    except Exception:
        _publish_report_status(
            ws.report_dir, status="analysis_failed", run_id=run_id,
            analysis_revision=_results_revision(ws.report_dir), replace_reports=True)
        raise
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
    return 75 if analysis_incomplete else 0

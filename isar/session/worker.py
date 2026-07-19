"""Persistent ISAR analysis worker (mighty-starfish §A).

A long-lived process the driver's :class:`~isar.drive.isar_io.worker_client.WorkerRunner` starts once
per campaign and feeds one ``run`` job per iteration over a JSONL request/reply protocol on
stdin/stdout. Reusing one warm interpreter avoids the interpreter + (formerly) matplotlib cold-start
that dominates per-invocation ``isar run`` cost.

Boundary: this module imports **stdlib + isar.session only**. It MUST NOT import ``isar.drive``,
``isar.cli``, or ``matplotlib`` — the worker never renders (``run_batch`` is called with
``formats=()`` and nothing here touches the report layer). ``tests/session/test_worker_boundary.py``
AST-enforces this.

The ``if __name__ == "__main__"`` guard at the bottom is load-bearing: ``run_batch`` fits scans in a
``ProcessPoolExecutor`` whose children re-import this module under the Windows ``spawn`` start method.
Without the guard those children would re-enter :func:`serve` on stdin they don't own.

Protocol (one JSON object per line, request → reply):

- Request:  ``{"id", "cmd": "run"|"ping"|"shutdown", "data_paths", "force", "errors_tier", "M", "workers"}``
- ``run`` reply:  ``{"id", "ok": true, "summary": {...}, "timings": {...}, "error": null}``
- failure reply:  ``{"id", "ok": false, "summary": null, "timings": null, "error": {type, message, traceback}}``
- ``ping`` reply:  ``{"id", "ok": true}``
- ``shutdown`` / stdin EOF → clean ``exit 0``.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback


def _reconfigure_stdio() -> None:
    """Reconfigure every std stream to UTF-8 (mirrors :func:`isar.cli.main._reconfigure_stdio`).

    Windows consoles default to cp1252 but request lines and reports carry ω/Δ/≤; stdin is included
    because request payloads (data paths) may be non-ASCII."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 — a non-reconfigurable stream (e.g. a plain pipe) is left as-is
            pass


def _reply(proto_out, obj) -> None:
    """Write one JSON reply line and flush (bufsize=1 text pipes still need an explicit flush)."""
    proto_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proto_out.flush()


def _run_job(workspace_path, req) -> tuple[dict, dict]:
    """Execute one ``run`` job: ``run_batch`` on the workspace, then persist + return its timings.

    Returns ``(summary_dict, timings_dict)``. ``run_timings.json`` is written to the workspace report
    dir BEFORE the caller replies — same contract as :func:`isar.cli.run_cmd.run`, so the loop's
    ``_read_run_timings`` off-disk read keeps working unchanged."""
    from isar.session import driver
    from isar.session.timing import StageTimer
    from isar.session.workspace import Workspace

    ws = Workspace.open(workspace_path)
    data_paths = req.get("data_paths") or []
    force = bool(req.get("force", False))
    errors_tier = req.get("errors_tier", "full")
    M = int(req.get("M") or 200)
    workers = req.get("workers")
    certification_manifest = req.get("certification_manifest")
    certification_request = req.get("certification_request")
    analysis_allowance_seconds = req.get("analysis_allowance_seconds")
    finish_initial_wave = bool(req.get("finish_initial_wave", False))

    timer = StageTimer(errors_tier=errors_tier, M=M, worker_job=True, job_id=req.get("id"))
    run_kwargs = {"force": force, "M": M, "workers": workers,
                  "errors_tier": errors_tier, "formats": (), "timer": timer}
    # Omit the additive extension entirely for normal jobs so legacy in-process adapters and test
    # doubles continue to satisfy the runner boundary unchanged.
    if certification_request is not None:
        if certification_manifest is not None:
            raise ValueError("certification request and manifest are mutually exclusive")
        from isar.session.certification import create_master_certification_manifest
        certification_manifest = create_master_certification_manifest(
            ws.root, certification_request)
    if certification_manifest is not None:
        run_kwargs["certification_manifest"] = certification_manifest
    if analysis_allowance_seconds is not None:
        run_kwargs["analysis_allowance_seconds"] = float(analysis_allowance_seconds)
        run_kwargs["finish_initial_wave"] = finish_initial_wave
    summary = driver.run_batch(ws, data_paths, **run_kwargs)
    # Persist run_timings.json (same contract as isar.cli.run_cmd.run — the loop reads it off disk),
    # then read it back so the inline reply timings are byte-identical to the file (timing contract).
    timings_path = ws.report_dir / "run_timings.json"
    timer.write(timings_path)
    try:
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a write/read hiccup falls back to the live dict
        timings = timer.to_dict()

    reply_summary = {
        "n_reprocessed": summary.n_reprocessed,
        "n_fitted_this_run": summary.n_fitted_this_run,
        "counts": summary.counts,
        "n_outcomes": len(summary.outcomes),
        "n_gaps": len(summary.gaps),
        "context_lines": list(summary.context_lines),
        "analysis_incomplete": bool(getattr(summary, "analysis_incomplete", False)),
    }
    return reply_summary, timings


def serve(workspace_path, stdin, proto_out) -> int:
    """JSONL request/reply loop. Returns 0 on ``shutdown`` or stdin EOF.

    A ``run_batch`` exception replies ``ok:false`` with a traceback and the worker keeps serving; a
    malformed line replies an ``id:null`` error and the loop continues (both are the driver's problem
    to escalate, never the worker's to die on)."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001 — malformed line: error reply, keep serving
            _reply(proto_out, {"id": None, "ok": False, "summary": None, "timings": None,
                               "error": {"type": type(e).__name__, "message": str(e), "traceback": ""}})
            continue
        if not isinstance(req, dict):   # valid JSON but not an object (a number/array/null) — same deal
            _reply(proto_out, {"id": None, "ok": False, "summary": None, "timings": None,
                               "error": {"type": "ValueError",
                                         "message": f"request must be a JSON object, got {type(req).__name__}",
                                         "traceback": ""}})
            continue

        cmd = req.get("cmd")
        if cmd == "shutdown":
            return 0
        if cmd == "ping":
            _reply(proto_out, {"id": req.get("id"), "ok": True})
            continue
        if cmd == "run":
            try:
                summary, timings = _run_job(workspace_path, req)
                _reply(proto_out, {"id": req.get("id"), "ok": True, "summary": summary,
                                   "timings": timings, "error": None})
            except Exception as e:  # noqa: BLE001 — job failure: report it, worker stays alive
                _reply(proto_out, {"id": req.get("id"), "ok": False, "summary": None, "timings": None,
                                   "error": {"type": type(e).__name__, "message": str(e),
                                             "traceback": traceback.format_exc()}})
            continue
        _reply(proto_out, {"id": req.get("id"), "ok": False, "summary": None, "timings": None,
                           "error": {"type": "ValueError", "message": f"unknown cmd: {cmd!r}",
                                     "traceback": ""}})
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="isar-worker", description="persistent ISAR analysis worker")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)

    _reconfigure_stdio()
    # Capture the protocol stream, then rebind sys.stdout → sys.stderr so any stray print() from the
    # analysis path lands on stderr (the worker.log) instead of corrupting the JSONL protocol.
    proto_out = sys.stdout
    sys.stdout = sys.stderr
    return serve(args.workspace, sys.stdin, proto_out)


if __name__ == "__main__":
    raise SystemExit(main())

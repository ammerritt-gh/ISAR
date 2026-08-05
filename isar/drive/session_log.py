"""The driver's decision log (WORKING — LLM_HARNESS_DESIGN §8 step 6, the deliverable).

An append-only JSONL record of every decision the campaign makes: what it proposed, why, what ISAR
said, and how it decided to stop. It is part of the campaign deliverable (LLM_HARNESS_DESIGN §8
step 6, "the driver's session log — what it measured, why, and how it decided to stop") and the audit
trail for a run that spent beamtime.

One JSON object per line, so the file stays valid and readable even if a run is killed mid-write.

Event ``kind``s (the documented vocabulary):

- ``propose``   — the acquisition engine proposed a scan (Q, energy window, neutron count).
- ``validate``  — a TAVI ``POST /validate`` result (would_queue / blockers).
- ``run``       — a scan was submitted / finished on the instrument (job_id, state).
- ``analyze``   — ISAR was run and its results/verdicts read.
- ``decide``       — the supervisor updated the ω(q) model or chose the next action.
- ``escalate_fit`` — a fit was promoted from a cheap error tier to a fuller one (``phase="promote"``),
  and the promotion's result (``phase="outcome"``: ``seated`` / ``failed`` / ``superseded``).
- ``branch_track_update`` — a pooled update caused a new joint path-assignment revision.
- ``branch_reassignment`` — neighboring evidence changed a candidate's branch identity.
- ``frontier_blocked`` — an unresolved identity gap currently prevents outward certification.
- ``exception``    — a non-routine branch (missed window, flagged/held, budget, validation rejection).
- ``stop``         — the terminal decision: met / incomplete / infeasible, with reasons.
- ``timing``       — a profiling span: how long a named stage took (``stage``, ``iter``, ``seconds``, ``ok``).
"""
from __future__ import annotations

import json
import os
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

EVENT_KINDS = ("propose", "validate", "run", "analyze", "decide", "escalate_fit",
               "branch_track_update", "branch_reassignment", "frontier_blocked", "exception",
               "stop", "timing", "activity_start", "activity_progress", "activity_end",
               "analysis_clock", "physical_submission", "pooled_master_update", "candidate_update",
               "certification_crossing", "recovery_pending", "recovery_completed",
               "scan_design_realization", "scan_design_realized_comparison",
               "scan_design_authority_decision", "submission_intent", "eig_decision")


class AnalysisLimitExceeded(RuntimeError):
    """A new top-level analysis operation may not start within the campaign ceiling."""


def log_event(path: str | Path, kind: str, payload: dict[str, Any], *, ts: float | None = None) -> dict[str, Any]:
    """Append one event to the JSONL log at ``path`` and return the written record.

    ``kind`` should be one of :data:`EVENT_KINDS` (not enforced — an unknown kind is logged with a
    ``"kind_unknown": true`` marker rather than dropped, so nothing is silently lost). ``payload`` must
    be JSON-serializable. A ``ts`` (epoch seconds) is stamped automatically when not supplied.
    """
    record = {
        "ts": float(ts) if ts is not None else time.time(),
        "kind": kind,
        "payload": payload,
    }
    if kind not in EVENT_KINDS:
        record["kind_unknown"] = True
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    # Append atomically-enough: one write of a single line with a trailing newline.
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        # Skip the fsync for advisory "timing" spans (~16/iteration): _resume_state reads only "run"
        # events, so their durability buys nothing; fsync every other kind for ledger/crash-safety.
        if kind not in ("timing", "activity_progress"):
            os.fsync(f.fileno())
    return record


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL session log into a list of event records. Missing file → ``[]``.

    Blank lines are skipped; a truncated final line (from a killed run) is tolerated and dropped.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a partial trailing write from an interrupted run — drop it, keep the rest
    return out


@contextmanager
def timed(path: str | Path, stage: str, *, iteration: int | None = None, **meta: Any):
    """Time a named span and append a ``"timing"`` event to the log at ``path`` on exit.

    Yields the ``meta`` dict so callers can mutate it inside the ``with`` block to attach results
    (e.g. ``n_rows``) that ride into the logged payload. ``seconds`` is a ``perf_counter`` delta
    rounded to 4 decimals; ``ok`` is ``False`` when the span exits via an exception (which is then
    re-raised). The event is always written, even on failure.
    """
    t0 = time.perf_counter()
    ok = True
    try:
        yield meta
    except BaseException:
        ok = False
        raise
    finally:
        payload = {
            "stage": stage,
            "iter": iteration,
            "seconds": round(time.perf_counter() - t0, 4),
            "ok": ok,
        }
        payload.update(meta)
        log_event(path, "timing", payload)


@contextmanager
def activity(path: str | Path, stage: str, *, q=None, job_id=None, fit_tier=None,
             total=None, progress_unit=None, **meta: Any):
    """Durable start/end pair for one operator-visible operation."""
    activity_id = uuid.uuid4().hex
    payload = {"activity_id": activity_id, "stage": stage, "q": q, "job_id": job_id,
               "fit_tier": fit_tier, "total": total, "progress_unit": progress_unit, **meta}
    payload = {key: value for key, value in payload.items() if value is not None}
    log_event(path, "activity_start", payload)
    try:
        yield activity_id
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, AnalysisLimitExceeded) else "failed"
        log_event(path, "activity_end", {**payload, "status": status,
                                          "error": f"{type(exc).__name__}: {exc}"})
        raise
    else:
        log_event(path, "activity_end", {**payload, "status": "completed"})


def log_activity_progress(path: str | Path, activity_id: str, *, completed: int,
                          total: int, progress_unit: str, **meta: Any) -> dict[str, Any]:
    return log_event(path, "activity_progress", {
        "activity_id": activity_id, "completed": int(completed), "total": int(total),
        "progress_unit": str(progress_unit), **meta})


def close_interrupted_activities(path: str | Path) -> None:
    """Close durable starts left unmatched by a killed previous invocation."""
    events = read_log(path)
    starts = {str((event.get("payload") or {}).get("activity_id")): event
              for event in events if event.get("kind") == "activity_start"
              and (event.get("payload") or {}).get("activity_id")}
    ended = {str((event.get("payload") or {}).get("activity_id"))
             for event in events if event.get("kind") == "activity_end"}
    for activity_id, event in starts.items():
        if activity_id in ended:
            continue
        original = dict(event.get("payload") or {})
        log_event(path, "activity_end", {**original, "activity_id": activity_id,
                                          "status": "interrupted"})


def analysis_clock_state(path: str | Path) -> dict[str, Any]:
    """Replay cumulative analysis time using the maximum elapsed value per stable operation ID."""
    elapsed: dict[str, float] = {}
    open_state: dict[str, bool] = {}
    for event in read_log(path):
        if event.get("kind") != "analysis_clock":
            continue
        payload = event.get("payload") or {}
        operation_id = payload.get("operation_id")
        if not operation_id:
            continue
        operation_id = str(operation_id)
        phase = payload.get("phase")
        if phase == "start":
            open_state[operation_id] = True
        if phase == "end":
            open_state[operation_id] = False
        try:
            value = max(float(payload.get("elapsed_seconds") or 0.0), 0.0)
        except (TypeError, ValueError):
            value = 0.0
        elapsed[operation_id] = max(elapsed.get(operation_id, 0.0), value)
    return {"spent_seconds": float(sum(elapsed.values())), "operations": elapsed,
            "unmatched": sorted(key for key, is_open in open_state.items() if is_open)}


def close_interrupted_analysis(path: str | Path) -> None:
    """Close clock starts left unmatched by a crash without adding another charge."""
    state = analysis_clock_state(path)
    for operation_id in state["unmatched"]:
        log_event(path, "analysis_clock", {
            "phase": "end", "operation_id": operation_id, "status": "interrupted",
            "elapsed_seconds": float(state["operations"].get(operation_id, 0.0))})


@contextmanager
def analysis_operation(path: str | Path, operation_id: str, stage: str, *,
                       limit_seconds: float | None, checkpoint_seconds: float = 10.0,
                       allow_overrun_start: bool = False, **meta: Any):
    """Durably charge one serialized top-level operation by monotonic wall time.

    The heartbeat is fsynced.  Worker concurrency is enclosed by this single span and therefore is
    never multiplied by worker count.  A running operation is allowed to finish; the ceiling gates
    only the start of a new operation.
    """
    state = analysis_clock_state(path)
    if (not allow_overrun_start and limit_seconds is not None
            and state["spent_seconds"] >= float(limit_seconds)):
        raise AnalysisLimitExceeded(
            f"analysis limit exhausted ({state['spent_seconds']:.1f}/{float(limit_seconds):.1f}s)")
    operation_id = str(operation_id)
    carried_elapsed = float(state["operations"].get(operation_id, 0.0))
    base = {"operation_id": operation_id, "stage": str(stage), **meta}
    log_event(path, "analysis_clock", {
        **base, "phase": "start", "elapsed_seconds": carried_elapsed})
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(max(float(checkpoint_seconds), 0.1)):
            log_event(path, "analysis_clock", {
                **base, "phase": "checkpoint",
                "elapsed_seconds": round(carried_elapsed + time.monotonic() - started, 4)})

    thread = threading.Thread(target=heartbeat, name=f"isar-analysis-clock-{operation_id[:12]}",
                              daemon=True)
    thread.start()
    status = "completed"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        stopped.set()
        thread.join(timeout=min(max(float(checkpoint_seconds), 0.1), 1.0))
        log_event(path, "analysis_clock", {
            **base, "phase": "end", "status": status,
            "elapsed_seconds": round(carried_elapsed + time.monotonic() - started, 4)})

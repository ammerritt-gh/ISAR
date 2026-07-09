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
- ``decide``    — the supervisor updated the ω(q) model or chose the next action.
- ``exception`` — a non-routine branch (missed window, flagged/held, budget, validation rejection).
- ``stop``      — the terminal decision: met / unmeetable, with reasons.
- ``timing``    — a profiling span: how long a named stage took (``stage``, ``iter``, ``seconds``, ``ok``).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

EVENT_KINDS = ("propose", "validate", "run", "analyze", "decide", "exception", "stop", "timing")


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
        if kind != "timing":
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

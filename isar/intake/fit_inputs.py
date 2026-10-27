"""What each fit used: the append-only logs ``report/fit_inputs.jsonl`` and ``report/fit_history.jsonl``.

``fit_inputs.jsonl`` has one writer, the run's parent process: a run start line, a ``started`` line per
analyzer and pass at the scan's start point (the inputs frozen there, C-effective ``unit_inputs``), a
``finished`` line as each completes, and an end line. ``latest_passes`` counts only passes of the run in
progress (its process alive), of a run that completed, or that the ledger holds by its stamp, so the log
agrees with the ledger: a run that died or failed (a cancelled run is killed, so writes no end line) never
saved its results.

``fit_history.jsonl`` keeps, in full, every ledger entry a BL43 re-import or refit replaces, written before
the replacing save, so a crash between the two leaves a repeat (collapsed on read), never a lost fit.

Both are appended with flush and fsync and read through ``store.read_jsonl``, which skips a partial last
line. Imports only stdlib and ``isar.schema`` at module level.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from isar.intake import store
from isar.schema import _json

log = logging.getLogger(__name__)

FIT_INPUTS = "report/fit_inputs.jsonl"
FIT_HISTORY = "report/fit_history.jsonl"
PASSES = ("fast", "deep")
OUTCOMES = ("complete", "incomplete", "failed")
_BY = ("reimport", "refit")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _append(path: Path, objs: Sequence[dict], *, allow_nan: bool = False) -> None:
    """One open, one write and one fsync for all ``objs``; an unterminated tail is ended first."""
    if not objs:
        return
    data = b"".join(json.dumps(o, sort_keys=True, ensure_ascii=False, allow_nan=allow_nan).encode("utf-8") + b"\n"
                    for o in objs)                  # serialised first: a bad value writes nothing
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                log.warning("%s: ended an unterminated last line (a torn write) with a newline", path)
                data = b"\n" + data
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _check_passes(passes) -> list[str]:
    out = list(passes)
    if not out or any(p not in PASSES for p in out):
        raise ValueError(f"passes must be a non-empty subset of {PASSES}, got {passes!r}")
    return out


def begin_run(root, *, passes: Sequence[str]) -> str:
    """Write the run start line; returns the run id (``"r"`` + 12 hex)."""
    run = "r" + uuid.uuid4().hex[:12]
    _append(Path(root) / FIT_INPUTS, [{"kind": "run", "event": "start", "run": run, "at": _now(),
                                       "passes": _check_passes(passes), "pid": os.getpid()}])
    return run


def _lines(run: str, kind: str, lines: Sequence[dict]) -> list[dict]:
    at = _now()
    out = []
    for line in lines:
        if line.get("pass") not in PASSES:
            raise ValueError(f"{kind} line for {line.get('unit')!r} names no pass of {PASSES}")
        out.append({**line, "fallbacks": list(line.get("fallbacks") or ()), "kind": kind, "run": run, "at": at})
    return out


def write_started(root, run: str, lines: Sequence[dict]) -> None:
    """Each line ``{"unit", "scan", "pass", "inputs", "hash", "fallbacks"}``."""
    _append(Path(root) / FIT_INPUTS, _lines(run, "started", lines))


def write_finished(root, run: str, lines: Sequence[dict]) -> None:
    """Each line ``{"unit", "scan", "pass", "status", "fallbacks", "result"}``."""
    _append(Path(root) / FIT_INPUTS, _lines(run, "finished", lines))


def end_run(root, run: str, outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    _append(Path(root) / FIT_INPUTS, [{"kind": "run", "event": "end", "run": run, "at": _now(), "outcome": outcome}])


@dataclass(frozen=True)
class RunState:
    run: str
    started_at: str
    passes: tuple[str, ...]
    outcome: str | None          # the end line's outcome; None when there is no end line
    running: bool                # no end line, the latest run start in the file, and its process still alive


@dataclass(frozen=True)
class PassRecord:
    unit: str                    # "spec1:42:a03"
    scan: str                    # "spec1:42"
    pass_name: str               # "fast" | "deep"
    run: str
    state: str                   # "running" | "finished"
    started_at: str
    finished_at: str | None
    inputs: dict                 # C-effective unit_inputs, as frozen at the start point
    hash: str
    fallbacks: tuple[str, ...]   # the finished line's when finished, else the started line's
    status: str | None           # ledger status of the finished unit
    result: dict | None          # fast: {verdict, disposition, omega0, omega0_err, best_label}; deep: {state, reason, best_count, p_best}


def read_log(root) -> list[dict]:
    return store.read_jsonl(Path(root) / FIT_INPUTS)


def _is(line: dict, kind: str, event: str | None = None) -> bool:
    return line.get("kind") == kind and (event is None or line.get("event") == event) \
        and isinstance(line.get("run"), str)


def _alive(pid) -> bool:
    """Whether process ``pid`` still exists; True when it cannot be told (no pid on the line, or no access).
    shortcut: a reused pid reads as alive; the ledger stamp in ``latest_passes`` still decides what counts."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":                  # os.kill(pid, 0) would terminate the process here
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return not ok or code.value == 259                                  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_states(lines: list[dict]) -> dict[str, RunState]:
    starts = [ln for ln in lines if _is(ln, "run", "start")]
    ends = {ln["run"]: ln for ln in lines if _is(ln, "run", "end")}
    latest = starts[-1]["run"] if starts else None
    out = {}
    for s in starts:
        end = ends.get(s["run"])
        out[s["run"]] = RunState(run=s["run"], started_at=s.get("at"), passes=tuple(s.get("passes") or ()),
                                 outcome=None if end is None else end.get("outcome"),
                                 running=end is None and s["run"] == latest and _alive(s.get("pid")))
    return out


def latest_passes(lines: list[dict], ledger: Mapping[str, Mapping] | None = None) -> dict[str, dict[str, PassRecord]]:
    """Per unit and pass, the last counting started line in file order (unit -> {"fast": ..., "deep": ...}).

    A pass counts when its run is running (a live process), or the unit finished and the run completed or the
    ledger holds that very fit: ``ledger`` maps a unit to its entry's ``provenance["fit_inputs"]`` stamp
    (``{"run", "hash"}``), which a killed run's checkpoint save leaves behind without an end line.
    """
    runs = run_states(lines)
    finished = {(ln["run"], ln.get("unit"), ln.get("pass")): ln for ln in lines if _is(ln, "finished")}
    out: dict[str, dict[str, PassRecord]] = {}
    for ln in lines:
        if not _is(ln, "started") or ln["run"] not in runs or not isinstance(ln.get("unit"), str):
            continue
        rs = runs[ln["run"]]
        fin = finished.get((ln["run"], ln["unit"], ln.get("pass")))
        held = (ledger or {}).get(ln["unit"]) or {}
        if not (rs.running or (fin is not None and (
                rs.outcome == "complete" or (held.get("run"), held.get("hash")) == (ln["run"], ln.get("hash"))))):
            continue                                   # a run that ended without completing saved nothing
        src = fin if fin is not None else ln
        out.setdefault(ln["unit"], {})[ln.get("pass")] = PassRecord(
            unit=ln["unit"], scan=ln.get("scan"), pass_name=ln.get("pass"), run=ln["run"],
            state="finished" if fin is not None else "running", started_at=ln.get("at"),
            finished_at=None if fin is None else fin.get("at"), inputs=ln.get("inputs") or {},
            hash=ln.get("hash"), fallbacks=tuple(src.get("fallbacks") or ()),
            status=None if fin is None else fin.get("status"), result=None if fin is None else fin.get("result"))
    return out


# --- C-fit-history ---------------------------------------------------------------------------
def superseded_line(prev, *, by: str, run: str | None) -> dict:
    """The history line of a ledger entry about to be replaced, kept in full (as the session save writes it)."""
    from isar.session.state import canonical_hash   # imported here: the read side never loads isar.session
    if by not in _BY:
        raise ValueError(f"by must be one of {_BY}, got {by!r}")
    entry = _json.to_jsonable(prev)
    return {"unit": prev.scan_id, "scan": prev.scan_id.rsplit(":", 1)[0], "by": by, "run": run,
            "fit_inputs": (prev.provenance or {}).get("fit_inputs") or None,
            "entry_hash": canonical_hash(entry), "entry": entry}


def write_superseded(root, lines: Sequence[dict]) -> None:
    """Append the lines (one open, one fsync), each with ``"kind": "superseded"`` and ``"at"``."""
    at = _now()
    # The entry is written as the session save writes it, which allows NaN; a strict writer would refuse it.
    _append(Path(root) / FIT_HISTORY, [{**ln, "kind": "superseded", "at": at} for ln in lines], allow_nan=True)


def read_history(root) -> list[dict]:
    """Every superseded entry once: a repeat of ``(unit, entry_hash)`` collapses to its first line.
    shortcut: parses the whole file (tens of MB at beamtime size); nothing reads it on a GUI refresh. Index it
    if a reader ever does."""
    seen = set()
    out = []
    for ln in store.read_jsonl(Path(root) / FIT_HISTORY):
        key = (ln.get("unit"), ln.get("entry_hash"))
        if ln.get("kind") == "superseded" and key not in seen:
            seen.add(key)
            out.append(ln)
    return out

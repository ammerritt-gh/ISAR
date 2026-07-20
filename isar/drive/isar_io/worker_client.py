"""Driver-side persistent-worker runner (mighty-starfish §A).

:class:`WorkerRunner` speaks the JSONL protocol of :mod:`isar.session.worker` over a long-lived child
process, presenting the same ``.run()/.close()`` runner seam as :class:`SubprocessRunner`. One warm
worker per campaign replaces the cold-start ``isar run`` subprocess per iteration.

**Boundary-clean.** This module lives under ``isar/drive`` and so must import stdlib only plus its
sibling :mod:`isar.drive.isar_io.runner` (``RunResult`` / ``IsarRunError``). It never imports
``isar.session`` — the worker is reached purely as ``python -m isar.session.worker`` (a subprocess
argv string), preserving the analysis boundary that ``tests/drive/test_boundary.py`` enforces.

**Failure ladder.** A transport failure (spawn error, broken pipe, per-job timeout, worker EOF, or a
malformed reply) kills the worker, falls back to a cold :class:`SubprocessRunner` for *that* job, and
respawns on the next ``run()``. ``max_respawns`` consecutive transport failures trip a permanent
fallback and fire the one-shot ``on_degraded`` callback. A clean ``ok:false`` reply (a real
``run_batch`` traceback) is NOT a transport failure — it re-raises as :class:`IsarRunError`, exactly
like today's nonzero subprocess exit, and the worker stays up. Restarting the worker mid-campaign is
correctness-free: the workspace files are the truth and the ledger re-derives freshness.
"""
from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from isar.drive.isar_io.runner import IsarRunError, RunResult

# Sentinel distinguishing a transport failure (fall back) from a real reply (dispatch it).
_TRANSPORT_FAIL = object()


class WorkerRunner:
    """A persistent :mod:`isar.session.worker` process behind the runner seam.

    Args:
        workspace: the ISAR workspace the worker analyzes (also where ``report/worker.log`` lands).
        job_timeout: per-job wall-clock ceiling (seconds) on awaiting a reply; a slower job is treated
            as a transport failure and falls back cold.
        max_respawns: consecutive transport failures tolerated before permanent fallback.
        fallback: the cold runner used per failed job and after permanent degradation (a
            :class:`SubprocessRunner`); required — a ``None`` fallback re-raises on the first failure.
        on_degraded: optional zero-arg callback fired exactly once when the runner permanently degrades
            (the loop logs a ``worker_degraded`` event). Kept as a callback so this module needs no
            ``isar.drive.session_log`` import.
    """

    def __init__(self, workspace, *, job_timeout: float = 900.0, max_respawns: int = 3,
                 fallback=None, on_degraded: Callable[[], None] | None = None) -> None:
        self._workspace = Path(workspace)
        self._job_timeout = float(job_timeout)
        self._max_respawns = int(max_respawns)
        self._fallback = fallback
        self._on_degraded = on_degraded

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue()
        self._log_fh = None
        self._next_id = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._atexit_registered = False

    @property
    def degraded(self) -> bool:
        """True once the runner has permanently fallen back to the cold runner."""
        return self._degraded

    # ----------------------------------------------------------------- runner protocol
    def run(self, workspace, data_paths: Sequence, *, force: bool = False, errors_tier: str = "full",
            render: bool = True, timeout: float | None = None,
            scan_errors_tier: str | None = None, master_errors_tier: str | None = None,
            certification_manifest: str | Path | None = None,
            certification_request: str | Path | None = None,
            analysis_allowance_seconds: float | None = None,
            finish_initial_wave: bool = False) -> RunResult:
        """Run one analysis job on the worker, falling back to the cold runner on transport failure.

        ``render`` is accepted for seam parity and ignored — the worker never renders. Returns a
        :class:`RunResult` (with the worker's inline ``timings`` on the fast path); raises
        :class:`IsarRunError` on a clean ``run_batch`` failure."""
        # The worker process is spawned bound to self._workspace; a different workspace here would
        # silently analyze the wrong one on the fast path (and the right one on fallback).
        if Path(workspace).resolve() != self._workspace.resolve():
            raise ValueError(
                f"WorkerRunner is bound to workspace {self._workspace}; got {workspace}")
        if self._degraded:
            return self._fallback_run(workspace, data_paths, force, errors_tier, render, timeout,
                                      certification_manifest, certification_request,
                                      analysis_allowance_seconds,
                                      finish_initial_wave, scan_errors_tier, master_errors_tier)

        job_timeout = self._job_timeout if timeout is None else float(timeout)
        reply = self._try_round_trip(data_paths, force, errors_tier, job_timeout,
                                     certification_manifest, certification_request,
                                     analysis_allowance_seconds,
                                     finish_initial_wave, scan_errors_tier, master_errors_tier)
        if reply is _TRANSPORT_FAIL:
            self._on_transport_failure()
            return self._fallback_run(workspace, data_paths, force, errors_tier, render, timeout,
                                      certification_manifest, certification_request,
                                      analysis_allowance_seconds,
                                      finish_initial_wave, scan_errors_tier, master_errors_tier)

        # A clean reply (ok or not) means transport is healthy → reset the failure streak.
        self._consecutive_failures = 0
        if reply.get("ok"):
            incomplete = bool((reply.get("summary") or {}).get("analysis_incomplete", False))
            return RunResult(75 if incomplete else 0, "", "", timings=reply.get("timings"),
                             completed=not incomplete)
        # A real run_batch traceback — surface it like a nonzero subprocess exit; worker stays up.
        err = reply.get("error") or {}
        tb = err.get("traceback") or f"{err.get('type', 'Error')}: {err.get('message', '')}"
        raise IsarRunError(1, "", tb, ["python", "-m", "isar.session.worker", "run"])

    def close(self) -> None:
        """Ask the worker to shut down, then ensure it is gone (terminate → kill)."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self._send({"id": None, "cmd": "shutdown"})
            except Exception:  # noqa: BLE001 — a broken pipe just means it is already dying
                pass
            try:
                proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                self._terminate(proc)
        self._cleanup_proc()
        if self._atexit_registered:
            try:
                atexit.unregister(self._atexit_close)
            except Exception:  # noqa: BLE001
                pass
            self._atexit_registered = False

    def __enter__(self) -> "WorkerRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- worker round-trip
    def _try_round_trip(self, data_paths, force, errors_tier, job_timeout,
                        certification_manifest=None, certification_request=None,
                        analysis_allowance_seconds=None,
                        finish_initial_wave=False, scan_errors_tier=None,
                        master_errors_tier=None):
        """Ensure a live worker, send one ``run`` request, await its reply.

        Returns the parsed reply dict, or :data:`_TRANSPORT_FAIL` on any spawn/pipe/timeout/EOF/parse
        failure (the caller then kills + falls back)."""
        if not self._ensure_worker():
            return _TRANSPORT_FAIL
        req_id = self._next_id
        self._next_id += 1
        req = {"id": req_id, "cmd": "run", "data_paths": [str(p) for p in data_paths],
               "force": bool(force), "errors_tier": errors_tier}
        if scan_errors_tier is not None:
            req["scan_errors_tier"] = scan_errors_tier
        if master_errors_tier is not None:
            req["master_errors_tier"] = master_errors_tier
        if certification_manifest is not None:
            req["certification_manifest"] = str(certification_manifest)
        if certification_request is not None:
            req["certification_request"] = str(certification_request)
        if analysis_allowance_seconds is not None:
            req["analysis_allowance_seconds"] = float(analysis_allowance_seconds)
        if finish_initial_wave:
            req["finish_initial_wave"] = bool(finish_initial_wave)
        try:
            self._send(req)
        except Exception:  # noqa: BLE001 — broken stdin pipe: worker died
            return _TRANSPORT_FAIL
        return self._await_reply(req_id, job_timeout)

    def _await_reply(self, req_id, job_timeout):
        deadline = time.monotonic() + job_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _TRANSPORT_FAIL
            try:
                kind, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                return _TRANSPORT_FAIL
            if kind != "line":          # "eof" (pipe closed) or "error" (reader died)
                return _TRANSPORT_FAIL
            line = payload.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001 — garbage on the protocol stream: transport is broken
                return _TRANSPORT_FAIL
            if obj.get("id") != req_id:  # stale/out-of-band line (e.g. an id:null error); skip it
                continue
            return obj

    # ----------------------------------------------------------------- process lifecycle
    def _ensure_worker(self) -> bool:
        """Return True with a live worker; spawn one if needed. False on spawn failure."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._cleanup_proc()
        try:
            self._spawn()
            return True
        except Exception:  # noqa: BLE001 — spawn failure counts as a transport failure upstream
            self._cleanup_proc()
            return False

    def _spawn(self) -> None:
        ws = str(self._workspace)
        report_dir = self._workspace / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        # stderr → a file, NEVER a PIPE: an unread stderr pipe deadlocks the worker on a large trace.
        self._log_fh = open(report_dir / "worker.log", "a", encoding="utf-8")
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "isar.session.worker", "--workspace", ws],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log_fh,
            text=True, encoding="utf-8", bufsize=1, **kwargs)
        # Fresh queue per spawn so a dead worker's trailing "eof" never leaks into the next one.
        self._queue = queue.Queue()
        self._reader = threading.Thread(
            target=_read_loop, args=(self._proc.stdout, self._queue), daemon=True)
        self._reader.start()
        if not self._atexit_registered:
            atexit.register(self._atexit_close)
            self._atexit_registered = True

    def _send(self, obj) -> None:
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _on_transport_failure(self) -> None:
        """Kill the worker, count the failure, and permanently degrade past ``max_respawns``."""
        self._terminate(self._proc)
        self._cleanup_proc()
        self._consecutive_failures += 1
        if not self._degraded and self._consecutive_failures >= self._max_respawns:
            self._degraded = True
            if self._on_degraded is not None:
                try:
                    self._on_degraded()
                except Exception:  # noqa: BLE001 — a logging hiccup never breaks the runner
                    pass

    def _fallback_run(self, workspace, data_paths, force, errors_tier, render, timeout,
                      certification_manifest=None, certification_request=None,
                      analysis_allowance_seconds=None,
                      finish_initial_wave=False, scan_errors_tier=None,
                      master_errors_tier=None) -> RunResult:
        if self._fallback is None:
            raise IsarRunError(1, "", "worker transport failed and no fallback runner is configured",
                               ["python", "-m", "isar.session.worker", "run"])
        kwargs = {"force": force, "errors_tier": errors_tier,
                  "render": render, "timeout": timeout}
        if scan_errors_tier is not None:
            kwargs["scan_errors_tier"] = scan_errors_tier
        if master_errors_tier is not None:
            kwargs["master_errors_tier"] = master_errors_tier
        # Preserve the pre-extension runner protocol for ordinary jobs and legacy adapters.
        if certification_manifest is not None:
            kwargs["certification_manifest"] = certification_manifest
        if certification_request is not None:
            kwargs["certification_request"] = certification_request
        if analysis_allowance_seconds is not None:
            kwargs["analysis_allowance_seconds"] = float(analysis_allowance_seconds)
        if finish_initial_wave:
            kwargs["finish_initial_wave"] = bool(finish_initial_wave)
        return self._fallback.run(workspace, data_paths, **kwargs)

    @staticmethod
    def _terminate(proc) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _cleanup_proc(self) -> None:
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None
        self._proc = None
        self._reader = None

    def _atexit_close(self) -> None:
        # Last-resort guard against a leaked worker if close() was never called.
        self._terminate(self._proc)
        self._cleanup_proc()


def _read_loop(stdout, q: queue.Queue) -> None:
    """Daemon reader: push each stdout line onto ``q``; a final ``eof`` marks the pipe closed.

    Runs on its own thread because Windows anonymous pipes are not selectable — the main thread waits
    on ``queue.get(timeout=...)`` for the per-job timeout instead of blocking on a raw read."""
    try:
        for line in stdout:
            q.put(("line", line))
    except Exception as e:  # noqa: BLE001 — a broken read surfaces as a transport failure
        q.put(("error", e))
    finally:
        q.put(("eof", None))

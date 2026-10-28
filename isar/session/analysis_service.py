"""The analysis service (C-service): one long-lived process per open experiment that fits queued analyzer
files concurrently, each item in its own process tree, and saves every result through one writer thread.

The GUI's queue (:mod:`isar.session.analysis_queue`) decides what starts and on how many cores; this process
only runs what it is told, relays what the fits report and saves what they produce, in arrival order.

Process::

    <env python> -m isar.session.analysis_service --workspace <root> [--log <path>]

``--log``: the service's stderr, and every item process's stdout and stderr, are appended to this file
(without it they go to the service's stderr). stdout carries the protocol and nothing else; stderr is free
text, never parsed. Exit 0 after ``bye``; exit 2 when ``<root>`` holds no ``card.policy.toml`` (one ``log``
error line, no ``ready``). Two test-only options, hidden from ``--help``: ``--fit-target`` and
``--save-target`` (``<module>:<function>``; defaults :func:`isar.session.file_fit.fit_file` and
:func:`isar.session.writer.save_results`). The fit target's module also provides ``FileItem``.

Commands (stdin, one UTF-8 JSON object per line)::

    {"op": "start", "item_id", "unit_key", "pass": "fast"|"deep", "cores": int >= 1}
    {"op": "stop", "item_id"}   {"op": "hold_writes"}   {"op": "release_writes"}
    {"op": "shutdown"}          {"op": "ping"}

Events (stdout, one JSON object per line, flushed per line)::

    {"ev": "ready", "workspace", "pid"}        {"ev": "pong"}
    {"ev": "started", "item_id", "pid"}
    {"ev": "cores", "item_id", "cores"}        deep only: the item keeps these, the rest are free now
    {"ev": "progress", "item_id", "t", **the C-progress dict}       t: seconds since "started"
    {"ev": "result", "item_id", "unit_key", "pass", "status", "reason", "saving"}
    {"ev": "saved", "item_id", "unit_key", "pass", "status", "reason"}
    {"ev": "save_failed", "item_id", "unit_key", "pass", "error"}
    {"ev": "finished", "item_id", "end": "completed"|"stopped"|"failed", "reason"}
    {"ev": "writes_held"}   {"ev": "writes_released"}
    {"ev": "log", "level", "text"}   {"ev": "error", "text"}   {"ev": "bye"}

The rules the consumer may rely on:

* ``ready`` is the first line and ``bye`` the last.
* Per item, ``started`` comes before its ``progress`` / ``cores`` / ``result`` events, and all of those
  before its ``finished``, in the order the item sent them. ``finished`` means the item's process and every
  process under it are gone and its cores are free.
* Every ``result`` with ``saving`` true gets exactly one ``saved`` or ``save_failed`` (before or after the
  item's ``finished``); saves follow arrival order across all items, and none is reported between
  ``writes_held`` and the next ``writes_released``.
* Every ``release_writes`` gets one ``writes_released``; released before its ``writes_held`` came, the
  hold is never taken.
* ``shutdown`` and stdin EOF: no new starts; every running item is drained (a result it already sent is
  saved), killed with its tree and reported ``finished stopped``; a write hold is released; every
  handed-over result is saved; then ``bye``.
* A refused ``start`` (a field missing or invalid, an item_id already used, a running item on the same
  unit_key) gets ``finished failed`` with the reason and no ``started``; a start with no usable item_id
  gets ``error``. ``stop`` on an item that is not running emits one ``log`` warning and nothing else.
* An item's ``attempt`` record goes to the writer only (a failed save of it is a ``log`` warning); its
  ``cores`` notice becomes one ``cores`` event and never reaches the writer.

Top-level imports are stdlib only: every item process re-imports this module as ``__mp_main__`` under
spawn, and analysis imports here would add to every item's start. The fitting stack loads in the item
process, through the fit target.
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import importlib
import json
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from multiprocessing import connection
from pathlib import Path

log = logging.getLogger("isar.session.analysis_service")     # not __name__: "__mp_main__" in an item

FIT_TARGET = "isar.session.file_fit:fit_file"
SAVE_TARGET = "isar.session.writer:save_results"
POLICY_NAME = "card.policy.toml"            # isar.session.workspace.POLICY_NAME, not imported (stdlib only)
PASSES = ("fast", "deep")
JOIN_S = 5.0                                # an ended or killed item's process is reaped within this
# One BLAS thread per process: driver._THREAD_ENV's names and rule, kept here because importing driver
# would load the fitting stack into every item's __mp_main__.
_THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
_LOG_FORMAT = "%(asctime)s %(process)d %(name)s %(levelname)s: %(message)s"


# --- Job Objects: one per item, so a stop kills its whole tree (win32) ---------------------------------
# The one Job Object wrapper: Qt-free and stdlib-only, so isar_gui/process.py imports this class for its runner.
if sys.platform == "win32":
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KILL_ON_JOB_CLOSE = 0x2000
    _BASIC_ACCOUNTING_INFO = 1          # JobObjectBasicAccountingInformation
    _EXTENDED_LIMIT_INFO = 9            # JobObjectExtendedLimitInformation
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    # Declared prototypes: ctypes' default 32-bit int would truncate 64-bit HANDLEs.
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _K32.SetInformationJobObject.restype = wintypes.BOOL
    _K32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _K32.QueryInformationJobObject.restype = wintypes.BOOL
    _K32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, wintypes.LPVOID)
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _K32.AssignProcessToJobObject.restype = wintypes.BOOL
    _K32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _K32.TerminateJobObject.restype = wintypes.BOOL
    _K32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _K32.CloseHandle.restype = wintypes.BOOL
    _K32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64),
                    ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64),
                    ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD),
                    ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD),
                    ("TotalTerminatedProcesses", wintypes.DWORD)]


class _WinJob:
    """One kill-on-close Windows Job Object holding one process tree (an item's here, the GUI runner's
    subprocess in isar_gui.process). Closing the handle, explicitly or because its owner died, terminates
    every process still in it."""

    def __init__(self, handle) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> _WinJob | None:
        """A job with kill-on-close set, or None (logged) off Windows or when the OS refuses."""
        if sys.platform != "win32":
            return None
        handle = _K32.CreateJobObjectW(None, None)
        if not handle:
            log.warning("job object create failed (winerror %s)", ctypes.get_last_error())
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _K32.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("job object limit set failed (winerror %s)", ctypes.get_last_error())
            _K32.CloseHandle(handle)
            return None
        return cls(handle)

    def assign(self, pid: int) -> bool:
        proc = _K32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not proc:
            log.warning("job assign: OpenProcess(%s) failed (winerror %s)", pid, ctypes.get_last_error())
            return False
        try:
            ok = bool(_K32.AssignProcessToJobObject(self._handle, proc))
            if not ok:
                log.warning("job assign: AssignProcessToJobObject(%s) failed (winerror %s)",
                            pid, ctypes.get_last_error())
            return ok
        finally:
            _K32.CloseHandle(proc)

    def terminate(self) -> bool:
        """Kill every process in the job at once (no enumeration race)."""
        ok = bool(self._handle) and bool(_K32.TerminateJobObject(self._handle, 1))
        if self._handle and not ok:
            log.warning("TerminateJobObject failed (winerror %s)", ctypes.get_last_error())
        return ok

    def wait_empty(self, timeout_s: float) -> bool:
        """Wait until no process is left in the job; False (logged) when some still are at the timeout."""
        info = _Accounting()
        deadline = time.monotonic() + timeout_s
        while True:
            if not _K32.QueryInformationJobObject(self._handle, _BASIC_ACCOUNTING_INFO, ctypes.byref(info),
                                                  ctypes.sizeof(info), None):
                log.warning("job query failed (winerror %s)", ctypes.get_last_error())
                return False
            if info.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                log.warning("%d process(es) still in the item's job after %.0f s", info.ActiveProcesses, timeout_s)
                return False
            time.sleep(0.02)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _K32.CloseHandle(handle)


# --- the item process ----------------------------------------------------------------------------------
def _target(spec: str):
    """``(module, function)`` of a ``<module>:<function>`` spec."""
    module_name, _, name = spec.partition(":")
    module = importlib.import_module(module_name)
    return module, getattr(module, name)


def _item_main(fit_spec: str, root: str, fields: tuple, conn) -> None:
    """The item process: run the fit target on one item and send what it reports down ``conn`` as
    ``("progress", dict)``, ``("result", dict)``, then ``("end",)`` or ``("error", text)``."""
    if os.name != "nt":
        os.setpgrp()                    # the stop kills the group (POSIX); Windows uses the item's job
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=_LOG_FORMAT)
    try:
        module, fit = _target(fit_spec)
        fit(Path(root), module.FileItem(*fields),
            progress=lambda ev: conn.send(("progress", dict(ev))),
            emit=lambda r: conn.send(("result", dataclasses.asdict(r))))
        conn.send(("end",))
    except Exception as exc:  # noqa: BLE001 — the item's failure is its finished reason
        log.exception("item %s (%s pass of %s) failed", *fields[:3])
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except OSError as send_exc:
            log.warning("item %s: the service is gone (%s)", fields[0], send_exc)
    finally:
        conn.close()


# --- the service ---------------------------------------------------------------------------------------
class _Out:
    """The event stream: one JSON line per event, written and flushed under a lock. A closed stream (the
    GUI is gone) is logged once; events after it are dropped, and the saves still happen."""

    def __init__(self, stream) -> None:
        self._stream, self._lock, self._dead = stream, threading.Lock(), False

    def __call__(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._lock:
            if self._dead:
                return
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self._dead = True
                log.warning("the event stream is closed (%s); further events are not sent", exc)


@dataclasses.dataclass
class _Job:
    item_id: str
    unit_key: str
    pass_kind: str
    status: str
    reason: str | None
    payload: dict


class _Writer(threading.Thread):
    """The one writer: a FIFO of results, saved in arrival order (every waiting result in one ``save_fn``
    call), and the write hold, which keeps the workspace write lock between saves for a folder copy."""

    def __init__(self, root: Path, save_fn, emit) -> None:
        super().__init__(name="analysis-writer", daemon=True)
        from isar.session import atomic, writer       # the service process only, never an item's __mp_main__
        self._lock, self._busy, self._stale = atomic.workspace_write_lock, atomic.WorkspaceBusy, writer.ProjectionStale
        self._root, self._save_fn, self._emit = root, save_fn, emit
        self._cv = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._hold = self._held = self._closing = False
        self._holding = False                       # the writer thread took up the hold: it answers the release
        self._may_report = threading.Event()        # cleared by shutdown while it reports items finished
        self._may_report.set()

    def put(self, job: _Job) -> None:
        with self._cv:
            self._jobs.append(job)
            self._cv.notify_all()

    def hold(self) -> None:
        with self._cv:
            again = self._held
            self._hold = True
            self._cv.notify_all()
        if again:
            self._emit({"ev": "writes_held"})

    def release(self) -> None:
        with self._cv:
            self._hold = False
            writer_answers = self._holding
            self._cv.notify_all()
        if not writer_answers:          # never held, or still waiting behind a save: no hold is taken now
            self._emit({"ev": "writes_released"})

    def end_hold(self) -> bool:
        """Shutdown: drop a hold; True when one was requested."""
        with self._cv:
            requested, self._hold = self._hold, False
            self._cv.notify_all()
        return requested

    def gate(self, open_: bool) -> None:
        (self._may_report.set if open_ else self._may_report.clear)()

    def close(self) -> None:
        """Save every result handed over, then end the thread."""
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        self.join()

    def run(self) -> None:
        while True:
            with self._cv:
                while not (self._jobs or self._hold or self._closing):
                    self._cv.wait()
                if self._hold:
                    batch = None
                    self._holding = True
                elif self._jobs:
                    batch = list(self._jobs)
                    self._jobs.clear()
                else:
                    return
            if batch is None:
                self._hold_lock()
            else:
                self._save(batch)

    def _hold_lock(self) -> None:
        while True:
            with self._cv:
                if not self._hold:              # released before the lock was ever taken
                    self._holding = False
                    self._emit({"ev": "writes_released"})
                    return
            try:
                with self._lock(self._root):
                    with self._cv:
                        self._held = True
                    self._emit({"ev": "writes_held"})
                    with self._cv:
                        while self._hold:
                            self._cv.wait()
                        self._held = self._holding = False
                self._emit({"ev": "writes_released"})
                return
            except self._busy as exc:           # another writer holds it longer than its timeout: keep trying
                log.warning("write hold: %s", exc)
                self._emit({"ev": "log", "level": "warning", "text": f"still waiting to hold writes: {exc}"})

    def _save(self, batch: list[_Job]) -> None:
        try:
            verdicts = self._save_fn(self._root, [j.payload for j in batch])
        except self._stale as exc:          # saved; the projections wait for the next save
            verdicts = exc.verdicts
            self._emit({"ev": "log", "level": "warning",
                        "text": f"saved; results.csv could not be rebuilt yet: {exc.cause}"})
        except Exception as exc:  # noqa: BLE001 — a failed save is reported per result; the writer goes on
            log.exception("saving %d result(s) failed", len(batch))
            verdicts = [str(exc) or type(exc).__name__] * len(batch)
        self._may_report.wait()
        for job, why in zip(batch, verdicts):
            if job.pass_kind == "attempt":
                if why is not None:
                    self._emit({"ev": "log", "level": "warning",
                                "text": f"{job.unit_key}: the attempt record was not saved: {why}"})
            elif why is None:
                self._emit({"ev": "saved", "item_id": job.item_id, "unit_key": job.unit_key,
                            "pass": job.pass_kind, "status": job.status, "reason": job.reason})
            else:
                self._emit({"ev": "save_failed", "item_id": job.item_id, "unit_key": job.unit_key,
                            "pass": job.pass_kind, "error": why})


class _Item:
    """One running item: its process, the read end of its pipe and its job."""

    def __init__(self, item_id: str, unit_key: str, pass_kind: str, proc, conn, job: _WinJob | None) -> None:
        self.item_id, self.unit_key, self.pass_kind = item_id, unit_key, pass_kind
        self.proc, self.conn, self.job = proc, conn, job
        self.t0 = time.monotonic()
        self.stopped = self.completed = self.cores_sent = False
        self.error: str | None = None


class _Service:
    # shortcut: one connection.wait over every running item's pipe, at most 62 running items (Windows' wait
    # limit is 63 handles); wait in batches if ISAR ever runs on a machine with more than 62 cores.
    # shortcut: one interpreter per item, spawn cost per item; upgrade to warm fast workers if spawn exceeds
    # ~20% of a typical fast item.

    def __init__(self, root: Path, fit_spec: str, save_fn, emit) -> None:
        self.root, self.fit_spec, self.emit = root, fit_spec, emit
        self.ctx = multiprocessing.get_context("spawn")
        self.running: dict[str, _Item] = {}
        self.used: set[str] = set()
        self.writer = _Writer(root, save_fn, emit)

    def serve(self, commands) -> int:
        self.writer.start()
        self.emit({"ev": "ready", "workspace": str(self.root), "pid": os.getpid()})
        while True:
            by_conn = {it.conn: it for it in self.running.values()}
            for ready in connection.wait([commands, *by_conn]):
                if ready is commands:
                    try:
                        line = commands.recv()
                    except EOFError:
                        line = None
                    if line is None or self._command(line) == "shutdown":
                        return self._shutdown()
                elif by_conn[ready].item_id in self.running:     # not stopped earlier in this round
                    item = by_conn[ready]
                    if self._drain(item):
                        self._finish(item)

    # -- commands
    def _command(self, line: str) -> str | None:
        line = line.strip().lstrip("﻿")
        if not line:
            return None
        try:
            cmd = json.loads(line)
        except ValueError as exc:
            self.emit({"ev": "error", "text": f"not a JSON line ({exc}): {line[:200]}"})
            return None
        if not isinstance(cmd, dict):
            self.emit({"ev": "error", "text": f"a command is a JSON object, not {type(cmd).__name__}"})
            return None
        op = cmd.get("op")
        if op == "start":
            self._start(cmd)
        elif op == "stop":
            self._stop(cmd.get("item_id"))
        elif op == "hold_writes":
            self.writer.hold()
        elif op == "release_writes":
            self.writer.release()
        elif op == "ping":
            self.emit({"ev": "pong"})
        elif op == "shutdown":
            return "shutdown"
        else:
            self.emit({"ev": "error", "text": f"unknown op {op!r}"})
        return None

    def _start(self, cmd: dict) -> None:
        iid = cmd.get("item_id")
        if not isinstance(iid, str) or not iid:
            self.emit({"ev": "error", "text": f"a start needs an item_id: {json.dumps(cmd)[:200]}"})
            return
        unit, kind, cores = cmd.get("unit_key"), cmd.get("pass"), cmd.get("cores")
        other = next((it.item_id for it in self.running.values() if it.unit_key == unit), None)
        why = (f"item id {iid} is already used in this service" if iid in self.used else
               f"invalid start: unit_key {unit!r} is not 'experiment:scan:analyzer'"
               if not isinstance(unit, str) or unit.count(":") < 2 else
               f"invalid start: pass {kind!r} is not one of {PASSES}" if kind not in PASSES else
               f"invalid start: cores {cores!r} is not a whole number of at least 1"
               if type(cores) is not int or cores < 1 else
               f"{unit} already has a running item ({other})" if other is not None else None)
        self.used.add(iid)
        if why is None:
            why = self._launch(iid, unit, kind, cores)
        if why is not None:
            self.emit({"ev": "finished", "item_id": iid, "end": "failed", "reason": why})

    def _launch(self, iid: str, unit: str, kind: str, cores: int) -> str | None:
        """Start the item's process in its own job; the refusal reason when it could not start."""
        read, write = self.ctx.Pipe(duplex=False)
        proc = self.ctx.Process(target=_item_main, args=(self.fit_spec, str(self.root), (iid, unit, kind, cores),
                                                         write), name=f"isar-item-{iid}", daemon=False)
        try:
            proc.start()
        except Exception as exc:  # noqa: BLE001 — a process that could not start is that item's failure
            log.exception("item %s: the fitting process did not start", iid)
            read.close()
            write.close()
            return f"the fitting process could not start ({type(exc).__name__}: {exc})"
        write.close()           # the child holds the only write end: its death reads as EOF here
        job = _WinJob.create()
        if job is not None and not job.assign(proc.pid):
            job.close()
            job = None
        self.running[iid] = _Item(iid, unit, kind, proc, read, job)
        self.emit({"ev": "started", "item_id": iid, "pid": proc.pid})
        return None

    def _stop(self, iid) -> None:
        item = self.running.get(iid) if isinstance(iid, str) else None
        if item is None:
            self.emit({"ev": "log", "level": "warning", "text": f"stop: no running item {iid!r}"})
            return
        item.stopped = True
        self._drain(item)               # a result already sent still reaches the writer
        self._kill(item)
        item.proc.join(JOIN_S)
        self._drain(item)               # what arrived before the kill; the pipe ends here
        self._finish(item)

    # -- items
    def _drain(self, item: _Item) -> bool:
        """Relay every message waiting on the item's pipe; True when the pipe has ended."""
        while True:
            try:
                if not item.conn.poll():
                    return False
                msg = item.conn.recv()
            except (EOFError, OSError):
                return True
            except Exception:  # noqa: BLE001 — a message cut by a kill: the channel ends here
                log.exception("item %s: unreadable message", item.item_id)
                return True
            self._message(item, msg)

    def _message(self, item: _Item, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            self.emit({"ev": "progress", "item_id": item.item_id, "t": time.monotonic() - item.t0, **msg[1]})
        elif kind == "result":
            self._result(item, msg[1])
        elif kind == "error":
            item.error = msg[1]
        elif kind == "end":
            item.completed = True
        else:
            log.warning("item %s: unknown message %r", item.item_id, kind)

    def _result(self, item: _Item, r: dict) -> None:
        status, payload = r.get("status"), r.get("payload")
        if status == "cores":
            if item.cores_sent:
                log.warning("item %s: a second cores notice (%s) ignored", item.item_id, r.get("cores"))
            else:
                item.cores_sent = True
                self.emit({"ev": "cores", "item_id": item.item_id, "cores": r.get("cores")})
            return
        if status == "attempt":
            if payload is not None:
                self.writer.put(_Job(item.item_id, item.unit_key, "attempt", status, None, payload))
            return
        self.emit({"ev": "result", "item_id": item.item_id, "unit_key": item.unit_key, "pass": r.get("pass_kind"),
                   "status": status, "reason": r.get("reason"), "saving": payload is not None})
        if payload is not None:         # after the result event, so its "saved" can only follow it
            self.writer.put(_Job(item.item_id, item.unit_key, r.get("pass_kind"), status, r.get("reason"), payload))

    def _kill(self, item: _Item) -> None:
        """Kill the item's process and every process under it."""
        if item.job is not None and item.job.terminate():
            return
        if sys.platform == "win32":
            # /T enumerates from the live root, so it runs before the root is killed.
            subprocess.run(["taskkill", "/PID", str(item.proc.pid), "/T", "/F"], capture_output=True,
                           check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(item.proc.pid, signal.SIGKILL)
            except OSError as exc:
                log.warning("item %s: killpg failed (%s)", item.item_id, exc)
        if item.proc.is_alive():
            item.proc.kill()

    def _finish(self, item: _Item) -> None:
        """The item's pipe has ended: reap its tree, free it and report ``finished``."""
        item.proc.join(JOIN_S)
        if item.proc.is_alive():
            log.warning("item %s: still alive %.0f s after its pipe closed; killing it", item.item_id, JOIN_S)
            self._kill(item)
            item.proc.join(JOIN_S)
        if item.job is not None:        # also takes any process the item left behind
            item.job.terminate()
            item.job.wait_empty(JOIN_S)
            item.job.close()
        item.conn.close()
        del self.running[item.item_id]
        end, reason = (("stopped", None) if item.stopped else ("completed", None) if item.completed else
                       ("failed", item.error) if item.error is not None else
                       ("failed", f"the fitting process ended unexpectedly (exit code {item.proc.exitcode})"))
        self.emit({"ev": "finished", "item_id": item.item_id, "end": end, "reason": reason})

    def _shutdown(self) -> int:
        self.writer.gate(False)         # every item's finished before any further saved
        for iid in list(self.running):
            self._stop(iid)
        if self.writer.end_hold():
            self.emit({"ev": "log", "level": "warning", "text": "write hold released by shutdown"})
        self.writer.gate(True)
        self.writer.close()
        self.emit({"ev": "bye"})
        return 0


# --- the process ---------------------------------------------------------------------------------------
def _redirect_stderr(path: str) -> None:
    """Append fd 2 (and so the stderr every item process inherits) to ``path``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stderr = open(2, "w", encoding="utf-8", errors="backslashreplace", buffering=1, closefd=False)


def _protocol_stream():
    """Set the real stdout aside for the protocol and point fd 1 at stderr, so a stray write of this
    process, an item process or a pool worker (which inherit fd 1) never reaches the protocol."""
    sys.stdout.flush()
    stream = open(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return stream


def _read_commands():
    """A connection that yields each stdin line (a str), then EOF; read on a thread so commands wake the
    same wait as the items' pipes.

    The command pipe is moved off fd 0, and fd 0 (the stdin every item process inherits) is pointed at the
    null device: on Windows a child starting up blocks on an inherited pipe handle while this thread waits
    in a read on it. Raw ``os.read``, not ``sys.stdin``: a daemon thread parked inside the buffered reader's
    lock aborts interpreter shutdown."""
    fd = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    read, write = multiprocessing.Pipe(duplex=False)

    def pump() -> None:
        pending = b""
        try:
            while chunk := os.read(fd, 65536):
                *lines, pending = (pending + chunk).split(b"\n")
                for raw in lines:
                    write.send(raw.decode("utf-8", errors="replace"))
            if pending:
                write.send(pending.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — a broken stdin is EOF, after the log line
            log.exception("reading commands failed")
        finally:
            write.close()

    threading.Thread(target=pump, name="analysis-commands", daemon=True).start()
    return read


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m isar.session.analysis_service",
                                     description="ISAR analysis service: fits queued analyzer files (JSONL on stdin/stdout)")
    parser.add_argument("--workspace", required=True, help="the experiment folder this service is bound to")
    parser.add_argument("--log", help="append the service's and every fit's text output to this file")
    parser.add_argument("--fit-target", default=FIT_TARGET, help=argparse.SUPPRESS)
    parser.add_argument("--save-target", default=SAVE_TARGET, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.log:
        _redirect_stderr(args.log)
    emit = _Out(_protocol_stream())
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=_LOG_FORMAT)
    root = Path(args.workspace).resolve()
    if not (root / POLICY_NAME).is_file():
        emit({"ev": "log", "level": "error", "text": f"no ISAR experiment at {root} (no {POLICY_NAME})"})
        return 2
    for name in _THREAD_ENV:
        os.environ.setdefault(name, "1")
    _module, save_fn = _target(args.save_target)
    return _Service(root, args.fit_target, save_fn, emit).serve(_read_commands())


if __name__ == "__main__":
    raise SystemExit(main())

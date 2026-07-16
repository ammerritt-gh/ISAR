"""ProcessRunner — a thin ``QProcess`` wrapper for the on-ramp's Run action.

Runs one engine subprocess (``isar init`` / ``isar run``, always assembled by
:mod:`isar_gui.commands`), streams its output to the log dock **line by line as it arrives**, writes
a retained copy to a log file, and reports the exit code so the window can show a visible
success/failure state (Operator Contract: failure is never a vanishing window). stdout and stderr
are merged so the operator sees one honest interleaved stream.

Qt is imported at module load here — this is Qt land (``isar_gui``), same as the docks. Nothing here
touches a workspace; it only runs a command and relays bytes.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

log = logging.getLogger(__name__)

if sys.platform == "win32":
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_INFO = 9            # JobObjectExtendedLimitInformation
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    # Declare prototypes: ctypes defaults every restype/argument to a 32-bit c_int, which would
    # silently truncate 64-bit HANDLEs on return AND on the way back in. Handles happen to be
    # small today; do not rely on it.
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _K32.SetInformationJobObject.restype = wintypes.BOOL
    _K32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                             wintypes.LPVOID, wintypes.DWORD)
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


class _WinJob:
    """One Windows Job Object holding one subprocess tree (best-effort — see ``kill_tree``).

    ``KILL_ON_JOB_CLOSE`` means closing the handle — explicitly, or implicitly when the GUI
    process dies and the OS reclaims its handles — terminates every process still in the job.
    GUI death therefore takes a running campaign's tree with it by design; campaigns are
    crash-resumable, so nothing is lost beyond the in-flight iteration.
    """

    def __init__(self) -> None:
        self._handle = None

    @classmethod
    def create(cls) -> _WinJob | None:
        """A job with kill-on-close set, or ``None`` (logged) when the OS refuses."""
        if sys.platform != "win32":
            return None
        handle = _K32.CreateJobObjectW(None, None)
        if not handle:
            log.warning("job object create failed (winerror %s)", ctypes.get_last_error())
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _K32.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFO,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("job object limit set failed (winerror %s)", ctypes.get_last_error())
            _K32.CloseHandle(handle)
            return None
        job = cls()
        job._handle = handle
        return job

    def assign(self, pid: int) -> bool:
        if self._handle is None:
            return False
        proc = _K32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not proc:
            log.warning("job assign: OpenProcess(%s) failed (winerror %s)",
                        pid, ctypes.get_last_error())
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
        """Kill every process in the job atomically (no enumeration race)."""
        if self._handle is None:
            return False
        ok = bool(_K32.TerminateJobObject(self._handle, 1))
        if not ok:
            log.warning("TerminateJobObject failed (winerror %s)", ctypes.get_last_error())
        return ok

    def close(self) -> None:
        """Close the handle (idempotent) — kill-on-close reaps any process still inside."""
        handle, self._handle = self._handle, None
        if handle:
            _K32.CloseHandle(handle)


class ProcessRunner(QObject):
    """Run a subprocess and relay its output.

    Signals:
      * ``started()`` — the process launched.
      * ``line(str)`` — one line of merged stdout/stderr (no trailing newline).
      * ``finished(int)`` — the process ended; the arg is the exit code (a synthetic non-zero code
        is emitted when the process failed to start or crashed, so a consumer only has to watch this
        one signal to know "done, ok?").
    """

    started = Signal()
    line = Signal(str)
    finished = Signal(int)

    _FAILED_TO_START = -1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf = ""              # partial trailing line not yet terminated by a newline
        self._log = None            # open text file handle for the retained log, or None
        self._emitted_finish = False
        self._job: _WinJob | None = None   # kill-on-close job holding the subprocess tree (win32)
        self._pid: int | None = None       # pid captured at launch, for the taskkill fallback

    # ---- lifecycle --------------------------------------------------------
    def start(self, argv: list[str], *, cwd: str | None = None, log_path: str | None = None,
              env: dict | None = None) -> None:
        """Launch ``argv``. Optionally set the working dir, a retained-log path, and env overrides.

        ``env`` overrides are merged onto the inherited system environment (used to pass the LLM
        profile/endpoint/key to ``isar run``). A previous still-running process is killed first (the
        Run button is single-flight per runner).
        """
        if self.is_running():
            self.stop()
        self._buf = ""
        self._emitted_finish = False
        self._release_job()            # a fresh run gets a fresh job; closing reaps any straggler
        self._pid = None
        self._open_log(log_path, argv)

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if cwd:
            proc.setWorkingDirectory(cwd)
        if env:
            pe = QProcessEnvironment.systemEnvironment()
            for k, v in env.items():
                pe.insert(str(k), str(v))
            proc.setProcessEnvironment(pe)
        proc.readyReadStandardOutput.connect(self._on_ready_read)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        proc.started.connect(self._on_proc_started)
        self._proc = proc
        # argv[0] is the program; the rest are arguments (QProcess wants them split, never a shell).
        proc.start(argv[0], list(argv[1:]))
        self.started.emit()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning

    def stop(self) -> None:
        """Kill the running process (if any). ``finished`` still fires with its (non-zero) code."""
        self.kill()
        if self._proc is not None:
            self._proc.waitForFinished(2000)

    def terminate(self) -> None:
        """Request graceful process termination without blocking the GUI event loop."""
        if self._proc is not None and self.is_running():
            self._proc.terminate()

    def kill(self) -> None:
        """Force process termination without blocking; ``finished`` remains the terminal signal."""
        if self._proc is not None and self.is_running():
            self._proc.kill()

    def kill_tree(self) -> None:
        """Take down the process AND every descendant (``isar drive`` spawns per-iteration
        ``isar run`` children / a warm worker that a plain :meth:`kill` would orphan).

        Best-effort ladder: TerminateJobObject (atomic — no parent-death enumeration race) →
        ``taskkill /T /F`` on the launch-captured pid (works while the root is alive) → plain
        :meth:`kill`. ``finished`` remains the terminal signal on every path.
        """
        if self._job is not None and self._job.terminate():
            self.kill()   # the root is already dying; this just makes QProcess notice promptly
            return
        if sys.platform == "win32" and self._pid is not None:
            self.line.emit(f"(stopping process tree {self._pid} via taskkill)")
            # Synchronous on purpose: /T resolves children FROM the live root, so the root must
            # still be alive when taskkill enumerates — killing it first (or racing a detached
            # taskkill against kill()) orphans the children. taskkill returns in milliseconds.
            QProcess.execute("taskkill", ["/PID", str(self._pid), "/T", "/F"])
            self.kill()   # belt: reap the root ourselves if taskkill missed it
            return
        self.kill()

    # ---- job-object tree lifetime (win32, best-effort) ---------------------
    def _on_proc_started(self) -> None:
        """Capture the pid and put the new process in a kill-on-close job.

        ``QProcess.start`` is asynchronous, so a child our subprocess spawned BEFORE this handler
        ran would escape the job — best-effort by design: the child is a Python interpreter that
        takes seconds to boot before it can spawn anything, and ``kill_tree``'s taskkill fallback
        covers the gap. Assignment failures are logged and survivable.
        """
        if self._proc is None:
            return
        self._pid = int(self._proc.processId())
        job = _WinJob.create()
        if job is None:
            return
        if job.assign(self._pid):
            self._job = job
            # A destroyed runner must still reap the tree. The lambda holds the _WinJob (a plain
            # Python object), so the close survives the QObject's C++ half being deleted; close()
            # is idempotent, so the normal-finish release below double-closing is harmless.
            self.destroyed.connect(lambda *_args, _job=job: _job.close())
            log.debug("process %s assigned to a kill-on-close job", self._pid)
        else:
            job.close()

    def _release_job(self) -> None:
        """Close the current job (idempotent). With kill-on-close set, this reaps any process
        still inside — e.g. a child whose root exited without cleaning it up."""
        job, self._job = self._job, None
        if job is not None:
            job.close()

    # ---- retained log -----------------------------------------------------
    def _open_log(self, log_path: str | None, argv: list[str]) -> None:
        self._close_log()
        if not log_path:
            self._log = None
            return
        try:
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._log = p.open("w", encoding="utf-8")
            self._log.write(f"# {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            self._log.write("# " + " ".join(argv) + "\n\n")
            self._log.flush()
        except OSError as exc:  # a log we can't open must not stop the run — degrade to no log
            self._log = None
            self.line.emit(f"(could not open log file {log_path}: {exc})")

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _write_log(self, text: str) -> None:
        if self._log is not None:
            try:
                self._log.write(text + "\n")
                self._log.flush()
            except OSError:
                pass  # a mid-run log write failure is non-fatal; the stream still reaches the dock

    # ---- output relay -----------------------------------------------------
    def _on_ready_read(self) -> None:
        if self._proc is None:
            return
        chunk = bytes(self._proc.readAllStandardOutput().data()).decode("utf-8", errors="replace")
        self._buf += chunk
        *complete, self._buf = self._buf.split("\n")
        for ln in complete:
            ln = ln.rstrip("\r")
            self.line.emit(ln)
            self._write_log(ln)

    def _flush_buffer(self) -> None:
        if self._buf:
            ln = self._buf.rstrip("\r")
            self._buf = ""
            self.line.emit(ln)
            self._write_log(ln)

    # ---- terminal states --------------------------------------------------
    def _on_error(self, error) -> None:
        # FailedToStart fires without a finished() — synthesize a terminal state so consumers that
        # only watch finished() are not left hanging.
        if error == QProcess.ProcessError.FailedToStart:
            self.line.emit("error: could not start the process (is the environment set up?)")
            self._emit_finished(self._FAILED_TO_START)

    def _on_finished(self, code: int, status) -> None:
        self._flush_buffer()
        if status == QProcess.ExitStatus.CrashExit and code == 0:
            code = self._FAILED_TO_START  # a crash with a 0 code still reads as failure
        self._emit_finished(code)

    def _emit_finished(self, code: int) -> None:
        if self._emitted_finish:
            return
        self._emitted_finish = True
        self._close_log()
        self._release_job()   # root is done — kill-on-close reaps any straggler child
        self.finished.emit(int(code))

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

import logging
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

# The one Job Object wrapper (kill-on-close, one subprocess tree); the engine's analysis service uses the same
# class. Stdlib-only at import, and the only name the GUI takes from isar.session.analysis_service.
from isar.session.analysis_service import _WinJob

log = logging.getLogger(__name__)


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

    def write_line(self, text: str) -> bool:
        """Send ``text`` plus a newline (UTF-8) to the process's stdin; False when it is not running or the
        write was refused. The write is buffered and flushed by the event loop."""
        if not self.is_running():
            return False
        return self._proc.write(text.encode("utf-8") + b"\n") >= 0

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

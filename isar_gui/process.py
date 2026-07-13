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

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


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
        self.finished.emit(int(code))

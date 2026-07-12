"""IngestController — the single serialization point for engine ingest subprocesses (Phase D).

Every path into the ledger — a drop onto a ready workspace, a drop that must first scaffold a
temporary workspace, a watched-folder burst — funnels through :meth:`request_ingest`. The controller
guarantees **exactly one** ``isar`` subprocess runs at a time and FIFO-queues the rest, because two
``isar ingest`` processes writing ``session.json`` concurrently would race the machine-owned ledger
(Hard Rule 5's trust contract lives in that file). A watched folder dumping ten numors in one poll
must never fan out into ten racing processes.

Ownership split with :class:`~isar_gui.workspace_manager.WorkspaceManager` (Phase D decision):
  * the **manager** owns pure lifecycle — reserve (``new_temp``), ``open``, ``save_as``,
    ``discard_temp``, ``adopt`` — and the none/pending/ready state machine;
  * the **controller** owns the *bootstrap sequence* that was Phase C's ``bootstrap_from_data``:
    ``isar init`` (scaffold the card) → ``isar ingest`` (fill the ledger) → ``manager.open(root)``.
    It consults the manager's state to decide what a request needs and calls back into the manager's
    lifecycle methods; the manager no longer drives subprocesses at all.

Per request, by manager state at dequeue time:
  * ``"none"``    → ``manager.new_temp()`` (reserve + enter pending), then the pending path;
  * ``"pending"`` → ``init`` first path, ``ingest`` all paths, then ``manager.open(root)``;
  * ``"ready"``   → ``ingest`` all paths straight into the open workspace.

Facade refresh is NOT done here: an ingest that writes ``session.json`` ticks the existing
``ws.watch`` "snapshot" name, and :class:`~isar_gui.watch_bridge.WatchBridge` refreshes the facade +
fans out to the viewmodels. The controller only emits its own report/failure signals.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from isar_gui.commands import (
    build_ingest_command,
    build_init_command,
    ingest_failure_cause,
    init_failure_cause,
    parse_ingest_json,
)
from isar_gui.process import ProcessRunner

log = logging.getLogger(__name__)


class IngestController(QObject):
    """Serializes ``isar init``/``isar ingest`` subprocesses behind a FIFO queue.

    Signals:
      * ``ingest_started(list)`` — a request's batch of paths began processing.
      * ``ingest_finished(object)`` — a :class:`~isar_gui.commands.GuiIngestReport` for a completed
        ingest (the caller reads ``deduped`` for its informational status/log entry).
      * ``ingest_failed(int, str)`` — a step failed; args are the exit code and a human cause (the
        JSON ``error.message`` when the engine emitted one, else the last ``error:`` line).
    """

    ingest_started = Signal(list)
    ingest_finished = Signal(object)
    ingest_failed = Signal(int, str)

    def __init__(self, python_exe, workspace_manager, log_sink, parent=None):
        super().__init__(parent)
        self._python = python_exe
        self._wsm = workspace_manager
        self._log = log_sink or (lambda _msg: None)
        self._runner: ProcessRunner | None = None
        self._queue: list[list[str]] = []        # FIFO of pending path batches
        # Per-job scratch (valid only while a job is in flight):
        self._current: list[str] | None = None   # the batch being processed
        self._steps: list[tuple[str, list[str]]] = []  # [(kind, argv), ...] for this job
        self._step_i = 0
        self._open_after = False                  # bootstrap job: open the root after the last step
        self._report = None                       # GuiIngestReport from the ingest step, if any
        self._lines: list[str] = []               # buffered output of the current step

    # ---- public API ------------------------------------------------------
    def request_ingest(self, paths) -> None:
        """Enqueue a batch of data paths for ingest; start immediately if idle, else FIFO-queue.

        A batch dropped/gathered while another is running is never refused — it waits its turn, so a
        watched-folder burst is naturally serialized instead of racing on ``session.json``.
        """
        batch = [str(p) for p in paths if str(p)]
        if not batch:
            return
        self._queue.append(batch)
        if self._runner is None:
            self._start_next()

    def is_busy(self) -> bool:
        """True while a job is in flight or queued (used by teardown; drops never consult it)."""
        return self._runner is not None or bool(self._queue) or self._current is not None

    # ---- job scheduling --------------------------------------------------
    def _start_next(self) -> None:
        if self._runner is not None or not self._queue:
            return
        paths = self._queue.pop(0)
        self._current = paths
        self._report = None
        self._step_i = 0

        state = self._wsm.state()
        if state == "none":
            # Reserve a throwaway workspace and drop into the pending bootstrap path.
            self._wsm.new_temp()
            state = self._wsm.state()

        root = self._wsm.root()
        if root is None:
            # Should not happen (new_temp reserves a root); refuse rather than build a bad command.
            self._log("ingest aborted — no workspace root to ingest into")
            self._finish_job()
            return
        root = str(root)

        if state == "pending":
            self._open_after = True
            self._steps = [
                ("init", build_init_command(self._python, paths[0], root, json=True)),
                ("ingest", build_ingest_command(self._python, paths, root, json=True)),
            ]
        else:  # ready
            self._open_after = False
            self._steps = [("ingest", build_ingest_command(self._python, paths, root, json=True))]

        self.ingest_started.emit(list(paths))
        self._run_step()

    def _run_step(self) -> None:
        kind, argv = self._steps[self._step_i]
        self._lines = []
        self._log("$ " + " ".join(argv))
        runner = ProcessRunner(self)

        def _on_line(ln: str) -> None:
            self._lines.append(ln)
            self._log(ln)

        runner.line.connect(_on_line)
        runner.finished.connect(self._on_step_finished)
        self._runner = runner
        runner.start(argv, log_path=self._step_log(kind))

    def _on_step_finished(self, code: int) -> None:
        lines = self._lines
        kind, _argv = self._steps[self._step_i]
        self._dispose_runner()

        if kind == "init" and code != 0:
            # Bootstrap init failed: nothing scaffolded, leave the pending state for a retry.
            cause = init_failure_cause(lines)
            log.warning("ingest bootstrap init failed (exit %s): %s", code, cause)
            self._log(f"init failed (exit {code}): {cause}")
            self.ingest_failed.emit(int(code), cause)
            self._finish_job()
            return

        if kind == "ingest":
            if code != 0:
                cause = ingest_failure_cause(lines)
                log.warning("ingest failed (exit %s): %s", code, cause)
                self._log(f"ingest failed (exit {code}): {cause}")
                # A bootstrap whose ingest hiccuped still gets opened — init already wrote the card,
                # so the scaffolded workspace must not be stranded unopened.
                if self._open_after:
                    self._open_root()
                self.ingest_failed.emit(int(code), cause)
                self._finish_job()
                return
            self._report = parse_ingest_json(lines)

        # Step succeeded — advance to the next step, or finish the job.
        self._step_i += 1
        if self._step_i < len(self._steps):
            self._run_step()
            return

        if self._open_after:
            self._open_root()
        if self._report is not None:
            self.ingest_finished.emit(self._report)
        self._finish_job()

    def _open_root(self) -> None:
        root = self._wsm.root()
        if root is not None and not self._wsm.open(str(root)):
            self._log(f"scaffolded {root} but could not open it as a workspace")

    def _finish_job(self) -> None:
        self._current = None
        self._steps = []
        self._step_i = 0
        self._open_after = False
        self._report = None
        self._lines = []
        # Drain the next queued batch (re-reads manager state, so a workspace that became ready mid-
        # queue routes the follow-on batch straight to ingest).
        self._start_next()

    def _dispose_runner(self) -> None:
        if self._runner is None:
            return
        try:
            self._runner.line.disconnect()
            self._runner.finished.disconnect()
        except (RuntimeError, TypeError):
            pass  # already detached / C++ side gone
        self._runner.deleteLater()
        self._runner = None

    def _step_log(self, kind: str) -> str | None:
        """A retained per-step log next to the workspace (best-effort; None if no root)."""
        root = self._wsm.root()
        if root is None:
            return None
        return str(Path(root) / "report" / "gui-runs"
                   / f"ingest-{kind}-{datetime.now():%Y%m%d-%H%M%S}.log")

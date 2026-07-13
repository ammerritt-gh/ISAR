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
    # Terminal notification for the coordinator.  Unlike the UI-facing report/failure signals this
    # is emitted even when a stale root guard intentionally suppresses those signals.
    job_finished = Signal()
    # Emitted only for the controller's own none->pending and pending->ready transitions.  The
    # workspace-wide coordinator uses it to adopt the resulting root/generation without treating
    # that expected bootstrap bind as an operator-initiated workspace switch.
    bootstrap_bound = Signal(str, int)

    def __init__(self, python_exe, workspace_manager, log_sink, parent=None):
        super().__init__(parent)
        self._python = python_exe
        self._wsm = workspace_manager
        self._log = log_sink or (lambda _msg: None)
        self._runner: ProcessRunner | None = None
        # FIFO of (path batch, pinned root). The root is stamped at ENQUEUE time (see request_ingest)
        # so a batch queued against workspace A is never ingested into workspace B if the operator
        # switches before it dequeues. ``None`` = queued in the none-state (no workspace yet), which
        # bootstraps a fresh temp at dequeue and so carries no pin to check.
        self._queue: list[tuple[list[str], str | None]] = []
        # Per-job scratch (valid only while a job is in flight):
        self._current: list[str] | None = None   # the batch being processed
        self._steps: list[tuple[str, list[str]]] = []  # [(kind, argv), ...] for this job
        self._step_i = 0
        self._open_after = False                  # bootstrap job: open the root after the last step
        self._report = None                       # GuiIngestReport from the ingest step, if any
        self._lines: list[str] = []               # buffered output of the current step
        self._current_root: str | None = None     # workspace root this job's argv is pinned to
        self._draining = False                    # re-entrancy guard for _start_next's drain loop

    # ---- public API ------------------------------------------------------
    def request_ingest(self, paths) -> None:
        """Enqueue a batch of data paths for ingest; start immediately if idle, else FIFO-queue.

        A batch dropped/gathered while another is running is never refused — it waits its turn, so a
        watched-folder burst is naturally serialized instead of racing on ``session.json``.

        The batch is stamped with the workspace root current *now* (at enqueue). If the operator
        switches to a different workspace before this batch dequeues, :meth:`_start_next` drops it
        rather than ingesting workspace A's files into workspace B's ledger. A batch enqueued in the
        none-state (no root yet) carries no pin and bootstraps a fresh temp when it dequeues.
        """
        batch = [str(p) for p in paths if str(p)]
        if not batch:
            return
        root = self._wsm.root()
        pinned = str(root) if root is not None else None
        self._queue.append((batch, pinned))
        if self._runner is None:
            self._start_next()

    def start_ingest(self, paths) -> None:
        """Start exactly one batch supplied by ``WorkspaceOperationCoordinator``.

        New GUI code uses this single-job entrypoint: the cross-operation FIFO lives in the
        coordinator so ingest and analysis share one writer gate.  ``request_ingest`` remains a
        compatibility wrapper for older callers/tests until they are migrated.
        """
        if self.is_busy():
            raise RuntimeError("cannot start ingest while another ingest job is active")
        batch = [str(p) for p in paths if str(p)]
        if not batch:
            return
        root = self._wsm.root()
        self._queue.append((batch, str(root) if root is not None else None))
        self._start_next()

    def is_busy(self) -> bool:
        """True while a job is in flight or queued (used by teardown; drops never consult it)."""
        return self._runner is not None or bool(self._queue) or self._current is not None

    def runner(self) -> ProcessRunner | None:
        """The active process for cooperative cancellation by the operation coordinator."""
        return self._runner

    # ---- job scheduling --------------------------------------------------
    def _start_next(self) -> None:
        # ``_draining`` guards re-entry: the ingest_failed.emit below runs slots synchronously, and a
        # slot that re-queued a batch would otherwise call back into _start_next mid-drain (runner not
        # yet set) and pop the same queue. The guard makes a re-entrant request_ingest simply enqueue;
        # the current drain picks it up (or the next _finish_job does).
        if self._runner is not None or self._draining:
            return
        self._draining = True
        try:
            # Drain (and drop) any queued batches whose pinned root no longer matches the live
            # workspace, so a burst queued for workspace A never spills into workspace B after a
            # mid-queue switch. ``pop(0)`` shrinks the queue each turn, so this always terminates.
            paths = None
            while self._queue:
                batch, pinned = self._queue.pop(0)
                if pinned is not None:
                    live = self._wsm.root()
                    if live is None or str(live) != pinned:
                        log.warning("dropping queued ingest batch pinned to %s — workspace is now %s",
                                    pinned, live)
                        # Informational (status bar + log dock via the failed slot); never modal.
                        # Exit code 0 marks "not an engine failure — dropped before it ran".
                        self.ingest_failed.emit(
                            0, f"queued ingest dropped — workspace changed away from {pinned}")
                        continue
                paths = batch
                break
            if paths is None:
                return
        finally:
            self._draining = False
        self._current = paths
        self._report = None
        self._step_i = 0

        state = self._wsm.state()
        if state == "none":
            # Reserve a throwaway workspace and drop into the pending bootstrap path.
            self._wsm.new_temp()
            state = self._wsm.state()
            root_after_reserve = self._wsm.root()
            if root_after_reserve is not None:
                self.bootstrap_bound.emit(str(root_after_reserve), self._wsm.generation())

        root = self._wsm.root()
        if root is None:
            # Should not happen (new_temp reserves a root); refuse rather than build a bad command.
            self._log("ingest aborted — no workspace root to ingest into")
            self._finish_job()
            return
        root = str(root)
        # Pin the job to THIS root: the argv below embeds it, and every finished-callback compares it
        # against the live manager root so a workspace switch mid-run can't fire this job's result
        # (open / report / highlight) into a different workspace.
        self._current_root = root

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

        # Workspace-identity guard: if the manager no longer points at the root this job was pinned
        # to (the operator opened/switched a workspace mid-run), the subprocess already wrote to the
        # captured root on disk, but every UI-facing effect — opening, the finished report, the
        # highlight, even a failure notice — would land in the WRONG workspace. Discard them all.
        if not self._job_root_is_current():
            log.warning("stale ingest result for %s discarded (workspace changed mid-run)",
                        self._current_root)
            self._log(f"stale ingest result for {self._current_root} discarded "
                      "(workspace changed mid-run)")
            self._finish_job()
            return

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
                # "ingest step failed" (not "ingest failed") so a generic cause of "ingest failed …"
                # does not stutter into "ingest failed (exit 1): ingest failed …".
                self._log(f"ingest step failed (exit {code}): {cause}")
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

    def _job_root_is_current(self) -> bool:
        """True while the manager still points at the root this job was pinned to (or is still
        pending on it). False once the operator switched to a different workspace mid-run."""
        live = self._wsm.root()
        return live is not None and self._current_root is not None \
            and str(live) == self._current_root

    def _open_root(self) -> None:
        # Open the CAPTURED root (guaranteed == the live root here by the _job_root_is_current gate).
        root = self._current_root
        if root is not None:
            if not self._wsm.open(root):
                self._log(f"scaffolded {root} but could not open it as a workspace")
            else:
                self.bootstrap_bound.emit(str(root), self._wsm.generation())

    def _finish_job(self) -> None:
        self._current = None
        self._current_root = None
        self._steps = []
        self._step_i = 0
        self._open_after = False
        self._report = None
        self._lines = []
        self.job_finished.emit()
        # Drain the next queued batch (re-reads manager state, so a workspace that became ready mid-
        # queue routes the follow-on batch straight to ingest).
        self._start_next()

    def _dispose_runner(self) -> None:
        if self._runner is None:
            return
        try:
            self._runner.line.disconnect()
            self._runner.finished.disconnect()
        except (RuntimeError, TypeError) as exc:
            log.debug("runner signals already detached / C++ side gone: %s", exc)
        self._runner.deleteLater()
        self._runner = None

    def _step_log(self, kind: str) -> str | None:
        """A retained per-step log next to the workspace (best-effort; None if no root)."""
        root = self._wsm.root()
        if root is None:
            return None
        return str(Path(root) / "report" / "gui-runs"
                   / f"ingest-{kind}-{datetime.now():%Y%m%d-%H%M%S}.log")

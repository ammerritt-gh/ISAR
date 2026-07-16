"""Single-writer coordinator for GUI-owned workspace operations.

``isar ingest`` and ``isar run`` both rewrite the machine-owned workspace state.  This QObject is
therefore the only GUI component allowed to start either subprocess.  It applies an intentionally
conservative FIFO policy: queued/active ingest work runs before analysis; files discovered during a
run wait until it finishes.  Each job is pinned to both a workspace root and a WorkspaceManager
generation, so a late callback cannot refresh a different binding.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from isar_gui.commands import DriveOptions, RunOptions, build_drive_command, build_run_command
from isar_gui.ingest_controller import IngestController
from isar_gui.process import ProcessRunner

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Job:
    kind: str
    payload: object
    root: str | None
    generation: int


def _same_root(a, b) -> bool:
    """One normalization for job-root identity — Windows case/separator variants must not defeat
    it (WindowsPath equality is case-insensitive; resolve() settles separators/relative parts)."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


class WorkspaceOperationCoordinator(QObject):
    """Serialize GUI initiated init/ingest/run work for one workspace binding.

    Consumers should use ``request_ingest`` and ``request_run`` rather than starting a process
    directly.  ``activity_changed`` carries ``kind``, ``queued`` and ``cancelling`` for a compact
    operator-facing progress indicator.
    """

    activity_changed = Signal(object)
    ingest_started = Signal(list)
    ingest_finished = Signal(object)
    ingest_failed = Signal(int, str)
    run_started = Signal()
    run_finished = Signal(int, bool)  # exit code, callback is still for the live workspace
    drive_started = Signal()
    drive_finished = Signal(int, bool)  # exit code (4 = stopped by operator), live workspace

    def __init__(self, python_exe: str, workspace_manager, log_sink, parent=None):
        super().__init__(parent)
        self._python = python_exe
        self._wsm = workspace_manager
        self._log = log_sink or (lambda _msg: None)
        self._ingest = IngestController(python_exe, workspace_manager, self._log, parent=self)
        self._ingest.ingest_started.connect(self._on_ingest_started)
        self._ingest.ingest_finished.connect(self._on_ingest_finished)
        self._ingest.ingest_failed.connect(self._on_ingest_failed)
        self._ingest.job_finished.connect(self._on_ingest_job_finished)
        self._ingest.bootstrap_bound.connect(self._adopt_bootstrap_binding)
        self._queue: list[_Job] = []
        self._active: _Job | None = None
        self._run_runner: ProcessRunner | None = None
        self._drive_runner: ProcessRunner | None = None
        self._ingest_report = None
        self._ingest_failure: tuple[int, str] | None = None
        self._cancelling = False
        self._stop_pending = False        # a cooperative campaign stop request is in flight
        self._drive_force_stopped = False  # the operator force-killed the campaign tree
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_if_still_running)

    # ---- public state / requests ----------------------------------------
    def is_busy(self) -> bool:
        return self._active is not None or bool(self._queue)

    def is_running_analysis(self) -> bool:
        return self._active is not None and self._active.kind == "run"

    def is_running_campaign(self) -> bool:
        return self._active is not None and self._active.kind == "drive"

    def is_stop_pending(self) -> bool:
        """A cooperative campaign stop request has been written and not yet honored."""
        return self._stop_pending

    def drive_options(self) -> DriveOptions | None:
        """The ACTIVE campaign's launch-time options (None when no campaign is running).

        Display code must read the running campaign's parameters from here — the setup form can
        be edited freely mid-run and no longer describes the subprocess."""
        if self._active is None or self._active.kind != "drive":
            return None
        opts, _env = self._active.payload
        return opts

    def queued_count(self) -> int:
        return len(self._queue)

    def request_ingest(self, paths) -> bool:
        batch = [str(path) for path in paths if str(path)]
        if not batch:
            return False
        root = self._wsm.root()
        self._queue.append(_Job("ingest", batch, str(root) if root is not None else None,
                                self._wsm.generation()))
        self._emit_activity()
        self._start_next()
        return True

    def request_run(self, data_paths, opts: RunOptions, *, env: dict | None = None) -> bool:
        root = self._wsm.root()
        if root is None or self._wsm.state() != "ready":
            return False
        self._queue.append(_Job("run", (list(data_paths), opts, dict(env or {})), str(root),
                                self._wsm.generation()))
        self._emit_activity()
        self._start_next()
        return True

    def request_drive(self, opts: DriveOptions, *, env: dict | None = None) -> bool:
        """Queue an ``isar drive`` campaign for the CURRENT ready OR pending workspace.

        A pending (reserved, card-less) root is a first-class campaign target: the drive engine
        is cold-start capable and lazily initializes the workspace itself after its first scan —
        the window promotes/adopts the binding once that happens (see ``adopt_drive_binding``).
        A fresh control token is stamped into the options here — the token names this
        invocation's stop-request file (``report/control/stop-<token>.json``), so Stop can only
        ever address the campaign this coordinator launched. The job captures root + generation
        at request time; a later rebind can neither hijack the stop path nor adopt the result.
        """
        root = self._wsm.root()
        if root is None or self._wsm.state() not in ("pending", "ready"):
            return False
        opts = replace(opts, control_token=uuid.uuid4().hex)
        self._queue.append(_Job("drive", (opts, dict(env or {})), str(root),
                                self._wsm.generation()))
        self._emit_activity()
        self._start_next()
        return True

    def request_drive_stop(self) -> bool:
        """Cooperatively stop the ACTIVE campaign: write its token's stop-request control file.

        The drive loop honors the request at its iteration boundary (the in-flight scan completes,
        persists, and is analyzed first), then exits with the ``operator_stopped`` state (exit 4).
        Idempotent — repeated clicks are no-ops once the file exists. Writes go to the job's
        CAPTURED root, never the currently bound workspace. Nothing is ever deleted."""
        active = self._active
        if active is None or active.kind != "drive" or active.root is None:
            return False
        if self._drive_runner is None or not self._drive_runner.is_running():
            return False   # already finishing — a request now would be a lie in the log
        opts, _env = active.payload
        control_dir = Path(active.root) / "report" / "control"
        marker = control_dir / f"stop-{opts.control_token}.json"
        if marker.exists():
            return True
        try:
            control_dir.mkdir(parents=True, exist_ok=True)
            tmp = control_dir / f".stop-{opts.control_token}.tmp"
            tmp.write_text(json.dumps({"token": opts.control_token,
                                       "requested_at": datetime.now().astimezone().isoformat(),
                                       "by": "gui"}), encoding="utf-8")
            os.replace(tmp, marker)   # atomic: the loop never sees a half-written request
        except OSError as exc:
            self._log(f"could not write the campaign stop request ({exc})")
            return False
        self._stop_pending = True
        self._log("stop requested — the campaign will stop after the current scan")
        self._emit_activity()
        return True

    def cancel_drive(self) -> bool:
        """Force-stop the ACTIVE campaign only: kill its whole process tree, keep the queue.

        No terminate()-then-wait grace — terminate is useless for console Python and the drive's
        children must never be orphaned; ``kill_tree`` handles the ladder. Queued non-drive jobs
        survive and start once ``finished`` flows through the normal completion path."""
        active = self._active
        if active is None or active.kind != "drive":
            return False
        self._log("force-stopping the campaign (process tree)")
        self._drive_force_stopped = True
        if self._drive_runner is not None and self._drive_runner.is_running():
            self._drive_runner.kill_tree()
        return True

    def cancel(self) -> bool:
        """Cancel the active process and clear jobs that have not written anything yet."""
        if not self.is_busy():
            return False
        dropped = len(self._queue)
        self._queue.clear()
        self._cancelling = True
        if dropped:
            self._log(f"cancelled {dropped} queued workspace operation(s)")
        if self._active is None:
            self._cancelling = False
            self._emit_activity()
            return True
        self._log(f"cancelling {self._active.kind}…")
        if self._active.kind == "drive":
            # A campaign's children must never be orphaned by ANY stop path; skip the
            # terminate-grace dance (useless for console Python) and take the tree down now.
            self._drive_force_stopped = True
            if self._drive_runner is not None and self._drive_runner.is_running():
                self._drive_runner.kill_tree()
            self._emit_activity()
            return True
        runner = self._run_runner if self._active.kind == "run" else self._ingest.runner()
        if runner is None or not runner.is_running():
            return True
        runner.terminate()
        self._kill_timer.start(3000)
        self._emit_activity()
        return True

    # ---- scheduling ------------------------------------------------------
    def _start_next(self) -> None:
        if self._active is not None:
            return
        while self._queue:
            job = self._queue.pop(0)
            # A root/generation mismatch can happen only through a programmatic lifecycle call:
            # UI actions are disabled while busy.  Never redirect a queued batch to the new root.
            if not self._job_is_current(job):
                self._log(f"dropped queued {job.kind} — workspace changed")
                continue
            self._active = job
            self._cancelling = False
            self._ingest_report = None
            self._ingest_failure = None
            self._emit_activity()
            if job.kind == "ingest":
                self._ingest.start_ingest(job.payload)
            elif job.kind == "drive":
                self._start_drive(job)
            else:
                self._start_run(job)
            return
        self._emit_activity()

    def _start_run(self, job: _Job) -> None:
        data_paths, opts, env = job.payload
        assert job.root is not None
        cmd = build_run_command(self._python, data_paths, job.root, opts)
        log_path = Path(job.root) / "report" / "gui-runs" / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
        self._log("$ " + " ".join(cmd))
        runner = ProcessRunner(self)
        runner.line.connect(self._log)
        runner.finished.connect(self._on_run_finished)
        self._run_runner = runner
        runner.start(cmd, log_path=str(log_path), env=env)
        self.run_started.emit()

    def _start_drive(self, job: _Job) -> None:
        opts, env = job.payload
        assert job.root is not None
        cmd = build_drive_command(self._python, job.root, opts)
        log_path = Path(job.root) / "report" / "gui-runs" / f"drive-{datetime.now():%Y%m%d-%H%M%S}.log"
        self._log("$ " + " ".join(cmd))
        runner = ProcessRunner(self)
        runner.line.connect(self._log)
        runner.finished.connect(self._on_drive_finished)
        self._drive_runner = runner
        runner.start(cmd, log_path=str(log_path), env=env)
        self.drive_started.emit()

    def _job_is_current(self, job: _Job) -> bool:
        live = self._wsm.root()
        return job.generation == self._wsm.generation() and (
            job.root is None or (live is not None and _same_root(live, job.root)))

    def _adopt_bootstrap_binding(self, root: str, generation: int) -> None:
        """Adopt a known init/ingest bootstrap transition from our active controller.

        ``new_temp`` and opening the just-scaffolded root deliberately create new workspace
        generations.  Those are not stale callbacks.  Only the controller emits this signal, and
        only the active ingest plus jobs that were queued for the same pending/no-workspace binding
        are rebased; an arbitrary root/generation change still fails ``_job_is_current``.
        """
        active = self._active
        if active is None or active.kind != "ingest":
            log.warning("ignoring unexpected bootstrap bind for %s", root)
            return
        if self._wsm.root() is None or str(self._wsm.root()) != root \
                or self._wsm.generation() != generation:
            log.warning("ignoring stale bootstrap bind for %s", root)
            return
        old_root, old_generation = active.root, active.generation
        self._active = _Job(active.kind, active.payload, root, generation)
        rebased: list[_Job] = []
        for job in self._queue:
            same_bootstrap = job.root is None or (
                job.root == root and job.generation == old_generation)
            if job.kind == "ingest" and same_bootstrap:
                rebased.append(_Job(job.kind, job.payload, root, generation))
            else:
                rebased.append(job)
        self._queue = rebased
        self._log(f"ingest bootstrap bound to {root}")
        self._emit_activity()

    def adopt_drive_binding(self, root: str, generation: int) -> bool:
        """Adopt the pending→ready promotion of the ACTIVE campaign's own workspace.

        Mirrors :meth:`_adopt_bootstrap_binding` for the drive job: the campaign that was
        launched into a reserved pending directory has initialized it, the manager just opened
        it (bumping the generation), and the running job must follow — its payload (control
        token included) is untouched, so cooperative Stop keeps addressing the same campaign
        before and after the promotion. Succeeds only for an active DRIVE job whose root is the
        SAME directory the manager now has bound at ``generation``.

        Returns whether the job was rebased. A refusal after the manager already bumped the
        generation means the campaign's eventual completion will be classified stale — the
        caller must surface that loudly rather than let it be discovered later."""
        active = self._active
        if active is None or active.kind != "drive":
            log.warning("refusing drive adoption for %s — no active campaign", root)
            return False
        if active.root is None or not _same_root(active.root, root):
            log.warning("refusing drive adoption for %s — the active campaign runs in %s",
                        root, active.root)
            return False
        live = self._wsm.root()
        if live is None or not _same_root(live, root) or self._wsm.generation() != generation:
            log.warning("refusing stale drive adoption for %s (live %s, generation %s)",
                        root, live, self._wsm.generation())
            return False
        self._active = _Job(active.kind, active.payload, active.root, generation)
        self._log(f"campaign workspace adopted: {root}")
        self._emit_activity()
        return True

    def _finish_active(self) -> None:
        self._kill_timer.stop()
        self._active = None
        self._cancelling = False
        self._stop_pending = False
        self._drive_force_stopped = False
        self._emit_activity()
        # IngestController emits its public terminal signal before it clears its private step
        # scratch.  Defer the next dequeue one Qt turn so the next coordinated batch cannot see
        # that controller as busy and accidentally overlap it.
        QTimer.singleShot(0, self._start_next)

    # ---- ingest relay ----------------------------------------------------
    def _on_ingest_started(self, paths) -> None:
        if self._active is not None and self._active.kind == "ingest":
            self.ingest_started.emit(paths)

    def _on_ingest_finished(self, report) -> None:
        self._ingest_report = report

    def _on_ingest_failed(self, code: int, cause: str) -> None:
        self._ingest_failure = (int(code), cause)

    def _on_ingest_job_finished(self) -> None:
        """Relay a terminal ingest result only after the controller cleared its step state.

        The controller intentionally suppresses UI results for a stale root.  Its unconditional
        ``job_finished`` signal prevents the workspace-wide queue from getting stuck in that case.
        """
        active = self._active
        if active is not None and active.kind == "ingest" and self._job_is_current(active):
            if self._ingest_failure is not None:
                self.ingest_failed.emit(*self._ingest_failure)
            elif self._ingest_report is not None:
                self.ingest_finished.emit(self._ingest_report)
        elif active is not None:
            self._log("stale ingest completion ignored — workspace changed")
        self._finish_active()

    # ---- run relay / cancellation ---------------------------------------
    def _on_run_finished(self, code: int) -> None:
        active = self._active
        live = active is not None and active.kind == "run" and self._job_is_current(active)
        runner = self._run_runner
        self._run_runner = None
        if runner is not None:
            try:
                runner.line.disconnect(self._log)
                runner.finished.disconnect(self._on_run_finished)
            except (RuntimeError, TypeError) as exc:
                log.debug("run runner signals already detached: %s", exc)
            runner.deleteLater()
        self.run_finished.emit(int(code), bool(live))
        self._finish_active()

    def _on_drive_finished(self, code: int) -> None:
        active = self._active
        live = active is not None and active.kind == "drive" and self._job_is_current(active)
        runner = self._drive_runner
        self._drive_runner = None
        if runner is not None:
            try:
                runner.line.disconnect(self._log)
                runner.finished.disconnect(self._on_drive_finished)
            except (RuntimeError, TypeError) as exc:
                log.debug("drive runner signals already detached: %s", exc)
            runner.deleteLater()
        # Exit 4 is the loop's operator_stopped state — an intentional, resumable outcome. A
        # force-kill exits with an arbitrary code; without the flag it would read as a failure.
        outcome = {0: "campaign met its objective", 3: "campaign stopped: objective unmeetable",
                   4: "campaign stopped by operator (resumable)"}.get(int(code))
        if outcome is None and self._drive_force_stopped:
            outcome = "campaign force-stopped by operator (resumable; the in-flight scan may be re-measured)"
        self._log(outcome if outcome else f"campaign failed (exit {code}) — see the drive log")
        self.drive_finished.emit(int(code), bool(live))
        self._finish_active()

    def _kill_if_still_running(self) -> None:
        if not self._cancelling or self._active is None:
            return
        if self._active.kind == "drive":
            # Never plain-kill a campaign root — that orphans its isar-run/worker children.
            if self._drive_runner is not None and self._drive_runner.is_running():
                self._log("campaign did not stop after 3 seconds; killing its process tree")
                self._drive_runner.kill_tree()
            return
        runner = self._run_runner if self._active.kind == "run" else self._ingest.runner()
        if runner is not None and runner.is_running():
            self._log(f"{self._active.kind} did not stop after 3 seconds; killing it")
            runner.kill()

    def _emit_activity(self) -> None:
        active = self._active
        self.activity_changed.emit({
            "kind": None if active is None else active.kind,
            "queued": len(self._queue),
            "cancelling": self._cancelling,
            "stopping": self._stop_pending,
        })

"""WorkspaceManager — the GUI's workspace lifecycle owner (Phase C).

ISAR always works "in a workspace". If the operator has not opened one, they work in a **temporary**
workspace: a real directory under a per-user recovery root that can be ``Save As``-d later, is
prompted-about on close, and is offered for recovery at next startup. This module owns that lifecycle
so :class:`~isar_gui.main_window.MainWindow` stays a wiring shell: the manager reserves/opens/copies/
discards directories and drives the ``isar init`` (+ minimal inline ``isar ingest``) bootstrap, and
emits signals the window merely reflects.

Three states:
  * ``"none"``    — nothing bound (empty-start).
  * ``"pending"`` — a directory is reserved (``mkdir``-ed under :func:`temp_root`) but has no card
    yet; docks show their empty state and drops are accepted.
  * ``"ready"``   — :meth:`Workspace.open <isar.workspace_api.Workspace.open>` succeeded.

The pure path/copy/scan helpers are module-level functions (Qt-free) so tests cover them without a
QApplication; the :class:`WorkspaceManager` QObject wraps them. All file I/O is utf-8, and every
caught exception is logged (never silently swallowed).

Seam note (Phase D): :meth:`bootstrap_from_data` owns the init -> ingest -> open sequence end-to-end.
The **ingest** half is a deliberately small, obviously-replaceable inline ``ProcessRunner`` call;
Phase D lifts it (and the ready-state drop ingest in MainWindow) into a dedicated IngestController.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar_gui.commands import (
    build_ingest_command,
    build_init_command,
    init_failure_cause,
)
from isar_gui.process import ProcessRunner

log = logging.getLogger(__name__)

# Markers whose presence means a directory holds real analysis worth recovering / prompting about.
_CONTENT_MARKERS = ("card.policy.toml", "session.json")


# ---- Qt-free module helpers (unit-tested without a QApplication) -------------
def temp_root() -> Path:
    """The per-user recovery root for temporary workspaces.

    ``%LOCALAPPDATA%/isar_gui/temp_workspaces`` on Windows; ``~/.isar_gui/temp_workspaces`` where
    ``LOCALAPPDATA`` is unset (Linux/CI). Created on demand by :func:`reserve_temp_dir`.
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "isar_gui" if local else Path.home() / ".isar_gui"
    return base / "temp_workspaces"


def reserve_temp_dir() -> Path:
    """Reserve (mkdir) a fresh ``<temp_root>/ws-<YYYYmmdd-HHMMSS>`` directory and return it.

    A same-second collision (two reservations in one second) is disambiguated with a numeric suffix
    so the reservation never clobbers or reuses an existing temp workspace.
    """
    root = temp_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / f"ws-{stamp}"
    n = 1
    while candidate.exists():
        candidate = root / f"ws-{stamp}-{n}"
        n += 1
    candidate.mkdir()
    return candidate


def has_content(root) -> bool:
    """True if ``root`` holds real analysis: a ``card.policy.toml`` or a ``session.json``."""
    root = Path(root)
    return any((root / marker).exists() for marker in _CONTENT_MARKERS)


def is_under(path, base) -> bool:
    """True if ``path`` is ``base`` or lives beneath it (both resolved). Never raises."""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def recovery_candidates() -> list[Path]:
    """Content-bearing child directories of :func:`temp_root`, newest first.

    These are leftover temporary workspaces from an earlier session (a crash, or a close where the
    operator chose neither Save As nor Discard) that still hold analysis worth recovering.
    """
    root = temp_root()
    if not root.is_dir():
        return []
    try:
        children = [c for c in root.iterdir() if c.is_dir() and has_content(c)]
    except OSError as exc:
        log.warning("could not scan temp_root %s for recovery candidates: %s", root, exc)
        return []
    return sorted(children, key=lambda p: p.name, reverse=True)


def discard_dir(root) -> bool:
    """``rmtree`` ``root`` ONLY when it is under :func:`temp_root`; refuse (log + no-op) otherwise.

    The refuse-outside-temp guard is the safety rail: a discard must never delete a real, saved
    workspace, only a throwaway temp directory. Returns whether the directory was removed.
    """
    root = Path(root)
    if not is_under(root, temp_root()):
        log.warning("refusing to discard %s — not under temp_root %s", root, temp_root())
        return False
    if not root.exists():
        return False
    try:
        shutil.rmtree(root)
        return True
    except OSError as exc:
        log.warning("could not discard temp workspace %s: %s", root, exc)
        return False


# ---- the QObject lifecycle owner --------------------------------------------
class WorkspaceManager(QObject):
    """Owns the workspace lifecycle (none / pending / ready) and the bootstrap sequence.

    Signals:
      * ``workspace_changed(object)`` — the bound :class:`Workspace` facade, or ``None`` for the
        none/pending states (the window rebinds its docks to whatever it receives).
      * ``state_changed(str)`` — the new state string (``"none" | "pending" | "ready"``).
      * ``bootstrap_failed(str)`` — an ``isar init`` bootstrap failed; the arg is a human cause.
    """

    workspace_changed = Signal(object)
    state_changed = Signal(str)
    bootstrap_failed = Signal(str)

    def __init__(self, parent=None, *, python: str | None = None):
        super().__init__(parent)
        import sys

        self._python = python or sys.executable
        self._state = "none"
        self._root: Path | None = None
        self._ws: Workspace | None = None
        self._runner: ProcessRunner | None = None  # the live bootstrap subprocess, if any
        self._bootstrapping = False   # True while an init(->ingest->open) sequence is in flight
        self._last_error: str | None = None  # cause of the last open/save_as failure, for the UI

    # ---- introspection ----------------------------------------------------
    def state(self) -> str:
        return self._state

    def root(self) -> Path | None:
        return self._root

    def workspace(self) -> Workspace | None:
        return self._ws

    def is_temporary(self) -> bool:
        """True when the bound root is a throwaway directory under :func:`temp_root`."""
        return self._root is not None and is_under(self._root, temp_root())

    def has_content(self) -> bool:
        """True when the bound root holds real analysis (card/session written)."""
        return self._root is not None and has_content(self._root)

    def is_bootstrapping(self) -> bool:
        """True while an ``init(->ingest->open)`` sequence is in flight (drops are refused)."""
        return self._bootstrapping

    def last_error(self) -> str | None:
        """The cause of the most recent ``open``/``save_as`` failure, or ``None``."""
        return self._last_error

    # ---- state transitions ------------------------------------------------
    def _set_state(self, state: str) -> None:
        self._state = state
        self.state_changed.emit(state)

    def adopt(self, ws: Workspace) -> None:
        """Adopt an already-open workspace as the ready state WITHOUT emitting (MainWindow binds it
        directly at construction; later transitions go through the emitting methods)."""
        self._ws = ws
        self._root = Path(ws.root)
        self._state = "ready"

    def new_temp(self) -> Path:
        """Reserve a fresh temporary workspace directory and enter the ``pending`` state.

        Emits ``workspace_changed(None)`` (docks fall to their empty state) then
        ``state_changed("pending")``. Returns the reserved directory.
        """
        root = reserve_temp_dir()
        self._root = root
        self._ws = None
        # Set state BEFORE the bind so the window's pending hint / unsaved title read the right state
        # while it rebinds on workspace_changed; state_changed then fires for the action/title sync.
        self._state = "pending"
        log.info("reserved temporary workspace %s", root)
        self.workspace_changed.emit(None)
        self._set_state("pending")
        return root

    def open(self, path) -> bool:
        """Open ``path`` as a workspace and enter the ``ready`` state. Returns success.

        A non-workspace path (or an unreadable one) leaves the current binding untouched, logs the
        cause, and returns ``False`` — the caller decides how to surface it.
        """
        try:
            ws = Workspace.open(path)
        except (WorkspaceNotFound, OSError) as exc:
            log.warning("cannot open workspace %s: %s", path, exc)
            self._last_error = f"cannot open {path}: {exc}"
            return False
        self._ws = ws
        self._root = Path(ws.root)
        self._state = "ready"      # set before the bind (see new_temp); state_changed re-syncs after
        self.workspace_changed.emit(ws)
        self._set_state("ready")
        return True

    # ---- bootstrap: init -> ingest -> open (pending only) -----------------
    def bootstrap_from_data(self, data_path) -> None:
        """Scaffold the pending workspace from a dropped data path and open it.

        Sequence (owned here so the window only reflects it): shell ``isar init <data> --out <root>
        --json``; on success shell the minimal inline ``isar ingest`` for the same data (so the very
        first bind already shows the scan ledger); then :meth:`open` the root. On an ``init`` failure
        it emits ``bootstrap_failed(cause)`` and leaves the pending state intact for a retry.
        """
        if self._state != "pending" or self._root is None:
            log.warning("bootstrap_from_data ignored — not in the pending state (state=%s)",
                        self._state)
            return
        if self._bootstrapping:
            # A second drop mid-bootstrap would orphan the live ProcessRunner (its finished callback
            # still connected) — refuse until the in-flight sequence reaches a terminal state.
            log.warning("bootstrap_from_data ignored — a bootstrap is already in flight")
            return
        self._bootstrapping = True
        root = str(self._root)
        data = str(data_path)
        cmd = build_init_command(self._python, data, root, json=True)
        log.info("bootstrap init: %s", " ".join(cmd))
        self._runner = self._spawn(
            cmd, lambda code, lines: self._after_init(code, lines, data),
            log_path=self._boot_log("init"))

    def _after_init(self, code: int, lines, data: str) -> None:
        if code != 0:
            self._bootstrapping = False   # terminal: init failed
            cause = init_failure_cause(lines)
            log.warning("bootstrap init failed (exit %s): %s", code, cause)
            self.bootstrap_failed.emit(cause)
            return
        # init succeeded — the card exists. Run the minimal inline ingest, then open regardless of
        # the ingest outcome (an ingest hiccup must not strand a scaffolded workspace unopened).
        root = str(self._root)
        cmd = build_ingest_command(self._python, [data], root, json=True)
        log.info("bootstrap ingest: %s", " ".join(cmd))
        self._runner = self._spawn(
            cmd, lambda c, _l: self._after_ingest(c, root),
            log_path=self._boot_log("ingest"))

    def _after_ingest(self, code: int, root: str) -> None:
        self._bootstrapping = False       # terminal: ingest done (open follows regardless of code)
        if code != 0:
            log.warning("bootstrap ingest returned exit %s — opening the workspace anyway", code)
        if not self.open(root):
            # init wrote a card, so open should succeed; if it somehow doesn't, surface it.
            self.bootstrap_failed.emit(f"scaffolded {root} but could not open it")

    def _spawn(self, cmd, on_done, *, log_path: str | None = None) -> ProcessRunner:
        """Run ``cmd`` via a fresh :class:`ProcessRunner` (parented to self so it outlives this
        call), buffering its lines; call ``on_done(exit_code, lines)`` when it finishes.

        Belt-and-braces: any prior runner is disconnected and scheduled for deletion before the
        replacement is created, so a stale finished-callback can never fire against the new sequence
        (the #1 guard already prevents concurrent bootstraps; this covers the sequential handoff)."""
        self._dispose_runner()
        lines: list[str] = []
        runner = ProcessRunner(self)
        runner.line.connect(lambda ln: lines.append(ln))
        runner.finished.connect(lambda code: on_done(code, lines))
        self._runner = runner
        runner.start(cmd, log_path=log_path)
        return runner

    def _dispose_runner(self) -> None:
        """Disconnect and schedule deletion of the current bootstrap runner, if any."""
        if self._runner is None:
            return
        try:
            self._runner.line.disconnect()
            self._runner.finished.disconnect()
        except (RuntimeError, TypeError):
            pass  # already disconnected / C++ side gone — nothing to detach
        self._runner.deleteLater()
        self._runner = None

    def _boot_log(self, phase: str) -> str | None:
        """A retained bootstrap log next to the workspace (best-effort; None if root is gone)."""
        if self._root is None:
            return None
        return str(self._root / "report" / "gui-runs"
                   / f"bootstrap-{phase}-{datetime.now():%Y%m%d-%H%M%S}.log")

    # ---- save / discard ---------------------------------------------------
    def save_as(self, dest) -> bool:
        """Copy the current (temporary) workspace tree to ``dest`` and open it there; the temp dir is
        then discarded. ``dest`` must not exist or must be empty. Returns success.

        On any failure (dest non-empty, copy error, or the copied tree not opening) the temp
        workspace is left untouched so nothing is lost, and ``False`` is returned.
        """
        self._last_error = None
        if self._root is None:
            self._last_error = "no workspace to save"
            log.warning("save_as with no workspace bound")
            return False
        dest = Path(dest)
        if dest.exists() and any(dest.iterdir()):
            self._last_error = f"destination is not empty: {dest}"
            log.warning("save_as refused — destination exists and is not empty: %s", dest)
            return False
        source = self._root
        # The emptiness pre-check above is advisory, not atomic; the copy is the real gate. Any IO
        # error (a dest that filled between the check and now, a permission fault) surfaces here as
        # the failure reason rather than being masked.
        try:
            shutil.copytree(source, dest, dirs_exist_ok=True)
        except OSError as exc:
            self._last_error = f"copy to {dest} failed: {exc}"
            log.warning("save_as copy %s -> %s failed: %s", source, dest, exc)
            return False
        if not self.open(dest):
            # open() sets its own _last_error; keep it as the surfaced reason.
            log.warning("save_as copied to %s but the tree did not open as a workspace", dest)
            return False
        # The copy is now the live workspace; drop the throwaway source.
        discard_dir(source)
        return True

    def discard_temp(self) -> None:
        """Discard the temporary workspace directory (rmtree), ONLY when it is under
        :func:`temp_root`; refuse (log + no-op) otherwise. Only the analysis in the temporary
        workspace is removed — the operator's source data files on disk are untouched.
        """
        if self._root is None:
            return
        if discard_dir(self._root):
            # The directory is gone — the manager must not keep pointing at it. Fall back to the
            # none-state and tell the window (docks return to the empty-start view).
            self._root = None
            self._ws = None
            self._state = "none"
            self.workspace_changed.emit(None)
            self.state_changed.emit("none")

    # ---- static conveniences (delegate to the Qt-free helpers) ------------
    @staticmethod
    def temp_root() -> Path:
        return temp_root()

    @staticmethod
    def recovery_candidates() -> list[Path]:
        return recovery_candidates()

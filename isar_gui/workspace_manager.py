"""WorkspaceManager — the GUI's workspace lifecycle owner (Phase C).

ISAR always works "in a workspace". If the operator has not opened one, they work in a **temporary**
workspace: a real directory under a per-user recovery root that can be ``Save As``-d later, is
prompted-about on close, and is offered for recovery at next startup. This module owns that lifecycle
so :class:`~isar_gui.main_window.MainWindow` stays a wiring shell: the manager reserves/opens/copies/
discards directories and emits signals the window merely reflects. Scaffolding a pending workspace
from dropped data (``isar init`` + ``isar ingest``) is driven by
:class:`~isar_gui.ingest_controller.IngestController`, which calls back into :meth:`new_temp` /
:meth:`open` here — see the ownership-split note below.

Three states:
  * ``"none"``    — nothing bound (empty-start).
  * ``"pending"`` — a directory is reserved (``mkdir``-ed under :func:`temp_root`) but has no card
    yet; docks show their empty state and drops are accepted.
  * ``"ready"``   — :meth:`Workspace.open <isar.workspace_api.Workspace.open>` succeeded.

The pure path/copy/scan helpers are module-level functions (Qt-free) so tests cover them without a
QApplication; the :class:`WorkspaceManager` QObject wraps them. All file I/O is utf-8, and every
caught exception is logged (never silently swallowed).

Ownership split (Phase D): the manager owns *pure lifecycle* — reserve (:meth:`new_temp`),
:meth:`open`, :meth:`save_as`, :meth:`discard_temp`, :meth:`adopt` — and the none/pending/ready state
machine. It no longer drives any ``isar`` subprocess. The bootstrap sequence that Phase C had here
(``init`` -> ``ingest`` -> ``open``) moved wholesale into
:class:`~isar_gui.ingest_controller.IngestController`, which consults this manager's state and calls
back into :meth:`new_temp` / :meth:`open` — so there is exactly one serialization point for engine
ingest processes and one owner of the FIFO queue.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from isar.workspace_api import Workspace, WorkspaceNotFound

log = logging.getLogger(__name__)

# Markers whose presence means a directory holds real analysis worth recovering / prompting about.
_CONTENT_MARKERS = ("card.policy.toml", "session.json")

# Everything `isar init` writes, in ITS order (policy first — init_cmd.py). Promotion of a pending
# campaign workspace waits for the full trio: the policy alone is a mid-init state.
_INIT_FILES = ("card.policy.toml", "session.json", "card.state.json")


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


def _unique_stage_path(destination: Path) -> Path:
    """Return a non-existent sibling staging path for a Save As operation."""
    # A sibling is deliberate: ``os.replace`` is atomic only within one filesystem.  The random
    # suffix also means a stale staging directory can never be mistaken for this operation's work.
    return destination.parent / f".{destination.name}.isar-saving-{uuid.uuid4().hex}"


def _clean_stage(stage: Path) -> None:
    """Best-effort cleanup of a staging directory created by :meth:`save_as`."""
    if not stage.exists():
        return
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        log.warning("could not clean Save As staging directory %s: %s", stage, exc)


# ---- the QObject lifecycle owner --------------------------------------------
class WorkspaceManager(QObject):
    """Owns the workspace lifecycle (none / pending / ready): reserve / open / save-as / discard.

    Signals:
      * ``workspace_changed(object)`` — the bound :class:`Workspace` facade, or ``None`` for the
        none/pending states (the window rebinds its docks to whatever it receives).
      * ``state_changed(str)`` — the new state string (``"none" | "pending" | "ready"``).
    """

    workspace_changed = Signal(object)
    state_changed = Signal(str)
    generation_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "none"
        self._root: Path | None = None
        self._ws: Workspace | None = None
        self._last_error: str | None = None  # cause of the last open/save_as failure, for the UI
        # A monotonically increasing binding token.  Long-running GUI work captures this alongside
        # its root and must not update a subsequently opened workspace when it completes.
        self._generation = 0

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

    def last_error(self) -> str | None:
        """The cause of the most recent ``open``/``save_as`` failure, or ``None``."""
        return self._last_error

    def generation(self) -> int:
        """Identity token for the currently bound workspace lifecycle state."""
        return self._generation

    def _bump_generation(self) -> None:
        self._generation += 1
        self.generation_changed.emit(self._generation)

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
        self._bump_generation()

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
        self._bump_generation()
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
        self._bump_generation()
        self.workspace_changed.emit(ws)
        self._set_state("ready")
        return True

    def promote_pending_if_ready(self, expected_root, expected_generation: int) -> bool:
        """Promote the pending workspace to ready once a campaign's lazy init has completed it.

        An ``isar drive`` campaign launched into a reserved (pending) directory initializes the
        workspace itself after its first scan lands. This is the ONE place that detects that —
        the window owns only a timer and never inspects workspace files. Returns True exactly
        when the workspace was opened (the ready rebind has then already happened).

        ``expected_root``/``expected_generation`` are the identity the caller captured when it
        armed its timer: a stale tick (the operator rebound to something else meanwhile) is
        refused here regardless of what the caller believes. Promotion requires ALL of
        :data:`_INIT_FILES` — ``isar init`` writes the policy first, so its presence alone is a
        mid-init state. A failed ``open`` (e.g. a transient half-written card) logs, leaves the
        state pending, and returns False so the caller simply retries on its next tick.
        """
        if self._state != "pending" or self._root is None:
            return False
        if self._generation != int(expected_generation):
            log.debug("promotion skipped: generation moved on (%s != %s)",
                      self._generation, expected_generation)
            return False
        try:
            if Path(expected_root).resolve() != self._root.resolve():
                log.debug("promotion skipped: pending root is %s, caller expected %s",
                          self._root, expected_root)
                return False
        except OSError as exc:
            log.warning("promotion identity check failed for %s: %s", expected_root, exc)
            return False
        if not all((self._root / name).exists() for name in _INIT_FILES):
            return False   # the campaign has not (fully) initialized the workspace yet
        return self.open(self._root)   # failure: open() logged, binding untouched — still pending

    # ---- save / discard ---------------------------------------------------
    def save_as(self, dest) -> bool:
        """Transactionally save the current temporary workspace at ``dest``.

        The source is copied into a unique sibling staging directory, validated as a workspace,
        then atomically promoted into an absent/empty destination.  The original temporary work is
        discarded *only after* the promoted destination has opened.  Thus an interrupted copy,
        collision, validation failure, or open failure always leaves the source recoverable.
        """
        self._last_error = None
        if self._root is None:
            self._last_error = "no workspace to save"
            log.warning("save_as with no workspace bound")
            return False
        if not self.is_temporary():
            self._last_error = "Save As is only available for a temporary workspace"
            log.warning("save_as refused for saved workspace %s", self._root)
            return False
        source = self._root.resolve()
        dest = Path(dest)
        try:
            resolved_dest = dest.resolve()
        except OSError as exc:
            self._last_error = f"cannot resolve destination {dest}: {exc}"
            log.warning("save_as could not resolve destination %s: %s", dest, exc)
            return False
        if resolved_dest == source or is_under(resolved_dest, source):
            self._last_error = "destination must not be this temporary workspace or a folder inside it"
            log.warning("save_as refused nested destination %s for source %s", dest, source)
            return False
        if is_under(resolved_dest, temp_root()):
            self._last_error = "destination must be outside the temporary-workspace recovery folder"
            log.warning("save_as refused temporary destination %s", dest)
            return False
        parent = resolved_dest.parent
        if not parent.is_dir():
            self._last_error = f"destination parent does not exist: {parent}"
            log.warning("save_as refused destination with missing parent %s", parent)
            return False
        try:
            if resolved_dest.exists() and (not resolved_dest.is_dir() or any(resolved_dest.iterdir())):
                self._last_error = f"destination is not empty: {resolved_dest}"
                log.warning("save_as refused — destination exists and is not empty: %s", resolved_dest)
                return False
        except OSError as exc:
            self._last_error = f"cannot inspect destination {resolved_dest}: {exc}"
            log.warning("save_as could not inspect destination %s: %s", resolved_dest, exc)
            return False

        stage = _unique_stage_path(resolved_dest)
        try:
            shutil.copytree(source, stage)
        except OSError as exc:
            _clean_stage(stage)
            self._last_error = f"copy to {resolved_dest} failed: {exc}"
            log.warning("save_as copy %s -> %s failed: %s", source, stage, exc)
            return False

        # Validate without rebinding.  A partial/corrupt copy must not become the live workspace.
        try:
            Workspace.open(stage)
        except (WorkspaceNotFound, OSError) as exc:
            _clean_stage(stage)
            self._last_error = f"saved copy is not a workspace: {exc}"
            log.warning("save_as staged copy %s did not open: %s", stage, exc)
            return False

        # Recheck immediately before promotion.  The initial check was advisory: a different
        # process may have created or populated the destination while this copy was running.
        try:
            if resolved_dest.exists():
                if not resolved_dest.is_dir() or any(resolved_dest.iterdir()):
                    self._last_error = f"destination changed and is not empty: {resolved_dest}"
                    log.warning("save_as promotion refused changed destination %s", resolved_dest)
                    _clean_stage(stage)
                    return False
                resolved_dest.rmdir()
            os.replace(stage, resolved_dest)
        except OSError as exc:
            _clean_stage(stage)
            self._last_error = f"could not promote saved workspace to {resolved_dest}: {exc}"
            log.warning("save_as promotion %s -> %s failed: %s", stage, resolved_dest, exc)
            return False

        if not self.open(resolved_dest):
            # The promoted destination is retained for manual recovery; the temporary source stays
            # bound and untouched because losing either would turn an open failure into data loss.
            log.warning("save_as promoted %s but it did not open; source %s retained", resolved_dest, source)
            return False
        discard_dir(source)
        return True

    def discard_empty_temp(self) -> bool:
        """Discard a bound *empty* temporary reservation before a lifecycle transition.

        Empty pending reservations are implementation scaffolding, not recoverable analysis.  This
        avoids accumulating one dead directory for every cancelled New/Open sequence.
        """
        if not self.is_temporary() or self.has_content():
            return False
        before = self._root
        self.discard_temp()
        return before is not None and not before.exists()

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
            self._bump_generation()
            self.workspace_changed.emit(None)
            self.state_changed.emit("none")

    # ---- static conveniences (delegate to the Qt-free helpers) ------------
    @staticmethod
    def temp_root() -> Path:
        return temp_root()

    @staticmethod
    def recovery_candidates() -> list[Path]:
        return recovery_candidates()

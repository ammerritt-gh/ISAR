"""WorkspaceManager — the GUI's experiment lifecycle owner.

An *experiment* is one ISAR workspace: a real folder, from the moment it exists, that holds one set
of data and everything ISAR derives from it. There is no temporary workspace and no "unsaved" state:
a drop with nothing open creates a named folder under :func:`experiments_root` at once, and
*Save copy as…* makes a copy while the original stays active. This module owns that lifecycle so
:class:`~isar_gui.main_window.MainWindow` stays a wiring shell: the manager creates, opens, renames
and copies experiment folders and emits signals the window merely reflects. Scaffolding a pending
experiment from dropped data (``isar init`` + ``isar ingest``) is driven by
:class:`~isar_gui.ingest_controller.IngestController`, which calls back into :meth:`new_experiment`
/ :meth:`open` here.

Three states:
  * ``"none"``    — nothing bound.
  * ``"pending"`` — an experiment folder exists but has no card yet (a fresh *New experiment…*, a
    campaign's lazy init, or the moment between ``init`` and ``open`` of a drop); docks show their
    empty state and drops are accepted. It is marked by the folder's ``gui.state.json``.
  * ``"ready"``   — :meth:`Workspace.open <isar.workspace_api.Workspace.open>` succeeded.

The pure path/copy/naming helpers are module-level functions (Qt-free) so tests cover them without a
QApplication; the :class:`WorkspaceManager` QObject wraps them. All file I/O is utf-8, and every
caught exception is logged (never silently swallowed).

Ownership split: the manager owns *pure lifecycle* and the none/pending/ready state machine. It
drives no ``isar`` subprocess; the ``init`` -> ``ingest`` -> ``open`` bootstrap lives in
:class:`~isar_gui.ingest_controller.IngestController`, so there is exactly one serialization point
for engine ingest processes and one owner of the FIFO queue.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Signal

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar_gui.gui_store import GuiStore

log = logging.getLogger(__name__)

# Markers whose presence means a legacy temporary directory holds real analysis (the startup pointer).
_CONTENT_MARKERS = ("card.policy.toml", "session.json")

# Everything `isar init` writes, in ITS order (policy first — init_cmd.py). Promotion of a pending
# campaign workspace waits for the full trio: the policy alone is a mid-init state.
_INIT_FILES = ("card.policy.toml", "session.json", "card.state.json")

# Characters a Windows folder name cannot hold, and the device names it reserves (also before a dot).
_BAD_NAME_CHARS = set('<>:"/\\|?*')
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_NAME_STRIP = " \t\r\n\f\v._"
_NAME_MAX = 80
# Where a name taken from several files is cut back to, so "x_sum_a03" and "x_sum_a04" give "x_sum".
_SEPARATORS = "_-. "


# ---- Qt-free module helpers (unit-tested without a QApplication) -------------
def experiments_root() -> Path:
    """Where a new experiment folder is created.

    ``ISAR_EXPERIMENTS_ROOT`` when set (the tests point it at a per-test folder, so none writes to
    the operator's Documents); else ``Documents\\ISAR``, with Documents as Windows reports it (it
    follows a OneDrive-redirected Documents, which ``Path.home() / "Documents"`` does not). Created
    on demand by :func:`unique_experiment_dir`. Not a setting: *Save copy as…* puts an experiment
    elsewhere.
    """
    env = os.environ.get("ISAR_EXPERIMENTS_ROOT")
    if env:
        return Path(env)
    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return (Path(docs) if docs else Path.home() / "Documents") / "ISAR"


def default_experiment_name() -> str:
    """``Experiment_<YYYY-MM-DD>``: what ISAR calls an experiment nothing else names."""
    return f"Experiment_{date.today():%Y-%m-%d}"


def clean_experiment_name(text: str) -> str:
    """A folder name made from ``text``; may be ``""`` when nothing usable is left.

    Each character a Windows folder name cannot hold (``<>:"/\\|?*`` and everything below U+0020)
    becomes ``_``; leading and trailing whitespace, ``.`` and ``_`` go; the result is capped at 80
    characters and stripped again; a reserved device name (``CON``, ``LPT1`` … , also before a dot)
    gets ``_`` appended to its stem so the folder can exist.
    """
    name = "".join("_" if (c in _BAD_NAME_CHARS or ord(c) < 0x20) else c for c in str(text))
    name = name.strip(_NAME_STRIP)[:_NAME_MAX].strip(_NAME_STRIP)
    stem, dot, rest = name.partition(".")
    if stem.upper() in _DEVICE_NAMES:
        name = f"{stem}_{dot}{rest}"
    return name


def experiment_name_for(paths) -> tuple[str, str]:
    """``(name, named_from)`` for the data a drop carries; ``named_from`` is one of
    ``"folder" | "data" | "data_folder" | "default"``.

    One existing folder names the experiment after itself. Otherwise the items' names (a folder's
    name, a file's stem) share a longest common prefix; if that is not one item's whole name it is
    cut back to its last separator (``_ - . space``) and stripped of trailing ones. When that leaves
    nothing, the first item's parent folder names it; failing that, ``Experiment_<date>``.
    """
    items = [Path(str(p)) for p in paths if str(p)]
    if len(items) == 1 and items[0].is_dir():
        name = clean_experiment_name(items[0].name)
        if name:
            return name, "folder"
    if items:
        names = [p.name if p.is_dir() else p.stem for p in items]
        prefix = os.path.commonprefix(names)
        if prefix not in names:
            cut = max(prefix.rfind(c) for c in _SEPARATORS)
            prefix = prefix[:cut] if cut > 0 else ""
        name = clean_experiment_name(prefix.rstrip(_SEPARATORS))
        if name:
            return name, "data"
        name = clean_experiment_name(items[0].parent.name)
        if name:
            return name, "data_folder"
    return default_experiment_name(), "default"


def unique_experiment_dir(parent: Path, name: str) -> Path:
    """Create and return ``parent/name``, or ``parent/name-2``, ``name-3`` … when that is taken.

    The folder is made with ``mkdir(exist_ok=False)`` inside the loop, so two creations never share
    one (the filesystem decides what counts as taken, case-insensitively on Windows). ``parent`` is
    created if absent. Raises ``OSError`` when no folder can be made.
    """
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = parent / (name if n == 1 else f"{name}-{n}")
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            n += 1


def openable(path) -> str | None:
    """``None`` when ``path`` can be opened as an experiment, else the reason it cannot.

    A folder opens when it holds ``card.policy.toml`` (a started experiment) or ``gui.state.json``
    (one created and not yet read into). Any other folder is refused, not adopted: *Open…* must never
    silently start an experiment in a random folder.
    """
    p = Path(path)
    if (p / "card.policy.toml").exists() or GuiStore.path_for(p).exists():
        return None
    return f"not an ISAR experiment (no card.policy.toml): {p}"


def temp_root() -> Path:
    """The per-user folder the retired temporary workspaces lived in (startup pointer only).

    ``%LOCALAPPDATA%/isar_gui/temp_workspaces`` on Windows; ``~/.isar_gui/temp_workspaces`` where
    ``LOCALAPPDATA`` is unset (Linux/CI). Nothing is created there any more.
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "isar_gui" if local else Path.home() / ".isar_gui"
    return base / "temp_workspaces"


def has_content(root) -> bool:
    """True if ``root`` holds real analysis: a ``card.policy.toml`` or a ``session.json``."""
    root = Path(root)
    return any((root / marker).exists() for marker in _CONTENT_MARKERS)


def legacy_temp_workspaces() -> list[Path]:
    """Content-bearing folders left in :func:`temp_root` by earlier builds, newest first.

    Only for the startup pointer in the Log: such a folder opens as an experiment in place, and
    nothing is deleted or read back by any recovery path.
    """
    root = temp_root()
    if not root.is_dir():
        return []
    try:
        children = [c for c in root.iterdir() if c.is_dir() and has_content(c)]
    except OSError as exc:
        log.warning("could not scan %s for legacy temporary workspaces: %s", root, exc)
        return []
    return sorted(children, key=lambda p: p.name, reverse=True)


def is_under(path, base) -> bool:
    """True if ``path`` is ``base`` or lives beneath it (both resolved). Never raises."""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def _unique_stage_path(destination: Path) -> Path:
    """Return a non-existent staging path for a Save copy operation, near the destination."""
    # The nearest EXISTING ancestor is deliberate on two counts: ``os.replace`` is atomic only
    # within one filesystem (an ancestor is always on the destination's volume), and staging there
    # means a destination whose parents do not exist yet stays uncreated until promotion — a failed
    # copy or validation leaves no new directories behind at all.  The random suffix also means a
    # stale staging directory can never be mistaken for this operation's work.
    base = Path(destination).parent
    while base != base.parent and not base.exists():
        base = base.parent
    return base / f".{Path(destination).name}.isar-saving-{uuid.uuid4().hex}"


def _clean_stage(stage: Path) -> None:
    """Best-effort cleanup of a staging directory created by :meth:`WorkspaceManager.save_copy`."""
    if not stage.exists():
        return
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        log.warning("could not clean Save copy staging directory %s: %s", stage, exc)


def missing_ancestors(destination: Path) -> list[Path]:
    """The not-yet-existing parent directories of ``destination``, outermost first.

    Save copy accepts a destination the operator merely *named* (``…/2026-08/beamtime/run3``), so the
    parent chain may not exist.  Returning it explicitly is what lets :meth:`save_copy` undo exactly
    the directories it created when the transaction later fails.
    """
    missing: list[Path] = []
    current = Path(destination).parent
    while current != current.parent and not current.exists():
        missing.append(current)
        current = current.parent
    missing.reverse()
    return missing


def _prune_created(created: list[Path]) -> None:
    """Remove directories created for a Save copy destination, innermost first, empty ones only.

    A failed transaction must leave the filesystem as it found it.  ``rmdir`` (never ``rmtree``) is
    the safety rail: if anything landed inside a directory we made, it stays and is logged.
    """
    for index, directory in enumerate(reversed(created)):
        try:
            directory.rmdir()
        except OSError as exc:
            # Stopping here is deliberate: a directory that would not go is still present, so every
            # ancestor above it is non-empty and would only produce a cascade of identical
            # warnings. Name what is being left behind instead.
            kept = [str(path) for path in reversed(created)][index:]
            log.warning("could not remove Save copy directory %s created for the destination (%s); "
                        "leaving %s in place", directory, exc, ", ".join(kept))
            return


# ---- the QObject lifecycle owner --------------------------------------------
class WorkspaceManager(QObject):
    """Owns the experiment lifecycle (none / pending / ready): create / open / rename / copy.

    Signals:
      * ``workspace_changed(object)`` — the bound :class:`Workspace` facade, or ``None`` for the
        none/pending states (the window rebinds its docks to whatever it receives).
      * ``state_changed(str)`` — the new state string (``"none" | "pending" | "ready"``).
      * ``generation_changed(int)`` — the binding token moved.
      * ``experiment_created(str, str)`` — a new folder was made: its root and ``named_from``
        (``"folder" | "data" | "data_folder" | "default"``), emitted after the state signals.
    """

    workspace_changed = Signal(object)
    state_changed = Signal(str)
    generation_changed = Signal(int)
    experiment_created = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "none"
        self._root: Path | None = None
        self._ws: Workspace | None = None
        self._last_error: str | None = None  # cause of the last open/rename/copy failure, for the UI
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

    def last_error(self) -> str | None:
        """The cause of the most recent ``open``/``rename``/``save_copy`` failure, or ``None``."""
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

    def _bind_pending(self, root: Path) -> None:
        """Bind ``root`` as a pending experiment: ``workspace_changed(None)`` then ``state_changed``."""
        self._root = root
        self._ws = None
        # Set state BEFORE the bind so the window's hints read the right state while it rebinds on
        # workspace_changed; state_changed then fires for the action/title sync.
        self._state = "pending"
        self._bump_generation()
        self.workspace_changed.emit(None)
        self._set_state("pending")

    def _unbind(self) -> None:
        """Fall to the none state (used only when a rename cannot be undone)."""
        self._root = None
        self._ws = None
        self._state = "none"
        self._bump_generation()
        self.workspace_changed.emit(None)
        self._set_state("none")

    def adopt(self, ws: Workspace) -> None:
        """Adopt an already-open workspace as the ready state WITHOUT emitting (MainWindow binds it
        directly at construction; later transitions go through the emitting methods)."""
        self._ws = ws
        self._root = Path(ws.root)
        self._state = "ready"
        self._bump_generation()

    def new_experiment(self, name: str, *, named_from: str = "default") -> Path:
        """Create a fresh experiment folder and enter the ``pending`` state.

        The folder is ``<experiments root>/<cleaned name>`` (numeric suffix when taken), marked by
        its ``gui.state.json``. Emits ``workspace_changed(None)``, ``state_changed("pending")``, then
        ``experiment_created(root, named_from)``. ``named_from`` is keyword-only so no caller can
        unpack a ``(name, named_from)`` tuple into the call. Raises ``OSError`` when the folder
        cannot be made; every caller catches it and shows it. (The marker write logs its own
        failures.)
        """
        root = unique_experiment_dir(
            experiments_root(), clean_experiment_name(name) or default_experiment_name())
        GuiStore(root).save()
        self._last_error = None
        log.info("created experiment folder %s (named from %s)", root, named_from)
        self._bind_pending(root)
        self.experiment_created.emit(str(root), str(named_from))
        return root

    def open(self, path) -> bool:
        """Open ``path`` as an experiment and enter ``ready`` (or ``pending``). Returns success.

        A folder with ``card.policy.toml`` opens ready. A folder with no card but with
        ``gui.state.json`` (an unstarted experiment) opens pending. Anything else — including an
        empty folder — leaves the current binding untouched, logs the cause, sets :meth:`last_error`
        and returns ``False``; the caller decides how to surface it.
        """
        self._last_error = None
        p = Path(path)
        if (p / "card.policy.toml").exists():
            try:
                ws = Workspace.open(path)
            except (WorkspaceNotFound, OSError) as exc:
                log.warning("cannot open workspace %s: %s", path, exc)
                self._last_error = f"cannot open {path}: {exc}"
                return False
            self._ws = ws
            self._root = Path(ws.root)
            self._state = "ready"      # set before the bind (see _bind_pending); state_changed re-syncs
            self._bump_generation()
            self.workspace_changed.emit(ws)
            self._set_state("ready")
            return True
        if GuiStore.path_for(p).exists():
            self._bind_pending(p)
            return True
        self._last_error = openable(p)
        log.warning("cannot open %s: %s", path, self._last_error)
        return False

    def promote_pending_if_ready(self, expected_root, expected_generation: int) -> bool:
        """Promote the pending workspace to ready once a campaign's lazy init has completed it.

        An ``isar drive`` campaign launched into a pending experiment initializes the
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

    # ---- rename -----------------------------------------------------------
    def rename(self, new_name: str) -> bool:
        """Rename the bound experiment's folder to the cleaned ``new_name``. Returns success.

        The folder name is the experiment's name, so one ``os.rename`` on the same volume does both:
        it happens completely or not at all. A refusal or failure before it leaves folder, binding
        and signals exactly as they were. After it, the experiment is re-bound at the new path; if
        that re-bind fails the rename is undone and the old root re-bound, and only if the undo also
        fails is the state ``none`` — with :meth:`last_error` naming where the folder now is. Never
        reports success for a binding that did not happen. The caller stops any watcher holding a
        handle in the folder first (a live directory handle blocks the rename on Windows).
        """
        self._last_error = None
        if self._state not in ("pending", "ready") or self._root is None:
            self._last_error = "no experiment is open"
            return False
        old = self._root
        clean = clean_experiment_name(new_name)
        if not clean:
            self._last_error = "enter a name with letters or digits"
            return False
        new = old.parent / clean
        if str(new) == str(old):
            return True
        try:
            if new.exists() and not os.path.samefile(new, old):
                self._last_error = f"a folder named {clean} already exists in {old.parent}"
                return False
        except OSError as exc:
            log.warning("rename: cannot compare %s with %s: %s", new, old, exc)
            self._last_error = f"could not check {new}: {exc}"
            return False
        was_ready = self._state == "ready"
        try:
            os.rename(old, new)
        except OSError as exc:
            log.warning("rename %s -> %s failed: %s", old, new, exc)
            self._last_error = (f"could not rename {old} to {clean}: {exc}. "
                                "Close any window or program using files in the folder and try again.")
            return False

        def rebind(path: Path) -> bool:
            if was_ready:
                return self.open(path)
            self._bind_pending(path)
            return True

        if rebind(new):
            return True
        reason = self._last_error or "unknown reason"
        try:
            os.rename(new, old)
        except OSError as exc:
            log.error("rename: could not open %s (%s) and could not rename it back: %s", new, reason, exc)
            self._unbind()
            self._last_error = (f"could not open the renamed folder: {reason}; renaming it back also "
                                f"failed ({exc}). The folder is now at {new}; open it with Open…")
            return False
        if rebind(old):
            self._last_error = f"could not open the renamed folder {new}: {reason}; the rename was undone"
            return False
        again = self._last_error or "unknown reason"
        log.error("rename: renamed back to %s but could not reopen it: %s", old, again)
        self._unbind()
        self._last_error = (f"could not open the renamed folder {new}: {reason}; the rename was undone, "
                            f"but the original could not be reopened either ({again}). "
                            f"The folder is at {old}; open it with Open…")
        return False

    # ---- save copy --------------------------------------------------------
    def save_copy(self, dest) -> Path | None:
        """Transactionally copy the bound experiment to ``dest``; return the copy's folder or ``None``.

        The source is the bound root wherever it lives. It is copied into a unique staging directory
        beside the destination's nearest existing ancestor, validated as a workspace, then atomically
        promoted into an absent/empty destination. The original stays bound and untouched, and no
        signal fires: an interrupted copy, collision, validation failure or promotion failure always
        leaves the original exactly as it was, with the cause in :meth:`last_error`. If the copy
        lists watched folders, watching is paused in the *copy's* store only, so an opened copy does
        not silently pull new beamline files that belong to the original.

        ``dest`` need not exist and neither need its parents — the missing chain is created at the
        promotion step (and removed again if promotion fails). An *existing* destination must still be
        an empty directory. Runs on the calling (GUI) thread on purpose: no Qt event runs during it,
        so no ingest can start mid-copy.
        """
        self._last_error = None
        if self._root is None:
            self._last_error = "need an experiment"
            log.warning("save_copy with no experiment bound")
            return None
        if self._state != "ready":
            self._last_error = "no files read into this experiment yet"
            log.warning("save_copy refused: %s has no card yet", self._root)
            return None
        source = self._root.resolve()
        dest = Path(dest)
        try:
            resolved_dest = dest.resolve()
        except OSError as exc:
            self._last_error = f"cannot resolve destination {dest}: {exc}"
            log.warning("save_copy could not resolve destination %s: %s", dest, exc)
            return None
        if resolved_dest == source or is_under(resolved_dest, source):
            self._last_error = "destination must not be this experiment or a folder inside it"
            log.warning("save_copy refused nested destination %s for source %s", dest, source)
            return None
        try:
            if resolved_dest.exists() and (not resolved_dest.is_dir() or any(resolved_dest.iterdir())):
                self._last_error = f"destination is not empty: {resolved_dest}"
                log.warning("save_copy refused — destination exists and is not empty: %s", resolved_dest)
                return None
        except OSError as exc:
            self._last_error = f"cannot inspect destination {resolved_dest}: {exc}"
            log.warning("save_copy could not inspect destination %s: %s", resolved_dest, exc)
            return None

        stage = _unique_stage_path(resolved_dest)
        try:
            shutil.copytree(source, stage)
        except OSError as exc:      # shutil.Error is an OSError: a partial copy is caught and cleaned
            _clean_stage(stage)
            self._last_error = f"copy to {resolved_dest} failed: {exc}"
            log.warning("save_copy copy %s -> %s failed: %s", source, stage, exc)
            return None

        # Pause watching in the staged store only, before anything can open the copy. The write logs
        # its own failures and does not raise, so read it back: a copy that could silently resume
        # pulling the original's beamline files is not a safe copy.
        staged = GuiStore.load(stage)
        if staged.watched_folders():
            staged.set_watch_paused(True)
            staged.save()
            if not GuiStore.load(stage).watch_paused():
                _clean_stage(stage)
                self._last_error = "could not pause folder watching in the copy"
                log.warning("save_copy could not pause watching in staged copy %s", stage)
                return None

        # Validate without rebinding.  A partial/corrupt copy must not be offered as a result.
        try:
            Workspace.open(stage)
        except (WorkspaceNotFound, OSError) as exc:
            _clean_stage(stage)
            self._last_error = f"saved copy is not a workspace: {exc}"
            log.warning("save_copy staged copy %s did not open: %s", stage, exc)
            return None

        # Recheck immediately before promotion.  The initial check was advisory: a different
        # process may have created or populated the destination while this copy was running.
        # The destination's missing parents are created HERE, at the promote step, so that an
        # earlier copy or validation failure leaves nothing at all on disk; whatever this call
        # creates is undone again if the promotion itself fails.
        created = missing_ancestors(resolved_dest)
        try:
            if resolved_dest.exists():
                if not resolved_dest.is_dir() or any(resolved_dest.iterdir()):
                    self._last_error = f"destination changed and is not empty: {resolved_dest}"
                    log.warning("save_copy promotion refused changed destination %s", resolved_dest)
                    _clean_stage(stage)
                    return None
                resolved_dest.rmdir()
            elif created:
                resolved_dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, resolved_dest)
        except OSError as exc:
            _clean_stage(stage)
            _prune_created(created)
            self._last_error = f"could not promote saved copy to {resolved_dest}: {exc}"
            log.warning("save_copy promotion %s -> %s failed: %s", stage, resolved_dest, exc)
            return None
        log.info("saved a copy of %s at %s", source, resolved_dest)
        return resolved_dest

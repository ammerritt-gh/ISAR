"""MainWindow — the Phase-A application shell.

A :class:`QMainWindow` that opens one read-only workspace and wires the shell: a dock scaffold (the
log dock and the C1 workspace browser; more component docks land in later tasks), a two-way-synced
**Docks** menu, a **File -> Open Workspace…** action, a status bar, the watch->Qt bridge, and
session-layout restore/autosave. It *wires*, it does not compute — anything smarter belongs in a
viewmodel (:mod:`isar_gui.viewmodels`).

The Docks-menu two-way sync (checkable action <-> dock ``visibilityChanged``) follows PUFFIN's
``view/main_window.py`` ``_init_menu_bar`` pattern (C:/Users/AMM/Documents/Github/PUFFIN).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtWidgets import QFileDialog, QLabel, QMainWindow

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar_gui.log_dock import LogDock
from isar_gui.session import Autosaver, Session, load_session
from isar_gui.viewmodels import BrowserViewModel, ViewModel
from isar_gui.views.browser_dock import BrowserDock
from isar_gui.watch_bridge import WatchBridge

_PLACEHOLDER = "Select a scan in the Browser."


class MainWindow(QMainWindow):
    """The shell window over one :class:`~isar.workspace_api.Workspace`."""

    def __init__(self, ws, *, session: Session | None = None, watch: bool = True, parent=None):
        super().__init__(parent)
        self._session = session if session is not None else load_session()
        self._watch_enabled = watch
        self.resize(1200, 800)

        central = QLabel(_PLACEHOLDER)
        central.setContentsMargins(24, 24, 24, 24)
        self.setCentralWidget(central)

        self._docks: dict[str, object] = {}
        self._init_file_menu()
        self._bind_workspace(ws)
        self._session.note_workspace(ws.root)

        # Session autosave: debounced 500ms, snapshotting the live layout at fire time.
        self._autosaver = Autosaver(self._snapshot_session, parent=self)

        # Restore the persisted dock layout after the event loop settles (QTimer.singleShot(0, ...)),
        # so all docks exist and their default geometry is applied first.
        QTimer.singleShot(0, self._restore_layout)

    # ---- workspace binding (shared by __init__ and load_workspace) --------
    def _bind_workspace(self, ws) -> None:
        """(Re)bind the window to ``ws``: title, viewmodels, dock scaffold, Docks menu, watch
        bridge. Assumes any previously bound docks/menu/bridge were already torn down by the caller.
        """
        self._ws = ws
        self.setWindowTitle(f"ISAR — {Path(ws.root).name}")

        # A base viewmodel so the watch signal already has a consumer; component viewmodels attach
        # to the same workspace in later tasks.
        self._viewmodel = ViewModel(ws, parent=self)
        self.browser_vm = BrowserViewModel(ws, parent=self)

        self._docks = {}
        self._init_docks()
        self._init_docks_menu()

        self.statusBar().showMessage(f"Workspace: {ws.root}")
        self.log(f"Opened workspace {ws.root}")

        # Watch bridge: refresh + fan out on the Qt thread. Optional (tests may disable it).
        self._bridge = None
        if self._watch_enabled:
            self._bridge = WatchBridge(ws, parent=self)
            self._bridge.changed.connect(self._on_workspace_changed)
            self._bridge.start()

    # ---- dock scaffold ----------------------------------------------------
    def _init_docks(self) -> None:
        from PySide6.QtCore import Qt

        self.log_dock = LogDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self._register_dock("Log", self.log_dock)

        self.browser_dock = BrowserDock(self.browser_vm, self)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.browser_dock)
        self._register_dock("Browser", self.browser_dock)

    def _register_dock(self, name: str, dock) -> None:
        """Track a dock so the Docks menu and layout autosave pick it up (call before menu build)."""
        self._docks[name] = dock

    # ---- File menu (built once; Open Workspace… persists across reloads) --
    def _init_file_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_action = file_menu.addAction("Open Workspace…")
        open_action.triggered.connect(self._on_open_workspace)

    def _on_open_workspace(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open Workspace", str(self._ws.root))
        if path:
            self.load_workspace(path)

    # ---- Docks menu (PUFFIN two-way-sync pattern) -------------------------
    def _init_docks_menu(self) -> None:
        self._docks_menu = self.menuBar().addMenu("&Docks")
        for name, dock in self._docks.items():
            action = self._docks_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(dock.isVisible())

            def _toggle(checked, d=dock):
                if checked:
                    d.show()
                    d.raise_()
                else:
                    d.hide()
                self._autosave()

            action.toggled.connect(_toggle)
            # Bind the action's own setChecked so Qt auto-drops the connection when the QAction is
            # destroyed (a Python closure capturing the action would outlive it and fire on a dead
            # C++ object during teardown). Same intent as PUFFIN's visibility handler, leak-safe.
            dock.visibilityChanged.connect(action.setChecked)

    # ---- load a different workspace ---------------------------------------
    def load_workspace(self, path) -> bool:
        """Swap the whole app to the workspace at ``path``: tear down the old VM/docks/bridge and
        rebuild fresh against the new one (no partial rebind). Returns ``True`` on success; an
        invalid path leaves the current workspace untouched and reports a status-bar message."""
        try:
            new_ws = Workspace.open(path)
        except WorkspaceNotFound as exc:
            self.statusBar().showMessage(f"Not a workspace: {path} ({exc})")
            return False

        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None

        self.menuBar().removeAction(self._docks_menu.menuAction())
        # removeAction() only detaches the menu; the QMenu (and its per-dock QActions, whose closures
        # capture the now-deleted docks) stay parented to the menu bar. Delete it so repeated
        # load_workspace calls don't leak one Docks menu per load.
        self._docks_menu.deleteLater()
        for dock in list(self._docks.values()):
            self.removeDockWidget(dock)
            dock.deleteLater()
        self._docks.clear()
        # The old viewmodels are QObjects parented to self; without an explicit deleteLater() they
        # would linger as inert children (their dock's connections already dropped above) for the
        # rest of the window's life. Drop them so repeated load_workspace calls don't accumulate.
        self._viewmodel.deleteLater()
        self.browser_vm.deleteLater()

        self._bind_workspace(new_ws)
        self._session.note_workspace(new_ws.root)
        self._autosave()
        return True

    # ---- watch -----------------------------------------------------------
    def _on_workspace_changed(self, changed) -> None:
        # The bridge already refreshed the facade on this (Qt) thread.
        self.log_dock.sync_warnings(self._ws.warnings)
        self._viewmodel.on_workspace_changed(changed)
        self.statusBar().showMessage(f"Updated: {', '.join(sorted(changed)) or '(none)'}")

    # ---- session layout --------------------------------------------------
    def _snapshot_session(self) -> Session:
        state = bytes(self.saveState().toBase64().data()).decode("ascii")
        self._session.dock_layout_b64 = state
        self._session.note_workspace(self._ws.root)
        return self._session

    def _restore_layout(self) -> None:
        b64 = self._session.dock_layout_b64
        if not b64:
            return
        try:
            self.restoreState(QByteArray.fromBase64(b64.encode("ascii")))
        except Exception as exc:  # noqa: BLE001 — a bad saved layout must never block the window
            self.log(f"could not restore dock layout ({exc})")

    def _autosave(self) -> None:
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.request()

    # ---- public helpers ---------------------------------------------------
    def log(self, msg: str) -> None:
        self.log_dock.append(msg)

    @property
    def viewmodel(self) -> ViewModel:
        return self._viewmodel

    # ---- teardown --------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._bridge is not None:
            self._bridge.stop()
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.flush_now()
        super().closeEvent(event)

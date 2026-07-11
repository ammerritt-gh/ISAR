"""MainWindow — the Phase-A application shell.

A :class:`QMainWindow` that opens one read-only workspace and wires the shell: a dock scaffold
(currently the log dock; component docks land in later tasks), a two-way-synced **Docks** menu, a
status bar, the watch->Qt bridge, and session-layout restore/autosave. It *wires*, it does not
compute — anything smarter belongs in a viewmodel (:mod:`isar_gui.viewmodels`).

The Docks-menu two-way sync (checkable action <-> dock ``visibilityChanged``) follows PUFFIN's
``view/main_window.py`` ``_init_menu_bar`` pattern (C:/Users/AMM/Documents/Github/PUFFIN).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtWidgets import QLabel, QMainWindow

from isar_gui.log_dock import LogDock
from isar_gui.session import Autosaver, Session, load_session
from isar_gui.viewmodels import ViewModel
from isar_gui.watch_bridge import WatchBridge

_PLACEHOLDER = "Open a workspace view from the Docks menu."


class MainWindow(QMainWindow):
    """The shell window over one :class:`~isar.workspace_api.Workspace`."""

    def __init__(self, ws, *, session: Session | None = None, watch: bool = True, parent=None):
        super().__init__(parent)
        self._ws = ws
        self._session = session if session is not None else load_session()
        self._session.note_workspace(ws.root)

        self.setWindowTitle(f"ISAR — {Path(ws.root).name}")
        self.resize(1200, 800)

        central = QLabel(_PLACEHOLDER)
        central.setContentsMargins(24, 24, 24, 24)
        self.setCentralWidget(central)

        # A base viewmodel so the watch signal already has a consumer; component viewmodels attach
        # to the same workspace in later tasks.
        self._viewmodel = ViewModel(ws, parent=self)

        self._docks: dict[str, object] = {}
        self._init_docks()
        self._init_menu_bar()

        self.statusBar().showMessage(f"Workspace: {ws.root}")
        self.log(f"Opened workspace {ws.root}")

        # Session autosave: debounced 500ms, snapshotting the live layout at fire time.
        self._autosaver = Autosaver(self._snapshot_session, parent=self)

        # Watch bridge: refresh + fan out on the Qt thread. Optional (tests may disable it).
        self._bridge = None
        if watch:
            self._bridge = WatchBridge(ws, parent=self)
            self._bridge.changed.connect(self._on_workspace_changed)
            self._bridge.start()

        # Restore the persisted dock layout after the event loop settles (QTimer.singleShot(0, ...)),
        # so all docks exist and their default geometry is applied first.
        QTimer.singleShot(0, self._restore_layout)

    # ---- dock scaffold ----------------------------------------------------
    def _init_docks(self) -> None:
        from PySide6.QtCore import Qt

        self.log_dock = LogDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self._register_dock("Log", self.log_dock)

    def _register_dock(self, name: str, dock) -> None:
        """Track a dock so the Docks menu and layout autosave pick it up (call before menu build)."""
        self._docks[name] = dock

    # ---- Docks menu (PUFFIN two-way-sync pattern) -------------------------
    def _init_menu_bar(self) -> None:
        docks_menu = self.menuBar().addMenu("&Docks")
        for name, dock in self._docks.items():
            action = docks_menu.addAction(name)
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

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

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QLabel, QMainWindow

from isar_gui.commands import RunOptions, build_run_command
from isar_gui.process import ProcessRunner

from isar.workspace_api import (
    SCHEMA_VERSION,
    BranchModelView,
    CardView,
    Workspace,
    WorkspaceNotFound,
)
from isar_gui.log_dock import LogDock
from isar_gui.session import Autosaver, Session, load_session
from isar_gui.viewmodels import (
    BrowserViewModel,
    DispersionViewModel,
    HeatmapViewModel,
    InspectorViewModel,
    ViewModel,
    WaterfallViewModel,
)
from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.card_dock import CardDock
from isar_gui.views.dispersion_dock import DispersionDock
from isar_gui.views.heatmap_dock import HeatmapDock
from isar_gui.views.inspector_widget import InspectorWidget
from isar_gui.views.waterfall_dock import WaterfallDock
from isar_gui.watch_bridge import WatchBridge

_EMPTY_HINT = "No workspace loaded.\n\nFile ▸ Open Workspace… to begin."


class _EmptyWorkspace:
    """Read-only null-object with the :class:`~isar.workspace_api.Workspace` read surface, all
    empty. Backs the empty-start state (``python -m isar_gui`` with no arg and no usable recent) so
    every dock/viewmodel renders its own honest "nothing yet" view — empty tree, "no fitted scans"
    planes — without threading ``None`` through and None-guarding each accessor. It reports empty
    models, never fake data: no scans, no card policy, no branch model."""

    root = None
    schema_version = SCHEMA_VERSION
    results: list = []
    groups: list = []

    def __init__(self):
        self.warnings: list = []

    @property
    def card(self) -> CardView:
        # state=None: CardView.lattice_epochs degrades to [] via getattr; no policy, no epochs.
        return CardView(policy=None, state=None)

    @property
    def branch_model(self) -> BranchModelView:
        return BranchModelView()

    def scan_data(self, scan_id):
        return None, None, None

    def evaluate_model(self, scan_id, detector_id, x_eval=None):
        raise KeyError(f"no fit for scan_id={scan_id!r} detector={detector_id!r}")

    def refresh(self) -> None:
        pass


class MainWindow(QMainWindow):
    """The shell window over one :class:`~isar.workspace_api.Workspace`."""

    def __init__(self, ws, *, session: Session | None = None, watch: bool = True,
                 watch_kwargs: dict | None = None, parent=None):
        super().__init__(parent)
        self._session = session if session is not None else load_session()
        self._watch_enabled = watch
        # Forwarded verbatim to WatchBridge -> Workspace.watch (debounce_ms/poll_interval_s/backend);
        # lets tests use a fast poll instead of the 1s production default.
        self._watch_kwargs = watch_kwargs or {}
        self.resize(1200, 800)

        self._docks: dict[str, object] = {}
        self._warn_generation = 0     # per-bridge-tick counter passed to LogDock.sync_warnings
        self._empty = False           # True while showing the empty-start (no workspace) state
        self.inspector = None         # the C2 central widget; None in the empty state
        self.setAcceptDrops(True)      # drop a data folder/numor onto the window -> New Experiment (D4)
        self._run_runner: ProcessRunner | None = None  # the live `isar run` subprocess, if any
        self._init_file_menu()
        self._init_run_menu()
        self._init_status_bar()
        self._bind_workspace(ws)
        if ws is not None:
            self._session.note_workspace(ws.root)

        # Session autosave: debounced 500ms, snapshotting the live layout at fire time.
        self._autosaver = Autosaver(self._snapshot_session, parent=self, on_error=self.log)

        # Restore the persisted dock layout after the event loop settles (QTimer.singleShot(0, ...)),
        # so all docks exist and their default geometry is applied first.
        QTimer.singleShot(0, self._restore_layout)

    # ---- workspace binding (shared by __init__ and load_workspace) --------
    def _bind_workspace(self, ws) -> None:
        """(Re)bind the window to ``ws``: title, viewmodels, dock scaffold, Docks menu, watch
        bridge. Assumes any previously bound docks/menu/bridge were already torn down by the caller.

        ``ws=None`` binds the EMPTY-START state: an :class:`_EmptyWorkspace` backs the viewmodels
        (so every dock renders its own empty view), the central widget is an "Open Workspace…" hint
        instead of the inspector, no watch bridge is started, and the status bar reads "no
        workspace". A later ``load_workspace`` from this state is a full bind from ``None``.
        """
        self._empty = ws is None
        self._ws = _EmptyWorkspace() if self._empty else ws
        ws = self._ws
        self._warn_generation = 0  # a fresh bind starts a new warnings-generation sequence
        if self._empty:
            self.setWindowTitle("ISAR — (no workspace)")
        else:
            self.setWindowTitle(f"ISAR — {Path(ws.root).name}")

        # A base viewmodel so the watch signal already has a consumer; component viewmodels attach
        # to the same workspace in later tasks.
        self._viewmodel = ViewModel(ws, parent=self)
        self.browser_vm = BrowserViewModel(ws, parent=self)
        self.inspector_vm = InspectorViewModel(ws, parent=self)
        self.heatmap_vm = HeatmapViewModel(ws, parent=self)
        self.dispersion_vm = DispersionViewModel(ws, parent=self)
        self.waterfall_vm = WaterfallViewModel(ws, parent=self)
        self.waterfall_vm.load_view_state(self._session)
        self.heatmap_vm.load_view_state(self._session)
        self.dispersion_vm.load_view_state(self._session)

        # The C2 fit inspector is the central widget; a browser selection drives it. In the empty
        # state there is nothing to inspect, so the central widget is an Open-Workspace hint instead.
        if self._empty:
            self.inspector = None
            self.setCentralWidget(self._make_empty_hint())
        else:
            self.inspector = InspectorWidget(self.inspector_vm, parent=self)
            self.setCentralWidget(self.inspector)
        self.browser_vm.selection_changed.connect(self.inspector_vm.set_fit)
        # The C3 plane, the C5 dispersion view, and the C4 waterfall/comparison view all route a
        # picked ω̂ point (or waterfall trace) to the same inspector.
        self.heatmap_vm.point_activated.connect(self.inspector_vm.set_fit)
        self.dispersion_vm.point_activated.connect(self.inspector_vm.set_fit)
        self.waterfall_vm.point_activated.connect(self.inspector_vm.set_fit)
        # A browser selection also nominates its q as the waterfall/comparison view's default q.
        self.browser_vm.selection_changed.connect(self.waterfall_vm.default_q_from_selection)

        self._docks = {}
        self._init_docks()
        self._init_docks_menu()

        if self._empty:
            self.statusBar().showMessage("No workspace loaded")
            self.log("Started with no workspace — File ▸ Open Workspace… to begin")
        else:
            self.statusBar().showMessage(f"Workspace: {ws.root}")
            self.log(f"Opened workspace {ws.root}")

        # Watch bridge: refresh + fan out on the Qt thread. Optional (tests may disable it). Never
        # started in the empty state — there is no workspace on disk to watch.
        self._bridge = None
        if self._watch_enabled and not self._empty:
            self._bridge = WatchBridge(ws, parent=self, **self._watch_kwargs)
            self._bridge.changed.connect(self._on_workspace_changed)
            self._bridge.start()
            self._live_label.setText("watching (no changes yet)")
        else:
            self._live_label.setText("")

        self._sync_run_actions()  # Run/Re-run enablement depends on empty-state + remembered runs

    def _make_empty_hint(self) -> QLabel:
        """The empty-start central widget: a centered "Open Workspace…" hint."""
        hint = QLabel(_EMPTY_HINT, self)
        hint.setObjectName("empty_hint")
        hint.setAlignment(Qt.AlignCenter)
        self._empty_hint = hint
        return hint

    # ---- dock scaffold ----------------------------------------------------
    def _init_docks(self) -> None:
        from PySide6.QtCore import Qt

        self.log_dock = LogDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self._register_dock("Log", self.log_dock)

        self.browser_dock = BrowserDock(self.browser_vm, self)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.browser_dock)
        self._register_dock("Browser", self.browser_dock)

        # The card editor (raw TOML, D2) shares the left area with the browser as a tabified sibling.
        self.card_dock = CardDock(self._ws, self)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.card_dock)
        self.tabifyDockWidget(self.browser_dock, self.card_dock)
        self._register_dock("Card", self.card_dock)

        self.heatmap_dock = HeatmapDock(self.heatmap_vm, self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.heatmap_dock)
        self._register_dock("Plane (q, E)", self.heatmap_dock)

        # The C5 dispersion view shares the right area with the plane (tabified siblings).
        self.dispersion_dock = DispersionDock(self.dispersion_vm, self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dispersion_dock)
        self.tabifyDockWidget(self.heatmap_dock, self.dispersion_dock)
        self._register_dock("Dispersion ω(q)", self.dispersion_dock)

        # The C4 waterfall/comparison view is a third tabified sibling in the same area.
        self.waterfall_dock = WaterfallDock(self.waterfall_vm, self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.waterfall_dock)
        self.tabifyDockWidget(self.heatmap_dock, self.waterfall_dock)
        self._register_dock("Waterfall / Comparison", self.waterfall_dock)

    def _register_dock(self, name: str, dock) -> None:
        """Track a dock so the Docks menu and layout autosave pick it up (call before menu build)."""
        self._docks[name] = dock
        # A user dragging a dock to a new area/float changes the layout saveState() would capture;
        # autosave (debounced) so that move survives a restart without waiting for an explicit toggle.
        dock.dockLocationChanged.connect(lambda _area: self._autosave())

    # ---- File menu (built once; actions persist across workspace reloads) --
    def _init_file_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        new_action = file_menu.addAction("New Experiment…")
        new_action.triggered.connect(lambda: self.open_new_experiment())
        open_action = file_menu.addAction("Open Workspace…")
        open_action.triggered.connect(self._on_open_workspace)

        file_menu.addSeparator()
        report_action = file_menu.addAction("Open Report")
        report_action.triggered.connect(self._open_report)
        export_menu = file_menu.addMenu("Export View as PNG")
        for label, key in (("Plane (q, E)", "Plane (q, E)"), ("Dispersion", "Dispersion ω(q)"),
                           ("Waterfall", "Waterfall / Comparison"), ("Inspector", "Inspector")):
            act = export_menu.addAction(label)
            act.triggered.connect(lambda _checked=False, k=key: self._export_view(k))

    def _on_open_workspace(self) -> None:
        start_dir = "" if self._ws.root is None else str(self._ws.root)
        path = QFileDialog.getExistingDirectory(self, "Open Workspace", start_dir)
        if path:
            self.load_workspace(path)

    # ---- New Experiment front door (wizard + drag-drop, D4) ---------------
    def open_new_experiment(self, prefill: str | None = None) -> None:
        """Open the New Experiment dialog (optionally pre-filled with a dropped data path). On a
        successful ``isar init`` the freshly-scaffolded workspace is loaded."""
        from isar_gui.new_experiment import NewExperimentDialog

        dlg = NewExperimentDialog(prefill, parent=self)
        if dlg.exec() and dlg.workspace_path:
            self.log(f"Created experiment {dlg.workspace_path}")
            self.load_workspace(dlg.workspace_path)

    @staticmethod
    def _is_workspace_dir(path: Path) -> bool:
        return path.is_dir() and (path / "card.policy.toml").exists()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        md = event.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()
        path = Path(urls[0].toLocalFile())
        # A dropped existing workspace opens directly; anything else is raw data -> New Experiment.
        if self._is_workspace_dir(path):
            self.load_workspace(str(path))
        else:
            self.open_new_experiment(prefill=str(path))

    # ---- Run menu (shell `isar run` as a subprocess, D3) ------------------
    def _init_run_menu(self) -> None:
        run_menu = self.menuBar().addMenu("&Run")
        self._run_action = run_menu.addAction("Run analysis…")
        self._run_action.triggered.connect(lambda: self.run_analysis())
        self._rerun_action = run_menu.addAction("Re-run (only new scans)")
        self._rerun_action.triggered.connect(self.rerun)

    def _sync_run_actions(self) -> None:
        """Runs need a real workspace and no run already in flight."""
        busy = self._run_runner is not None and self._run_runner.is_running()
        can_run = (not self._empty) and (not busy)
        self._run_action.setEnabled(can_run)
        # Re-run additionally needs a remembered previous invocation for this workspace.
        self._rerun_action.setEnabled(can_run and self._run_state() is not None)

    def run_analysis(self) -> None:
        """Open the Run dialog (pre-filled from the last run) and launch ``isar run`` on accept."""
        if self._empty or (self._run_runner is not None and self._run_runner.is_running()):
            return
        from isar_gui.run_dialog import RunDialog

        prev = self._run_state() or {}
        data = (prev.get("data") or [""])[0]
        opts = RunOptions(**prev["opts"]) if prev.get("opts") else RunOptions()
        dlg = RunDialog(data=data, opts=opts, parent=self)
        if not dlg.exec() or not dlg.data_paths:
            return
        self._launch_run(dlg.data_paths, dlg.options)

    def rerun(self) -> None:
        """Re-run the last invocation verbatim (idempotent — the engine only fits new scans)."""
        if self._empty or (self._run_runner is not None and self._run_runner.is_running()):
            return
        state = self._run_state()
        if state is None:
            return
        self._launch_run(state["data"], RunOptions(**state["opts"]))

    def _launch_run(self, data_paths, opts: RunOptions) -> None:
        ws_root = str(self._ws.root)
        cmd = build_run_command(sys.executable, data_paths, ws_root, opts)
        self._remember_run(data_paths, opts)
        # Retain each run's log next to the report so a finished/failed run is inspectable later.
        log_path = Path(ws_root) / "report" / "gui-runs" / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.log("$ " + " ".join(cmd))
        self._set_run_status("running…", "#b8860b")
        self._run_runner = ProcessRunner(self)
        self._run_runner.line.connect(self.log)
        self._run_runner.finished.connect(self._on_run_finished)
        self._run_runner.start(cmd, log_path=str(log_path))
        self._sync_run_actions()

    def _on_run_finished(self, code: int) -> None:
        if code == 0:
            self._set_run_status(f"run finished OK ({datetime.now():%H:%M:%S})", "#2e7d32")
            self.statusBar().showMessage("Run finished — results updated below")
        else:
            self._set_run_status(f"run FAILED (exit {code}) — see log", "#c62828")
            self.statusBar().showMessage(f"Run failed (exit {code})")
        self._sync_run_actions()

    def _set_run_status(self, text: str, color: str) -> None:
        self._run_status.setText(text)
        self._run_status.setStyleSheet(f"color: {color};")

    # ---- remembered run state (per workspace, in the GUI session) ----------
    def _run_state(self) -> dict | None:
        if self._empty:
            return None
        return self._session.view_state.get("run", {}).get(str(self._ws.root))

    def _remember_run(self, data_paths, opts: RunOptions) -> None:
        from dataclasses import asdict

        runs = self._session.view_state.setdefault("run", {})
        runs[str(self._ws.root)] = {"data": list(data_paths), "opts": asdict(opts)}
        self._autosave()

    # ---- Open report / Export view (Operator-Contract wins, D3) -----------
    def _open_report(self) -> bool:
        """Open ``<workspace>/report/report.html`` in the default browser. Returns success."""
        import webbrowser

        if self._empty:
            self.statusBar().showMessage("No workspace — nothing to open")
            return False
        report = Path(self._ws.root) / "report" / "report.html"
        if not report.exists():
            self.statusBar().showMessage("No report yet — run analysis first")
            return False
        webbrowser.open(report.as_uri())
        return True

    def _export_view(self, dock_key: str, path: str | None = None) -> bool:
        """Save the named plot view to a PNG (grabs exactly what is on screen). ``path`` bypasses the
        file dialog (tests). Returns success."""
        from isar_gui.plotspec_pg import save_widget_png

        if self._empty:
            self.statusBar().showMessage("No workspace — nothing to export")
            return False
        glw = self._glw_for(dock_key)
        if glw is None:
            self.statusBar().showMessage(f"No '{dock_key}' view to export")
            return False
        if path is None:
            default = f"{Path(self._ws.root).name}_{dock_key.split()[0].lower()}.png"
            path, _ = QFileDialog.getSaveFileName(self, "Export view as PNG", default,
                                                  "PNG image (*.png)")
            if not path:
                return False
        ok = save_widget_png(glw, path)
        self.statusBar().showMessage(f"Exported {path}" if ok else f"Export failed: {path}")
        return ok

    def _glw_for(self, dock_key: str):
        """The pyqtgraph graphics widget backing a named view, or ``None``."""
        if dock_key == "Inspector":
            return None if self.inspector is None else self.inspector._glw
        dock = self._docks.get(dock_key)
        return getattr(dock, "_glw", None) if dock is not None else None

    # ---- status bar: persistent live indicator ----------------------------
    def _init_status_bar(self) -> None:
        """A permanent status-bar label (distinct from the transient ``showMessage`` text) so "is it
        live" stays visible even after the transient "Updated: ..." message expires."""
        self._live_label = QLabel()
        self.statusBar().addPermanentWidget(self._live_label)
        # A persistent run-state indicator (colour-coded): survives the transient showMessage text so
        # a finished/failed run stays visible. Empty until the first run.
        self._run_status = QLabel()
        self.statusBar().addPermanentWidget(self._run_status)

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
        self.inspector_vm.deleteLater()
        self.heatmap_vm.save_view_state(self._session)
        self.heatmap_vm.deleteLater()
        self.dispersion_vm.save_view_state(self._session)
        self.dispersion_vm.deleteLater()
        self.waterfall_vm.save_view_state(self._session)
        self.waterfall_vm.deleteLater()
        # The central inspector widget is rebuilt against the new workspace in _bind_workspace;
        # setCentralWidget will reparent+delete the old one, but drop it explicitly so its VM
        # connection is gone before the fresh bind.
        old_inspector = self.takeCentralWidget()
        if old_inspector is not None:
            old_inspector.deleteLater()

        self._bind_workspace(new_ws)
        self._session.note_workspace(new_ws.root)
        self._autosave()
        return True

    # ---- watch -----------------------------------------------------------
    def _on_workspace_changed(self, changed) -> None:
        # The bridge already refreshed the facade on this (Qt) thread.
        self._warn_generation += 1
        self.log_dock.sync_warnings(self._ws.warnings, self._warn_generation)
        self._viewmodel.on_workspace_changed(changed)
        # "results"/"snapshot" carry the fit data the plots read; "card_state" carries lattice
        # epochs / dispersion prior / branch belief that feed the browser HKL, the dispersion prior
        # curve, and the heatmap overlays — so it must also re-derive the component views.
        if changed & {"results", "snapshot", "card_state"}:
            self.browser_vm.refresh()
            self.heatmap_vm.refresh()
            self.dispersion_vm.refresh()
            self.waterfall_vm.refresh()
            self._refresh_inspector_selection()
        self._live_label.setText(f"live — last update {datetime.now():%H:%M:%S}")
        self.statusBar().showMessage(f"Updated: {', '.join(sorted(changed)) or '(none)'}")

    def _refresh_inspector_selection(self) -> None:
        """Re-find the currently-inspected fit (by scan_number+detector_id — a FitView carries no
        stable identity across a rebuild) in the just-refreshed ``ws.results``; re-select it if it
        still exists, else drop the inspector to its empty state."""
        fit = self.inspector_vm.current_fit
        if fit is None:
            return
        fresh = next((fv for fv in self._ws.results
                     if fv.scan_number == fit.scan_number and fv.detector_id == fit.detector_id),
                    None)
        self.inspector_vm.set_fit(fresh)

    # ---- session layout --------------------------------------------------
    def _snapshot_session(self) -> Session:
        state = bytes(self.saveState().toBase64().data()).decode("ascii")
        self._session.dock_layout_b64 = state
        if not self._empty:
            self._session.note_workspace(self._ws.root)
        self.heatmap_vm.save_view_state(self._session)
        self.dispersion_vm.save_view_state(self._session)
        self.waterfall_vm.save_view_state(self._session)
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

    @property
    def is_empty(self) -> bool:
        """True while the window is in the empty-start (no workspace loaded) state."""
        return self._empty

    # ---- teardown --------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._bridge is not None:
            self._bridge.stop()
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.flush_now()
        super().closeEvent(event)

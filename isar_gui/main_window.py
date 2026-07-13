"""MainWindow — the Phase-A application shell.

A :class:`QMainWindow` that opens one read-only workspace and wires the shell: a dock scaffold (the
log dock and the C1 workspace browser; more component docks land in later tasks), a two-way-synced
**Docks** menu, a **File -> Open Workspace…** action, a status bar, the watch->Qt bridge, and
session-layout restore/autosave. It *wires*, it does not compute — anything smarter belongs in a
viewmodel (:mod:`isar_gui.viewmodels`).

The Docks-menu two-way sync (checkable action <-> dock ``visibilityChanged``) follows PUFFIN's
``view/main_window.py`` ``_init_menu_bar`` pattern (C:/Users/AMM/Documents/Github/Science/PUFFIN).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from weakref import WeakSet

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QToolBar,
    QToolButton,
)

from isar_gui.commands import RunOptions, build_run_command
from isar_gui.active_unit import ActiveUnitController
from isar_gui.folder_watcher import FolderWatcher
from isar_gui.gui_store import GuiStore
from isar_gui.health import HealthProbe, LLM_DEFAULT_ENDPOINT, TAVI_DEFAULT_URL, find_tavi_bat, ping_http
from isar_gui.operation_coordinator import WorkspaceOperationCoordinator
from isar_gui.workspace_manager import WorkspaceManager


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
    FilesViewModel,
    HeatmapViewModel,
    InspectorViewModel,
    ScansViewModel,
    ViewModel,
    WaterfallViewModel,
)
from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.card_dock import CardDock
from isar_gui.views.dispersion_dock import DispersionDock
from isar_gui.views.files_dock import FilesDock
from isar_gui.views.heatmap_dock import HeatmapDock
from isar_gui.views.inspector_widget import InspectorWidget
from isar_gui.views.scan_table_dock import ScanTableDock
from isar_gui.views.scans_dock import ScansDock
from isar_gui.views.waterfall_dock import WaterfallDock
from isar_gui.watch_bridge import WatchBridge

log = logging.getLogger(__name__)

_EMPTY_HINT = "No workspace loaded.\n\nFile ▸ Open Workspace… to begin."
_PENDING_HINT = "Drop data files to begin — they'll land in a temporary workspace."
# The log is useful context, not the primary empty-start surface.  Qt gives a lone bottom dock a
# surprisingly generous share of the window unless we establish a deliberate starting size.
_EMPTY_START_LOG_HEIGHT = 180


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
    # Phase D/E viewmodels read a scan ledger and a file list off the workspace; expose them empty
    # now (harmless) so a pending/empty bind never AttributeErrors when those consumers land.
    scans: list = []
    files: list = []

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
        self.setAcceptDrops(True)      # drop a data folder/numor onto the window -> ingest (D2/D5)
        self._close_when_operations_finish = False
        self._bound = False           # True once a first workspace binding exists (rebind guard)
        self._gui_store: GuiStore | None = None   # per-workspace GUI-only state (watched folders)
        # {resolved_path: (size, mtime)} of manifest files, rebuilt once per facade refresh (bind +
        # each "snapshot" watch tick). Lets the folder watcher's per-second is_known guard be an O(1)
        # dict lookup instead of resolving every manifest row every poll — the beamtime-scale freeze
        # fix. None until the first bind builds it.
        self._known_files_cache: dict[str, tuple] | None = None
        # One inspected atomic unit, deliberately separate from the table/chip multi-selection.
        # It survives ordinary workspace refreshes only while the same unit remains in the ledger.
        self.active_units = ActiveUnitController(self)

        # The workspace lifecycle owner (none/pending/ready + temp/save-as). MainWindow is the view:
        # it reflects the manager's signals and never reaches into a workspace directory itself. `ws`
        # passed at construction is adopted directly (no re-open, no signal); every later transition
        # flows through the manager's emitting methods.
        self._wsm = WorkspaceManager(self)
        self._wsm.workspace_changed.connect(self._on_wsm_workspace_changed)
        self._wsm.state_changed.connect(self._on_wsm_state_changed)
        if ws is not None:
            self._wsm.adopt(ws)

        # One workspace-wide writer gate for init/ingest/run.  Keep the historical attribute alias
        # because FilesDock consumes a controller-shaped object; it now points at the coordinator.
        self._operations = WorkspaceOperationCoordinator(sys.executable, self._wsm, self.log, parent=self)
        self._ingest_controller = self._operations
        self._operations.ingest_started.connect(self._on_ingest_started)
        self._operations.ingest_finished.connect(self._on_ingest_report)
        self._operations.ingest_failed.connect(self._on_ingest_failed)
        self._operations.run_started.connect(self._on_run_started)
        self._operations.run_finished.connect(self._on_run_finished)
        self._operations.activity_changed.connect(self._on_operation_activity_changed)

        # Watched-folder poller: settled new files feed the same controller queue. Lives across
        # switches; its folder set is re-seeded from each ready workspace's GuiStore.
        self._folder_watcher = FolderWatcher(is_known=self._is_file_known, parent=self)
        self._folder_watcher.files_stable.connect(self._on_watched_files_stable)
        if self._watch_enabled:
            self._folder_watcher.start()
        # Debounced save of the per-workspace GUI store (mirrors the session Autosaver, 500 ms).
        self._store_save_timer = QTimer(self)
        self._store_save_timer.setSingleShot(True)
        self._store_save_timer.setInterval(500)
        self._store_save_timer.timeout.connect(self._save_gui_store)

        self._init_file_menu()
        self._init_workspace_toolbar()
        self._init_run_menu()
        self._init_backends_menu()
        self._init_status_bar()
        self._init_health()
        # The Log dock is created once and PERSISTS across workspace rebinds — _teardown_binding
        # tears down every other dock, but wiping the log would lose the operator's scrollback, and
        # the drop-to-ingest bootstrap ends in a rebind (that was the "log clears on load" bug).
        # _init_docks only re-registers it into each fresh binding. Tracks docks whose
        # dockLocationChanged is already wired (WeakSet auto-drops deleted transient docks) so the
        # persistent log dock isn't double-connected on every rebind.
        self._loc_wired: WeakSet = WeakSet()
        self.log_dock = LogDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self._bind_workspace(ws)
        self._bound = True
        if ws is not None and not self._wsm.is_temporary():
            self._session.note_workspace(ws.root)
        self._sync_workspace_actions()

        # Session autosave: debounced 500ms, snapshotting the live layout at fire time.
        self._autosaver = Autosaver(self._snapshot_session, parent=self, on_error=self.log)

        # Restore the persisted dock layout after the event loop settles (QTimer.singleShot(0, ...)),
        # so all docks exist and their default geometry is applied first.  An empty start has a
        # deliberately simpler layout; apply that after restoration so an old full-workspace layout
        # cannot make Log dominate a first-time screen.
        QTimer.singleShot(0, self._restore_layout)
        QTimer.singleShot(0, self._apply_empty_start_dock_geometry)
        # Recovery adopts a temporary workspace directly, so there is no ingest-finished signal to
        # promote its raw scans. If it contains parsed rows but no fit results, treat Scans as the
        # next operator surface after the saved layout is restored. Saved workspaces and analyzed
        # temporary workspaces keep their layout exactly as the operator left it.
        if ws is not None and self._wsm.is_temporary() and self.scans_vm.rows() and not ws.results:
            self._queue_scans_promotion()

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
        self._update_title()
        # The Log dock persists across rebinds, so mark the workspace boundary in its scrollback and
        # reset the (now stale) per-tick warning bookkeeping carried over from the previous binding.
        self.log_dock.reset_warnings()
        _ws_label = "(no workspace)" if self._empty else Path(self._ws.root).name
        self.log_dock.append(f"── {datetime.now():%H:%M:%S} workspace: {_ws_label} ──")

        # A base viewmodel so the watch signal already has a consumer; component viewmodels attach
        # to the same workspace in later tasks.
        self._viewmodel = ViewModel(ws, parent=self)
        self.browser_vm = BrowserViewModel(ws, parent=self)
        self.inspector_vm = InspectorViewModel(ws, parent=self)
        self.heatmap_vm = HeatmapViewModel(ws, parent=self)
        self.dispersion_vm = DispersionViewModel(ws, parent=self)
        self.waterfall_vm = WaterfallViewModel(ws, parent=self)
        # The Files viewmodel is bound with no store yet; a ready bind loads the per-workspace
        # GuiStore below and calls set_store (empty/pending has no workspace file to persist into).
        self.files_vm = FilesViewModel(ws, store=None, parent=self)
        self.files_vm.watch_config_changed.connect(self._on_watch_config_changed)
        # The Scans viewmodel (physical chips + table) shares the same GuiStore for manual groups; it
        # binds store=None now and gets set_store on a ready bind like the Files VM. Group mutations
        # persist (debounced) through the same store save timer; a double-clicked fitted scan routes
        # to the C2 inspector.
        self.scans_vm = ScansViewModel(ws, store=None, parent=self)
        self.scans_vm.inspect_requested.connect(self._route_inspect)
        self.scans_vm.groups_changed.connect(self._on_groups_changed)
        self.scans_vm.store_dirty.connect(self._on_groups_changed)
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
        # Every route that changes what is being inspected passes through the active-unit controller.
        # This keeps the Results tree, physical chips, and table honest about the one detector in the
        # central inspector, without treating a multi-selection as an inspection request.
        self.browser_vm.selection_changed.connect(self._inspect_fit)
        # The C3 plane, the C5 dispersion view, and the C4 waterfall/comparison view all route a
        # picked ω̂ point (or waterfall trace) to the same inspector.
        self.heatmap_vm.point_activated.connect(self._inspect_fit)
        self.dispersion_vm.point_activated.connect(self._inspect_fit)
        self.waterfall_vm.point_activated.connect(self._inspect_fit)
        # A browser selection also nominates its q as the waterfall/comparison view's default q.
        self.browser_vm.selection_changed.connect(self.waterfall_vm.default_q_from_selection)

        self._docks = {}
        self._init_docks()
        self._init_docks_menu()

        # ``restoreState`` can only restore docks which exist at the time it runs.  A first launch
        # may deliberately bind the sparse empty-start scaffold, then open a real workspace later
        # in the same session.  Reapply the saved layout after that ready bind creates the complete
        # dock set.  Conversely, every none/pending rebind gets its compact data-entry geometry,
        # not just the constructor's initial bind.
        if self._empty:
            QTimer.singleShot(0, self._apply_empty_start_dock_geometry)
        elif self._bound and self._session.dock_layout_b64:
            QTimer.singleShot(0, self._restore_layout)

        if self._empty:
            if self._wsm.state() == "pending":
                self.statusBar().showMessage("Temporary workspace — drop data files to begin")
                self.log(f"Reserved temporary workspace {self._wsm.root()} — drop data to begin")
            else:
                self.statusBar().showMessage("No workspace loaded")
                self.log("Started with no workspace — File ▸ Open Workspace… to begin")
        else:
            self.statusBar().showMessage(f"Workspace: {ws.root}")
            self.log(f"Opened workspace {ws.root}")

        # Index the manifest for the folder watcher's is_known guard before seeding the watcher (its
        # set_folders triggers a poll that consults the guard).
        self._rebuild_known_files_cache()

        # Per-workspace GUI store (watched folders) — only for a ready workspace (the file lives in
        # the workspace dir). An empty/pending bind clears any watcher folders from the last binding.
        if not self._empty:
            self._gui_store = GuiStore.load(ws.root)
            self.files_vm.set_store(self._gui_store)
            self.scans_vm.set_store(self._gui_store)
            # Seed the watcher from the persisted config directly (a bind is not a user mutation, so
            # it must not re-persist the store; user add/remove/pause go through watch_config_changed).
            self._folder_watcher.configure(
                self.files_vm.watched(), self.files_vm.watch_paused(), reset_binding=True)
        else:
            self._gui_store = None
            self._folder_watcher.configure([], False, reset_binding=True)

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
        """The empty central widget: a centered hint. Pending (a temp workspace is reserved) invites
        a drop; a true none-state points at Open Workspace…."""
        text = _PENDING_HINT if self._wsm.state() == "pending" else _EMPTY_HINT
        hint = QLabel(text, self)
        hint.setObjectName("empty_hint")
        hint.setAlignment(Qt.AlignCenter)
        self._empty_hint = hint
        return hint

    def _update_title(self) -> None:
        """``ISAR — <name>`` for a saved workspace; ``ISAR — Unsaved workspace ●`` while temporary
        (pending or a not-yet-saved temp); ``ISAR — (no workspace)`` for the none-state."""
        if self._wsm.is_temporary():
            self.setWindowTitle("ISAR — Unsaved workspace ●")
        elif self._empty:
            self.setWindowTitle("ISAR — (no workspace)")
        else:
            self.setWindowTitle(f"ISAR — {Path(self._ws.root).name}")

    # ---- dock scaffold ----------------------------------------------------
    def _init_docks(self) -> None:
        from PySide6.QtCore import Qt

        # The Log dock is created once in __init__ and survives rebinds; only re-register it into
        # this binding's docks map + Docks menu (do NOT recreate it — that wiped the scrollback).
        self._register_dock("Log", self.log_dock)

        # The Files dock is the data entry point (drop target + manifest + watched folders); it leads
        # the left area so it is the first thing an operator sees on an empty/pending workspace.
        self.files_dock = FilesDock(self.files_vm, self._ingest_controller, self)
        self._folder_watcher.status_changed.connect(self.files_dock.set_watch_status)
        self.files_dock.set_watch_status(self._folder_watcher.status())
        self.addDockWidget(Qt.LeftDockWidgetArea, self.files_dock)
        self._register_dock("Files", self.files_dock)

        if self._empty:
            # Before a workspace exists, results, card, scan, and plot docks are not merely empty:
            # they describe concepts the operator cannot act on yet.  Keep just the data-entry dock
            # visible, matching the useful New Workspace pending state.  A ready bind rebuilds the
            # complete normal dock scaffold below.
            self.files_dock.show()
            self.files_dock.raise_()
            return

        self.browser_dock = BrowserDock(
            self.browser_vm, self, unit_key_for_fit=self._unit_key_for_fit)
        self.active_units.active_changed.connect(self.browser_dock.set_active_key)
        self.browser_dock.set_active_key(self.active_units.key)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.browser_dock)
        self.tabifyDockWidget(self.files_dock, self.browser_dock)
        self._register_dock("Browser", self.browser_dock)

        # The card editor (raw TOML, D2) shares the left area with the browser as a tabified sibling.
        self.card_dock = CardDock(self._ws, self)
        # The card remains editable during a run/ingest, but saving/reloading it must not race the
        # engine's on-disk policy read.  The dock owns the precise button state.
        self.card_dock.set_operation_busy(self._operations.is_busy())
        generation = self._wsm.generation()
        root = self._wsm.root()
        self.card_dock.saved.connect(
            lambda path, g=generation, r=root: self._on_card_saved(path, g, r))
        self.addDockWidget(Qt.LeftDockWidgetArea, self.card_dock)
        self.tabifyDockWidget(self.browser_dock, self.card_dock)
        self._register_dock("Card", self.card_dock)

        # The Phase-E physical scan view + its tabular twin, tabified together in the right area (they
        # share one model + selection, so a chip and a row light up together). Placed before the plane
        # so the scan-centric views lead the right column.
        self.scans_dock = ScansDock(self.scans_vm, self)
        self.active_units.active_changed.connect(self.scans_dock.set_active_key)
        self.scans_dock.set_active_key(self.active_units.key)
        self.addDockWidget(Qt.RightDockWidgetArea, self.scans_dock)
        self._register_dock("Scans", self.scans_dock)
        self.scan_table_dock = ScanTableDock(self.scans_vm, self)
        self.active_units.active_changed.connect(self.scans_vm.model.set_active_key)
        self.scans_vm.model.set_active_key(self.active_units.key)
        self.addDockWidget(Qt.RightDockWidgetArea, self.scan_table_dock)
        self.tabifyDockWidget(self.scans_dock, self.scan_table_dock)
        self._register_dock("Scan Table", self.scan_table_dock)

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

    def _apply_empty_start_dock_geometry(self) -> None:
        """Give the no-workspace/start-new screen a compact Log dock.

        This is intentionally conditional: a scientist who has opened a real workspace retains
        their saved layout.  In the none/pending state the useful first action is adding data, so
        Files remains raised and Log gets a modest diagnostic height rather than most of the window.
        The queued call runs after ``restoreState`` at startup and also covers later ``New`` binds.
        """
        if not self._empty:
            return
        self.files_dock.show()
        self.files_dock.raise_()
        self.resizeDocks([self.log_dock], [_EMPTY_START_LOG_HEIGHT], Qt.Vertical)

    def _register_dock(self, name: str, dock) -> None:
        """Track a dock so the Docks menu and layout autosave pick it up (call before menu build)."""
        self._docks[name] = dock
        # A user dragging a dock to a new area/float changes the layout saveState() would capture;
        # autosave (debounced) so that move survives a restart without waiting for an explicit toggle.
        # Guard against double-wiring: the persistent Log dock is re-registered on every rebind, and
        # a second connection would fire _autosave twice per move.
        if dock not in self._loc_wired:
            dock.dockLocationChanged.connect(lambda _area: self._autosave())
            self._loc_wired.add(dock)

    # ---- File menu (built once; actions persist across workspace reloads) --
    def _init_file_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._new_workspace_menu_action = file_menu.addAction("New Workspace")
        self._new_workspace_menu_action.triggered.connect(self.new_workspace)
        self._open_workspace_menu_action = file_menu.addAction("Open Workspace…")
        self._open_workspace_menu_action.triggered.connect(self._on_open_workspace)
        self._save_as_menu_action = file_menu.addAction("Save Workspace As…")
        self._save_as_menu_action.triggered.connect(self.save_workspace_as)

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

    # ---- Workspace toolbar (New / Open / Save As / Recent, Phase C) --------
    def _init_workspace_toolbar(self) -> None:
        """The always-present Workspace toolbar. ``objectName`` is set so ``QMainWindow.saveState``
        round-trips it in the session layout."""
        tb = QToolBar("Workspace", self)
        tb.setObjectName("workspace_toolbar")
        self.addToolBar(tb)

        self._new_workspace_action = tb.addAction("New")
        self._new_workspace_action.triggered.connect(self.new_workspace)
        self._open_workspace_action = tb.addAction("Open…")
        self._open_workspace_action.triggered.connect(self._on_open_workspace)
        self._save_as_action = tb.addAction("Save As…")
        self._save_as_action.triggered.connect(self.save_workspace_as)

        self._recents_button = QToolButton(self)
        self._recents_button.setText("Recent ▾")
        self._recents_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recents_menu = QMenu(self._recents_button)
        self._recents_button.setMenu(self._recents_menu)
        tb.addWidget(self._recents_button)
        self._refresh_recents_menu()

    def _refresh_recents_menu(self) -> None:
        """Rebuild the recents dropdown from the GUI session's recent-workspaces list."""
        self._recents_menu.clear()
        recents = self._session.recent_workspaces
        if not recents:
            act = self._recents_menu.addAction("(no recent workspaces)")
            act.setEnabled(False)
            return
        for path in recents:
            act = self._recents_menu.addAction(path)
            act.triggered.connect(lambda _checked=False, p=path: self.load_workspace(p))

    def _sync_workspace_actions(self) -> None:
        """Save As is meaningful only for a temporary workspace with a card already scaffolded
        (state ready). A saved workspace has nothing to Save As; a pending/none one has no card."""
        busy = getattr(self, "_operations", None) is not None and self._operations.is_busy()
        can_save_as = self._wsm.state() == "ready" and self._wsm.is_temporary() and not busy
        for action in (getattr(self, "_save_as_action", None),
                       getattr(self, "_save_as_menu_action", None)):
            if action is not None:
                action.setEnabled(can_save_as)
        for action in (getattr(self, "_new_workspace_action", None),
                       getattr(self, "_open_workspace_action", None),
                       getattr(self, "_new_workspace_menu_action", None),
                       getattr(self, "_open_workspace_menu_action", None)):
            if action is not None:
                action.setEnabled(not busy)
        if getattr(self, "_recents_button", None) is not None:
            self._recents_button.setEnabled(not busy)

    # ---- workspace lifecycle actions (delegate to the manager) ------------
    def new_workspace(self) -> None:
        """Reserve a fresh temporary workspace and bind it (pending state)."""
        if self._operations.is_busy():
            self.statusBar().showMessage("Workspace is busy — cancel or wait for the current operation")
            return
        if not self._resolve_dirty_card("start a new workspace"):
            return
        if not self._resolve_temporary_workspace("start a new workspace"):
            return
        self._wsm.new_temp()

    def save_workspace_as(self) -> bool:
        """Copy the current temporary workspace to a chosen directory and open it there. Returns
        whether a save actually happened (dialog cancelled or the copy failing yields ``False``)."""
        if self._operations.is_busy():
            self.statusBar().showMessage("Workspace is busy — Save As is unavailable during analysis or ingest")
            return False
        if not self._resolve_dirty_card("save this workspace elsewhere"):
            return False
        if not (self._wsm.state() == "ready" and self._wsm.is_temporary()):
            self.statusBar().showMessage("Save As is only for an unsaved temporary workspace")
            return False
        dest = QFileDialog.getExistingDirectory(self, "Save Workspace As (choose an empty folder)")
        if not dest:
            return False
        # Flush a pending GUI-store write into the temp workspace BEFORE it is copied, so the saved
        # copy carries the operator's latest watched-folders/groups rather than a stale snapshot.
        self._flush_gui_store_if_pending()
        if self._wsm.save_as(dest):
            self.statusBar().showMessage(f"Saved workspace to {dest}")
            return True
        reason = self._wsm.last_error() or "folder must be empty and writable"
        self.statusBar().showMessage(f"Could not save to {dest}: {reason}")
        self.log(f"Save As failed for {dest}: {reason}")
        return False

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
        paths = [u.toLocalFile() for u in urls]
        # A single dropped existing workspace opens directly. Anything else is raw data handed to the
        # IngestController, which routes by workspace state (none -> reserve temp + bootstrap;
        # pending -> bootstrap; ready -> ingest) and serializes everything behind one FIFO queue.
        first = Path(paths[0])
        if len(paths) == 1 and self._is_workspace_dir(first):
            self.load_workspace(str(first))
            return
        self._ingest_controller.request_ingest(paths)

    # ---- ingest-controller signal slots -----------------------------------
    def _on_ingest_started(self, paths) -> None:
        n = len(paths)
        self.statusBar().showMessage(f"Ingesting {n} path{'s' if n != 1 else ''}…")
        dock = getattr(self, "card_dock", None)
        if dock is not None and dock.is_dirty:
            self.log(
                "Ingest started while the Card has unsaved edits — parsing uses the on-disk "
                "card.policy.toml, not the visible draft")

    def _on_ingest_report(self, report) -> None:
        """A completed ingest: informational status + log (deduped is a cue, never modal), and a
        transient highlight of the files that produced added/updated scans."""
        # Parse errors are a non-terminal ingest outcome: preserve the manifest evidence, but let
        # the watched-folder policy retry the unchanged physical file on its bounded backoff.
        self._folder_watcher.record_ingest_report(report)
        # Do not wait for WatchBridge to notice the just-written snapshot.  In particular a window
        # constructed with watch=False otherwise keeps its cached empty ledger after a successful
        # ingest, making the new workspace appear to contain no scans.  The facade is read-only;
        # refreshing merely drops its cache before the ordinary viewmodel fan-out.
        if not self._empty:
            self._ws.refresh()
            self._on_workspace_changed({"snapshot"})

        n_files = len(report.files)
        a, u, d = len(report.added), len(report.updated), len(report.deduped)
        failed = [f for f in report.files if f.get("error")]
        note = f" — {len(failed)} file(s) failed to parse" if failed else ""
        scan_rows = self.scans_vm.rows() if not self._empty else []
        parsed_waiting = sum(1 for unit in scan_rows if unit.status == "ingested")
        if parsed_waiting:
            self.statusBar().showMessage(
                f"Ingested {n_files} file(s): {parsed_waiting} parsed scan(s) awaiting analysis "
                f"; open Scans or choose Run > Run analysis...{note}")
        else:
            self.statusBar().showMessage(
                f"Ingested {n_files} files: +{a} scans, ~{u} updated ({d} duplicates){note}")
        if parsed_waiting:
            # A pending bootstrap has just rebuilt the full dock set and queued restoration of the
            # operator's saved layout. Promote Scans on the following Qt turn so that restoration
            # cannot bury the newly-loaded data. Capture the lifecycle generation/root: if the user
            # opens another workspace before this callback runs, it must not touch the new binding.
            self._queue_scans_promotion()
        for dedup in report.deduped:
            self.log(f"dedup: {dedup.get('scan')} kept {dedup.get('kept')} "
                     f"(superseded {', '.join(dedup.get('superseded') or [])})")
        for f in failed:
            self.log(f"parse failed: {f.get('path')} — {f.get('error')}")
        # Highlight the files whose scans were newly added/updated by this ingest.
        changed_ids = set(report.added) | set(report.updated)
        added_paths = [f.get("path") for f in report.files
                       if set(f.get("scan_ids") or []) & changed_ids]
        dock = self._docks.get("Files")
        if dock is not None and added_paths:
            dock.highlight_added(added_paths)

    def _queue_scans_promotion(self) -> None:
        """Queue Scans promotion behind any layout restore for the current workspace binding."""
        generation = self._wsm.generation()
        root = self._wsm.root()
        QTimer.singleShot(
            0, lambda g=generation, r=root: self._promote_scans_if_current(g, r))

    def _promote_scans_if_current(self, generation: int, root: Path | None) -> None:
        """Show parsed scans after layout restoration, if still on the captured workspace."""
        if generation != self._wsm.generation() or root != self._wsm.root() or self._empty:
            return
        if not self.scans_vm.rows():
            return
        dock = self._docks.get("Scans")
        if dock is not None:
            dock.show()
            dock.raise_()

    def _on_ingest_failed(self, code: int, cause: str) -> None:
        self.statusBar().showMessage(f"Could not ingest data: {cause}")
        self.log(f"ingest failed (exit {code}): {cause}")
        # A real failure (exit != 0) must be visible on screen, never buried in a hidden dock
        # (operator contract). Code 0 is the informational "queued batch dropped" note — not a
        # failure, so it does not force the dock up.
        if code != 0:
            self.log_dock.show()
            self.log_dock.raise_()

    def _on_watched_files_stable(self, paths) -> None:
        """A watched folder settled some new files — enqueue them for ingest (serialized)."""
        if paths:
            self.log(f"watched folder: {len(paths)} new file(s) settled — queuing ingest")
            self._ingest_controller.request_ingest(list(paths))

    def _on_watch_config_changed(self) -> None:
        """A watched-folder mutation (add/remove/pause) from the dock: re-seed the folder watcher
        immediately and debounce-persist the GUI store. (A bind seeds the watcher directly and does
        NOT route through here, so opening a workspace never re-persists an unchanged store.)"""
        self._folder_watcher.configure(self.files_vm.watched(), self.files_vm.watch_paused())
        if self._gui_store is not None:
            self._store_save_timer.start()

    def _on_groups_changed(self) -> None:
        """A manual-group mutation or ui-scrap change (order/collapsed) from the Scans dock:
        debounce-persist the GUI store (the same store the watched folders live in)."""
        if self._gui_store is not None:
            self._store_save_timer.start()

    def _save_gui_store(self) -> None:
        if self._gui_store is not None:
            self._gui_store.save()

    def _flush_gui_store_if_pending(self) -> None:
        """Flush a pending debounced GUI-store write NOW and disarm the save timer.

        Called before any store swap (a rebind/load/Save As) and on close, so a mutation made <500 ms
        earlier is not lost when ``_gui_store`` is replaced and the armed timer would otherwise re-save
        into the *new* store. Guarded against the discard path: after ``discard_temp`` rmtree's the
        temp dir, ``GuiStore.save``'s ``mkdir(parents=True)`` would resurrect it — so only write when
        the outgoing store's root still exists. The timer is stopped unconditionally.
        """
        timer = getattr(self, "_store_save_timer", None)
        if timer is None:
            return
        pending = timer.isActive()
        timer.stop()
        if pending and self._gui_store is not None and Path(self._gui_store.root).exists():
            self._save_gui_store()

    def _rebuild_known_files_cache(self) -> None:
        """Rebuild the ``{resolved_path: (size, mtime)}`` index the folder watcher's ``is_known`` guard
        consults. Built once per facade refresh (a bind and each "snapshot" watch tick) so the guard
        never resolves every manifest row on every poll — resolving the manifest at beamtime scale on
        the Qt thread every second is the freeze this avoids."""
        cache: dict[str, tuple] = {}
        if not self._empty:
            for fv in self._ws.files:
                if getattr(fv, "error", None):
                    # A failed manifest row is deliberately *not* known: the watcher owns a bounded
                    # retry loop for unchanged files, while successful rows suppress duplicate work.
                    continue
                try:
                    cache[str(Path(fv.path).resolve())] = (fv.size, fv.mtime)
                except (OSError, AttributeError) as exc:
                    log.debug("skipping manifest row while indexing known files: %s", exc)
                    continue
        self._known_files_cache = cache

    def _is_file_known(self, path: str, size, mtime) -> bool:
        """Cheap O(1) guard for the folder watcher: True if ``path`` is already in the manifest with
        the same (size, mtime) — a file unchanged since ingest. Reads the cached manifest index (built
        once per facade refresh); only the single candidate ``path`` is resolved here, never the
        manifest. Engine idempotency is the real guarantee; this just avoids re-queuing a file the
        ledger already holds identically."""
        cache = self._known_files_cache
        if not cache:
            return False
        try:
            rp = str(Path(path).resolve())
        except OSError as exc:
            log.debug("could not resolve watched path %s: %s", path, exc)
            return False
        known = cache.get(rp)
        return known is not None and known == (size, mtime)

    # ---- workspace-manager signal slots (the window reflects the manager) --
    def _on_wsm_workspace_changed(self, ws) -> None:
        """The bound workspace changed (open / new_temp / bootstrap / save_as). Rebind the docks to
        it (``None`` for the empty/pending state), refresh recents (saved workspaces only), and
        re-sync the action enablement."""
        self._rebind(ws)
        if ws is not None and not self._wsm.is_temporary():
            self._session.note_workspace(ws.root)
            self._refresh_recents_menu()
        self._autosave()

    def _on_wsm_state_changed(self, _state: str) -> None:
        self._update_title()
        self._sync_workspace_actions()

    def _rebind(self, ws) -> None:
        """Tear down the current binding (if any) and bind ``ws`` (``None`` = empty/pending)."""
        if self._bound:
            # Unit keys are workspace-local.  Clear before the old dock QObject instances disappear
            # so every visible surface is reset rather than carrying an old workspace's inspection.
            self.active_units.set_active(None)
            self._teardown_binding()
        self._bind_workspace(ws)
        self._bound = True

    # ---- Run menu (shell `isar run` as a subprocess, D3) ------------------
    def _init_run_menu(self) -> None:
        run_menu = self.menuBar().addMenu("&Run")
        self._run_action = run_menu.addAction("Run analysis…")
        self._run_action.triggered.connect(lambda: self.run_analysis())
        self._rerun_action = run_menu.addAction("Re-run (only new scans)")
        self._rerun_action.triggered.connect(self.rerun)
        self._cancel_operation_action = run_menu.addAction("Cancel active operation")
        self._cancel_operation_action.triggered.connect(self._cancel_active_operation)

    def _sync_run_actions(self) -> None:
        """Runs need a real workspace and the workspace-wide writer gate to be idle."""
        busy = self._operations.is_busy()
        can_run = (not self._empty) and (not busy)
        self._run_action.setEnabled(can_run)
        # Re-run additionally needs a remembered previous invocation for this workspace.
        self._rerun_action.setEnabled(can_run and self._run_state() is not None)
        self._cancel_operation_action.setEnabled(busy)

    def run_analysis(self) -> None:
        """Open the Run dialog (pre-filled from the last run) and launch ``isar run`` on accept."""
        if self._empty or self._operations.is_busy():
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
        if self._empty or self._operations.is_busy():
            return
        state = self._run_state()
        if state is None:
            return
        self._launch_run(state["data"], RunOptions(**state["opts"]))

    def _launch_run(self, data_paths, opts: RunOptions) -> None:
        # A run reads the on-disk card.  Do not let a scientist believe their visible draft was
        # used; Save / Discard / Cancel makes that boundary explicit.
        if not self._resolve_dirty_card("start analysis"):
            return
        self._remember_run(data_paths, opts)
        self._operations.request_run(data_paths, opts, env=self._llm_env())
        self._sync_run_actions()

    def _on_run_started(self) -> None:
        self._set_run_status("running…", "#b8860b")

    def _on_run_finished(self, code: int, live_workspace: bool) -> None:
        if not live_workspace:
            self.log("stale run completion ignored — workspace changed")
            return
        if code == 0:
            self._set_run_status(f"run finished OK ({datetime.now():%H:%M:%S})", "#2e7d32")
            self.statusBar().showMessage("Run finished — results updated below")
        else:
            self._set_run_status(f"run FAILED (exit {code}) — see log", "#c62828")
            self.statusBar().showMessage(f"Run failed (exit {code})")
        self._sync_run_actions()

    def _cancel_active_operation(self) -> None:
        if self._operations.cancel():
            self.statusBar().showMessage("Cancelling active workspace operation…")

    def _on_operation_activity_changed(self, state: dict) -> None:
        kind = state["kind"]
        queued = state["queued"]
        if kind is None:
            self._operation_status.setText("")
            if self._close_when_operations_finish:
                self._close_when_operations_finish = False
                QTimer.singleShot(0, self.close)
        else:
            verb = "Cancelling" if state["cancelling"] else ("Ingesting" if kind == "ingest" else "Analysing")
            suffix = f" + {queued} queued" if queued else ""
            self._operation_status.setText(f"{verb}{suffix}")
        dock = getattr(self, "card_dock", None)
        if dock is not None:
            dock.set_operation_busy(kind is not None)
        self._sync_run_actions()
        self._sync_workspace_actions()

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

    # ---- Backends: LLM settings + TAVI launch + health (D1/D3) ------------
    def _init_backends_menu(self) -> None:
        menu = self.menuBar().addMenu("&Backends")
        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self._open_settings)
        self._launch_tavi_action = menu.addAction("Launch TAVI")
        self._launch_tavi_action.triggered.connect(self._launch_tavi)

    def _settings(self) -> dict:
        """GUI-owned backend settings (LLM profile/endpoint/model/key + TAVI bat), persisted in the
        session. Defaults keep LLMM as the default profile — never assumed reachable."""
        defaults = {"profile": "llmm", "llm_endpoint": LLM_DEFAULT_ENDPOINT, "llm_model": "",
                    "llm_api_key": "", "tavi_bat": ""}
        return {**defaults, **self._session.view_state.get("settings", {})}

    def _open_settings(self) -> None:
        from isar_gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self._settings(), parent=self)
        if dlg.exec():
            self._session.view_state["settings"] = dlg.settings
            self._autosave()
            self.log("Backend settings updated")
            self._refresh_health()

    def _llm_env(self) -> dict:
        """The ``ISAR_LLM_*`` env overrides for ``isar run``, from the current settings (D3). Only
        set keys are exported, so an unconfigured field keeps the engine/card default."""
        s = self._settings()
        env = {}
        if s.get("profile"):
            env["ISAR_LLM_PROFILE"] = s["profile"]
        if s.get("llm_endpoint"):
            env["ISAR_LLM_ENDPOINT"] = s["llm_endpoint"]
        if s.get("llm_model"):
            env["ISAR_LLM_MODEL"] = s["llm_model"]
        if s.get("llm_api_key"):
            env["ISAR_LLM_API_KEY"] = s["llm_api_key"]
        return env

    def _launch_tavi(self) -> bool:
        """Start TAVI's own ``run-tavi-dev.bat`` detached (ISAR does not own TAVI's lifecycle, D1).
        Returns whether a launcher was found and started."""
        from PySide6.QtCore import QProcess

        bat = find_tavi_bat(self._settings().get("tavi_bat"))
        if not bat:
            self.statusBar().showMessage("TAVI not found — set run-tavi-dev.bat in Backends ▸ Settings")
            return False
        QProcess.startDetached("cmd.exe", ["/c", "start", "", bat])
        self.statusBar().showMessage(f"Launched TAVI: {bat}")
        self._health_start_timer.start(4000)  # give the server a moment, then re-probe
        return True

    # ---- health probe (threaded; never blocks the UI) ---------------------
    def _init_health(self) -> None:
        # The worker owns no Qt objects. A timer on this thread polls its Python queue instead, so a
        # late daemon result after the window is deleted is harmless.
        self._health_probe = HealthProbe(lambda url: ping_http(url))
        self._health_generation = 0
        self._health_closed = False
        self._health_statuses: dict[str, str] = {}
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(30000)
        self._health_timer.timeout.connect(self._refresh_health)
        self._health_timer.start()
        self._health_poll_timer = QTimer(self)
        self._health_poll_timer.setInterval(50)
        self._health_poll_timer.timeout.connect(self._poll_health_results)
        self._health_start_timer = QTimer(self)
        self._health_start_timer.setSingleShot(True)
        self._health_start_timer.timeout.connect(self._refresh_health)
        self._health_start_timer.start(0)

    def _refresh_health(self) -> None:
        if self._health_closed:
            return
        llm_url = self._settings().get("llm_endpoint") or LLM_DEFAULT_ENDPOINT
        self._health_generation = self._health_probe.request({"llm": llm_url,
                                                               "tavi": TAVI_DEFAULT_URL})
        if not self._health_poll_timer.isActive():
            self._health_poll_timer.start()

    def _poll_health_results(self) -> None:
        """Apply only the latest endpoint snapshot, on the GUI thread."""
        if self._health_closed:
            return
        for result in self._health_probe.poll():
            if result.generation != self._health_generation:
                log.debug("ignored stale health probe generation %s", result.generation)
                continue
            if result.error:
                self._show_health_error(result.error)
                continue
            assert result.health is not None
            self._apply_health({name: (item.ok, item.reachable, item.detail, result.urls[name])
                                for name, item in result.health.items()})
        if not self._health_probe.in_flight:
            self._health_poll_timer.stop()

    def _show_health_error(self, detail: str) -> None:
        message = f"Backend health check failed: {detail}"
        self.log(message)
        self.statusBar().showMessage(message)

    def _apply_health(self, res: dict) -> None:
        if "error" in res:
            self._show_health_error(str(res["error"]))
            return
        for key, label in (("llm", self._llm_health), ("tavi", self._tavi_health)):
            item = res[key]
            # Accept the pre-HealthProbe 3-tuple used by a few external/plugin callers.
            if len(item) == 3:
                ok, detail, url = item
                reachable = ok
            else:
                ok, reachable, detail, url = item
            name = "LLM" if key == "llm" else "TAVI"
            label.setText(f"{name} ●")
            color = "#2e7d32" if ok else ("#b26a00" if reachable else "#c62828")
            label.setStyleSheet(f"color: {color};")
            label.setToolTip(f"{url} — {detail}")
            status = "ok" if ok else ("reachable_error" if reachable else "down")
            previous = self._health_statuses.get(key)
            if status != previous:
                self._health_statuses[key] = status
                if status == "ok" and previous is not None:
                    self.log(f"{name} backend recovered: {url}")
                elif status != "ok":
                    message = f"{name} backend {detail}: {url}"
                    self.log(message)
                    self.statusBar().showMessage(message)

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
        self._operation_status = QLabel()
        self.statusBar().addPermanentWidget(self._operation_status)
        # Backend reachability indicators (LLM + TAVI): grey until the first probe returns.
        self._llm_health = QLabel("LLM ●")
        self._tavi_health = QLabel("TAVI ●")
        for lbl in (self._llm_health, self._tavi_health):
            lbl.setStyleSheet("color: #888;")
            self.statusBar().addPermanentWidget(lbl)

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

            # `triggered` (user interaction only), NOT `toggled`: a tabified dock going behind its
            # sibling fires visibilityChanged(False) -> action.setChecked(False), which under
            # `toggled` would re-enter _toggle and *hide* the dock — permanently losing the buried
            # tab. `triggered` fires only on an actual menu click, so the programmatic setChecked
            # from the visibility sync no longer hides anything.
            action.triggered.connect(_toggle)
            # Bind the action's own setChecked so Qt auto-drops the connection when the QAction is
            # destroyed (a Python closure capturing the action would outlive it and fire on a dead
            # C++ object during teardown). Same intent as PUFFIN's visibility handler, leak-safe.
            dock.visibilityChanged.connect(action.setChecked)

    # ---- load a different workspace ---------------------------------------
    def load_workspace(self, path) -> bool:
        """Open the workspace at ``path`` through the manager, which emits ``workspace_changed`` and
        drives the full teardown+rebind in :meth:`_on_wsm_workspace_changed`. Returns ``True`` on
        success; an invalid path leaves the current workspace untouched and reports a status message.
        """
        if self._operations.is_busy():
            self.statusBar().showMessage("Workspace is busy — cancel or wait before opening another workspace")
            return False
        # Validate before resolving the current draft.  The manager still performs the actual bind,
        # but a failed candidate must not cause a Save/Discard prompt for the workspace being kept.
        try:
            Workspace.open(path)
        except (WorkspaceNotFound, OSError) as exc:
            self.statusBar().showMessage(f"Not a workspace: {path}")
            self.log(f"Not opening {path}: {exc}")
            return False
        if not self._resolve_dirty_card("open another workspace"):
            return False
        if self._wsm.root() is not None:
            try:
                if Path(path).resolve() == self._wsm.root().resolve():
                    return True
            except OSError as exc:
                self.log(f"could not compare current workspace to {path}: {exc}")
        if not self._resolve_temporary_workspace("open another workspace"):
            return False
        if self._wsm.open(path):
            return True
        self.statusBar().showMessage(f"Not a workspace: {path}")
        return False

    def _resolve_dirty_card(self, action: str) -> bool:
        """Resolve the current card draft before an action changes its meaning or binding."""
        dock = getattr(self, "card_dock", None)
        return True if dock is None else dock.resolve_dirty(action)

    def _resolve_temporary_workspace(self, action: str) -> bool:
        """Resolve temporary work before leaving it, without silently losing analysis.

        Content-bearing temporary work has four deliberate outcomes: Save As, leave it available
        for startup recovery, discard it, or cancel the transition.  Empty pending reservations are
        not analysis; remove them on the way out so cancelled setup does not accumulate directories.
        """
        if not self._wsm.is_temporary():
            return True
        if not self._wsm.has_content():
            if self._wsm.discard_empty_temp():
                return True
            self.statusBar().showMessage("Could not clean up the empty temporary workspace")
            self.log("Could not clean up empty temporary workspace before " + action)
            return False

        root = self._wsm.root()
        label = root.name if root is not None else "temporary workspace"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Temporary workspace has unsaved analysis")
        box.setText(f"{label} has not been saved.")
        box.setInformativeText(
            f"Before you {action}, save it, keep it for recovery at the next launch, or discard "
            "only the temporary analysis. Source data files are never removed.")
        save_btn = box.addButton("Save As…", QMessageBox.ButtonRole.AcceptRole)
        keep_btn = box.addButton("Keep for Recovery", QMessageBox.ButtonRole.ActionRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is keep_btn:
            self.log(f"Keeping temporary workspace for recovery: {root}")
            return True
        if clicked is discard_btn:
            before = self._wsm.root()
            self._wsm.discard_temp()
            if before is not None and not before.exists():
                return True
            self.statusBar().showMessage("Could not discard the temporary workspace")
            self.log(f"Could not discard temporary workspace: {before}")
            return False
        # Save As…: a cancelled/failed save must not silently drop the workspace — back out.
        return self.save_workspace_as()

    def _on_card_saved(self, path: str, generation: int, root: Path | None) -> None:
        """Refresh card-derived views only for the workspace that produced the save signal."""
        if generation != self._wsm.generation() or root != self._wsm.root() or self._empty:
            return
        if Path(path) != Path(root) / "card.policy.toml":
            self.log(f"ignored card-save notification for unexpected path: {path}")
            return
        self._ws.refresh()
        self._on_workspace_changed({"card"})
        if self._ws.results:
            self.statusBar().showMessage(
                "Card saved — existing results used the previous policy; re-run analysis to apply it")
            self.log("Card policy changed after results existed — re-run analysis to apply it")
        else:
            self.statusBar().showMessage("Card saved — new analysis will use this policy")

    def _teardown_binding(self) -> None:
        """Tear down the current VM/docks/bridge/central-widget so :meth:`_bind_workspace` can build
        a fresh binding (no partial rebind). Assumes a binding exists (guarded by ``self._bound``)."""
        # Flush any pending GUI-store write into the OUTGOING store before it is swapped out — a
        # watched-folder/group mutation made just before this rebind must land in the old workspace,
        # not silently follow the timer into the new one (or be dropped entirely).
        self._flush_gui_store_if_pending()
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None

        self.menuBar().removeAction(self._docks_menu.menuAction())
        # removeAction() only detaches the menu; the QMenu (and its per-dock QActions, whose closures
        # capture the now-deleted docks) stay parented to the menu bar. Delete it so repeated
        # rebinds don't leak one Docks menu per load.
        self._docks_menu.deleteLater()
        for dock in list(self._docks.values()):
            if dock is self.log_dock:
                continue  # persists across rebinds (created in __init__); keep its scrollback
            self.removeDockWidget(dock)
            dock.deleteLater()
        self._docks.clear()
        # The old viewmodels are QObjects parented to self; without an explicit deleteLater() they
        # would linger as inert children (their dock's connections already dropped above) for the
        # rest of the window's life. Drop them so repeated rebinds don't accumulate.
        self._viewmodel.deleteLater()
        self.browser_vm.deleteLater()
        self.files_vm.deleteLater()
        self.scans_vm.deleteLater()
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

    # ---- watch -----------------------------------------------------------
    def _on_workspace_changed(self, changed) -> None:
        # The bridge already refreshed the facade on this (Qt) thread.
        self._warn_generation += 1
        self.log_dock.sync_warnings(self._ws.warnings, self._warn_generation)
        self._viewmodel.on_workspace_changed(changed)
        # "snapshot" (session.json) carries the ingest manifest + scan ledger — refresh the Files
        # dock so a finished ingest's new files appear without a manual re-read, and re-index the
        # manifest for the folder watcher's is_known guard off the just-refreshed facade.
        if changed & {"snapshot"}:
            self._rebuild_known_files_cache()
            self.files_vm.refresh()
            self.scans_vm.refresh()
            self.active_units.retain(self.scans_vm.available_keys())
        # A saved human policy changes detector/card summaries and can change grouping/display
        # interpretation.  The facade cache was refreshed by _on_card_saved; rebuild every
        # card-derived surface from that one coherent policy rather than leaving mixed old/new UI.
        if changed & {"card"}:
            self.files_vm.refresh()
            self.scans_vm.refresh()
            self.active_units.retain(self.scans_vm.available_keys())
        # "results"/"snapshot" carry the fit data the plots read; "card_state" carries lattice
        # epochs / dispersion prior / branch belief that feed the browser HKL, the dispersion prior
        # curve, and the heatmap overlays — so it must also re-derive the component views.
        if changed & {"results", "snapshot", "card_state", "card"}:
            self.browser_vm.refresh()
            self.heatmap_vm.refresh()
            self.dispersion_vm.refresh()
            self.waterfall_vm.refresh()
            self._refresh_inspector_selection()
            # The scan rows' q/HKL cells and the auto-q-groups derive from the lattice epochs carried
            # by card_state; a card_state-only tick (no snapshot) still needs them re-derived.
            if changed & {"card_state"} and not (changed & {"snapshot"}):
                self.scans_vm.refresh()
                self.active_units.retain(self.scans_vm.available_keys())
        self._live_label.setText(f"live — last update {datetime.now():%H:%M:%S}")
        self.statusBar().showMessage(f"Updated: {', '.join(sorted(changed)) or '(none)'}")

    def _route_inspect(self, payload) -> None:
        """Route a ``ScansViewModel.inspect_requested`` payload to the inspector: a unit-key ``str``
        shows the raw parsed unit (metadata + counts); anything else (a ``FitView`` or ``None``)
        drives the fit inspector as before."""
        if isinstance(payload, str):
            self.active_units.set_active(payload)
            self.inspector_vm.set_unit(payload)
        else:
            self._inspect_fit(payload)

    def _inspect_fit(self, fit) -> None:
        """Inspect a fit selected by Results or one of the plot views, retaining its unit identity."""
        self.active_units.set_active(self._unit_key_for_fit(fit))
        self.inspector_vm.set_fit(fit)

    def _unit_key_for_fit(self, fit) -> str | None:
        """Return a fit's exact unit id, with a conservative legacy-results fallback.

        Older results files predate ``FitView.unit_id``.  They can still synchronize only when their
        scan number and detector identify exactly one current ledger unit; ambiguity is represented
        by no active chip rather than a plausible but wrong experiment.
        """
        if fit is None:
            return None
        unit_id = getattr(fit, "unit_id", None)
        if unit_id:
            return str(unit_id)
        candidates = [u.key for u in self.scans_vm.rows()
                      if u.scan_number == getattr(fit, "scan_number", None)
                      and u.det == getattr(fit, "detector_id", None)]
        return candidates[0] if len(candidates) == 1 else None

    def _refresh_inspector_selection(self) -> None:
        """Rebuild inspection from the canonical full unit key after a facade refresh."""
        unit_key = self.active_units.key
        if unit_key is None:
            return
        fresh = next((fv for fv in self._ws.results
                      if self._unit_key_for_fit(fv) == unit_key), None)
        if fresh is not None:
            self.inspector_vm.set_fit(fresh)
        elif unit_key in self.scans_vm.available_keys():
            self.inspector_vm.set_unit(unit_key)
        else:
            self.active_units.set_active(None)
            self.inspector_vm.set_fit(None)

    # ---- session layout --------------------------------------------------
    def _snapshot_session(self) -> Session:
        # The empty/pending screen intentionally owns only Files + Log. Serializing that sparse
        # scaffold would overwrite the operator's complete workspace layout while New is waiting
        # for its first ingest, so retain the last full layout until a ready workspace is bound.
        if not self._empty:
            state = bytes(self.saveState().toBase64().data()).decode("ascii")
            self._session.dock_layout_b64 = state
        # Only saved workspaces belong in recents — a temporary (pending/unsaved) workspace must
        # never pollute the recent-workspaces list (it may be discarded moments later).
        if not self._empty and not self._wsm.is_temporary():
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
        # Do not save, discard, or tear down a workspace while an engine process can still be
        # writing it.  Request graceful termination, then re-enter close once its terminal callback
        # has fired (the coordinator escalates to kill after three seconds).
        if getattr(self, "_operations", None) is not None and self._operations.is_busy():
            self._close_when_operations_finish = True
            self._operations.cancel()
            self.statusBar().showMessage("Cancelling workspace operation before closing…")
            event.ignore()
            return
        if not self._resolve_dirty_card("close ISAR"):
            event.ignore()
            return
        # A content-bearing temporary workspace is unsaved analysis — prompt before it is lost. If
        # the operator cancels (or backs out of the Save As dialog), abort the close entirely.
        if not self._resolve_unsaved_temp_on_close():
            event.ignore()
            return
        if getattr(self, "_health_timer", None) is not None:
            self._health_timer.stop()
        if getattr(self, "_health_start_timer", None) is not None:
            self._health_start_timer.stop()
        if getattr(self, "_health_poll_timer", None) is not None:
            self._health_poll_timer.stop()
        self._health_closed = True
        if getattr(self, "_health_probe", None) is not None:
            self._health_probe.shutdown()
        if getattr(self, "_folder_watcher", None) is not None:
            self._folder_watcher.stop()
        if self._bridge is not None:
            self._bridge.stop()
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.flush_now()
        # Flush any pending debounced GUI-store write (watched folders / groups) before the window
        # goes — guarded so a just-discarded temp dir is not resurrected by the save's mkdir.
        self._flush_gui_store_if_pending()
        super().closeEvent(event)

    def _resolve_unsaved_temp_on_close(self) -> bool:
        """Resolve temporary analysis before close; kept work remains a recovery candidate."""
        return self._resolve_temporary_workspace("close ISAR")

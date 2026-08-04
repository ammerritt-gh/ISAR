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
import shutil
import sys
from datetime import datetime
from pathlib import Path
from weakref import WeakSet

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QToolBar,
    QToolButton,
)

from isar_gui.commands import RunOptions, build_run_command
from isar_gui.active_unit import ActiveUnitController
from isar_gui.credentials import (
    CredentialError,
    api_key_present,
    delete_api_key,
    load_api_key,
    store_api_key,
    supported as credential_storage_supported,
)
from isar_gui.folder_watcher import FolderWatcher
from isar_gui.gui_store import GuiStore
from isar_gui.health import (
    HealthProbe, LLM_DEFAULT_ENDPOINT, TAVI_DEFAULT_URL, find_tavi_bat, health_url, ping_http)
from isar_gui.operation_coordinator import WorkspaceOperationCoordinator
from isar_gui.workspace_manager import WorkspaceManager
from isar.workspace_api import (
    DEFAULT_CLOUD_FALLBACK_MODEL,
    SCHEMA_VERSION,
    BranchModelView,
    CardView,
    Workspace,
    WorkspaceNotFound,
    validate_endpoint,
)
from isar_gui.log_dock import LogDock
from isar_gui.logging_setup import configure_gui_logging
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
from isar_gui.viewmodels.campaign import CampaignViewModel
from isar_gui.viewmodels.sandbox import SandboxViewModel
from isar_gui.views.campaign_dock import CampaignDock
from isar_gui.views.campaign_monitor import CampaignMonitor
from isar_gui.views.sandbox_pane import SandboxPane
from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.card_dock import CardDock
from isar_gui.views.center_host import CenterHost
from isar_gui.views.dispersion_pane import DispersionPane
from isar_gui.views.files_dock import FilesDock
from isar_gui.views.heatmap_pane import HeatmapPane
from isar_gui.views.inspector_widget import InspectorWidget
from isar_gui.views.scan_table_dock import ScanTableDock
from isar_gui.views.scans_dock import ScansDock
from isar_gui.views.waterfall_pane import WaterfallPane
from isar_gui.watch_bridge import WatchBridge

log = logging.getLogger(__name__)


_WORKFLOW_HINT = ("Workflow:  add data (top left)  →  organize scans (left)  →  "
                  "views (center)  →  drive a campaign (right)")
_EMPTY_HINT = ("No workspace loaded.\n\nFile ▸ Open Workspace… to begin, or drive a virtual "
               f"campaign (right) — it creates its own workspace.\n\n{_WORKFLOW_HINT}")
_PENDING_HINT = ("Drop data files to begin — they'll land in a temporary workspace.\n"
                 "Or drive a virtual campaign (right) — it creates its own data."
                 f"\n\n{_WORKFLOW_HINT}")

# Bump when the dock inventory/geometry contract changes: restoreState of any OTHER version fails
# atomically and the window falls back to _apply_default_layout (never binary-migrate Qt blobs).
# v1 (implicit, unversioned) = pre-Pass-3; v2 = Pass-3 M1 (plot docks moved into the CenterHost,
# Scans column moved left); v3 = Pass-3 M2 (the Campaign dock joins the right area).
_LAYOUT_VERSION = 3
_DEFAULT_CAMPAIGN_WIDTH = 380  # px — the right-hand campaign workspace column
_DEFAULT_FILES_HEIGHT = 170   # px — data entry is deliberately small; Scans below it dominates
_DEFAULT_LEFT_WIDTH = 300     # px — the Files/Scans tool column
_DEFAULT_LOG_HEIGHT = 140     # px — the Log is a ticker, not a workspace surface
# The five central display surfaces, in switcher order: (host key, button label).
_CENTER_PAGES = (("scan", "Scan"), ("waterfall", "Waterfall"), ("plane", "Plane (q, E)"),
                 ("dispersion", "Dispersion ω(q)"), ("sandbox", "Sandbox"))
_CENTER_KEYS = tuple(key for key, _label in _CENTER_PAGES)
# The File ▸ Export menu keeps its historical display keys; map them to the host page keys so PNG
# export is unchanged for the operator. "Inspector" is the C2 fit inspector, now the "scan" page.
_EXPORT_KEY_TO_CENTER = {"Plane (q, E)": "plane", "Dispersion ω(q)": "dispersion",
                         "Waterfall / Comparison": "waterfall", "Inspector": "scan"}
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
    last_ingest = None
    #: The campaign dashboard's read surface: no log -> no events, no terminal summary.
    campaign = None

    def __init__(self):
        self.warnings: list = []

    def session_events(self, kind=None, since=None):
        return iter(())

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
        # MainWindow is also created directly in GUI tests and embedded use, bypassing app.main.
        # Ensure the durable operator log exists in those entry paths too; setup is idempotent.
        self._gui_log_path = configure_gui_logging()
        self._watch_enabled = watch
        # Forwarded verbatim to WatchBridge -> Workspace.watch (debounce_ms/poll_interval_s/backend);
        # lets tests use a fast poll instead of the 1s production default.
        self._watch_kwargs = watch_kwargs or {}
        self.resize(1200, 800)

        self._docks: dict[str, object] = {}
        self._warn_generation = 0     # per-bridge-tick counter passed to LogDock.sync_warnings
        self._empty = False           # True while showing the empty-start (no workspace) state
        self.inspector = None         # the C2 fit inspector (a CenterHost page); None in the empty state
        self._center_host: CenterHost | None = None   # hosts the five display surfaces once bound
        self.setAcceptDrops(True)      # drop a data folder/numor onto the window -> ingest (D2/D5)

        # One permanent central QStackedWidget. Page 0 is the empty/pending hint (persists across
        # rebinds like the Log dock); page 1 is the CenterHost added on a ready bind. setCentralWidget
        # is called exactly once, here — _bind_workspace/_teardown_binding add/remove stack pages, they
        # never swap the central widget. (A future page 2 — a compact monitor — is a later task.)
        self._center_stack = QStackedWidget(self)
        self.setCentralWidget(self._center_stack)
        self._empty_hint = QLabel("", self)
        self._empty_hint.setObjectName("empty_hint")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._center_stack.addWidget(self._empty_hint)   # stack index 0
        # Compact campaign-monitor mode (M3-1): the monitor is a per-binding stack page built
        # lazily on first toggle; the stash holds the full window state to restore on expand.
        self._compact_active = False
        self._compact_stash: tuple | None = None
        self._campaign_monitor = None
        self._compact_hint_shown = False
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
        self._operations.drive_started.connect(self._on_drive_started)
        self._operations.drive_finished.connect(self._on_drive_finished)
        self._operations.activity_changed.connect(self._on_operation_activity_changed)

        # Virtual-campaign promotion: a campaign launched into a reserved (pending) directory
        # initializes the workspace itself after its first scan; this timer merely asks the
        # manager to promote it (the window never inspects workspace files — the readiness
        # check lives in WorkspaceManager.promote_pending_if_ready). Armed in _on_drive_started
        # with a captured (root, generation) identity; disarmed on rebind and drive completion.
        self._promote_timer = QTimer(self)
        self._promote_timer.setInterval(1000)
        self._promote_timer.timeout.connect(self._try_promote_campaign_workspace)
        self._promote_identity: tuple[str, int] | None = None

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
        self._init_view_menu()
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
        QTimer.singleShot(0, self, self._restore_layout)
        QTimer.singleShot(0, self, self._apply_empty_start_dock_geometry)
        QTimer.singleShot(0, self, self._surface_session_security_warnings)
        # Cold launch: _bind_workspace queued its file-less-workspace Files raise while self._bound
        # was still False, so the restore branch there did NOT run — instead _restore_layout is
        # queued just above, AFTER that raise. A saved layout with a different front tab would then
        # re-bury Files. Re-queue the raise here so it runs after the restore on the construction path
        # too (idempotent with the bind's queue). The rebind path queues its restore inside
        # _bind_workspace, before its own raise, so it needs no repeat.
        if ws is not None and not self._empty and not self.files_vm.rows:
            QTimer.singleShot(0, self, self._raise_files_dock_for_empty_data)
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
        # The G7 sandbox preview-fit worker; keyed on the workspace ROOT (it re-parses in-process).
        self.sandbox_vm = SandboxViewModel(None if self._empty else self._ws.root, parent=self)
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
        # The campaign feed/status VM: event-log ticks fan out to it in _on_workspace_changed;
        # process liveness arrives via the window's stable drive slots (wired once in __init__ —
        # the coordinator outlives bindings, so per-bind lambdas would accumulate and dangle).
        self.campaign_vm = CampaignViewModel(ws, parent=self)
        self.campaign_vm.set_process_alive(self._operations.is_running_campaign())
        self.waterfall_vm.load_view_state(self._session)
        self.heatmap_vm.load_view_state(self._session)
        self.dispersion_vm.load_view_state(self._session)
        self._entropy_action = QAction("Colour by entropy", self)
        self._entropy_action.setCheckable(True)
        self._entropy_action.setChecked(self.heatmap_vm.color_by_entropy)
        self._entropy_action.toggled.connect(self.heatmap_vm.set_color_by_entropy)
        self._entropy_action.toggled.connect(self.dispersion_vm.set_color_by_entropy)

        # The five display surfaces (the C2 fit inspector + the four plot panes) live as switchable
        # pages of the central CenterHost; a browser selection drives the inspector. In the empty
        # state there is nothing to inspect, so the stack shows the Open-Workspace hint (page 0).
        if self._empty:
            self.inspector = None
            self._center_host = None
            self._show_empty_hint()
        else:
            self.inspector = InspectorWidget(self.inspector_vm, parent=self)
            self._build_center_host()
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
        # Impose the Pass-3 default arrangement synchronously, before the window paints or the
        # operator (or a test) can touch a dock — a queued default would race and trample manual
        # visibility changes. A valid saved layout, restored via the queued paths below/in
        # __init__, then overrides it wholesale; a failed restore falls back onto this default.
        if not self._empty:
            self._apply_default_layout()

        # ``restoreState`` can only restore docks which exist at the time it runs.  A first launch
        # may deliberately bind the sparse empty-start scaffold, then open a real workspace later
        # in the same session.  Reapply the saved layout after that ready bind creates the complete
        # dock set.  Conversely, every none/pending rebind gets its compact data-entry geometry,
        # not just the constructor's initial bind.
        if self._empty:
            QTimer.singleShot(0, self, self._apply_empty_start_dock_geometry)
        elif self._bound and self._session.dock_layout_b64:
            QTimer.singleShot(0, self, self._restore_layout)

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
            if getattr(self, "campaign_dock", None) is not None:
                self.campaign_dock.set_store(self._gui_store)
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

        # A ready workspace with no ingested files still needs a data-entry surface: without this the
        # Files dock is buried behind its tabified siblings (Browser/Card) or a restored layout, so
        # the "Drop data files here to begin." hint is invisible and the operator's first action has
        # no home — mirroring the empty/pending state (_init_docks raises Files there). Queue the
        # raise so it runs AFTER the _restore_layout queued at the top of a ready bind (both are
        # singleShot(0, ...), FIFO), otherwise restoreState would immediately re-bury Files. A
        # workspace that already has files keeps its saved layout untouched.
        if not self._empty and not self.files_vm.rows:
            QTimer.singleShot(0, self, self._raise_files_dock_for_empty_data)

    def _raise_files_dock_for_empty_data(self) -> None:
        """Surface the Files dock as the data-entry point for a ready-but-data-less workspace.

        Guarded against a rebind that happened before this queued call fires: _teardown_binding nulls
        files_dock, and a workspace that gained files in the interim no longer needs the raise.
        """
        if self._empty or self.files_dock is None or self.files_vm.rows:
            return
        self.files_dock.show()
        self.files_dock.raise_()

    def _show_empty_hint(self) -> None:
        """Show the persistent hint page (stack index 0). Pending (a temp workspace is reserved)
        invites a drop; a true none-state points at Open Workspace…."""
        self._empty_hint.setText(_PENDING_HINT if self._wsm.state() == "pending" else _EMPTY_HINT)
        self._center_stack.setCurrentWidget(self._empty_hint)

    def _build_center_host(self) -> None:
        """Build the CenterHost with the five display surfaces, register it as the stack's ready page,
        restore the persisted view + auto-follow, and make it the visible central page."""
        host = CenterHost(self)
        host.add_page("scan", "Scan", self.inspector)
        host.add_page("waterfall", "Waterfall", WaterfallPane(self.waterfall_vm))
        host.add_page(
            "plane", "Plane (q, E)",
            HeatmapPane(self.heatmap_vm, entropy_action=self._entropy_action))
        host.add_page(
            "dispersion", "Dispersion ω(q)",
            DispersionPane(self.dispersion_vm, entropy_action=self._entropy_action))
        host.add_page("sandbox", "Sandbox", SandboxPane(self.sandbox_vm))
        # Restore the persisted view state (default: the Scan/inspector page, auto-follow on). Set
        # auto BEFORE show_view so the programmatic (non-user) restore does not clear it.
        host.auto_follow = bool(self._session.view_state.get("center_auto_follow", True))
        want = self._session.view_state.get("center_view", "scan")
        host.show_view(want if want in _CENTER_KEYS else "scan")
        host.view_changed.connect(self._on_center_view_changed)
        self._center_host = host
        self._center_stack.addWidget(host)   # stack index 1 (the ready page)
        self._center_stack.setCurrentWidget(host)

    def _on_center_view_changed(self, key: str) -> None:
        """Persist the operator's current view + auto-follow when the central view switches."""
        self._session.view_state["center_view"] = key
        if self._center_host is not None:
            self._session.view_state["center_auto_follow"] = self._center_host.auto_follow
        self._autosave()

    def _show_center_view(self, key: str, *, user: bool = False) -> None:
        """View-menu / routing entry point: switch the central view if a host is bound."""
        if self._center_host is not None:
            self._center_host.show_view(key, user=user)

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

        # The campaign workspace (GUI Pass 3, M2-6) exists in EVERY state, including empty/pending:
        # a virtual campaign needs no data — it creates its own workspace, so the entry point must
        # not be gated behind one. Store binds late (set_store below, ready binds only); TAVI
        # launch routes through the window's Backends plumbing; health status is fed from
        # _apply_health; the "none"-state Start hands the launch back via virtual_start_requested
        # (the reserve rebind tears the emitting dock down, so the window owns that sequence).
        self.campaign_dock = CampaignDock(self.campaign_vm, self._operations, store=None,
                                          llm_env=self._llm_env, workspace_state=self._wsm.state,
                                          parent=self)
        self.campaign_dock.tavi_launch_requested.connect(self._launch_tavi)
        self.campaign_dock.virtual_start_requested.connect(self._on_virtual_start_requested)
        self.addDockWidget(Qt.RightDockWidgetArea, self.campaign_dock)
        self._register_dock("Campaign", self.campaign_dock)

        if self._empty:
            # Before a workspace exists, results, card, scan, and plot docks are not merely empty:
            # they describe concepts the operator cannot act on yet.  Keep the data-entry dock and
            # the campaign workspace (the two ways IN), matching the New Workspace pending state.
            # A ready bind rebuilds the complete normal dock scaffold below.
            self.files_dock.show()
            self.files_dock.raise_()
            self.campaign_dock.show()
            return

        self.browser_dock = BrowserDock(
            self.browser_vm, self, unit_key_for_fit=self._unit_key_for_fit)
        self.browser_vm.selection_changed.connect(self.campaign_dock.set_selected_fit)
        self.active_units.active_changed.connect(self.browser_dock.set_active_key)
        self.browser_dock.set_active_key(self.active_units.key)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.browser_dock)
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
        self._register_dock("Card", self.card_dock)

        # The Phase-E physical scan view + its tabular twin (they share one model + selection, so a
        # chip and a row light up together). Pass 3 puts the whole scan-organizing column on the
        # LEFT, under the compact Files dock — the right area is the campaign workspace (M2-6).
        self.scans_dock = ScansDock(self.scans_vm, self)
        self.active_units.active_changed.connect(self.scans_dock.set_active_key)
        self.scans_dock.set_active_key(self.active_units.key)
        self.scans_dock.send_group_requested.connect(self._on_send_group)   # "Send to" routing (2A)
        self.scans_dock.scan_drag_active.connect(self._on_scan_drag)        # drag-reveal on plots (R12)
        self.scans_dock.fit_requested.connect(self._on_fit_requested)       # sandbox preview fit (2B)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.scans_dock)
        self._register_dock("Scans", self.scans_dock)
        self.scan_table_dock = ScanTableDock(self.scans_vm, self)
        self.active_units.active_changed.connect(self.scans_vm.model.set_active_key)
        self.scans_vm.model.set_active_key(self.active_units.key)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.scan_table_dock)
        self._register_dock("Scan Table", self.scan_table_dock)

        # (The Campaign dock was created above, before the empty-state early return.)

        # The four plot panes (plane / dispersion / waterfall / sandbox) are no longer docks: the C2
        # inspector and those four surfaces are switchable pages of the central CenterHost, built in
        # _bind_workspace (see _build_center_host). Splits/tabs/sizes for the remaining tool docks
        # are imposed by _apply_default_layout (and overridden by a valid saved layout).

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
        if self.campaign_dock is not None:
            self.campaign_dock.show()
            self.resizeDocks([self.campaign_dock], [380], Qt.Horizontal)
        self.resizeDocks([self.log_dock], [_EMPTY_START_LOG_HEIGHT], Qt.Vertical)

    def _apply_default_layout(self) -> None:
        """The Pass-3 default dock geometry (left-to-right workflow).

        Files sits small at the top-left; the scan-organizing stack (Scans + Browser/Card/Scan
        Table tabs) fills the left column under it; the center is the CenterHost view switcher;
        the bottom Log is a compact ticker. (The Campaign dock joins the right area in M2-6.)
        Idempotent. Never called for the empty (no-workspace) scaffold; a pending temporary
        workspace is a real binding and does get the default arrangement."""
        if self._empty:
            return
        from PySide6.QtCore import Qt
        for dock in (self.files_dock, self.scans_dock, self.browser_dock, self.card_dock,
                     self.scan_table_dock, self.campaign_dock, self.log_dock):
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(Qt.LeftDockWidgetArea, self.files_dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.scans_dock)
        self.splitDockWidget(self.files_dock, self.scans_dock, Qt.Vertical)
        self.tabifyDockWidget(self.scans_dock, self.browser_dock)
        self.tabifyDockWidget(self.browser_dock, self.card_dock)
        self.tabifyDockWidget(self.card_dock, self.scan_table_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.campaign_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.scans_dock.raise_()
        self.resizeDocks([self.campaign_dock], [_DEFAULT_CAMPAIGN_WIDTH], Qt.Horizontal)
        # A large fixed hint (not self.height() — meaningless before the window is realized):
        # resizeDocks distributes proportionally, so [170, 700] reads "Files small, Scans the rest".
        self.resizeDocks([self.files_dock, self.scans_dock],
                         [_DEFAULT_FILES_HEIGHT, 700], Qt.Vertical)
        self.resizeDocks([self.files_dock, self.scans_dock],
                         [_DEFAULT_LEFT_WIDTH, _DEFAULT_LEFT_WIDTH], Qt.Horizontal)
        self.resizeDocks([self.log_dock], [_DEFAULT_LOG_HEIGHT], Qt.Vertical)

    # ---- compact campaign-monitor mode (M3-1) ------------------------------
    def toggle_compact_mode(self, *_a) -> None:
        """Swap between the full workspace and the glanceable campaign monitor (AD-14).

        Enter: stash ``saveState``+geometry, hide every dock, flip the central stack to the
        monitor page, shrink to a corner-of-the-screen card. Leave: restore the stash wholesale.
        The stash lives only in memory — ``_snapshot_session`` is guarded while compact, so the
        session file always carries the FULL layout (close-while-compact expands first)."""
        if not self._compact_active:
            if self._empty:
                self.statusBar().showMessage("Compact mode needs an open workspace")
                self._compact_mode_action.setChecked(False)
                return
            if self._campaign_monitor is None:
                self._campaign_monitor = CampaignMonitor(
                    self.campaign_vm, self.heatmap_vm, self._operations, parent=self)
                self._campaign_monitor.expand_requested.connect(self.toggle_compact_mode)
                self.log_dock.appended.connect(self._campaign_monitor.append_log_line)
                self._center_stack.addWidget(self._campaign_monitor)
            self._compact_stash = (self.saveState(_LAYOUT_VERSION), self.saveGeometry())
            for dock in self.findChildren(QDockWidget):
                dock.hide()
            self._workspace_toolbar.hide()   # its buttons would pin the minimum width
            self._center_stack.setCurrentWidget(self._campaign_monitor)
            # QStackedLayout's minimum size spans ALL pages, visible or not — detach the big
            # CenterHost page while compact or the window cannot shrink (re-added on expand;
            # the widget stays parented to the stack, just out of the layout).
            if self._center_host is not None:
                self._center_stack.removeWidget(self._center_host)
                self._center_host.hide()   # already hidden as a non-current page; keep it that
                # way explicitly so the enter-order (switch, then detach) is not load-bearing
            self._compact_active = True
            self._compact_mode_action.setChecked(True)
            # An explicit minimum overrides hint-based clamping: without it the resize races the
            # pending dock-hide/page-detach LayoutRequest and is silently clamped to the STALE
            # minimum width (a zero-timer defer still lost that race under offscreen Qt).
            self.setMinimumSize(360, 280)
            self.resize(430, 340)
        else:
            self._compact_active = False
            self._compact_mode_action.setChecked(False)
            self.setMinimumSize(0, 0)        # back to hint-driven minimums for the full layout
            self._workspace_toolbar.show()   # explicit — never depend on the restore for it
            if self._center_host is not None:
                self._center_stack.addWidget(self._center_host)   # re-attach the detached page
                self._center_stack.setCurrentWidget(self._center_host)
            else:
                self._center_stack.setCurrentWidget(self._empty_hint)
            if self._compact_stash is not None:
                state, geometry = self._compact_stash
                self._compact_stash = None
                self.restoreGeometry(geometry)
                if not self.restoreState(state, _LAYOUT_VERSION):
                    self._apply_default_layout()   # a stash can't be stale, but never half-apply
        self._sync_run_actions()

    def _dispose_campaign_monitor(self) -> None:
        """Per-binding teardown: leave compact first so the full layout is what persists."""
        if self._compact_active:
            self.toggle_compact_mode()
        if self._campaign_monitor is not None:
            try:
                self.log_dock.appended.disconnect(self._campaign_monitor.append_log_line)
            except (RuntimeError, TypeError) as exc:
                log.debug("monitor ticker already detached: %s", exc)
            self._center_stack.removeWidget(self._campaign_monitor)
            self._campaign_monitor.deleteLater()
            self._campaign_monitor = None

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Auto-OFFER (never auto-switch): a small window during a live campaign probably wants
        # the monitor. One hint per session; the operator stays in charge.
        if (not self._compact_active and not self._compact_hint_shown and event.size().width() < 700
                and getattr(self, "_operations", None) is not None
                and self._operations.is_running_campaign()):
            self._compact_hint_shown = True
            self.statusBar().showMessage("Tip: View ▸ Compact mode is a glanceable campaign monitor")

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
        self._create_o41_example_action = file_menu.addAction(
            "Create O41 Authoritative Example…")
        self._create_o41_example_action.triggered.connect(self._create_o41_example)
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

        help_menu = self.menuBar().addMenu("&Help")
        open_log_action = help_menu.addAction("Open GUI Log Folder")
        open_log_action.triggered.connect(self._open_gui_log_folder)

    def _open_gui_log_folder(self) -> None:
        """Open the durable log directory and say explicitly if the shell cannot do so."""
        if self._gui_log_path is None:
            message = "GUI log location is unavailable; see the Log dock for this session"
            self.statusBar().showMessage(message)
            self.log(message)
            return
        folder = self._gui_log_path.parent
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        if opened:
            self.statusBar().showMessage(f"Opened GUI log folder: {folder}")
        else:
            message = f"Could not open GUI log folder: {folder}"
            self.statusBar().showMessage(message)
            self.log(message)

    def _on_open_workspace(self) -> None:
        start_dir = "" if self._ws.root is None else str(self._ws.root)
        path = QFileDialog.getExistingDirectory(self, "Open Workspace", start_dir)
        if path:
            self.load_workspace(path)

    def _create_o41_example(self) -> None:
        """Copy the maintained positive seed to a user workspace and prefill normal controls."""
        if self._operations.is_busy():
            self.statusBar().showMessage("Workspace is busy — wait before creating the example")
            return
        parent = QFileDialog.getExistingDirectory(
            self, "Create O41 Example (choose a parent folder)")
        if not parent:
            return
        repo = Path(__file__).resolve().parents[1]
        source = (repo / "docs" / "campaigns" /
                  "2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3" /
                  "artifacts" / "s8-c01-strong-confirmed")
        resources = repo / "isar_gui" / "resources" / "eig4-o41-authoritative"
        destination = Path(parent) / "isar-eig4-o41-authoritative"
        if destination.exists():
            QMessageBox.warning(
                self, "Example already exists",
                f"Choose another parent folder or rename this existing folder:\n{destination}")
            return
        try:
            shutil.copytree(source, destination)
            shutil.copy2(resources / "card.policy.toml", destination / "card.policy.toml")
            shutil.copy2(resources / "compact-goal.json", destination / "compact-goal.json")
            shutil.copy2(resources / "example-manifest.json",
                         destination / "example-manifest.json")
            # Historical gate events are evidence for the seed, not the new operator campaign's
            # spend ledger. The copied prior/model artifacts remain; the campaign log starts clean.
            (destination / "report" / "session_log.jsonl").write_text("", encoding="utf-8")
        except OSError as exc:
            log.exception("failed to create the O41 example workspace")
            self.statusBar().showMessage(f"Could not create O41 example — {exc}")
            return
        self.load_workspace(str(destination))
        if self.campaign_dock is not None:
            self.campaign_dock.apply_form({
                "source": "spec",
                "spec_path": str(destination / "compact-goal.json"),
                "tier": "deterministic",
                "scan_window_eig_mode": "authoritative",
                "budget": "1e10n",
                "analysis_limit": "10m",
                "seed": 0,
                "max_iters": 3,
            })
            self.campaign_dock._save_form()
        self.statusBar().showMessage(
            "O41 example ready — review the Campaign settings, then select Start")

    # ---- Workspace toolbar (New / Open / Save As / Recent, Phase C) --------
    def _init_workspace_toolbar(self) -> None:
        """The always-present Workspace toolbar. ``objectName`` is set so ``QMainWindow.saveState``
        round-trips it in the session layout."""
        tb = QToolBar("Workspace", self)
        tb.setObjectName("workspace_toolbar")
        self.addToolBar(tb)
        self._workspace_toolbar = tb   # compact mode hides it (saveState restores it on expand)

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
                       getattr(self, "_open_workspace_menu_action", None),
                       getattr(self, "_create_o41_example_action", None)):
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
        summary = getattr(report, "last_ingest", None) or {}
        logical_count = summary.get("logical_scan_count")
        unit_count = summary.get("analyzer_unit_count")
        count_text = ""
        if logical_count is not None and unit_count is not None:
            count_text = f"{logical_count} logical scans / {unit_count} analyzer units"
        scan_rows = self.scans_vm.rows() if not self._empty else []
        parsed_waiting = sum(1 for unit in scan_rows if unit.status == "ingested")
        if parsed_waiting:
            prefix = f"Ingested {n_files} file(s)"
            if count_text:
                prefix += f" ({count_text})"
            self.statusBar().showMessage(
                f"{prefix}: {parsed_waiting} analyzer unit(s) awaiting analysis "
                f"; open Scans or choose Run > Run analysis...{note}")
        else:
            prefix = f"Ingested {n_files} files"
            if count_text:
                prefix += f" ({count_text})"
            self.statusBar().showMessage(
                f"{prefix}: +{a} logical scans, ~{u} updated ({d} duplicates){note}")
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
            0, self, lambda g=generation, r=root: self._promote_scans_if_current(g, r))

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

    def _on_send_group(self, keys, target: str, label: str) -> None:
        """Route a folder's "Send to <plot>" to the destination pane: validate, restrict, switch (2A).

        The engine is never handed a "group" — only the unit ids in ``keys`` reach the view VM. A
        refusal (e.g. a multi-T group on a single-T plane) is surfaced loudly on the status bar."""
        # target keys are the "Send to" verbs; the "heatmap" plot is the CenterHost "plane" page.
        mapping = {"waterfall": ("waterfall", getattr(self, "waterfall_vm", None)),
                   "dispersion": ("dispersion", getattr(self, "dispersion_vm", None)),
                   "heatmap": ("plane", getattr(self, "heatmap_vm", None))}
        center_key, vm = mapping.get(target, (None, None))
        keys = list(keys or [])
        if vm is None or not keys or self._center_host is None:
            return
        ok, reason = vm.can_accept(keys)
        if not ok:
            self.statusBar().showMessage(reason)
            return
        self._center_host.pane(center_key).apply_group(keys, label or "selection")
        self._center_host.show_view(center_key)

    def _on_scan_drag(self, active: bool, keys) -> None:
        """Arm the currently-shown plot pane as a drop target while a scan drag is in flight (R12).
        Only the visible central page can glow; the group-drop arm itself no-ops (no glow) when the
        page cannot accept the scans, and the Scan/Sandbox pages have no ``arm_group_drop`` at all."""
        if self._center_host is None:
            return
        pane = self._center_host.pane(self._center_host.current_key)
        arm = getattr(pane, "arm_group_drop", None)
        if callable(arm):
            arm(bool(active), keys)

    def _on_fit_requested(self, unit_key: str) -> None:
        """Run a G7 sandbox preview fit for ``unit_key`` and switch to the Sandbox page (2B). The fit
        runs off-thread; the pane updates when it finishes. Nothing is written to the workspace."""
        if self._empty:
            self.statusBar().showMessage("No workspace — nothing to fit")
            return
        self.sandbox_vm.set_workspace_root(self._ws.root)
        if self._center_host is not None:
            self._center_host.show_view("sandbox")
        self.statusBar().showMessage(f"Sandbox preview fit for {unit_key}…")
        self.sandbox_vm.request_fit(unit_key)

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

    def _on_virtual_start_requested(self, opts, env: dict) -> None:
        """Launch a campaign with NO workspace at all: reserve → rebind → carry the form → drive.

        The dock cannot call request_drive itself here — reserving tears the emitting dock down
        mid-click — so it hands the built options over and this slot owns the sequence. The
        campaign then initializes the reserved directory itself (lazy `isar init` after its
        first scan); the promotion timer adopts it (see _try_promote_campaign_workspace)."""
        dock = getattr(self, "campaign_dock", None)
        form = dock.form_dict() if dock is not None else None
        self._wsm.new_temp()                       # rebind: a fresh pending dock appears
        if form is not None and self.campaign_dock is not None:
            self.campaign_dock.apply_form(form)    # in-memory carry — pending has no GuiStore yet
        if not self._operations.request_drive(opts, env=dict(env or {})):
            self.log("could not start the virtual campaign — workspace not ready")

    def _seed_pending_campaign_form(self) -> None:
        """Persist the typed campaign form into the reserved directory's GuiStore BEFORE the
        promotion rebind loads it — set_store fires during the ready bind, so seeding afterwards
        would be invisible. Never overwrites an existing form (a recovered temporary workspace
        keeps its own). The GuiStore is a machine-written GUI file — sanctioned, unlike the card."""
        root = self._wsm.root()
        dock = getattr(self, "campaign_dock", None)
        if root is None or dock is None:
            return
        try:
            store = GuiStore.load(root)
            if not store.campaign_form():
                store.set_campaign_form(dock.form_dict())
                store.save()
        except OSError as exc:
            self.log(f"could not persist the campaign form into {root}: {exc}")

    def _try_promote_campaign_workspace(self, *, final: bool = False) -> None:
        """One promotion attempt for a campaign running in a pending workspace.

        The readiness check itself lives in WorkspaceManager.promote_pending_if_ready (the
        window never inspects workspace files); this slot owns only the cadence and the
        follow-up adoption. ``final`` is the drive-finished last chance — the campaign may have
        initialized the workspace and exited within one tick interval; without it a valid
        workspace would stay pending forever. On the final attempt no adoption is needed (the
        job is finishing), and the timer disarms whether or not promotion happened."""
        ident = self._promote_identity
        if ident is None:
            self._promote_timer.stop()
            return
        root, generation = ident
        if self._wsm.promote_pending_if_ready(root, generation):
            self._promote_timer.stop()
            self._promote_identity = None
            if not final and not self._operations.adopt_drive_binding(
                    root, self._wsm.generation()):
                msg = ("campaign workspace opened, but the running campaign could not be "
                       "re-adopted — its completion will be reported as stale")
                self.log(msg)
                self.statusBar().showMessage(msg)
        elif final:
            self._promote_timer.stop()
            self._promote_identity = None

    def _on_drive_started(self) -> None:
        vm = getattr(self, "campaign_vm", None)
        if vm is not None:
            vm.set_process_alive(True)
        # A campaign launched into a reserved (pending) directory initializes the workspace
        # itself: persist the typed form for the coming ready rebind, capture the promotion
        # identity, and start polling the manager. (A stale tick is refused by the manager's own
        # identity check too — this capture is the caller's independent half of that contract.)
        if self._wsm.state() == "pending" and self._wsm.root() is not None:
            self._seed_pending_campaign_form()
            self._promote_identity = (str(self._wsm.root()), self._wsm.generation())
            self._promote_timer.start()
        # Auto-follow (AD-15): a starting campaign turns the center to the growing plane view.
        host = getattr(self, "_center_host", None)
        if host is not None and host.auto_follow:
            host.show_view("plane")
        self.statusBar().showMessage("Campaign started")

    def _on_drive_finished(self, code: int, live_workspace: bool) -> None:
        # Final promotion attempt FIRST: the campaign may have initialized the workspace and
        # exited inside one tick interval. Ordering matters — the promotion rebind constructs a
        # new CampaignViewModel whose constructor reads is_running_campaign(), still True while
        # this signal is being delivered (the coordinator clears its active job afterwards) —
        # so set_process_alive(False) must run AFTER the promotion, on the post-rebind VM.
        self._try_promote_campaign_workspace(final=True)
        vm = getattr(self, "campaign_vm", None)
        if vm is not None:
            vm.set_process_alive(False)
        if not live_workspace:
            self.log("stale campaign completion ignored — workspace changed")
            return
        msg = {0: "Campaign met its objective", 3: "Campaign incomplete — see the feed",
               4: "Campaign stopped by operator",
               5: "Campaign scan recovery pending — press Resume to retry",
               6: "Campaign infeasible — see the feed"}.get(
                   int(code), f"Campaign failed (exit {code})")
        self.statusBar().showMessage(msg)
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
                QTimer.singleShot(0, self, self.close)
        else:
            if state["cancelling"]:
                verb = "Cancelling"
            elif state.get("stopping"):
                verb = "Stopping campaign after the current scan"
            elif kind == "ingest":
                verb = "Ingesting"
            elif kind == "drive":
                verb = "Campaign running"
            else:
                verb = "Analysing"
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
        """The pyqtgraph graphics widget backing a named export view, or ``None``.

        The File ▸ Export menu keeps its historical display keys (``Plane (q, E)`` / ``Dispersion
        ω(q)`` / ``Waterfall / Comparison`` / ``Inspector``); they map to CenterHost page keys."""
        if self._center_host is None:
            return None
        center_key = _EXPORT_KEY_TO_CENTER.get(dock_key)
        if center_key is None:
            return None
        return self._center_host.glw_for(center_key)

    # ---- Backends: LLM settings + TAVI launch + health (D1/D3) ------------
    def _init_backends_menu(self) -> None:
        menu = self.menuBar().addMenu("&Backends")
        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self._open_settings)
        self._launch_tavi_action = menu.addAction("Launch TAVI")
        self._launch_tavi_action.triggered.connect(self._launch_tavi)

    # ---- View menu (central view switcher; built once, persists across rebinds) ----
    def _init_view_menu(self) -> None:
        """The View menu selects the central display surface. It is built once, before the per-bind
        Docks menu (which is always appended last), so View reads before Docks in the menu bar. A
        trigger is an explicit user view change (clears auto-follow); it is a no-op with no host."""
        menu = self.menuBar().addMenu("&View")
        for key, label in _CENTER_PAGES:
            act = menu.addAction(label)
            act.triggered.connect(lambda _checked=False, k=key: self._show_center_view(k, user=True))
        menu.addSeparator()
        # Compact campaign-monitor mode (M3-1): same window, docks stashed, stack page swapped.
        self._compact_mode_action = menu.addAction("Compact mode")
        self._compact_mode_action.setCheckable(True)
        self._compact_mode_action.triggered.connect(self.toggle_compact_mode)

    def _settings(self) -> dict:
        """Return GUI-owned non-secret backend settings.

        API keys live only in Windows Credential Manager; this return value is intentionally safe to
        hand to dialogs and future session serialization.
        """
        defaults = {"profile": "llmm", "llm_endpoint": LLM_DEFAULT_ENDPOINT, "llm_model": "",
                    "cloud_fallback_enabled": False, "cloud_fallback_model":
                    DEFAULT_CLOUD_FALLBACK_MODEL, "tavi_bat": ""}
        saved = dict(self._session.view_state.get("settings", {}))
        saved.pop("llm_api_key", None)
        # Retired-ref migration: settings saved before 2026-07-16 carry the old cloud-fallback pin
        # (gpt-oss-120b, whose NanoGPT serving returns empty replies) — a stale saved value would
        # silently override the repaired default forever. Drop it so the default wins; the dialog
        # re-persists the current constant on the next save.
        if saved.get("cloud_fallback_model") == "nanogpt/openai/gpt-oss-120b":
            saved.pop("cloud_fallback_model")
        return {**defaults, **saved}

    def _open_settings(self) -> None:
        from isar_gui.settings_dialog import SettingsDialog

        current = self._settings()
        endpoint = current.get("llm_endpoint") or LLM_DEFAULT_ENDPOINT
        present = False
        if credential_storage_supported():
            try:
                present = api_key_present(endpoint)
            except (CredentialError, ValueError) as exc:
                self.log(f"could not check Windows Credential Manager: {exc}")
        # A failed legacy migration is still usable for this session, but never displayed or copied
        # into the editor.  The status says a key is present without revealing it.
        present = present or bool(self._legacy_llm_api_key())
        dlg = SettingsDialog(current, api_key_present=present,
                             credential_storage_available=credential_storage_supported(), parent=self)
        if dlg.exec():
            new_settings = dlg.settings
            change = dlg.api_key_change
            endpoint = new_settings["llm_endpoint"] or LLM_DEFAULT_ENDPOINT
            if change is not None:
                try:
                    if not credential_storage_supported():
                        raise CredentialError("Windows Credential Manager is unavailable on this platform")
                    if change:
                        store_api_key(endpoint, change)
                    else:
                        delete_api_key(endpoint)
                except (CredentialError, ValueError) as exc:
                    message = f"Backend settings were not saved because the API key could not be updated: {exc}"
                    self.log(message)
                    QMessageBox.warning(self, "Could not update API key", message)
                    return
            old_settings = self._session.view_state.get("settings", {})
            legacy_key = old_settings.get("llm_api_key") if isinstance(old_settings, dict) else None
            # Preserve a failed migration until it is explicitly repaired; otherwise saving a model
            # field would silently drop the only copy of an operator's key.
            if (self._session._retain_legacy_llm_key and isinstance(legacy_key, str)
                    and change is None):
                new_settings["llm_api_key"] = legacy_key
            else:
                self._session._retain_legacy_llm_key = False
            self._session.view_state["settings"] = new_settings
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
        endpoint = s.get("llm_endpoint")
        if endpoint:
            try:
                env["ISAR_LLM_ENDPOINT"] = validate_endpoint(endpoint)
            except ValueError as exc:
                self.log(f"LLM endpoint is invalid and was not passed to analysis: {exc}")
        if s.get("llm_model"):
            env["ISAR_LLM_MODEL"] = s["llm_model"]
        key = self._credential_llm_api_key(endpoint or LLM_DEFAULT_ENDPOINT)
        if key:
            env["ISAR_LLM_API_KEY"] = key
        # Presence-sensitive in the engine: an explicit empty value means a card fallback may not
        # override this GUI choice.  Cloud use remains opt-in and visibly named in Settings.
        if s.get("cloud_fallback_enabled"):
            env["ISAR_LLM_FALLBACK_MODEL"] = s.get("cloud_fallback_model") or \
                DEFAULT_CLOUD_FALLBACK_MODEL
        else:
            env["ISAR_LLM_FALLBACK_MODEL"] = ""
        return env

    def _legacy_llm_api_key(self) -> str | None:
        """Return a retained failed-migration key only inside this process, never to a view."""
        settings = self._session.view_state.get("settings")
        key = settings.get("llm_api_key") if isinstance(settings, dict) else None
        return key if self._session._retain_legacy_llm_key and isinstance(key, str) and key else None

    def _credential_llm_api_key(self, endpoint: str) -> str | None:
        if credential_storage_supported():
            try:
                stored = load_api_key(endpoint)
            except (CredentialError, ValueError) as exc:
                self.log(f"could not read Windows Credential Manager for LLM API key: {exc}")
            else:
                if stored:
                    return stored
        return self._legacy_llm_api_key()

    def _surface_session_security_warnings(self) -> None:
        """Make failed plaintext-key migration visible on screen as well as in the durable log."""
        warnings = list(getattr(self._session, "migration_warnings", []))
        if not warnings:
            return
        message = "\n\n".join(warnings)
        self.log(f"SECURITY WARNING: {message}")
        self.statusBar().showMessage("LLM credential migration needs attention — see the warning dialog")
        QMessageBox.warning(self, "LLM credential migration needs attention", message)

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
        # Probe each backend's liveness endpoint, never its bare base — both bases 404 by design,
        # which would render a running backend amber (and block campaign launch) — see health_url.
        self._health_generation = self._health_probe.request({"llm": health_url(llm_url),
                                                               "tavi": health_url(TAVI_DEFAULT_URL)})
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
            if key == "tavi" and getattr(self, "campaign_dock", None) is not None:
                self.campaign_dock.set_backend_status("tavi", status, f"{url} — {detail}")
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
        # Any rebind invalidates an armed promotion watch. (The promotion path itself rebinds via
        # wsm.open — its caller keeps the identity in a local and finishes safely after this.)
        if getattr(self, "_promote_timer", None) is not None:
            self._promote_timer.stop()
        self._promote_identity = None
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
            if dock is self.campaign_dock:
                dock.dispose()
            self.removeDockWidget(dock)
            dock.deleteLater()
        self._docks.clear()
        # deleteLater() leaves the Python attributes pointing at now-dead C++ widgets. An
        # empty/pending rebind returns early from _init_docks without recreating the analysis docks,
        # so a slot firing between binds (e.g. _on_operation_activity_changed reaching card_dock)
        # would touch a deleted widget and raise "Internal C++ object already deleted". Null every
        # transient dock attribute so those sites see None (their getattr default) instead. log_dock
        # is intentionally not touched — it survives the rebind.
        self.card_dock = None
        self.browser_dock = None
        self.files_dock = None
        self.scans_dock = None
        self.scan_table_dock = None
        self.campaign_dock = None
        # The old viewmodels are QObjects parented to self; without an explicit deleteLater() they
        # would linger as inert children (their dock's connections already dropped above) for the
        # rest of the window's life. Drop them so repeated rebinds don't accumulate.
        self._viewmodel.deleteLater()
        self.browser_vm.deleteLater()
        self.files_vm.deleteLater()
        self.scans_vm.deleteLater()
        self.inspector_vm.deleteLater()
        self.campaign_vm.deleteLater()
        if getattr(self, "_entropy_action", None) is not None:
            self._entropy_action.deleteLater()
            self._entropy_action = None
        self.heatmap_vm.save_view_state(self._session)
        self.heatmap_vm.deleteLater()
        self.dispersion_vm.save_view_state(self._session)
        self.dispersion_vm.deleteLater()
        self.waterfall_vm.save_view_state(self._session)
        self.waterfall_vm.deleteLater()
        # The compact monitor projects the outgoing binding's VMs; leave compact and drop it
        # before those VMs are torn down.
        self._dispose_campaign_monitor()
        # The five display surfaces live inside the CenterHost (stack page 1). Dispose it — detaching
        # and deleting every page (incl. the inspector) — drop its view_changed connection, remove it
        # from the stack, and delete it, so the fresh bind rebuilds a clean host. The permanent stack
        # and its hint page (index 0) are left in place. The panes' VM connections drop when both the
        # panes (deleteLater'd here) and the VMs (deleteLater'd just above) are torn down.
        if self._center_host is not None:
            self._center_host.view_changed.disconnect(self._on_center_view_changed)
            # Park the stack on the persistent hint page BEFORE removing the host: removeWidget on
            # the current page leaves the stack at index -1 (a grey flash) until the rebind adds
            # the next host.
            self._center_stack.setCurrentWidget(self._empty_hint)
            self._center_stack.removeWidget(self._center_host)
            self._center_host.dispose()
            self._center_host.deleteLater()
            self._center_host = None
        self.inspector = None

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
        if changed & {"results", "snapshot", "card_state", "card", "events"}:
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
        # "events" is the drive loop's session log (the watcher has always reported it; the GUI
        # ignored it until the campaign dashboard). results/card_state also feed the campaign
        # status (scan verdicts, spend context), so fan those out too.
        if changed & {"events", "results", "card_state"}:
            self.campaign_vm.on_workspace_changed(changed)
        self._live_label.setText(f"live — last update {datetime.now():%H:%M:%S}")
        self.statusBar().showMessage(f"Updated: {', '.join(sorted(changed)) or '(none)'}")

    def _route_inspect(self, payload) -> None:
        """Route a ``ScansViewModel.inspect_requested`` payload to the inspector: a unit-key ``str``
        shows the raw parsed unit (metadata + counts); anything else (a ``FitView`` or ``None``)
        drives the fit inspector as before."""
        if isinstance(payload, str):
            self.active_units.set_active(payload)
            self.inspector_vm.set_unit(payload)
            self._raise_inspector_if_auto()
        else:
            self._inspect_fit(payload)

    def _inspect_fit(self, fit) -> None:
        """Inspect a fit selected by Results or one of the plot views, retaining its unit identity."""
        self.active_units.set_active(self._unit_key_for_fit(fit))
        self.inspector_vm.set_fit(fit)
        self._raise_inspector_if_auto()

    def _raise_inspector_if_auto(self) -> None:
        """Switch the central view to the Scan/inspector page when an inspect request is routed AND
        auto-follow is on. (Full campaign-event auto-follow rules land in a later task; this is the
        selection hook only.) With auto off, the operator's chosen view is left untouched."""
        if self._center_host is not None and self._center_host.auto_follow:
            self._center_host.show_view("scan")

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
        """Re-resolve the inspected fit/unit against the just-refreshed facade after a workspace
        change, then keep the active chip in sync.

        Anchored on the inspector's OWN current selection, never on the derived ``active_units.key``:
        a FitView with no resolvable ledger unit (legacy results, or results written before any
        ingest) still carries a scan_number/detector_id identity that must survive a refresh —
        keying off the chip would strand such a selection so it never updates or clears."""
        unit_key = self.inspector_vm.current_unit
        if unit_key is not None:
            if unit_key in self.scans_vm.available_keys():
                self.inspector_vm.set_unit(unit_key)
            else:
                self.active_units.set_active(None)
                self.inspector_vm.set_fit(None)
            return
        fit = self.inspector_vm.current_fit
        if fit is None:
            return
        want_unit = getattr(fit, "unit_id", None)

        def _matches(fv) -> bool:
            fv_unit = getattr(fv, "unit_id", None)
            if want_unit and fv_unit:
                return fv_unit == want_unit          # exact unit identity when both carry it
            return fv.scan_number == fit.scan_number and fv.detector_id == fit.detector_id
        fresh = next((fv for fv in self._ws.results if _matches(fv)), None)
        self.inspector_vm.set_fit(fresh)             # None clears the inspector to its empty state
        self.active_units.set_active(self._unit_key_for_fit(fresh))

    # ---- session layout --------------------------------------------------
    def _snapshot_session(self) -> Session:
        # The empty/pending screen intentionally owns only Files + Log. Serializing that sparse
        # scaffold would overwrite the operator's complete workspace layout while New is waiting
        # for its first ingest, so retain the last full layout until a ready workspace is bound.
        # Compact mode is likewise never serialized: the stash holds the real layout in memory
        # and closeEvent expands before the final snapshot.
        if not self._empty and not self._compact_active:
            state = bytes(self.saveState(_LAYOUT_VERSION).toBase64().data()).decode("ascii")
            self._session.dock_layout_b64 = state
        # Only saved workspaces belong in recents — a temporary (pending/unsaved) workspace must
        # never pollute the recent-workspaces list (it may be discarded moments later).
        if not self._empty and not self._wsm.is_temporary():
            self._session.note_workspace(self._ws.root)
        self.heatmap_vm.save_view_state(self._session)
        self.dispersion_vm.save_view_state(self._session)
        self.waterfall_vm.save_view_state(self._session)
        # Persist the central view switcher (current page + auto-follow) alongside the other
        # view-state writes; restored on the next ready bind (default: Scan page, auto-follow on).
        if self._center_host is not None:
            self._session.view_state["center_view"] = self._center_host.current_key
            self._session.view_state["center_auto_follow"] = self._center_host.auto_follow
        return self._session

    def _restore_layout(self) -> None:
        """Restore the saved dock layout; any failure falls back to the Pass-3 default.

        ``restoreState`` is version-gated (``_LAYOUT_VERSION``): a pre-Pass-3 (unversioned) or
        future-version blob returns False atomically instead of half-applying stale geometry that
        names docks which no longer exist. The one-time cost is that upgrading users get the new
        default arrangement once, with an honest log line."""
        b64 = self._session.dock_layout_b64
        if not b64:
            return
        try:
            ok = self.restoreState(QByteArray.fromBase64(b64.encode("ascii")), _LAYOUT_VERSION)
        except Exception as exc:  # noqa: BLE001 — a bad saved layout must never block the window
            self.log(f"could not restore dock layout ({exc})")
            ok = False
        if not ok:
            self._apply_default_layout()
            self.log("applied the default Pass-3 arrangement (saved dock layout was from an "
                     "older version or unreadable)")

    def _autosave(self) -> None:
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.request()

    # ---- public helpers ---------------------------------------------------
    def log(self, msg: str) -> None:
        self.log_dock.append(msg)
        log.info("%s", msg)

    @property
    def viewmodel(self) -> ViewModel:
        return self._viewmodel

    @property
    def is_empty(self) -> bool:
        """True while the window is in the empty-start (no workspace loaded) state."""
        return self._empty

    # ---- teardown --------------------------------------------------------
    def closeEvent(self, event) -> None:
        # Leave compact mode FIRST so the session snapshot below captures the full dock layout,
        # not a hidden-everything scaffold (the compact stash is memory-only by design).
        if self._compact_active:
            self.toggle_compact_mode()
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

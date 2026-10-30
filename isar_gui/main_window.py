"""MainWindow — the application shell.

A :class:`QMainWindow` that opens one read-only workspace and wires the shell. The window has ONE
arrangement in every state (nothing appears or rearranges when the first data arrives):

* **left column**, top to bottom: the experiment strip, the files strip, the scan list (it takes the
  remaining height). It is a dock, and the left dock area owns both left corners, so it runs past the
  bottom bar. The optional scan inspector is part of it: opened, it extends the left panel outward
  beside the scan list (the display gives up that width), and its close button pulls the panel back;
* **centre**: the display (``CenterHost`` pages);
* **right panel**: the *Processing* and *Campaign* tabs, collapsible to a slim rail;
* **bottom bar**, under the centre and the right panel only: the Log on the left, the Output on the
  right.

The old Files, Scans, Browser, Card and Scan Table panels stay in the code, hidden and reachable from
**View ▸ Panels**. The window *wires*, it does not compute — anything smarter belongs in a viewmodel
(:mod:`isar_gui.viewmodels`).

The Panels menu's two-way sync (checkable action <-> dock ``visibilityChanged``) follows PUFFIN's
``view/main_window.py`` ``_init_menu_bar`` pattern (C:/Users/AMM/Documents/Github/Science/PUFFIN).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from weakref import WeakSet

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from isar_gui import collector_guard, file_dialogs
from isar_gui.active_unit import ActiveUnitController
from isar_gui.export_action import ExportController
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
from isar_gui.queue_controller import QueueController
from isar_gui.workspace_manager import (
    WorkspaceManager, default_experiment_name, experiments_root, openable)
from isar.workspace_api import (
    DEFAULT_CLOUD_FALLBACK_MODEL,
    SCHEMA_VERSION,
    BranchModelView,
    CardView,
    Workspace,
    WorkspaceNotFound,
    validate_endpoint,
)
from isar.workspace_api.references import outside_references
from isar.workspace_api.watch import last_written
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
)
from isar_gui.viewmodels.campaign import CampaignViewModel
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.inter_state import InterState
from isar_gui.viewmodels.sandbox import SandboxViewModel
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.campaign_dock import CampaignDock
from isar_gui.views.campaign_monitor import CampaignMonitor
from isar_gui.views.sandbox_pane import SandboxPane
from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.card_dock import CardDock
from isar_gui.views.attention_list import AttentionList
from isar_gui.views.center_host import CenterHost
from isar_gui.views.details_dialog import DetailsDialog
from isar_gui.views.dispersion_pane import DispersionPane
from isar_gui.views import help_window
from isar_gui.views.experiment_strip import ExperimentStrip
from isar_gui.views.files_dock import FilesDock
from isar_gui.views.files_strip import FilesStrip
from isar_gui.views.heatmap_pane import HeatmapPane
from isar_gui.views.inter_settings_dialog import InterSettingsDialog
from isar_gui.views.output_bar import OutputBar
from isar_gui.views.processing_panel import ProcessingPanel, ProcessingRail
from isar_gui.views.readiness_line import ReadinessLine
from isar_gui.views.rename_dialog import RenameDialog
from isar_gui.views.scans_page import ScansPage
from isar_gui.views.scan_inspector import ScanInspectorPanel
from isar_gui.views.scan_list import ScanListPanel
from isar_gui.views.scan_table_dock import ScanTableDock
from isar_gui.views.setup_section import RUN_TEXT, RUN_TIP, SetupSection, locate_gpi_folder, locate_resolution_folder
from isar_gui.views.scans_dock import ScansDock
from isar_gui.selection import ORIGIN_GROUP, ORIGIN_LIST, ORIGIN_OTHER
from isar_gui.watch_bridge import WatchBridge

log = logging.getLogger(__name__)


# The display's words before any scan is picked.
_EMPTY_HINT = "Select an analyzer in the scan list."

# Bump when the dock inventory/geometry contract changes: restoreState of any OTHER version fails
# atomically and the window falls back to _apply_default_arrangement (never binary-migrate Qt blobs).
# v1 (implicit, unversioned) = pre-Pass-3; v2 = Pass-3 M1 (plot docks moved into the CenterHost,
# Scans column moved left); v3 = Pass-3 M2 (the Campaign dock joins the right area); v4 = the one
# arrangement (full-height left column, Processing|Campaign right tabs, Log|Output bottom bar).
_LAYOUT_VERSION = 4


class _Arrangement(NamedTuple):
    """One screen class's default sizes (px). Algorithm settings, retuned on evidence: the geometry
    tests (tests/gui/test_window_frame.py) are their check."""
    left: int          # left column width
    inspector: int     # scan inspector width when opened (the left panel widens by it)
    right: int         # right panel width when expanded
    log_height: int    # bottom bar height
    output_width: int  # the Output half of the bottom bar


_ARRANGEMENTS = {
    "laptop": _Arrangement(left=340, inspector=260, right=300, log_height=140, output_width=360),
    "desktop": _Arrangement(left=520, inspector=420, right=440, log_height=220, output_width=480),
}
# Minimum widths (px) each column must hold: every mount's minimumSizeHint fits inside its column.
_LEFT_MIN = 280
_INSPECTOR_MIN = 220
_RIGHT_MIN = 240
_CENTER_MIN = (300, 250)       # the display (CenterHost stack), width x height
_OUTPUT_MAX_WIDTH = 520        # the Output half of the bottom bar never grows past this
# A screen at least this big (available logical px) starts with everything open. Derivation: the
# desktop arrangement needs left 520 + inspector 420 + right 440 + rail 24 = 1404 px beside a display
# of at least 400 px, so about 1800; and a 220 px log beside a display of at least 600 px plus about
# 80 px of window chrome gives 1000.
DESKTOP_FROM_WIDTH = 1800
DESKTOP_FROM_HEIGHT = 1000


def initial_arrangement(width: int, height: int) -> str:
    """``"laptop"`` or ``"desktop"`` for an available screen area in logical px (1108x851 and
    1536x826 are laptop; 1920x1040 and 2560x1392 are desktop)."""
    return "desktop" if width >= DESKTOP_FROM_WIDTH and height >= DESKTOP_FROM_HEIGHT else "laptop"


_HEALTH_WORDS = {"tavi": "TAVI (instrument control) is not running. Only needed for campaigns.",
                 "llm": "The LLM service is not running. Only needed for the optional LLM review."}

# The four central display surfaces, in switcher order: (host key, button label).
_CENTER_PAGES = (("scan", "Scan(s)"), ("plane", "Plane (q, E)"),
                 ("dispersion", "Dispersion ω(q)"), ("sandbox", "Sandbox"))
_CENTER_KEYS = tuple(key for key, _label in _CENTER_PAGES)
# Map the export menu labels to the host page keys.
_EXPORT_KEY_TO_CENTER = {"Plane (q, E)": "plane", "Dispersion ω(q)": "dispersion",
                         "Scan(s)": "scan"}


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
    def _report_dir(self):
        # No workspace root at all -> a path that can never exist, so latest_decision's is_file()
        # check reads honestly as "no decisions.jsonl" rather than AttributeError-ing.
        return Path("/nonexistent-empty-workspace/report")

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


# The watch events after which the intake reads are re-read (the intake files and what they derive from).
# What an inter-spectra overview's authority and previews depend on: its own stores, the inputs in force (user
# values, import records, gpi, calibration). A refresh only reads (hard rule 7).
_INTER_RELOAD = frozenset({"inter_user", "inter_log", "gpi", "intake_entries", "intake_scans", "card_state",
                           "snapshot", "results", "card"})
_INTAKE_RELOAD = frozenset({"snapshot", "results", "card", "card_state", "intake_entries",
                           "intake_settings", "intake_scans", "fit_inputs", "curves", "gpi"})
# The status bar's and Processing panel's word for each kind the coordinator emits.
_ACTIVITY_VERBS = {"ingest": "Ingesting", "drive": "Campaign running", "compile": "Compiling campaign",
                   "export": "Exporting"}
_CORES_TIP = ("How many processor cores ISAR may use for analysis. A lower number takes effect as running "
              "analyzers finish; a deep pass that is already running keeps its cores until you stop it.")
_INTER_SETTINGS_TIP = ("Penalties and tolerances for inter-spectra batches. They belong to this experiment; a batch "
                       "records the settings it used, so a change affects only batches started afterwards.")


def _sentence(text: str) -> str:
    """A reason as a sentence: first letter up, a full stop at the end."""
    text = text.strip()
    return text if not text else text[0].upper() + text[1:] + ("" if text.endswith((".", "…")) else ".")


# The leave question's words per action: (title, the button that stays, the button that leaves, when ISAR leaves).
_LEAVE_WORDS = {"close": ("Close ISAR?", "Keep ISAR open", "Stop and close", "before ISAR closes"),
                "open": ("Open another experiment?", "Stay in this experiment", "Stop and open",
                         "before ISAR leaves this experiment"),
                "new": ("Start a new experiment?", "Stay in this experiment", "Stop and start a new one",
                        "before ISAR leaves this experiment")}


def _are(n: int, noun: str = "") -> str:
    """``"1 analyzer is"`` / ``"5 analyzers are"``; without a noun ``"1 is"`` / ``"5 are"``."""
    noun = f" {noun}{'' if n == 1 else 'es' if noun.endswith('ch') else 's'}" if noun else ""
    return f"{n}{noun} {'is' if n == 1 else 'are'}"


def _leave_text(s, when: str) -> str:
    """What leaving the experiment now loses (design step 17), from the queue's ``LeaveSummary``."""
    # The queue counts batches inside running and waiting; the analyzer files are what is left over.
    n_run, n_wait = s.n_running - s.n_batches, s.n_waiting - s.n_batches_waiting
    mixed = bool(s.n_batches or s.n_batches_waiting)       # then "analyzers" is said in full, never "are waiting" alone
    said = []
    if n_run:
        running = f"{_are(n_run, 'analyzer')} running"
        if s.n_running_deep:
            minutes = int((s.longest_deep_s or 0.0) // 60)
            running += (f" (one deep pass, {minutes} min in)" if s.n_running_deep == 1
                        else f" ({s.n_running_deep} deep passes, the longest {minutes} min in)")
        said.append(running)
    if s.n_batches:
        said.append(f"{_are(s.n_batches, 'inter-spectra batch')} running")
    if n_wait:
        said.append(f"{_are(n_wait) if n_run and not mixed else _are(n_wait, 'analyzer')} waiting")
    if s.n_batches_waiting:
        said.append(f"{_are(s.n_batches_waiting, 'inter-spectra batch')} waiting")
    if s.n_saving:
        said.append(f"{_are(s.n_saving, 'finished result')} being saved")
    text = (", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0]) + "."
    lost = []
    if n_run:
        lost.append("stops the running analyzer, discarding its work" if n_run == 1
                    else "stops the running analyzers, discarding their work")
    if n_wait:
        lost.append("forgets the waiting ones")
    if lost:
        text += " Leaving " + (", and ".join(lost) if len(lost) > 1 else lost[0]) + "."
    batch_lost = []
    if s.n_batches:
        # the batch's process is killed at once (E13): only the rings or sweeps already saved survive
        batch_lost.append("stops the running batch at once, keeping what it has already saved"
                          if s.n_batches == 1 else
                          "stops the running batches at once, keeping what they have already saved")
    if s.n_batches_waiting:
        batch_lost.append("forgets the waiting batch" if s.n_batches_waiting == 1 else "forgets the waiting batches")
    if batch_lost:
        text += " Leaving " + (", and ".join(batch_lost) if len(batch_lost) > 1 else batch_lost[0]) + "."
    if s.n_saving:
        text += f" Every finished result is saved {when}."
    return text


def _confirm_leave(parent, title: str, text: str, stay: str, go: str) -> bool:
    """Ask before leaving stops or forgets queued analysis; the default button stays."""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, parent)
    leave = box.addButton(go, QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton(stay, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is leave


class _WindowQueue(QueueController):
    """The window's queue controller. ``add_selection`` (the Processing panel's *Fast pass* / *Deep pass*, its one
    caller) first settles an unsaved Card draft, as ``start_analysis`` does: an item fits the saved card, never a
    draft still on screen (hard rule 7). Cancel adds nothing; the automatic switch's adds do not ask."""

    def __init__(self, *args, resolve_draft, show_scan, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._resolve_draft = resolve_draft
        self._show_scan = show_scan

    def add_selection(self, pass_kind: str) -> None:
        if self._resolve_draft(f"add a {pass_kind} pass"):
            super().add_selection(pass_kind)

    def show(self, unit_key: str) -> None:
        super().show(unit_key)
        self._show_scan()


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
        self._help_window = None      # the one Help window, made on first use
        self._empty = False           # True while showing the empty-start (no workspace) state
        self.scans_page = None        # the Scan(s) page is rebuilt for each workspace binding
        self._center_host: CenterHost | None = None   # hosts the four display surfaces once bound
        self.setAcceptDrops(True)      # drop a data folder/numor onto the window -> ingest (D2/D5)

        # One permanent QStackedWidget holds the display. Page 0 is a parking page (persists across
        # rebinds like the Log dock; it holds the display's place while a binding is torn down); page 1
        # is the CenterHost, built on EVERY bind so the first screen already shows the display tabs.
        # _bind_workspace/_teardown_binding add/remove stack pages, they never swap the stack.
        # (A future page 2 — a compact monitor — is a later task.) setCentralWidget is called once,
        # in _build_frame, with the frame that carries the stack.
        self._center_stack = QStackedWidget(self)
        self._center_stack.setMinimumSize(*_CENTER_MIN)
        self._empty_hint = QLabel(_EMPTY_HINT, self)
        self._empty_hint.setObjectName("empty_hint")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._center_stack.addWidget(self._empty_hint)   # stack index 0
        self._activity_kind: str | None = None   # the coordinator's last activity kind (ingest/drive/…)
        self._renaming = False        # True while a rename re-binds: the queue keeps its waiting items
        self._leave_confirmed = False  # the leave question was answered; set for the re-entry after the queue stops
        self._export_hold = False     # an export holds the queue's writer until its first progress line
        self._inspector_width = _ARRANGEMENTS["laptop"].inspector   # remembered across open/close
        # Compact campaign-monitor mode (M3-1): the monitor is a per-binding stack page built
        # lazily on first toggle; the stash holds the full window state to restore on expand.
        self._compact_active = False
        self._compact_stash: tuple | None = None
        self._campaign_monitor = None
        self._compact_hint_shown = False
        self._close_when_operations_finish = False
        self._bound = False           # True once a first workspace binding exists (rebind guard)
        self._gui_store: GuiStore | None = None   # per-workspace GUI-only state (watched folders)
        self._reread_paths: tuple[str, ...] | None = None   # the batch "Read source file again" sent, until it ends
        self._reread_running = False                        # True while that batch's job is the one running
        # {resolved_path: (size, mtime)} of manifest files, rebuilt once per facade refresh (bind +
        # each "snapshot" watch tick). Lets the folder watcher's per-second is_known guard be an O(1)
        # dict lookup instead of resolving every manifest row every poll — the beamtime-scale freeze
        # fix. None until the first bind builds it.
        self._known_files_cache: dict[str, tuple] | None = None
        # One inspected atomic unit, deliberately separate from the table/chip multi-selection.
        # It survives ordinary workspace refreshes only while the same unit remains in the ledger.
        self.active_units = ActiveUnitController(self)

        # The experiment lifecycle owner (none/pending/ready; create, open, rename, copy). MainWindow is
        # the view: it reflects the manager's signals and never reaches into a workspace directory
        # itself. `ws` passed at construction is adopted directly (no re-open, no signal); every later
        # transition flows through the manager's emitting methods.
        self._wsm = WorkspaceManager(self)
        self._wsm.workspace_changed.connect(self._on_wsm_workspace_changed)
        self._wsm.state_changed.connect(self._on_wsm_state_changed)
        # A folder ISAR just created gets the one-time note saying what named it (after its bind).
        self._wsm.experiment_created.connect(
            lambda _root, named_from: self.experiment_strip.show_created_note(named_from))
        if ws is not None:
            self._wsm.adopt(ws)

        # One workspace-wide writer gate for init/ingest/run.  Keep the historical attribute alias
        # because FilesDock consumes a controller-shaped object; it now points at the coordinator.
        self._operations = WorkspaceOperationCoordinator(sys.executable, self._wsm, self.log, parent=self)
        self._ingest_controller = self._operations
        self._operations.ingest_started.connect(self._on_ingest_started)
        self._operations.ingest_finished.connect(self._on_ingest_report)
        self._operations.ingest_failed.connect(self._on_ingest_failed)
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
        self._init_run_menu()
        self._init_config_menu()
        self._init_backends_menu()
        self._init_view_menu()
        self._init_status_bar()
        self._init_health()
        # The Log dock is created once and PERSISTS across workspace rebinds — _teardown_binding
        # tears down every other dock, but wiping the log would lose the operator's scrollback, and
        # the drop-to-ingest bootstrap ends in a rebind (that was the "log clears on load" bug).
        # The frame docks (left column, Processing, Output) persist the same way: they carry the
        # persistent mounts. _init_docks only re-registers them into each fresh binding. Tracks docks
        # whose dockLocationChanged is already wired (WeakSet auto-drops deleted transient docks) so
        # a persistent dock isn't double-connected on every rebind.
        self._loc_wired: WeakSet = WeakSet()
        self.log_dock = LogDock(self)
        # The one Qt-side holder of the intake reads and writers (stage two); every intake panel reads it.
        # (file, sha256, problem) of each rejected gpi file the notice has shown in this GUI session.
        self._gpi_shown: set[tuple] = set()
        self.intake = IntakeState(self)
        # The inter-spectra state: one per window, beside the intake it writes through.
        self.inter = InterState(self.intake, self)
        self._build_frame()
        self._bind_workspace(ws)
        self._bound = True
        if ws is not None:
            self._session.note_workspace(ws.root)

        # Session autosave: debounced 500ms, snapshotting the live layout at fire time.
        self._autosaver = Autosaver(self._snapshot_session, parent=self, on_error=self.log)

        # The arrangement is settled synchronously, before the window is shown: every mount and dock
        # exists, so the saved layout (or the default for this screen) is applied once and nothing
        # re-arranges when the first data arrives.
        self.scan_list_vm.load_view_state(self._session)
        self.processing_panel.inter_box.load_view_state(self._session)
        self._restore_layout()
        self._sync_workspace_actions()
        QTimer.singleShot(0, self, self._surface_session_security_warnings)
    # ---- workspace binding (shared by __init__ and load_workspace) --------
    def _bind_workspace(self, ws) -> None:
        """(Re)bind the window to ``ws``: title, viewmodels, dock scaffold, Panels menu, watch
        bridge. Assumes any previously bound docks/menu/bridge were already torn down by the caller.

        ``ws=None`` binds the EMPTY-START state: an :class:`_EmptyWorkspace` backs the viewmodels
        (so every pane renders its own empty view), the display is built exactly as for a ready
        workspace (it shows its own "nothing yet" views), no watch bridge is started, and the status
        bar reads "no workspace". A later ``load_workspace`` from this state is a full bind from
        ``None``. The arrangement of the window is the same in both.
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
        self.inspector_vm = InspectorViewModel(ws, parent=self, specs=False)
        self.heatmap_vm = HeatmapViewModel(ws, parent=self)
        self.dispersion_vm = DispersionViewModel(ws, parent=self)
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
        self.scans_vm.set_intake(self.intake)
        # The campaign feed/status VM: event-log ticks fan out to it in _on_workspace_changed;
        # process liveness arrives via the window's stable drive slots (wired once in __init__ —
        # the coordinator outlives bindings, so per-bind lambdas would accumulate and dangle).
        self.campaign_vm = CampaignViewModel(ws, parent=self)
        self.campaign_vm.set_process_alive(self._operations.is_running_campaign())
        self.heatmap_vm.load_view_state(self._session)
        self.dispersion_vm.load_view_state(self._session)
        self._entropy_action = QAction("Colour by entropy", self)
        self._entropy_action.setCheckable(True)
        self._entropy_action.setChecked(self.heatmap_vm.color_by_entropy)
        self._entropy_action.toggled.connect(self.heatmap_vm.set_color_by_entropy)
        self._entropy_action.toggled.connect(self.dispersion_vm.set_color_by_entropy)

        # The four display surfaces live as switchable pages of the central CenterHost. They are built
        # in EVERY state, over the null workspace when none is open, so the first screen already shows
        # the display tabs and each pane its own "nothing yet" view (never a hint page that vanishes
        # when data arrives).
        self._build_center_host()
        # Every route that changes the active spectrum passes through the selection bridge.
        # This keeps the Results tree, physical chips, and table honest about the one detector in the
        # scan inspector, without treating a multi-selection as an inspection request.
        self.browser_vm.selection_changed.connect(self._inspect_fit)
        # Plane and dispersion picks route through the same active-spectrum bridge.
        self.heatmap_vm.point_activated.connect(self._inspect_fit)
        self.dispersion_vm.point_activated.connect(self._inspect_fit)

        self._docks = {}
        self._init_docks()
        self._init_docks_menu()
        # The arrangement itself is applied by __init__ (first bind) or carried across by _rebind
        # (later binds): _init_docks only places each dock in its area.

        # A pending experiment is a real folder with nothing read into it yet; ``root`` is None only
        # with no experiment at all.
        root = self._wsm.root() if self._wsm.state() != "none" else None
        if root is not None and self._empty:
            self.statusBar().showMessage(f"Experiment {root.name} — drop data files to begin")
            self.log(f"Experiment {root}: no files read into it yet — drop data to begin")
        elif root is None:
            self.statusBar().showMessage("No experiment open")
            self.log("No experiment open — New experiment…, Open experiment… or drop data to begin")
        else:
            self.statusBar().showMessage(f"Experiment: {ws.root}")
            self.log(f"Opened experiment {ws.root}")

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
        else:
            self._gui_store = None
        self.scans_page.set_store(self._gui_store)
        self.scans_page.store_dirty.connect(self._on_groups_changed)
        self.scan_list_vm.set_store(self._gui_store)      # the scans this experiment's list has removed
        self.scans_page.load_view_state(self._session)
        self._seed_folder_watcher()

        # Watch bridge: refresh + fan out on the Qt thread. Optional (tests may disable it). Never
        # started in the empty state — there is no workspace on disk to watch.
        self._bridge = None
        self._start_watch_bridge()

        # The persistent mounts learn of the binding.
        self.scan_list_vm.set_workspace(self._ws)
        self.experiment_strip.set_experiment(root)
        self.intake.bind(root)
        self.inter.bind(root)
        self._queue.bind(root, auto_fast=True if self._gui_store is None else self._gui_store.auto_fast_pass(),
                         keep_waiting=self._renaming)
        self.files_strip.bind(self.files_vm, root is not None)   # experiment_open
        self.output_bar.set_experiment(root)
        self._show_outside_references()
        self._note_last_written()
        self._sync_run_actions()  # the entry point depends on the empty state and what is ready
        self._sync_workspace_actions()

    def _seed_folder_watcher(self) -> None:
        """Seed the folder watcher from the bound experiment's persisted config (a bind is not a user
        mutation, so it must not re-persist the store; user add/remove/pause go through
        watch_config_changed). With no GUI store (none/pending) it watches nothing."""
        if self._gui_store is not None:
            self._folder_watcher.configure(
                self.files_vm.watched(), self.files_vm.watch_paused(), reset_binding=True)
        else:
            self._folder_watcher.configure([], False, reset_binding=True)

    def _start_watch_bridge(self) -> None:
        """Start the watch bridge on the bound workspace (ready only, and only when watching is on)."""
        if self._watch_enabled and not self._empty:
            self._bridge = WatchBridge(self._ws, parent=self, **self._watch_kwargs)
            self._bridge.changed.connect(self._on_workspace_changed)
            self._bridge.start()
            self._live_label.setText("watching (no changes yet)")
        else:
            self._live_label.setText("")

    def _stop_watch_bridge(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None

    def _note_last_written(self) -> None:
        """Show on the Output bar when the experiment's watched files were last written (a stat)."""
        root = self._wsm.root()
        stamp = None if root is None else last_written(root)
        if stamp is not None:
            self.output_bar.note_written(datetime.fromtimestamp(stamp))

    def _show_outside_references(self) -> None:
        """Say in the experiment strip and the Log when an opened experiment's source data folders
        are gone. Ready only: a pending experiment has no manifest. A missing resolution folder is
        not shown here (today's held reasons already say it)."""
        missing = () if self._empty else outside_references(self._ws).missing_source_folders
        if not missing:
            self.experiment_strip.set_outside_warning(None)
            return
        tail = "stored scans still show; reading or re-running them needs it"
        folder, n = missing[0]
        more = f" and {len(missing) - 1} more" if len(missing) > 1 else ""
        self.experiment_strip.set_outside_warning(
            f"Source data folder not found: {folder} ({n} files){more} — {tail}",
            "Source data folders not found:\n" + "\n".join(f"{f} ({k} files)" for f, k in missing))
        for f, k in missing:
            self.log(f"Source data folder not found: {f} ({k} files) — {tail}")

    def _build_center_host(self) -> None:
        """Build the CenterHost with its four display surfaces, register it as the stack's ready page,
        restore the persisted view + auto-follow, and make it the visible central page."""
        host = CenterHost(self)
        self.scans_page = ScansPage(self.scan_list_vm, self._ws, parent=self)
        self.scans_page.set_inter(self.inter)
        host.add_page("scan", "Scan(s)", self.scans_page)
        host.add_page(
            "plane", "Plane (q, E)",
            HeatmapPane(self.heatmap_vm, entropy_action=self._entropy_action))
        host.add_page(
            "dispersion", "Dispersion ω(q)",
            DispersionPane(self.dispersion_vm, entropy_action=self._entropy_action))
        host.add_page("sandbox", "Sandbox", SandboxPane(self.sandbox_vm))
        # Restore the persisted view state (default: Scan(s), auto-follow on). Set
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

    def _on_selection_active(self, key: str | None) -> None:
        """Forward the one active spectrum to the legacy single-spectrum surfaces."""
        self.active_units.set_active(key)
        if key is None:
            self.inspector_vm.set_fit(None)
            return
        row = self.scan_list_vm.snapshot.spectrum(key)
        if row is not None and row.fit is not None:
            self.inspector_vm.set_fit(row.fit)
        else:
            self.inspector_vm.set_unit(key)

    def _on_selection_changed(self, change) -> None:
        """Follow list gestures and group recall while automatic page following is on."""
        if (change.origin in (ORIGIN_LIST, ORIGIN_GROUP) and self._center_host is not None
                and self._center_host.auto_follow):
            self._center_host.show_view("scan")

    def _on_group_key(self, slot: int, save: bool) -> None:
        if self._center_host is None:
            return
        page = self._center_host.pane("scan")
        page.groups.key_pressed(slot, save)

    def _update_title(self) -> None:
        """``ISAR — <folder name>`` for a pending or ready experiment, ``ISAR — no experiment``
        otherwise."""
        root = self._wsm.root() if self._wsm.state() != "none" else None
        self.setWindowTitle("ISAR — no experiment" if root is None else f"ISAR — {root.name}")

    # ---- the frame: persistent mounts, central widget, frame docks ----------
    def _build_frame(self) -> None:
        """Build the window's persistent mounts and frame once and wire them to the window.

        Every mount lives for the life of the window (like the Log dock): it learns of each binding
        through ``bind`` / ``set_experiment`` / ``set_workspace`` and of each change through
        ``on_workspace_changed``, and the window tears down only per-binding things. The wiring here
        is complete: a mount never needs a wire added later.
        """
        # ---- the mounts ----
        self.experiment_strip = ExperimentStrip(self)
        # The scan list re-reads (S2U4) on both intake signals; it is wired before the panels that take their
        # scan-set labels from it, so those read a fresh list (Qt runs slots in connection order).
        self.scan_list_vm = ScanListViewModel(_EmptyWorkspace(), self)
        self.selection = self.scan_list_vm.selection
        self.selection.active_changed.connect(self._on_selection_active)
        self.selection.changed.connect(self._on_selection_changed)
        self.scan_list_vm.set_intake(self.intake)
        self.scan_list_vm.set_inter(self.inter)
        self.intake.changed.connect(lambda: self.scan_list_vm.on_workspace_changed(None))
        self.intake.worker_changed.connect(lambda: self.scan_list_vm.on_workspace_changed(None))
        self.setup_section = SetupSection(self.intake)
        self.attention_list = AttentionList(self.intake, self._scan_label)
        saved = self._session.view_state.get("attention_list")
        if isinstance(saved, dict) and isinstance(saved.get("expanded"), bool):
            self.attention_list.set_expanded(saved["expanded"])
        self.files_strip = FilesStrip(self)
        self.scan_list = ScanListPanel(self.scan_list_vm)
        self.scan_list_vm.store_dirty.connect(self._on_groups_changed)
        self.scan_list.read_again_requested.connect(self._read_source_again)
        self.scan_inspector = ScanInspectorPanel(self.scan_list_vm)
        self.scan_inspector.set_intake(self.intake)
        self.scan_inspector.setMinimumWidth(_INSPECTOR_MIN)
        self.scan_inspector.hide()          # opens closed; the window owns its visibility and width
        self.processing_panel = ProcessingPanel(self)
        self.readiness_line = ReadinessLine(self.intake, self._scan_label)
        self.processing_panel.set_readiness_widget(self.readiness_line)
        self.processing_panel.setMinimumWidth(_RIGHT_MIN)
        self.processing_rail = ProcessingRail(self)
        self.output_bar = OutputBar(self)
        self._export = ExportController(self._operations, self)
        # The analysis queue: the one owner of queue state; the panel, the rail and the scan list read it.
        self._queue = _WindowQueue(
            sys.executable, self.intake, self.scan_list_vm, cores=self._cores_setting(),
            blocked=lambda: ("a campaign is running in this experiment"
                             if self._operations.has_campaign() else None),
            resolve_draft=self._resolve_dirty_card, show_scan=self._raise_inspector_if_auto, parent=self)
        self.processing_panel.set_controller(self._queue)
        self.processing_panel.set_inter(self.inter, self.scan_list_vm)
        self.processing_rail.set_controller(self._queue)
        self.scan_list_vm.set_queue(self._queue)
        self._queue.log_line.connect(self.log)
        self._queue.auto_fast_changed.connect(self._on_auto_fast_changed)
        self._queue.changed.connect(self._push_run_row)
        self._queue.results_saved.connect(self._on_results_saved)
        self.intake.saved.connect(lambda _message: self.inter.reload())        # a saved value can make a refit stale
        self._operations.set_writer_guard(self._campaign_refusal)

        self._group_shortcuts = []
        for slot in range(1, 11):
            for save, sequence in ((True, f"Ctrl+{slot % 10}"), (False, str(slot % 10))):
                shortcut = QShortcut(QKeySequence(sequence), self)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(
                    lambda n=slot, saving=save: self._on_group_key(n, saving))
                self._group_shortcuts.append(shortcut)

        # ---- the left column: experiment strip, setup section, needs-attention list, files strip, then
        # the scan list (all the rest) ----
        column = QWidget(self)
        column.setObjectName("left_column")
        column.setMinimumWidth(_LEFT_MIN)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(0)
        column_layout.addWidget(self.experiment_strip, 0)
        column_layout.addWidget(self.setup_section, 0)
        column_layout.addWidget(self.attention_list, 0)
        column_layout.addWidget(self.files_strip, 0)
        column_layout.addWidget(self.scan_list, 1)

        # ---- the left panel: the column, and beside it the scan inspector (hidden until opened) ----
        # The inspector is part of the left panel, not a column of its own: opening it widens the
        # left dock by its width and closing it pulls the dock back (_open/_close_scan_inspector).
        # Stretch puts a dragged dock edge on the inspector, never on the scan list.
        self._left_split = QSplitter(Qt.Horizontal, self)
        self._left_split.setChildrenCollapsible(False)
        self._left_split.addWidget(column)
        self._left_split.addWidget(self.scan_inspector)
        self._left_split.setStretchFactor(0, 0)
        self._left_split.setStretchFactor(1, 1)
        self._left_split.splitterMoved.connect(self._on_left_splitter_moved)

        # ---- the central frame: the display, then the rail ----
        frame = QWidget(self)
        frame.setObjectName("central_frame")
        frame_layout = QHBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        frame_layout.addWidget(self._center_stack, 1)
        frame_layout.addWidget(self.processing_rail, 0)
        self.setCentralWidget(frame)

        # ---- the frame docks: persistent, never closable, floatable or lost ----
        # The left dock area owns both left corners, so the left column runs the full height of the
        # window and the bottom bar (Log | Output) spans only the centre and the right panel.
        self.setCorner(Qt.TopLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setTabPosition(Qt.RightDockWidgetArea, QTabWidget.TabPosition.North)
        self.left_column_dock = self._frame_dock("left_column_dock", "Experiment", self._left_split)
        self.left_column_dock.setTitleBarWidget(QWidget(self.left_column_dock))   # no title bar
        self.processing_dock = self._frame_dock("processing_dock", "Processing", self.processing_panel)
        self.output_dock = self._frame_dock("output_dock", "Output", self.output_bar)
        # The bottom bar splits by ratio when the window resizes; capping the Output half keeps the Log
        # the wide one whatever size the window had when the default arrangement was applied.
        self.output_dock.setMaximumWidth(_OUTPUT_MAX_WIDTH)
        self.campaign_dock = None           # built per binding (it is tied to the binding's viewmodel)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_column_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.processing_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.output_dock)
        self.splitDockWidget(self.log_dock, self.output_dock, Qt.Horizontal)
        for dock in (self.left_column_dock, self.processing_dock, self.output_dock):
            self._wire_dock_autosave(dock)
        self.processing_dock.visibilityChanged.connect(self._sync_rail)

        # ---- the wiring (complete) ----
        self.experiment_strip.new_requested.connect(self.new_workspace)
        self.experiment_strip.open_requested.connect(self._on_open_workspace)
        self.experiment_strip.recent_chosen.connect(self.load_workspace)
        self.experiment_strip.rename_requested.connect(self.rename_experiment)
        self.experiment_strip.details_requested.connect(self._open_details)
        self.setup_section.locate_requested.connect(self._locate_resolution_folder)
        self.setup_section.gpi_locate_requested.connect(self._locate_gpi_folder)
        self.setup_section.run_requested.connect(lambda: self.start_analysis())   # looked up at the click
        self.attention_list.locate_requested.connect(self._locate_resolution_folder)
        self.attention_list.add_data_requested.connect(self.files_strip.choose_files)
        self.attention_list.run_requested.connect(lambda: self.start_analysis())
        self.readiness_line.locate_requested.connect(self._locate_resolution_folder)
        self.readiness_line.reveal_gap.connect(self.attention_list.reveal)
        self.readiness_line.run_requested.connect(lambda: self.start_analysis())
        for changed in (self.intake.changed, self.intake.worker_changed, self.scan_list_vm.snapshot_changed):
            changed.connect(self._push_run_row)
        self.intake.saved.connect(self.log)
        self.inter.saved.connect(self.log)
        self.intake.changed.connect(self._announce_gpi_rejections)
        self.files_strip.paths_dropped.connect(self._on_paths_dropped)
        self.files_strip.log_message.connect(self.log)
        self._folder_watcher.status_changed.connect(self.files_strip.set_watch_status)
        self.scan_list_vm.inspect_requested.connect(self._inspect_from_list)
        self.scan_inspector.close_requested.connect(self._close_scan_inspector)
        self.output_bar.save_copy_requested.connect(self.save_copy_as)
        self.output_bar.export_requested.connect(self._on_export_requested)
        self.output_bar.open_copy_requested.connect(lambda path: self.load_workspace(str(path)))
        self._export.started.connect(lambda: self.output_bar.show_export_progress("Export started…"))
        self._export.started.connect(self._sync_workspace_actions)
        self._export.progress.connect(self.output_bar.show_export_progress)
        self._export.progress.connect(self.log)
        self._export.finished.connect(self.output_bar.show_export_done)
        self._export.finished.connect(lambda folder: self.log(f"Export written to {folder}"))
        self._export.finished.connect(self._sync_workspace_actions)
        self._export.failed.connect(self.output_bar.show_export_failed)
        self._export.failed.connect(lambda reason, path: self.log(f"Export failed: {reason}"))
        self._export.failed.connect(self._sync_workspace_actions)
        # The export guard: one export is made from one state of the intake records.
        self._export.started.connect(lambda: self.intake.set_busy_reason("an export is running"))
        self._export.finished.connect(lambda _folder: self.intake.set_busy_reason(None))
        self._export.failed.connect(lambda _reason, _path: self.intake.set_busy_reason(None))
        # The export has read what the queue's writer writes before its first progress line (isar.report.export).
        for signal in (self._export.progress, self._export.finished, self._export.failed):
            signal.connect(self._release_export_hold)
        self.processing_panel.run_requested.connect(lambda: self.start_analysis())
        self.processing_panel.batch_requested.connect(self.start_inter_batch)
        self.processing_rail.toggled.connect(
            lambda: self.set_right_panel_collapsed(not self._right_panel_collapsed()))
        self._refresh_recents_menu()
        self._push_run_row()

    def _scan_label(self, key: str) -> str:
        """A scan set's label from the scan list's current read; the key stands in for an unknown set."""
        ss = self.scan_list_vm.snapshot.scan_set(key)
        return key if ss is None else ss.label

    def _locate_resolution_folder(self) -> None:
        """*Resolution curves (rf)…* (setup line, attention list, readiness list): the one route; a refused save is
        shown in the setup section (and, like every save, in the Log)."""
        self.setup_section.show_save_outcome(locate_resolution_folder(self, self.intake))

    def _locate_gpi_folder(self) -> None:
        """*gpi files…* on the setup line: the one route that saves a gpi folder the user picks; a refused
        save is shown in the setup section (and, like every save, in the Log)."""
        self.setup_section.show_save_outcome(locate_gpi_folder(self, self.intake))

    def _announce_gpi_rejections(self) -> None:
        """One non-modal notice for the gpi files newly rejected since the last notice in this GUI session: a refresh
        that finds nothing new shows nothing, and a file whose problem changed shows again."""
        gpi = None if self.intake.summary is None else self.intake.summary.gpi
        if gpi is None:
            return
        rejected = list(dict.fromkeys((s.file, s.sha256, s.problem) for s in gpi.scans.values() if not s.accepted))
        fresh = [k for k in rejected if k not in self._gpi_shown]
        if not fresh:
            return
        self._gpi_shown.update(fresh)
        text = ("ISAR did not use these gpi files:\n\n" + "\n".join(f"• {problem}" for _f, _sha, problem in fresh)
                + "\n\nNothing from a file listed here is used: no Q, slit or curve set for its scan sets. Fix the "
                  "file, then add the data again or start the analysis; ISAR reads it again then.")
        box = QMessageBox(QMessageBox.Icon.Warning, "gpi files not used", text, QMessageBox.StandardButton.Ok, self)
        box.setObjectName("gpi_notice")
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setWindowModality(Qt.WindowModality.NonModal)   # set before showing: Qt ignores a change once visible
        box.show()                              # never exec(): the window and the suite keep running

    def _open_details(self) -> None:
        """*Details…* on the strip and *Add details* in the one-time note."""
        root = self._wsm.root()
        if root is None:
            return
        dialog = DetailsDialog(self.intake, root.name, root, self)
        dialog.rename_requested.connect(self.rename_experiment)
        dialog.exec()
        dialog.deleteLater()

    def _frame_dock(self, name: str, title: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        dock.setWidget(widget)
        return dock

    def _scroll_campaign(self, dock) -> None:
        """Wrap the Campaign dock's content in a scroll area. Its natural minimum (about 624x518 px)
        fits no laptop right panel; the content is unchanged and scrolls instead."""
        scroll = QScrollArea(dock)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(_RIGHT_MIN)
        scroll.setWidget(dock.widget())
        dock.setWidget(scroll)

    # ---- dock scaffold ----------------------------------------------------
    def _init_docks(self) -> None:
        # The Log and Processing docks are created once in __init__ and survive rebinds; only
        # re-register them into this binding's docks map + Panels menu (do NOT recreate them — that
        # wiped the Log's scrollback).
        self._register_dock("Log", self.log_dock)

        # The old Files dock (drop target + manifest + watched folders) stays in the code, hidden and
        # reachable from View > Panels; a drop on it goes through the window's one drop route.
        self.files_dock = FilesDock(self.files_vm, SimpleNamespace(request_ingest=self._on_paths_dropped),
                                    self)
        self._folder_watcher.status_changed.connect(self.files_dock.set_watch_status)
        self.files_dock.set_watch_status(self._folder_watcher.status())
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
        self.campaign_dock.visibilityChanged.connect(self._sync_rail)
        self._scroll_campaign(self.campaign_dock)
        self._register_dock("Campaign", self.campaign_dock)
        self._register_dock("Processing", self.processing_dock)

        if not self._empty:
            self.browser_dock = BrowserDock(
                self.browser_vm, self, unit_key_for_fit=self._unit_key_for_fit)
            self.browser_vm.selection_changed.connect(self.campaign_dock.set_selected_fit)
            self.active_units.active_changed.connect(self.browser_dock.set_active_key)
            self.browser_dock.set_active_key(self.active_units.key)
            self._register_dock("Browser", self.browser_dock)

            # The card editor (raw TOML, D2).
            self.card_dock = CardDock(self._ws, self)
            # The card remains editable during a run/ingest, but saving/reloading it must not race the
            # engine's on-disk policy read.  The dock owns the precise button state.
            self.card_dock.set_operation_busy(self._operations.is_busy())
            generation = self._wsm.generation()
            root = self._wsm.root()
            self.card_dock.saved.connect(
                lambda path, g=generation, r=root: self._on_card_saved(path, g, r))
            self._register_dock("Card", self.card_dock)

            # The Phase-E physical scan view + its tabular twin (they share one model + selection, so
            # a chip and a row light up together). Kept, hidden, beside the new scan list.
            self.scans_dock = ScansDock(self.scans_vm, self)
            self.active_units.active_changed.connect(self.scans_dock.set_active_key)
            self.scans_dock.set_active_key(self.active_units.key)
            self.scans_dock.send_group_requested.connect(self._on_send_group)   # "Send to" routing (2A)
            self.scans_dock.scan_drag_active.connect(self._on_scan_drag)        # drag-reveal on plots (R12)
            self.scans_dock.fit_requested.connect(self._on_fit_requested)       # sandbox preview fit (2B)
            self._register_dock("Scans", self.scans_dock)
            self.scan_table_dock = ScanTableDock(self.scans_vm, self)
            self.active_units.active_changed.connect(self.scans_vm.model.set_active_key)
            self.scans_vm.model.set_active_key(self.active_units.key)
            self._register_dock("Scan Table", self.scan_table_dock)

        # Scan(s), Plane, Dispersion and Sandbox are switchable pages of the central CenterHost,
        # built in _bind_workspace (see _build_center_host).
        self._place_docks()

    def _old_panels(self) -> list:
        """The campaign-era panels: parked (hidden, out of the layout) by default and reachable from
        View > Panels, which shows one as a tab behind the left column (their code is unchanged)."""
        return [dock for dock in (getattr(self, name, None) for name in
                                  ("files_dock", "scans_dock", "browser_dock", "card_dock",
                                   "scan_table_dock")) if dock is not None]

    def _show_panel(self, dock) -> None:
        """View > Panels shows ``dock``. A parked old panel joins the left column as a tab first."""
        if dock in self._old_panels() and self.dockWidgetArea(dock) == Qt.NoDockWidgetArea:
            dock.setFloating(False)
            self.addDockWidget(Qt.LeftDockWidgetArea, dock)
            self.tabifyDockWidget(self.left_column_dock, dock)
        dock.show()
        dock.raise_()

    def _hide_panel(self, dock) -> None:
        """View > Panels hides ``dock``. An old panel is parked again: a hidden tab left in the left
        column would keep its tab bar on screen."""
        if dock in self._old_panels():
            self.removeDockWidget(dock)         # hides it and takes it out of the layout
        else:
            dock.hide()

    def _place_docks(self) -> None:
        """Put every dock in its area (idempotent): the left column, Processing and Campaign
        tabbed on the right, the Log | Output bottom bar, and the old panels parked. Sizes, and
        whether the right panel is open, are not touched here."""
        for dock in (self.left_column_dock, self.processing_dock, self.output_dock, self.log_dock):
            dock.setFloating(False)
        for dock in (self.left_column_dock, self.output_dock, self.log_dock):
            dock.show()
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_column_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.processing_dock)
        if self.campaign_dock is not None:
            self.campaign_dock.setFloating(False)
            self.addDockWidget(Qt.RightDockWidgetArea, self.campaign_dock)
            self.tabifyDockWidget(self.processing_dock, self.campaign_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.output_dock)
        self.splitDockWidget(self.log_dock, self.output_dock, Qt.Horizontal)
        for dock in self._old_panels():
            self._hide_panel(dock)
        self.left_column_dock.raise_()
        self.processing_dock.raise_()

    def _available_size(self) -> tuple[int, int]:
        """The screen's available area in logical px (the seam the geometry tests patch)."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return 1280, 800
        area = screen.availableGeometry()
        return area.width(), area.height()

    def _apply_default_arrangement(self, kind: str) -> None:
        """The one arrangement at ``kind``'s ("laptop" or "desktop") default sizes (idempotent).

        Every frame dock in its area, the old panels hidden behind the left column, the right panel
        collapsed to its rail on a laptop and open on a desktop, the inspector closed with its
        remembered width set to the kind's default."""
        sizes = _ARRANGEMENTS[kind]
        self._place_docks()
        self._close_scan_inspector()        # remembers the open width: reset the width after it
        self._inspector_width = sizes.inspector
        self.resizeDocks([self.left_column_dock], [sizes.left], Qt.Horizontal)
        self.resizeDocks([self.log_dock, self.output_dock],
                         [max(self.width() - sizes.left - sizes.output_width, _CENTER_MIN[0]),
                          sizes.output_width], Qt.Horizontal)
        self.resizeDocks([self.log_dock], [sizes.log_height], Qt.Vertical)
        self.set_right_panel_collapsed(kind == "laptop")
        if kind != "laptop":
            self.resizeDocks([self.processing_dock], [sizes.right], Qt.Horizontal)

    # ---- the right panel and its rail -----------------------------------------
    def _right_panel_collapsed(self) -> bool:
        """Collapsed = both right docks hidden. Reads ``isHidden()``, not ``isVisible()``: a buried
        tab reports invisible without being hidden."""
        campaign = getattr(self, "campaign_dock", None)
        return self.processing_dock.isHidden() and (campaign is None or campaign.isHidden())

    def set_right_panel_collapsed(self, collapsed: bool) -> None:
        """Collapse the right panel to its rail, or expand it (Processing raised) at the default
        width of the current screen. Expanding takes width from the display, never draws over it."""
        for dock in (self.processing_dock, getattr(self, "campaign_dock", None)):
            if dock is not None:
                dock.setVisible(not collapsed)
        if not collapsed:
            self.processing_dock.raise_()
            kind = initial_arrangement(*self._available_size())
            self.resizeDocks([self.processing_dock], [_ARRANGEMENTS[kind].right], Qt.Horizontal)
        self._sync_rail()
        self._autosave()

    def _sync_rail(self, *_args) -> None:
        """Keep the rail and the View > Processing panel action in step with the docks."""
        collapsed = self._right_panel_collapsed()
        self.processing_rail.set_collapsed(collapsed)
        self._processing_panel_action.setChecked(not collapsed)

    # ---- the scan inspector, inside the left panel -----------------------------
    def _column_width(self) -> int:
        """The left panel's width without the inspector: the scan-list column's own."""
        return max(_LEFT_MIN, self._left_split.widget(0).width())

    def _inspect_from_list(self, key=None) -> None:
        """The list's Inspect: on a laptop the inspector opens with Processing collapsed to its rail, so
        the display keeps its width (the user may re-expand it). A compact-mode restore does not come here."""
        if (self.scan_inspector.isHidden() and not self._right_panel_collapsed()
                and initial_arrangement(*self._available_size()) == "laptop"):
            self.set_right_panel_collapsed(True)
            QApplication.sendPostedEvents(self, QEvent.Type.LayoutRequest)   # the room is measured next
        self._open_scan_inspector(key)

    def _open_scan_inspector(self, key=None) -> None:
        """Open the inspector beside the scan list: the left panel widens by the inspector's width and
        the display gives that width up. The width is the remembered one, the smaller of it and what
        leaves the display its minimum, so the window never grows to make room."""
        if not self.scan_inspector.isHidden():
            return
        col = self._column_width()
        handle = self._left_split.handleWidth()
        room = self._center_stack.width() - _CENTER_MIN[0] - handle
        width = max(_INSPECTOR_MIN, min(self._inspector_width, room))
        self.scan_inspector.show()
        self._left_split.setSizes([col, width])
        self.resizeDocks([self.left_column_dock], [col + width + handle], Qt.Horizontal)

    def _close_scan_inspector(self) -> None:
        """Close the inspector (remembering its width): the left panel pulls back to the scan list's
        width and the display regains the room."""
        if not self.scan_inspector.isHidden():
            self._remember_inspector_width()
            col = self._column_width()
            self.scan_inspector.hide()
            # The dock's minimum width still counts the hidden inspector until its pending layout
            # request runs, and resizeDocks clamps to it: run the request now.
            for obj in (self._left_split, self.left_column_dock, self):
                QApplication.sendPostedEvents(obj, QEvent.Type.LayoutRequest)
            self.resizeDocks([self.left_column_dock], [col], Qt.Horizontal)

    def _remember_inspector_width(self) -> None:
        if not self.scan_inspector.isHidden() and self.scan_inspector.width() >= _INSPECTOR_MIN:
            self._inspector_width = self.scan_inspector.width()

    def _on_left_splitter_moved(self, _pos: int, _index: int) -> None:
        self._remember_inspector_width()
        self._autosave()

    # ---- compact campaign-monitor mode (M3-1) ------------------------------
    def toggle_compact_mode(self, *_a) -> None:
        """Swap between the full workspace and the glanceable campaign monitor (AD-14).

        Enter: stash ``saveState``+geometry, hide every dock, flip the central stack to the
        monitor page, shrink to a corner-of-the-screen card. Leave: restore the stash wholesale.
        The stash lives only in memory — ``_snapshot_session`` is guarded while compact, so the
        session file always carries the FULL layout (close-while-compact expands first)."""
        if not self._compact_active:
            if self._empty:
                self.statusBar().showMessage("Mini monitor needs an open workspace")
                self._compact_mode_action.setChecked(False)
                return
            if self._campaign_monitor is None:
                self._campaign_monitor = CampaignMonitor(
                    self.campaign_vm, self.heatmap_vm, self._operations, parent=self)
                self._campaign_monitor.expand_requested.connect(self.toggle_compact_mode)
                self.log_dock.appended.connect(self._campaign_monitor.append_log_line)
                self._center_stack.addWidget(self._campaign_monitor)
            # The stash holds the closed arrangement (the left panel without the inspector); coming
            # back reopens the inspector, which widens the left panel again.
            inspector_was_open = not self.scan_inspector.isHidden()
            self._close_scan_inspector()
            self._compact_stash = (self.saveState(_LAYOUT_VERSION), self.saveGeometry(),
                                   inspector_was_open)
            for dock in self.findChildren(QDockWidget):
                dock.hide()
            # The rail would pin the minimum width of the small card.
            self.processing_rail.hide()
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
            self.processing_rail.show()      # explicit — never depend on the restore for it
            if self._center_host is not None:
                self._center_stack.addWidget(self._center_host)   # re-attach the detached page
                self._center_stack.setCurrentWidget(self._center_host)
            else:
                self._center_stack.setCurrentWidget(self._empty_hint)
            if self._compact_stash is not None:
                state, geometry, inspector_was_open = self._compact_stash
                self._compact_stash = None
                self.restoreGeometry(geometry)
                if not self.restoreState(state, _LAYOUT_VERSION):
                    # a stash can't be stale, but never half-apply
                    self._apply_default_arrangement(initial_arrangement(*self._available_size()))
                if inspector_was_open:
                    # The restored docks take their sizes on the next layout pass; the inspector is
                    # sized from them, so it opens after that pass.
                    QTimer.singleShot(0, lambda: None if self._compact_active
                                      else self._open_scan_inspector())
                self._sync_rail()
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
            self.statusBar().showMessage("Tip: View ▸ Mini monitor shows campaign progress in a small window")

    def _register_dock(self, name: str, dock) -> None:
        """Track a dock so the Panels menu and layout autosave pick it up (call before menu build)."""
        self._docks[name] = dock
        self._wire_dock_autosave(dock)

    def _wire_dock_autosave(self, dock) -> None:
        # A user dragging a dock to a new area/float changes the layout saveState() would capture;
        # autosave (debounced) so that move survives a restart without waiting for an explicit toggle.
        # Guard against double-wiring: the persistent docks are re-registered on every rebind, and
        # a second connection would fire _autosave twice per move.
        if dock not in self._loc_wired:
            dock.dockLocationChanged.connect(lambda _area: self._autosave())
            self._loc_wired.add(dock)

    # ---- File menu (built once; actions persist across workspace reloads) --
    def _init_file_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        # The same window slots as the experiment strip and the Output bar.
        self._new_workspace_menu_action = file_menu.addAction("New experiment…")
        self._new_workspace_menu_action.triggered.connect(self.new_workspace)
        self._open_workspace_menu_action = file_menu.addAction("Open experiment…")
        self._open_workspace_menu_action.triggered.connect(self._on_open_workspace)
        self._rename_menu_action = file_menu.addAction("Rename experiment…")
        self._rename_menu_action.triggered.connect(self.rename_experiment)
        self._save_copy_menu_action = file_menu.addAction("Save experiment copy…")
        self._save_copy_menu_action.setToolTip("Copy this whole experiment folder to a new place. You keep "
                                               "working in the original.")
        self._save_copy_menu_action.triggered.connect(self.save_copy_as)

        file_menu.addSeparator()
        report_action = file_menu.addAction("Open HTML report")
        report_action.setToolTip("Open report/report.html of this experiment in your browser.")
        report_action.triggered.connect(self._open_report)
        export_menu = file_menu.addMenu("Save view as image")
        export_menu.setToolTip("Save the chosen display as a PNG picture.")
        for label, key in (("Scan(s)", "Scan(s)"), ("Plane (q, E)", "Plane (q, E)"),
                           ("Dispersion", "Dispersion ω(q)")):
            act = export_menu.addAction(label)
            act.triggered.connect(lambda _checked=False, k=key: self._export_view(k))

        for menu in (file_menu, export_menu):
            menu.setToolTipsVisible(True)
        help_menu = self.menuBar().addMenu("&Help")
        self._guide_action = help_menu.addAction("Quick guide")
        self._guide_action.triggered.connect(lambda: self._show_help(help_window.GUIDE))
        self._words_action = help_menu.addAction("What the status words mean")
        self._words_action.triggered.connect(lambda: self._show_help(help_window.WORDS))
        help_menu.addSeparator()
        open_log_action = help_menu.addAction("Open GUI Log Folder")
        open_log_action.triggered.connect(self._open_gui_log_folder)

    def default_save_folder(self) -> Path | None:
        """Where a save dialog offered a bare file name opens: the open experiment's folder (see file_dialogs)."""
        return None if self._empty else Path(self._ws.root)

    def _show_help(self, page: str) -> None:
        """Help pages open in one reused, non-modal window (never exec())."""
        if self._help_window is None:
            self._help_window = help_window.HelpWindow(self)
        self._help_window.show_page(page)

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
        start = experiments_root()
        start_dir = str(start) if start.is_dir() else ""
        # Non-native, like every other experiment dialog here: the Windows-native directory picker
        # refuses a typed path whose folder does not exist yet and offers no way to make one, so
        # the operator had to leave the application and create the folder in Explorer first.
        path = file_dialogs.get_existing_directory(self, "Open experiment", start_dir)
        if path:
            self.load_workspace(path)

    # ---- the experiment strip's recents and the availability rule -----------
    def _refresh_recents_menu(self) -> None:
        """Hand the experiment strip the GUI session's recent-workspaces list (most recent first)."""
        self.experiment_strip.set_recent(list(self._session.recent_workspaces))

    def _availability(self) -> tuple[bool, str, str | None]:
        """The one availability rule for *Save copy as…* and *Export…* (first match wins).

        Returns ``(available, reason, strip_busy)``. The reason is shown beside the disabled buttons;
        ``strip_busy`` is the same reason while an engine command is writing to the experiment (the
        experiment strip then disables New / Open / Recent / Rename), else ``None``. A copy or an
        export therefore never mixes files from the middle of an update."""
        if self._wsm.root() is None:
            return False, "need an experiment", None
        busy = {"ingest": "files are being read into the experiment",
                "drive": "a campaign is writing to the experiment",
                "compile": "a campaign is writing to the experiment",
                "export": "an export is running"}.get(self._activity_kind)
        if busy is None and self._export.is_running():
            busy = "an export is running"
        if busy is not None:
            return False, busy, busy
        if self._wsm.state() == "pending":
            return False, "no files read into this experiment yet", None
        return True, "", None

    def _sync_workspace_actions(self) -> None:
        """Mirror the availability rule into the Output bar, the experiment strip and the File menu."""
        if getattr(self, "output_bar", None) is None:
            return                      # the frame is not built yet (the manager adopts first)
        busy = self._operations.is_busy()
        for action in (self._new_workspace_menu_action, self._open_workspace_menu_action):
            action.setEnabled(not busy)
        self._rename_menu_action.setEnabled(self._wsm.root() is not None and not busy)
        bound = self._wsm.root() is not None
        self._inter_settings_action.setEnabled(bound)
        self._inter_settings_action.setToolTip(_INTER_SETTINGS_TIP if bound else "Open an experiment first.")
        available, reason, strip_busy = self._availability()
        self._save_copy_menu_action.setEnabled(available)
        self.output_bar.set_available(available, available, reason)
        self.experiment_strip.set_busy(strip_busy)

    def _say(self, message: str) -> None:
        """One line on the status bar and in the Log."""
        self.statusBar().showMessage(message)
        self.log(message)

    # ---- experiment lifecycle actions (delegate to the manager) ----------------
    def new_workspace(self) -> None:
        """*New experiment…*: create an empty experiment folder under a default name (pending)."""
        confirmed, self._leave_confirmed = self._leave_confirmed, False    # only the re-entry skips the question
        if self._operations.is_busy():
            self.statusBar().showMessage("ISAR is busy — cancel or wait for the current operation")
            return
        if not (confirmed or self._confirm_leave_queue("new")):
            return
        if not self._resolve_dirty_card("start a new experiment"):
            return
        if not self._queue_stopped_for(self.new_workspace):
            return
        try:
            self._wsm.new_experiment(default_experiment_name(), named_from="default")
        except OSError as exc:
            log.warning("could not create an experiment folder: %s", exc)
            self._say(f"Could not create the experiment folder in {experiments_root()}: {exc}")

    # ---- leaving the experiment with queued analysis ------------------------------
    def _confirm_leave_queue(self, verb: str) -> bool:
        """Before closing ISAR (``"close"``), opening another experiment (``"open"``) or starting a new one
        (``"new"``): with analysis running, saving or waiting, ask, naming what is lost. True: go on."""
        summary = self._queue.leave_summary()
        if summary is None:
            return True
        title, stay, go, when = _LEAVE_WORDS[verb]
        return _confirm_leave(self, title, _leave_text(summary, when), stay, go)

    def _queue_stopped_for(self, action) -> bool:
        """True when the queue holds nothing of this experiment any more. Otherwise stop the analysis process in
        order (results handed to its writer are saved first) and run ``action`` again, unasked, once it is gone."""
        if self._queue.leave_summary() is None:
            return True
        if self._queue.view().service in ("off", "failed"):     # no process to wait for: forgotten at once
            self._queue.shutdown(lambda: None)
            return True

        def again() -> None:
            self._leave_confirmed = True
            action()
        self._queue.shutdown(lambda: QTimer.singleShot(0, self, again))
        self._say("Stopping the analysis before leaving the experiment; finished results are saved first…")
        return False

    def _campaign_refusal(self) -> str | None:
        """The coordinator's writer guard: why a campaign may not start here now (one writer per experiment)."""
        busy = self._queue.writes_busy()
        if busy is not None:
            return busy
        if sum(self._queue.view().n_waiting.values()):
            return "analyzers are waiting in the analysis queue; remove them or let them finish first"
        return None

    def _rename_refusal(self) -> str | None:
        """Why *Rename* cannot run now, or ``None``: an engine command, an export or the analysis queue is
        writing in the folder, or the experiment's manifest names source files inside it (their absolute
        paths would point at the old folder)."""
        if self._operations.is_busy() or self._export.is_running():
            _available, _reason, strip_busy = self._availability()
            return strip_busy or "an engine command is running in the experiment"
        busy = self._queue.writes_busy()
        if busy is not None:
            return busy
        if not self._empty and outside_references(self._ws).sources_inside:
            return ("this experiment holds its own source files; rename would leave ISAR unable to "
                    "find them")
        return None

    def rename_experiment(self) -> None:
        """Rename the open experiment: its name and its folder's name together, completely or not at
        all. The watchers are stopped around the rename (a live directory handle blocks it)."""
        old = self._wsm.root()
        if old is None:
            self.statusBar().showMessage("Rename needs an experiment")
            return
        refusal = self._rename_refusal()
        if refusal is not None:
            self._say(f"Rename refused: {refusal}")
            return
        if not self._resolve_dirty_card("rename this experiment"):
            return
        name = RenameDialog.ask(old, None if self._empty else self._ws, self)
        if name is None:
            return
        if self._queue.view().service not in ("off", "failed"):
            # The idle analysis process keeps its log open in report/gui-runs/, which blocks the folder rename.
            self._queue.shutdown(lambda: QTimer.singleShot(0, self, lambda: self._rename_to(old, name)),
                                 keep_waiting=True)
            self._say("Rename: stopping the idle analysis process first…")
            return
        self._rename_to(old, name)

    def _rename_to(self, old: Path, name: str) -> None:
        """The rename itself, once the dialog is answered and no analysis process holds the folder."""
        if self._wsm.root() != old:
            self._say("Rename cancelled: another experiment was opened meanwhile")
            return
        # The dialog's event loop may have let a watched folder start an ingest meanwhile.
        refusal = self._rename_refusal()
        if refusal is not None:
            self._say(f"Rename refused: {refusal}")
            return
        # A debounced store save firing after the rename would recreate the old folder (its mkdir).
        self._flush_gui_store_if_pending()
        self._stop_watch_bridge()
        self._folder_watcher.configure([], True, reset_binding=True)
        self.intake.wait_idle()                 # its reads hold files open in the folder (WinError 5)
        self.inter.wait_idle()                  # the inter-spectra reads hold files open in it too
        self._renaming = True                   # the re-bind keeps the queue's waiting items
        try:
            renamed = self._wsm.rename(name)
        finally:
            self._renaming = False
        if renamed:
            new = self._wsm.root()
            if str(new) == str(old):           # the same name: nothing moved and nothing re-bound
                self._start_watch_bridge()
                self._seed_folder_watcher()
                return
            # The re-bind has started both watchers on the new root.
            self._session.rename_workspace(old, new)
            self._refresh_recents_menu()
            self._autosave()
            self._say(f"Renamed experiment {old.name} to {new.name} ({new})")
            return
        reason = self._wsm.last_error() or "unknown reason"
        live = self._wsm.root()
        if live is not None and str(live) == str(old):
            # The rename failed, or was undone. An undone one re-bound the old root and restarted the
            # bridge already; a failed one left the binding as it was, with the bridge stopped.
            if self._bridge is None:
                self._start_watch_bridge()
            self._seed_folder_watcher()
        self._say(f"Rename failed: {reason}")

    def save_copy_as(self) -> None:
        """*Save copy as…*: copy the whole experiment folder to a new one. The original stays the
        active experiment; the copy is named on the Output bar with *Open copy*."""
        root = self._wsm.root()
        available, reason, _strip_busy = self._availability()
        if root is None or not available:
            self.statusBar().showMessage(f"Save copy as… is unavailable: {reason}")
            return
        if not self._resolve_dirty_card("save a copy of this experiment"):
            return
        # Save mode, not directory mode: the operator names the folder to create. Overwrite
        # confirmation is declined because the manager's "absent or empty" check is the authority.
        # Non-native: the Windows dialog rejects typed paths whose parents do not exist yet, while
        # the manager creates the whole parent chain at promotion.
        dest, _selected = file_dialogs.get_save_file_name(
            self, "Save copy as — name a new folder", str(root.parent / f"{root.name}-copy"), "",
            QFileDialog.Option.DontConfirmOverwrite)
        if not dest:
            return
        # The queue's writer saves no result while the copy reads the folder; a result being saved finishes first.
        self.statusBar().showMessage("Save copy as…: waiting for the result being saved to finish…")
        self._queue.hold_writes(then=lambda: self._copy_held(root, dest))

    def _copy_held(self, root: Path, dest: str) -> None:
        """The copy itself, while the queue's writer is held; the hold is released whatever happens."""
        try:
            if self._wsm.root() != root:
                self._say("Save copy as… cancelled: another experiment was opened meanwhile")
                return
            available, reason, _strip_busy = self._availability()
            if not available:                   # the dialog's event loop may have let an ingest in
                self._say(f"Save copy as… is unavailable: {reason}")
                return
            # The copy carries the latest watched folders and groups, not a stale snapshot.
            self._flush_gui_store_if_pending()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                copy = self._wsm.save_copy(dest)
            finally:
                QApplication.restoreOverrideCursor()
        finally:
            self._queue.release_writes()
        if copy is None:
            reason = self._wsm.last_error() or "the folder must be empty and writable"
            self.output_bar.show_copy_failed(reason)
            self._say(f"Could not save a copy to {dest}: {reason}")
            return
        self.output_bar.show_copy_done(copy)
        self._session.note_workspace(copy)
        self._session.note_workspace(root)     # the original stays the most recent
        self._refresh_recents_menu()
        self._autosave()
        self._say(f"Saved a copy of {root.name} to {copy}; new data keeps going to {root.name}")
        self._log_copy_references(copy)

    def _log_copy_references(self, copy: Path) -> None:
        """Say what the copy still reads from outside its own folder."""
        try:
            refs = outside_references(Workspace.open(copy))
        except (WorkspaceNotFound, OSError) as exc:
            log.warning("could not read the copy's outside references at %s: %s", copy, exc)
            return
        if refs.source_folders:
            self.log(f"The copy reads its source data from {', '.join(refs.source_folders)}; "
                     "keep them available to re-read or re-run.")
        # The copied calibration records the ORIGINAL's curve folders, so "inside" is judged there.
        if not self._empty and outside_references(self._ws).resolution_inside:
            self.log(f"Its stored fits name the resolution curves of {self._ws.root}; add them to the "
                     "queue in the copy to refit them from the copy's own folder.")

    def _on_export_requested(self) -> None:
        """*Export…*: only when the availability rule allows it, then through the export controller."""
        available, reason, _strip_busy = self._availability()
        if not available:
            self.statusBar().showMessage(f"Export is unavailable: {reason}")
            return
        if self._export_hold:
            self.statusBar().showMessage("Export is already waiting for the result being saved to finish")
            return
        root = Path(self._ws.root)
        self._export_hold = True    # the queue's writer saves nothing until the export has read the results
        self._queue.hold_writes(then=lambda: self._start_held_export(root))
        if self._export_hold and not self._export.is_running():
            self.statusBar().showMessage("Export: waiting for the result being saved to finish…")

    def _start_held_export(self, root: Path) -> None:
        if not self._export.start(root):
            self._release_export_hold()
            self.statusBar().showMessage("Export did not start")

    def _release_export_hold(self, *_args) -> None:
        """At the export's first progress line, its end or its failure, once: the queue's writer may save again."""
        if self._export_hold:
            self._export_hold = False
            self._queue.release_writes()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        md = event.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()
        self._on_paths_dropped([u.toLocalFile() for u in urls])

    def _on_paths_dropped(self, paths: list[str]) -> None:
        """The one drop route: every path that arrives (the window's own drops, the files strip's
        drops and choices, the old Files dock) lands here, in drop order.

        With no experiment open, one dropped experiment folder opens it. Anything else, and with an
        experiment open everything (an experiment folder included), is read into the experiment by
        the ingest route, which creates and names a new experiment when none is open and serializes
        every batch behind one FIFO. *Open…* is how one switches experiments."""
        paths = [str(Path(p)) for p in paths if str(p)]
        if not paths:
            return
        if (self._wsm.state() == "none" and len(paths) == 1
                and (Path(paths[0]) / "card.policy.toml").is_file()):
            self.load_workspace(paths[0])
            return
        self._ingest_controller.request_ingest(paths)

    # ---- ingest-controller signal slots -----------------------------------
    def _read_source_again(self, keys) -> None:
        """Send the selected scans' source files through the drop route again. Any file that cannot be sent is said
        in the status bar and the Log, with the way out: drag the file in again."""
        paths: list[str] = []
        for key in keys:
            found = self.scan_list_vm.source_paths(key)
            if not found:
                row = self.scan_list_vm.snapshot.spectrum(key)
                name = f"{self._scan_label(row.set_key)} {row.analyzer or key}"
                self._say_reread_failure(name, "no source file is recorded for this scan")
                return
            paths += [str(Path(p)) for p in found if str(Path(p)) not in paths]
        gone = next((p for p in paths if not Path(p).is_file()), None)
        if gone is not None:
            self._say_reread_failure(Path(gone).name, f"the file is no longer at {gone}")
            return
        self._reread_paths = tuple(paths)
        self._ingest_controller.request_ingest(paths)

    def _say_reread_failure(self, name: str, reason: str) -> None:
        text = f"Could not read {name} again ({reason}). Drag the file into the window to add it again."
        self.statusBar().showMessage(text)
        self.log(text)

    def _on_ingest_started(self, paths) -> None:
        self._reread_running = self._reread_paths is not None and tuple(paths) == self._reread_paths
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
        if self._reread_running:
            self._reread_paths, self._reread_running = None, False
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
            self._queue.on_arrivals([*report.added, *report.updated])

        n_files = len(report.files)
        a, u, d = len(report.added), len(report.updated), len(report.deduped)
        failed = [f for f in report.files if f.get("error")]
        note = f" — {len(failed)} file(s) failed to parse" if failed else ""
        record_error = getattr(report, "import_record_error", None)
        if record_error:
            note += " — import record not written (see Log)"
            self.log(f"WARNING: the import record could not be written; file values in the scan "
                     f"list may be stale: {record_error}")
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
                f"{prefix}: {parsed_waiting} analyzer unit(s) awaiting analysis; "
                + ("their fast passes are queued in Processing" if self._queue.auto_fast()
                   else "add them to the queue in Processing") + note)
        else:
            prefix = f"Ingested {n_files} files"
            if count_text:
                prefix += f" ({count_text})"
            self.statusBar().showMessage(
                f"{prefix}: +{a} logical scans, ~{u} updated ({d} duplicates){note}")
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
        self.files_strip.show_arrival(report)
        self._note_last_written()               # a window built with watch=False has no later tick

    def _on_ingest_failed(self, code: int, cause: str) -> None:
        self.statusBar().showMessage(f"Could not ingest data: {cause}")
        self.log(f"ingest failed (exit {code}): {cause}")
        self.intake.reload()                # its curve check may already have rewritten report/curves.json
        # A real failure (exit != 0) must be visible on screen, never buried in a hidden dock
        # (operator contract). Code 0 is the informational "queued batch dropped" note — not a
        # failure, so it does not force the dock up.
        if code != 0:
            self.log_dock.show()
            self.log_dock.raise_()
        if self._reread_running:
            names = ", ".join(Path(p).name for p in self._reread_paths)
            self._reread_paths, self._reread_running = None, False
            self._say_reread_failure(names, cause)

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
        """A manual-group mutation, ui-scrap change (order/collapsed) or removed-scan change from the scan list or
        the Scans dock: debounce-persist the GUI store (the same store the watched folders live in)."""
        if self._gui_store is not None:
            self._store_save_timer.start()

    def _on_send_group(self, keys, target: str, label: str) -> None:
        """Route a folder's "Send to <plot>" to the destination pane: validate, restrict, switch (2A).

        The engine is never handed a "group" — only the unit ids in ``keys`` reach the view VM. A
        refusal (e.g. a multi-T group on a single-T plane) is surfaced loudly on the status bar."""
        # target keys are the "Send to" verbs; the "heatmap" plot is the CenterHost "plane" page.
        mapping = {"dispersion": ("dispersion", getattr(self, "dispersion_vm", None)),
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

        Called before any store swap (a rebind/load), before a rename or a copy, and on close, so a
        mutation made <500 ms earlier is not lost when ``_gui_store`` is replaced and the armed timer
        would otherwise re-save into the *new* store. Only writes while the outgoing store's root
        still exists: ``GuiStore.save``'s ``mkdir(parents=True)`` would otherwise recreate a folder
        that has moved. The timer is stopped unconditionally.
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
        """The bound experiment changed (new / open / bootstrap / rename). Rebind the docks to it
        (``None`` for the none/pending state), note it in the recents (a pending experiment is a
        real folder too), and re-sync the action enablement."""
        self._rebind(ws)
        root = self._wsm.root()
        if root is not None:
            self._session.note_workspace(root)
        self._refresh_recents_menu()
        self._autosave()

    def _on_wsm_state_changed(self, _state: str) -> None:
        self._update_title()
        self._sync_workspace_actions()

    def _rebind(self, ws) -> None:
        """Tear down the current binding (if any) and bind ``ws`` (``None`` = empty/pending).

        A rebind changes nothing on screen: the arrangement is captured before the teardown and
        restored after the bind (the per-binding docks come back under the same object names). The
        scan inspector is the one exception, closed because spectrum keys are local to a
        workspace."""
        state = None
        if self._bound:
            if self._compact_active:
                self.toggle_compact_mode()      # leave compact first: its stash is not the layout
            # Unit keys are workspace-local.  Clear before the old dock QObject instances disappear
            # so every visible surface is reset rather than carrying an old workspace's inspection.
            self.selection.clear()
            self.active_units.set_active(None)
            self._close_scan_inspector()
            state = self.saveState(_LAYOUT_VERSION)
            self._teardown_binding()
        self._bind_workspace(ws)
        self._bound = True
        if state is not None:
            if not self.restoreState(state, _LAYOUT_VERSION):
                self.log("Window layout: could not carry the arrangement across; using the default "
                         "for this screen.")
                self._apply_default_arrangement(initial_arrangement(*self._available_size()))
            self._sync_rail()

    # ---- Run menu: the one analysis entry point queues work -------------------
    def _init_run_menu(self) -> None:
        run_menu = self.menuBar().addMenu("&Run")
        self._run_action = run_menu.addAction(RUN_TEXT)
        self._run_action.setToolTip(RUN_TIP)
        run_menu.setToolTipsVisible(True)
        self._run_action.triggered.connect(lambda: self.start_analysis())
        self._cancel_operation_action = run_menu.addAction("Cancel active operation")
        self._cancel_operation_action.setToolTip(
            "Stops the file reading, export or campaign that is running now. Analyzers in the queue are "
            "stopped with the stop button beside each in the Processing panel.")
        self._cancel_operation_action.triggered.connect(self._cancel_active_operation)

    def _sync_run_actions(self) -> None:
        """The Run menu and the Processing panel follow the entry point and the busy state."""
        self._cancel_operation_action.setEnabled(self._operations.is_busy())
        self._push_processing_state()

    def _push_processing_state(self) -> None:
        """The Processing panel's activity line (an ingest, export or campaign) and the entry point's controls."""
        self.processing_panel.set_activity(self._operation_status.text())
        self._push_run_row()

    def _push_run_row(self) -> None:
        """The entry point's controls (the setup section's button, the Processing button, the Run menu entry): on
        when the queue can take work, with the count of analyzer files it would add in the label; else off, with
        the reason beside the button."""
        curves_only = False
        count = None
        if self._empty:
            enabled, note = False, "Open or create an experiment first."
        elif not (self.scan_list_vm.snapshot.sets or self.scan_list_vm.snapshot.files_not_read):
            enabled, note = False, "No data yet. Add data files below."
        elif self.readiness_line.checking:
            enabled, note = False, "Checking what is ready…"
        else:
            count, why = self._queue.fast_all()
            ready, blockers = self.readiness_line.ready_count, self.readiness_line.blockers
            if count is None:
                enabled, note = False, _sentence(why or "the queue cannot take work now")
            elif count == 0:
                enabled = False
                note = ((f"Nothing is ready yet: {blockers}" if blockers else "Nothing is ready yet.") if ready == 0
                        else "Every ready analyzer already has a current fast pass or is queued.")
                curves_only = self.readiness_line.only_curves_blocked
            else:
                enabled = True
                note = "" if ready is None else f"{ready} ready for analysis"
                curves_only = self.readiness_line.only_curves_blocked
        label = RUN_TEXT if count is None else f"{RUN_TEXT} ({count})"
        self.setup_section.set_run_state(enabled, note, curves_only=curves_only, label=label)
        self.processing_panel.set_run_state(enabled, label, note)
        tip = RUN_TIP if enabled else (note or RUN_TIP)
        self.attention_list.set_run_state(enabled, label, tip)
        self.readiness_line.set_run_state(enabled, count, tip)
        self._run_action.setEnabled(enabled)
        # a greyed menu entry says why in its own text
        self._run_action.setText(label if enabled or not note else f"{label} — {note.rstrip('.')}")
        self._run_action.setToolTip(RUN_TIP if enabled else (note or RUN_TIP))

    def start_analysis(self) -> None:
        """The one window-level entry point for analysis: add a fast pass for every ready analyzer file that has no
        current one (the queue's duplicate rules apply). Every start button, menu entry and remedy goes through here."""
        if not self._resolve_dirty_card("start analysis"):    # queued items fit the saved card, not the draft
            return
        self._queue.fast_pass_all_ready()

    def start_inter_batch(self, req) -> None:
        """The Processing dock's batch Start. The queue re-reads the preview and refuses what cannot run now; a refusal
        is said here, an accepted batch is logged by the queue."""
        if not self._resolve_dirty_card("start an inter-spectra batch"):
            return
        ok, message = self._queue.start_inter_batch(req)
        if not ok:
            self._say(message)

    def _open_inter_settings(self) -> None:
        InterSettingsDialog(self.inter, self).exec()

    # ---- Config menu: machine-level settings (TAVI's form) --------------------
    def _init_config_menu(self) -> None:
        menu = self.menuBar().addMenu("&Config")
        self._cores_action = menu.addAction(self._cores_action_text())
        self._cores_action.setToolTip(_CORES_TIP)
        menu.setToolTipsVisible(True)
        self._cores_action.triggered.connect(self._ask_cores)
        self._inter_settings_action = menu.addAction("Inter-spectra fitting settings…")
        self._inter_settings_action.triggered.connect(self._open_inter_settings)
        scans_menu = menu.addMenu("Scan(s)")
        scans_menu.setToolTipsVisible(True)
        self._scans_display_actions = {}
        for name, label, tip in (
            ("zero_lines", "Show scan zero lines", "Dotted grey zero-intensity lines for offset scans."),
            ("peak_lines", "Show mode centre lines", "Lines from each stored mode centre to its scan zero, on both energy sides, in the mode colour."),
            ("filled_symbols", "Filled data symbols", "Fill data symbols; turn off for empty symbols that reveal curves behind them."),
        ):
            action = scans_menu.addAction(label)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.triggered.connect(lambda checked, option=name: self._set_scans_display(option, checked))
            self._scans_display_actions[name] = action
        scans_menu.aboutToShow.connect(self._sync_scans_display)

    def _sync_scans_display(self):
        for name, action in self._scans_display_actions.items():
            action.setEnabled(self.scans_page is not None)
            if self.scans_page is not None:
                action.setChecked(getattr(self.scans_page.vm, name))

    def _set_scans_display(self, name, checked):
        if self.scans_page is not None:
            self.scans_page.vm.set_display_option(name, checked)
            self.scans_page.save_view_state(self._session)
            self._autosaver.request()

    @staticmethod
    def _cores_max() -> int:
        return max(1, os.cpu_count() or 1)

    def _cores_setting(self) -> int:
        """ISAR's core budget on this machine: the saved number, else all the machine's cores but two."""
        saved = self._session.view_state.get("cores")
        if isinstance(saved, int) and not isinstance(saved, bool) and saved >= 1:
            return min(saved, self._cores_max())
        return max(1, self._cores_max() - 2)

    def _cores_action_text(self) -> str:
        return f"Cores for analysis… ({self._cores_setting()} of {self._cores_max()})"

    def _ask_cores(self) -> None:
        current = self._cores_setting()
        label = (f"Cores ISAR may use for analysis (this computer has {self._cores_max()}).\n"
                 "A lower number takes effect as running analyzers finish. A deep pass that is already "
                 "running keeps its cores until you stop it.")
        value, ok = QInputDialog.getInt(self, "Cores for analysis", label, current, 1, self._cores_max())
        if not ok or value == current:
            return
        self._session.view_state["cores"] = int(value)
        self._autosave()
        self._queue.set_cores(int(value))
        self._cores_action.setText(self._cores_action_text())
        self.log(f"Cores for analysis: {value}")

    def _on_auto_fast_changed(self, flag: bool) -> None:
        """The switch moved: it belongs to the experiment, so it is saved in the experiment's GUI store."""
        if self._gui_store is not None:
            self._gui_store.set_auto_fast_pass(flag)
            self._store_save_timer.start()

    def _on_virtual_start_requested(self, opts, env: dict) -> None:
        """Launch a campaign with NO experiment open: create one → rebind → carry the form → drive.

        The dock cannot call request_drive itself here — creating the experiment tears the emitting
        dock down mid-click — so it hands the built options over and this slot owns the sequence.
        ISAR names the experiment, as a drop does (G19). The campaign then initializes the pending
        folder itself (lazy `isar init` after its first scan); the promotion timer adopts it (see
        _try_promote_campaign_workspace)."""
        dock = getattr(self, "campaign_dock", None)
        form = dock.form_dict() if dock is not None else None
        try:
            # rebind: a fresh pending dock appears
            self._wsm.new_experiment(f"Campaign_{date.today():%Y-%m-%d}", named_from="default")
        except OSError as exc:
            log.warning("could not create an experiment folder for the campaign: %s", exc)
            self._say(f"Campaign not started: could not create the experiment folder in "
                      f"{experiments_root()}: {exc}")
            return
        if form is not None and self.campaign_dock is not None:
            self.campaign_dock.apply_form(form)    # in-memory carry — pending has no GuiStore yet
        if not self._operations.request_drive(opts, env=dict(env or {})):
            self.log("could not start the virtual campaign — "
                     + (self._operations.last_refusal() or "workspace not ready"))

    def _seed_pending_campaign_form(self) -> None:
        """Persist the typed campaign form into the reserved directory's GuiStore BEFORE the
        promotion rebind loads it — set_store fires during the ready bind, so seeding afterwards
        would be invisible. Never overwrites an existing form (a pending experiment opened from
        Recent keeps its own). The GuiStore is a machine-written GUI file — sanctioned, unlike the card."""
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
        self.intake.reload()                # a stopped campaign's passes stop counting as running
        msg = {0: "Campaign met its objective", 3: "Campaign incomplete — see the feed",
               4: "Campaign stopped by operator",
               5: "Campaign scan recovery pending — press Resume to retry",
               6: "Campaign infeasible — see the feed"}.get(
                   int(code), f"Campaign failed (exit {code})")
        self.statusBar().showMessage(msg)
        self._sync_run_actions()

    def _cancel_active_operation(self) -> None:
        if self._operations.cancel():
            self.statusBar().showMessage("Cancelling active workspace operation…")

    def _on_operation_activity_changed(self, state: dict) -> None:
        kind = state["kind"]
        queued = state["queued"]
        self._activity_kind = kind          # read by the availability rule (_sync_workspace_actions)
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
            else:
                verb = _ACTIVITY_VERBS.get(kind, "Working")
            suffix = f" + {queued} queued" if queued else ""
            self._operation_status.setText(f"{verb}{suffix}")
        dock = getattr(self, "card_dock", None)
        if dock is not None:
            dock.set_operation_busy(kind is not None)
        self._queue.poke()          # a campaign that ended frees the queue to start; one that began holds it
        self._sync_run_actions()
        self._sync_workspace_actions()

    # ---- Open report / Export view (Operator-Contract wins, D3) -----------
    def _open_report(self) -> bool:
        """Open ``<workspace>/report/report.html`` in the default browser. Returns success."""
        import webbrowser

        if self._empty:
            self.statusBar().showMessage("No workspace — nothing to open")
            return False
        report = Path(self._ws.root) / "report" / "report.html"
        if not report.exists():
            self.statusBar().showMessage("No report yet — fast-pass some analyzers first")
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
            path, _ = file_dialogs.get_save_file_name(self, "Export view as PNG", default,
                                                      "PNG image (*.png)")
            if not path:
                return False
        ok = save_widget_png(glw, path)
        self.statusBar().showMessage(f"Exported {path}" if ok else f"Export failed: {path}")
        return ok

    def _glw_for(self, dock_key: str):
        """The pyqtgraph graphics widget backing a named export view, or ``None``.

        The File ▸ Export menu labels map to CenterHost page keys."""
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
        settings_action.setToolTip("Optional: the LLM review service and where TAVI is installed.")
        settings_action.triggered.connect(self._open_settings)
        self._launch_tavi_action = menu.addAction("Launch TAVI (instrument control)")
        self._launch_tavi_action.setToolTip("Only needed for campaigns that measure.")
        menu.setToolTipsVisible(True)
        self._launch_tavi_action.triggered.connect(self._launch_tavi)

    # ---- View menu (central view switcher; built once, persists across rebinds) ----
    def _init_view_menu(self) -> None:
        """The View menu selects the central display surface, opens or closes the right panel,
        resets the layout, and holds the per-bind Panels submenu (always appended last). A page
        trigger is an explicit user view change (clears auto-follow); it is a no-op with no host."""
        menu = self.menuBar().addMenu("&View")
        self._view_menu = menu
        for key, label in _CENTER_PAGES:
            act = menu.addAction(label)
            act.triggered.connect(lambda _checked=False, k=key: self._show_center_view(k, user=True))
        menu.addSeparator()
        # Compact campaign-monitor mode (M3-1): same window, docks stashed, stack page swapped.
        self._compact_mode_action = menu.addAction("Mini monitor")
        self._compact_mode_action.setCheckable(True)
        self._compact_mode_action.triggered.connect(self.toggle_compact_mode)
        # `triggered` (user interaction only): _sync_rail's programmatic setChecked must not collapse.
        self._processing_panel_action = menu.addAction("Processing panel")
        self._processing_panel_action.setCheckable(True)
        self._processing_panel_action.setChecked(True)
        self._processing_panel_action.triggered.connect(
            lambda checked: self.set_right_panel_collapsed(not checked))
        reset = menu.addAction("Reset layout")
        reset.triggered.connect(self.reset_layout)

    def reset_layout(self) -> None:
        """View > Reset layout: the default arrangement for the current screen."""
        if self._compact_active:
            self.toggle_compact_mode()
        self._apply_default_arrangement(initial_arrangement(*self._available_size()))
        self._autosave()

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
        # Campaign controls are health-gated.  Keep the localhost readiness view fresh enough that
        # starting TAVI outside ISAR never leaves Start disabled behind a stale 30-second result.
        self._health_timer.setInterval(3000)
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
        self._health_generation = self._health_probe.request({"tavi": health_url(TAVI_DEFAULT_URL),
                                                               "llm": health_url(llm_url)})
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
            label.setText(f"{'LLM (optional)' if key == 'llm' else 'TAVI (campaigns)'} ●")
            color = "#2e7d32" if ok else ("#b26a00" if reachable else "#c62828")
            label.setStyleSheet(f"color: {color};")
            label.setToolTip(f"{url} — {detail}\ngreen = reachable, amber = reachable but reports a problem, "
                             "red = not reachable, grey = not checked yet")
            status = "ok" if ok else ("reachable_error" if reachable else "down")
            if key == "tavi" and getattr(self, "campaign_dock", None) is not None:
                self.campaign_dock.set_backend_status("tavi", status, f"{url} — {detail}")
            previous = self._health_statuses.get(key)
            if status != previous:
                self._health_statuses[key] = status
                if status == "ok" and previous is not None:
                    self.log(f"{name} backend recovered: {url}")
                elif status != "ok":
                    technical = f"{name} backend {detail}: {url}"
                    self.log(technical)                         # the log keeps the exact text
                    self.statusBar().showMessage(_HEALTH_WORDS[key] if status == "down" else
                                                 f"{name} answered but reports a problem.")
                    self.statusBar().setToolTip(technical)      # and so does the hover

    # ---- status bar: persistent live indicator ----------------------------
    def _init_status_bar(self) -> None:
        """A permanent status-bar label (distinct from the transient ``showMessage`` text) so "is it
        live" stays visible even after the transient "Updated: ..." message expires."""
        self._live_label = QLabel()
        self.statusBar().addPermanentWidget(self._live_label)
        self._operation_status = QLabel()
        self.statusBar().addPermanentWidget(self._operation_status)
        # Backend reachability indicators (LLM + TAVI): grey until the first probe returns.
        self._llm_health = QLabel("LLM ●")
        self._tavi_health = QLabel("TAVI ●")
        for lbl in (self._llm_health, self._tavi_health):
            lbl.setStyleSheet("color: #888;")
            self.statusBar().addPermanentWidget(lbl)

    # ---- View > Panels (PUFFIN two-way-sync pattern) ----------------------
    def _init_docks_menu(self) -> None:
        # A submenu of View, rebuilt per bind. The left column and Output are frame, not panels:
        # they are not listed and cannot be hidden.
        self._docks_menu = self._view_menu.addMenu("Panels")
        for name, dock in self._docks.items():
            action = self._docks_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(dock.isVisible())

            def _toggle(checked, d=dock):
                if checked:
                    self._show_panel(d)
                else:
                    self._hide_panel(d)
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

    # ---- open a different experiment ---------------------------------------
    def load_workspace(self, path) -> bool:
        """Open the experiment at ``path`` through the manager, which emits ``workspace_changed`` and
        drives the full teardown+rebind in :meth:`_on_wsm_workspace_changed`. Returns ``True`` on
        success; a folder that is not an experiment leaves the current one untouched and says why.
        """
        confirmed, self._leave_confirmed = self._leave_confirmed, False    # only the re-entry skips the question
        if self._operations.is_busy():
            self.statusBar().showMessage("ISAR is busy — cancel or wait before opening another experiment")
            return False
        # Validate before resolving the current draft: a refused candidate must not cause a
        # Save/Discard prompt for the experiment being kept.
        reason = openable(path)
        if reason is not None:
            self.statusBar().showMessage(f"Cannot open {path}: {reason}")
            self.log(f"Not opening {path}: {reason}")
            return False
        same = False
        if self._wsm.root() is not None:
            try:
                same = Path(path).resolve() == self._wsm.root().resolve()
            except OSError as exc:
                self.log(f"could not compare the open experiment to {path}: {exc}")
        if not (same or confirmed or self._confirm_leave_queue("open")):
            return False
        if not self._resolve_dirty_card("open another experiment"):
            return False
        if same:
            return True
        if not self._queue_stopped_for(lambda: self.load_workspace(path)):
            return False                        # opened once the analysis process has stopped
        if self._wsm.open(path):
            return True
        reason = self._wsm.last_error() or "not an ISAR experiment"
        self.statusBar().showMessage(f"Cannot open {path}: {reason}")
        self.log(f"Not opening {path}: {reason}")
        return False

    def _resolve_dirty_card(self, action: str) -> bool:
        """Resolve the current card draft before an action changes its meaning or binding."""
        dock = getattr(self, "card_dock", None)
        return True if dock is None else dock.resolve_dirty(action)

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
                "Card saved — existing results used the previous policy; add the analyzers to the queue again to apply it")
            self.log("Card policy changed after results existed — add the analyzers to the queue again to apply it")
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
        self._stop_watch_bridge()

        self._view_menu.removeAction(self._docks_menu.menuAction())
        # removeAction() only detaches the menu; the QMenu (and its per-dock QActions, whose closures
        # capture the now-deleted docks) stay parented to the View menu. Delete it so repeated
        # rebinds don't leak one Panels menu per load.
        self._docks_menu.deleteLater()
        for dock in list(self._docks.values()):
            if dock is self.log_dock or dock is self.processing_dock:
                continue  # persist across rebinds (created in __init__); keep the Log's scrollback
            if dock is self.campaign_dock:
                dock.dispose()
            self.removeDockWidget(dock)
            # Unparent as well as delete later: until the event loop runs, a dock pending deletion
            # is still a child with its object name, and the restoreState that follows the rebind
            # would restore it instead of the new dock of the same name.
            dock.setParent(None)
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
        # The compact monitor projects the outgoing binding's VMs; leave compact and drop it
        # before those VMs are torn down.
        self._dispose_campaign_monitor()
        # The four display surfaces live inside the CenterHost (stack page 1). Dispose it — detaching
        # and deleting every page — drop its view_changed connection, remove it
        # from the stack, and delete it, so the fresh bind rebuilds a clean host. The permanent stack
        # and its hint page (index 0) are left in place. The panes' VM connections drop when both the
        # panes (deleteLater'd here) and the VMs (deleteLater'd just above) are torn down.
        if self._center_host is not None:
            self.scans_page.save_view_state(self._session)
            self._center_host.view_changed.disconnect(self._on_center_view_changed)
            # Park the stack on the persistent hint page BEFORE removing the host: removeWidget on
            # the current page leaves the stack at index -1 (a grey flash) until the rebind adds
            # the next host.
            self._center_stack.setCurrentWidget(self._empty_hint)
            self._center_stack.removeWidget(self._center_host)
            self._center_host.dispose()
            self._center_host.deleteLater()
            self._center_host = None
        self.scans_page = None

    # ---- watch -----------------------------------------------------------
    def _on_results_saved(self, keys) -> None:
        """The queue saved results for ``keys``. A stopped watch bridge (a rename, a window built with
        ``watch=False``) re-reads nothing, so re-read the experiment here as an ingest does; the shown analyzer
        re-renders only when it is one of them, and the selection never moves. A running bridge's tick does this."""
        if self._empty or self._bridge is not None:
            return
        shown = self.inspector_vm.current_unit or self._unit_key_for_fit(self.inspector_vm.current_fit)
        self._ws.refresh()
        self._on_workspace_changed({"snapshot", "results"}, refresh_shown=False)
        if shown in keys:
            self._refresh_inspector_selection()

    def _on_workspace_changed(self, changed, *, refresh_shown: bool = True) -> None:
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
            if refresh_shown:
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
        # The scan list re-reads from the intake's `changed` below (_INTAKE_RELOAD covers snapshot, results,
        # card and card_state), after the bridge refresh; it never asks for the display, so the central
        # page does not move under the user.
        if changed & {"results", "snapshot", "card_state"}:
            self.output_bar.note_written(datetime.now())
        if changed & _INTAKE_RELOAD:
            self.intake.reload()
        if changed & _INTER_RELOAD:
            self.inter.reload()
        self._live_label.setText(f"live — last update {datetime.now():%H:%M:%S}")
        self.statusBar().showMessage(f"Updated: {', '.join(sorted(changed)) or '(none)'}")

    def _route_inspect(self, payload) -> None:
        """Route a ``ScansViewModel.inspect_requested`` payload to the inspector: a unit-key ``str``
        shows the raw parsed unit (metadata + counts); anything else (a ``FitView`` or ``None``)
        drives the fit inspector as before."""
        if isinstance(payload, str):
            if self.scan_list_vm.snapshot.spectrum(payload) is not None:
                self.selection.set([payload], clicked=payload, origin=ORIGIN_OTHER)
            self.active_units.set_active(payload)
            self.inspector_vm.set_unit(payload)
            self._raise_inspector_if_auto()
        else:
            self._inspect_fit(payload)

    def _inspect_fit(self, fit) -> None:
        """Inspect a fit selected by Results or one of the plot views, retaining its unit identity."""
        key = self._unit_key_for_fit(fit)
        if fit is None:
            self.selection.clear()
        elif key is not None and self.scan_list_vm.snapshot.spectrum(key) is not None:
            self.selection.set([key], clicked=key, origin=ORIGIN_OTHER)
        self.active_units.set_active(key)
        self.inspector_vm.set_fit(fit)
        self._raise_inspector_if_auto()

    def _raise_inspector_if_auto(self) -> None:
        """Switch the central view to Scan(s) when an inspect request is routed AND
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
        # The one arrangement is saved in every state (the empty state has the same one). Compact
        # mode is never serialized: the stash holds the real layout in memory and closeEvent expands
        # before the final snapshot.
        if not self._compact_active:
            self._remember_inspector_width()
            state = bytes(self.saveState(_LAYOUT_VERSION).toBase64().data()).decode("ascii")
            self._session.dock_layout_b64 = state
            # What the arrangement needs of a screen: everything except the display at its saved
            # size, plus the display's minimum. _restore_layout refuses it on a smaller screen. The
            # inspector opens closed on every start, so while it is open the record describes the
            # arrangement without it: its width and handle come off ``needs`` and the left panel's own
            # width is kept apart for _restore_layout to apply over the blob's widened dock.
            open_extra = (0 if self.scan_inspector.isHidden()
                          else self.scan_inspector.width() + self._left_split.handleWidth())
            layout = {
                "version": _LAYOUT_VERSION,
                "needs": [self.width() - self._center_stack.width() + _CENTER_MIN[0] - open_extra,
                          self.height() - self._center_stack.height() + _CENTER_MIN[1]],
                "inspector_width": int(self._inspector_width)}
            if open_extra:
                layout["left_width"] = int(self._column_width())
            self._session.view_state["window_layout"] = layout
        # Every experiment is a real folder, a pending one included, so it belongs in the recents.
        if self._wsm.root() is not None:
            self._session.note_workspace(self._wsm.root())
        self.heatmap_vm.save_view_state(self._session)
        self.dispersion_vm.save_view_state(self._session)
        if self.scans_page is not None:
            self.scans_page.save_view_state(self._session)
        self.scan_list_vm.save_view_state(self._session)
        self._session.view_state["attention_list"] = {"expanded": self.attention_list.is_expanded()}
        self.processing_panel.inter_box.save_view_state(self._session)
        # Persist the central view switcher (current page + auto-follow) alongside the other
        # view-state writes; restored on the next ready bind (default: Scan page, auto-follow on).
        if self._center_host is not None:
            self._session.view_state["center_view"] = self._center_host.current_key
            self._session.view_state["center_auto_follow"] = self._center_host.auto_follow
        return self._session

    def _restore_layout(self) -> None:
        """Apply the saved arrangement, or the default for this screen.

        A saved arrangement is restored only when it is version 4, its recorded ``needs`` fit the
        screen's available area in both dimensions, and ``restoreState`` accepts it (it is
        version-gated and atomic: a blob of another version never half-applies). Otherwise the default
        for the current screen is used and, when a saved arrangement existed, exactly one Log line
        says why. This is what keeps a desktop arrangement from clipping the display on a laptop."""
        width, height = self._available_size()
        kind = initial_arrangement(width, height)
        b64 = self._session.dock_layout_b64
        if not b64:
            self._apply_default_arrangement(kind)
            return
        meta = self._session.view_state.get("window_layout")
        needs = None
        if isinstance(meta, dict) and meta.get("version") == _LAYOUT_VERSION:
            raw = meta.get("needs")
            if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
                needs = (int(raw[0]), int(raw[1]))
        if needs is not None and (needs[0] > width or needs[1] > height):
            message = (f"Window layout: the saved arrangement needs about {needs[0]}×{needs[1]} px; "
                       f"this screen gives {width}×{height}. Using the {kind} layout for this screen; "
                       "View ▸ Reset layout restores it any time.")
        else:
            ok = False
            if needs is not None:
                try:
                    ok = self.restoreState(QByteArray.fromBase64(b64.encode("ascii")), _LAYOUT_VERSION)
                except Exception as exc:  # noqa: BLE001 — a bad saved layout must never block the window
                    log.warning("could not restore the saved window layout: %s", exc)
            if ok:
                saved = meta.get("inspector_width")
                if isinstance(saved, (int, float)) and not isinstance(saved, bool):
                    self._inspector_width = max(_INSPECTOR_MIN, int(saved))
                left = meta.get("left_width")      # saved with the inspector open: the blob's dock is wide
                if isinstance(left, (int, float)) and not isinstance(left, bool):
                    self.resizeDocks([self.left_column_dock], [max(_LEFT_MIN, int(left))],
                                     Qt.Horizontal)
                self._sync_rail()
                return
            message = (f"Window layout: the saved arrangement is from an older ISAR or unreadable. "
                       f"Using the {kind} layout for this screen.")
        self._apply_default_arrangement(kind)
        self.log(message)

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
        confirmed, self._leave_confirmed = self._leave_confirmed, False    # only the re-entry skips the question
        if not (confirmed or self._confirm_leave_queue("close")):
            event.ignore()
            return
        # Do not save, discard, or tear down a workspace while an engine process can still be
        # writing it.  Request graceful termination, then re-enter close once its terminal callback
        # has fired (the coordinator escalates to kill after three seconds).
        if getattr(self, "_operations", None) is not None and self._operations.is_busy():
            self._close_when_operations_finish = True
            # A leave the user has just confirmed is not asked again by the re-entered close.
            self._leave_confirmed = confirmed or self._queue.leave_summary() is not None
            self._operations.cancel()
            self.statusBar().showMessage("Cancelling workspace operation before closing…")
            event.ignore()
            return
        if not self._resolve_dirty_card("close ISAR"):
            event.ignore()
            return
        # Results handed to the queue's writer are saved before the window goes; close again once it has stopped.
        if not self._queue_stopped_for(self.close):
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
        # Threads (health, sandbox) that already ended give the collector back now; one still blocked
        # keeps it off until the process exits, which is the safe side of the close-time deadlock.
        collector_guard.release()
        if getattr(self, "_folder_watcher", None) is not None:
            self._folder_watcher.stop()
        if self._bridge is not None:
            self._bridge.stop()
        if getattr(self, "_autosaver", None) is not None:
            self._autosaver.flush_now()
        # Flush any pending debounced GUI-store write (watched folders / groups) before the window
        # goes. Every experiment is already a real folder, so closing asks nothing.
        self._flush_gui_store_if_pending()
        super().closeEvent(event)

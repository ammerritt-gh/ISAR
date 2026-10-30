"""The window frame: one arrangement in every state, its mount points, and their wiring.

Offscreen Qt with the real widgets (the floor's rule for any "the window does X" acceptance). The
mounts that later units fill (experiment strip, files strip, scan list, scan inspector, output bar) are stubs here, so this file checks the *frame*: where things are, what is wired to
what, and that the geometry holds at four screen sizes. Each filler reruns it on its real class.

The screen is patched through the one seam, ``MainWindow._available_size``.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QPushButton, QTabBar

import bl43_fixture
from isar.workspace_api import Workspace
from isar.workspace_api.scan_list import ScanListSnapshot, natural_key
from isar_gui import main_window as mw_mod
from isar_gui.export_action import ExportController
from isar_gui.main_window import MainWindow, initial_arrangement
from isar_gui.session import Session
from isar_gui.theme import (DEEP, FAST, PARSED, Tone, TONE_COLOR, dot_icon, status_tone,
                            summary_tone)
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.experiment_strip import ExperimentStrip
from isar_gui.views.files_strip import FilesStrip
from isar_gui.views.output_bar import OutputBar
from isar_gui.views.processing_panel import ProcessingPanel, ProcessingRail
from isar_gui.views.scan_inspector import ScanInspectorPanel
from isar_gui.views.scan_list import ScanListPanel
from synthetic_ws import scan_entry
from gui.window_frame_checks import assert_frame_geometry, rect_in

LAPTOP = (1108, 851)
LAPTOP_125 = (1536, 826)          # 1920x1080 at 125 %
DESKTOP = (1920, 1040)
LARGE = (2560, 1392)
SIZES = [LAPTOP, LAPTOP_125, DESKTOP, LARGE]


def _window(qtbot, monkeypatch, size, ws=None, session=None):
    """A shown window on a patched screen of ``size``, as the app makes it: constructed, then
    resized (the app maximises) and shown, with no second call into the arrangement."""
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(ws, session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(30)
    return w


def _ready_ws(make_ws, **parts):
    parts.setdefault("rows", [{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(make_ws(**parts))


def _spy(monkeypatch, cls, name):
    """Record every call of ``cls.name`` (the original still runs); returns the list of arg tuples."""
    calls = []
    original = getattr(cls, name)

    def wrapper(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, wrapper)
    return calls


def _drop(window, widget, paths):
    """A real drop: drag-enter then drop at the centre of ``widget``, sent to the window's native
    handle so Qt hit-tests the widget under the cursor and walks the event up to the first ancestor
    that accepts drops (exactly what a user's drop does). Returns whether the drop was accepted."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    pos = widget.mapTo(window, widget.rect().center())
    handle = window.windowHandle()
    enter = QDragEnterEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(handle, enter)
    event = QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(handle, event)
    return event.isAccepted()      # read while the mime data is alive: the event does not own it


def _right_tabs(w):
    """The right area's tab bar texts and the current one, or None when it has no tab bar yet."""
    for bar in w.findChildren(QTabBar):
        texts = [bar.tabText(i) for i in range(bar.count())]
        if "Processing" in texts and "Campaign" in texts:
            return texts, bar.tabText(bar.currentIndex())
    return None


# ---------------------------------------------------------------------------
# worked example 1: no experiment, laptop
# ---------------------------------------------------------------------------
def test_example1_no_experiment_laptop(qtbot, monkeypatch):
    set_experiment = _spy(monkeypatch, ExperimentStrip, "set_experiment")
    bind = _spy(monkeypatch, FilesStrip, "bind")
    set_workspace = _spy(monkeypatch, ScanListViewModel, "set_workspace")
    output_experiment = _spy(monkeypatch, OutputBar, "set_experiment")
    set_available = _spy(monkeypatch, OutputBar, "set_available")
    w = _window(qtbot, monkeypatch, LAPTOP)

    assert set_experiment == [(None,)]
    assert len(bind) == 1 and bind[0][0] is w.files_vm and bind[0][1] is False
    assert len(set_workspace) == 1 and isinstance(set_workspace[0][0], mw_mod._EmptyWorkspace)
    assert output_experiment == [(None,)]
    assert w.scan_list_vm.snapshot == ScanListSnapshot()
    assert set_available[-1] == (False, False, "need an experiment")

    # The right docks are hidden and the rail reads "Processing ‹".
    assert w.processing_dock.isHidden() and w.campaign_dock.isHidden()
    assert w.processing_rail.text() == "Processing ‹"
    assert_frame_geometry(w, LAPTOP)
    left, log = rect_in(w, w.left_column_dock), rect_in(w, w.log_dock)
    assert left.bottom() >= log.bottom() and log.left() > left.right()

    # Expanded, Processing and Campaign are tabs of the right area with Processing in front.
    w.set_right_panel_collapsed(False)
    qtbot.wait(30)
    assert _right_tabs(w) == (["Processing", "Campaign"], "Processing")

    # Scans, Browser, Card and Scan Table are absent; the other panels are in View > Panels.
    for name in ("scans_dock", "browser_dock", "card_dock", "scan_table_dock"):
        assert getattr(w, name, None) is None
    assert {a.text() for a in w._docks_menu.actions()} == {"Log", "Files", "Campaign", "Processing"}


# ---------------------------------------------------------------------------
# worked example 2: open an experiment from there
# ---------------------------------------------------------------------------
def _frame_state(w):
    return {"left": w.left_column_dock.width(), "log_w": w.log_dock.width(),
            "log_h": w.log_dock.height(), "out_w": w.output_dock.width(),
            "collapsed": w._right_panel_collapsed(),
            "visible": (w.left_column_dock.isVisible(), w.log_dock.isVisible(),
                        w.output_dock.isVisible())}


def test_example2_opening_an_experiment_changes_nothing_on_screen(qtbot, monkeypatch, make_ws):
    set_available = _spy(monkeypatch, OutputBar, "set_available")
    w = _window(qtbot, monkeypatch, LAPTOP)
    before = _frame_state(w)
    ws = _ready_ws(make_ws)
    assert w.load_workspace(str(ws.root)) is True
    qtbot.wait(50)
    after = _frame_state(w)
    assert after["collapsed"] is before["collapsed"] and after["visible"] == before["visible"]
    for key in ("left", "log_w", "log_h", "out_w"):
        assert abs(after[key] - before[key]) <= 4, (key, before, after)
    assert_frame_geometry(w, LAPTOP)

    # The old panels now exist, hidden, and are listed in View > Panels.
    for name in ("scans_dock", "browser_dock", "card_dock", "scan_table_dock"):
        assert getattr(w, name).isHidden()
    assert {a.text() for a in w._docks_menu.actions()} == {
        "Log", "Files", "Campaign", "Processing", "Scans", "Browser", "Card", "Scan Table", "Analyzer array"}
    assert w.scan_list_vm.workspace is w._ws
    assert set_available[-1] == (True, True, "")


# ---------------------------------------------------------------------------
# worked example 3: a drop anywhere reaches the one route
# ---------------------------------------------------------------------------
def test_example3_a_drop_anywhere_in_the_window_reaches_the_ingest_route(qtbot, monkeypatch, make_ws):
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    requested = []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    path = str(Path(__file__))                        # any local path: it is only carried, not read
    expected = str(Path(QUrl.fromLocalFile(path).toLocalFile()))   # normalised to native separators

    w._open_scan_inspector()                          # so every target is on screen to be hit
    w.set_right_panel_collapsed(False)
    qtbot.wait(50)
    page = w._center_host.pane(w._center_host.current_key)
    for target in (page, w.scan_list, w.scan_inspector, w.processing_panel, w.log_dock,
                   w.output_bar, w.experiment_strip):
        requested.clear()
        assert target.isVisible()
        assert _drop(w, target, [path]), type(target).__name__
        assert requested == [[expected]], type(target).__name__


def test_only_the_files_strip_accepts_drops(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    assert w.files_strip.acceptDrops()
    for mount in (w.experiment_strip, w.output_bar, w.processing_panel, w.processing_rail,
                  w.scan_list, w.scan_inspector):
        assert not mount.acceptDrops(), type(mount).__name__


def test_files_strip_choices_and_drops_leave_through_paths_dropped(qtbot, monkeypatch, make_ws):
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    requested = []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    path = str(Path(__file__))
    assert _drop(w, w.files_strip, [path])
    assert requested == [[str(Path(path))]]           # native separators, like the old Files dock


# ---------------------------------------------------------------------------
# worked example 4: selection and inspect
# ---------------------------------------------------------------------------
def test_example4_selection_and_inspect(qtbot, monkeypatch, make_ws):
    key = "tavi:41:det"
    ws = _ready_ws(make_ws, scans={key: scan_entry(41, status="ingested")}, rows=[])
    w = _window(qtbot, monkeypatch, LAPTOP, ws)
    vm = w.scan_list_vm
    displayed, inspected = [], []
    vm.selection.changed.connect(displayed.append)
    vm.inspect_requested.connect(inspected.append)

    vm.select(key)
    assert len(displayed) == 1 and displayed[0].keys == (key,)
    assert w.active_units.key == key
    assert w.inspector_vm.current_unit == key
    assert vm.selection.active == key
    assert w.scan_inspector.isHidden()

    width_before = w._center_stack.width()
    vm.inspect(key)
    qtbot.wait(30)
    assert inspected == [key] and not w.scan_inspector.isHidden()
    assert w.scan_inspector.width() >= mw_mod._INSPECTOR_MIN
    assert w._center_stack.width() < width_before                   # the display gave up the width
    assert abs((width_before - w._center_stack.width()) - w.scan_inspector.width()) <= 12
    assert_frame_geometry(w, LAPTOP)

    w.scan_inspector.close_requested.emit()
    qtbot.wait(30)
    assert w.scan_inspector.isHidden()
    assert abs(w._center_stack.width() - width_before) <= 4


# ---------------------------------------------------------------------------
# worked example 5: an ingest running
# ---------------------------------------------------------------------------
def test_example5_activity_gates_save_copy_and_export(qtbot, monkeypatch, make_ws):
    set_available = _spy(monkeypatch, OutputBar, "set_available")
    set_busy = _spy(monkeypatch, ExperimentStrip, "set_busy")
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    idle = {"kind": None, "queued": 0, "cancelling": False}
    for kind, reason in (("ingest", "files are being read into the experiment"),
                         ("drive", "a campaign is writing to the experiment"),
                         ("compile", "a campaign is writing to the experiment"),
                         ("export", "an export is running")):
        w._on_operation_activity_changed({**idle, "kind": kind})
        assert set_available[-1] == (False, False, reason), kind
        assert set_busy[-1] == (reason,), kind
        w._on_operation_activity_changed(idle)
        assert set_available[-1] == (True, True, ""), kind
        assert set_busy[-1] == (None,), kind


def test_export_running_blocks_save_copy_and_a_pending_experiment_is_not_ready(qtbot, monkeypatch):
    set_available = _spy(monkeypatch, OutputBar, "set_available")
    w = _window(qtbot, monkeypatch, LAPTOP)
    assert w._availability() == (False, "need an experiment", None)
    w._wsm.new_experiment("X", named_from="default")  # a real folder: bound, nothing read
    qtbot.wait(30)
    assert set_available[-1] == (False, False, "no files read into this experiment yet")
    monkeypatch.setattr(w._export, "is_running", lambda: True)
    assert w._availability()[:2] == (False, "an export is running")


def test_export_request_goes_through_the_controller_only_when_available(qtbot, monkeypatch, make_ws):
    started = []
    monkeypatch.setattr(ExportController, "start", lambda self, root: started.append(root) or True)
    w = _window(qtbot, monkeypatch, LAPTOP)
    w.output_bar.export_requested.emit()
    assert started == []                              # no experiment: refused before the controller
    assert "need an experiment" in w.statusBar().currentMessage()
    ws = _ready_ws(make_ws)
    assert w.load_workspace(str(ws.root))
    w.output_bar.export_requested.emit()
    assert started == [Path(ws.root)]


# ---------------------------------------------------------------------------
# the ScanListViewModel core
# ---------------------------------------------------------------------------
class _Spy:
    """Collects the emissions of a signal."""

    def __init__(self, signal):
        self.calls = []
        signal.connect(lambda *a: self.calls.append(a))


def _vm(monkeypatch, bl43_ws_readonly):
    snap = bl43_fixture.sample_snapshot(bl43_ws_readonly)
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **_reads: snap)
    return ScanListViewModel(mw_mod._EmptyWorkspace()), snap


def test_vm_select_follows_the_selection_rule(qtbot, monkeypatch, bl43_ws_readonly):
    vm, snap = _vm(monkeypatch, bl43_ws_readonly)
    changes = _Spy(vm.selection.changed)
    fitted = snap.sets[2].spectra[0]
    unfitted = snap.sets[2].spectra[2]
    vm.select(fitted.key)
    assert vm.selection.keys == (fitted.key,) and vm.selection.active == fitted.key
    vm.select(unfitted.key)
    assert vm.selection.keys == (unfitted.key,)
    vm.select("no:such:key")
    assert vm.selection.keys == () and vm.selection.active is None
    count = len(changes.calls)
    vm.select(None)
    assert len(changes.calls) == count


def test_vm_set_active_key_moves_only_within_the_selection(qtbot, monkeypatch, bl43_ws_readonly):
    vm, snap = _vm(monkeypatch, bl43_ws_readonly)
    a, b = snap.sets[2].spectra[:2]
    vm.select_keys((a.key, b.key))
    current = _Spy(vm.selection.active_changed)
    vm.set_active_key(b.key)
    vm.set_active_key(b.key)
    vm.set_active_key("not:selected")
    assert current.calls == [(b.key,)] and vm.selection.keys == (a.key, b.key)


def test_vm_rebind_keeps_a_current_key_that_still_exists_and_drops_one_that_does_not(
        qtbot, monkeypatch, bl43_ws_readonly):
    vm, snap = _vm(monkeypatch, bl43_ws_readonly)
    key = snap.sets[2].spectra[1].key
    vm.select(key)
    current, changed, display = (_Spy(vm.selection.active_changed), _Spy(vm.snapshot_changed),
                                 _Spy(vm.selection.changed))
    vm.set_workspace(mw_mod._EmptyWorkspace())
    assert vm.selection.active == key and current.calls == [] and len(changed.calls) == 1
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **_reads: ScanListSnapshot())
    vm.set_workspace(mw_mod._EmptyWorkspace())
    assert vm.selection.active is None and current.calls == [(None,)]
    assert len(display.calls) == 1 and display.calls[0][0].origin == "refresh"


def test_vm_background_update_emits_snapshot_changed_and_never_the_display(
        qtbot, monkeypatch, bl43_ws_readonly):
    vm, snap = _vm(monkeypatch, bl43_ws_readonly)
    vm.select(snap.sets[2].spectra[0].key)
    changed, display = _Spy(vm.snapshot_changed), _Spy(vm.selection.changed)
    vm.on_workspace_changed({"results"})
    assert len(changed.calls) == 1 and display.calls == []


def test_vm_inspect_selects_then_asks_for_the_inspector(qtbot, monkeypatch, bl43_ws_readonly):
    vm, snap = _vm(monkeypatch, bl43_ws_readonly)
    key = snap.sets[2].spectra[1].key
    order = []
    vm.selection.active_changed.connect(lambda k: order.append(("current", k)))
    vm.inspect_requested.connect(lambda k: order.append(("inspect", k)))
    vm.inspect(key)
    assert order == [("current", key), ("inspect", key)]
    order.clear()
    vm.inspect(key)                                    # already current: no second selection
    assert order == [("inspect", key)]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size, kind", [(LAPTOP, "laptop"), (LAPTOP_125, "laptop"),
                                        (DESKTOP, "desktop"), (LARGE, "desktop")])
def test_initial_arrangement_picks_the_screen_class(size, kind):
    assert initial_arrangement(*size) == kind


@pytest.mark.parametrize("size", SIZES)
def test_the_frame_holds_at_each_screen_size(qtbot, monkeypatch, size):
    kind = initial_arrangement(*size)
    sizes = mw_mod._ARRANGEMENTS[kind]
    w = _window(qtbot, monkeypatch, size)
    assert_frame_geometry(w, size)
    assert abs(w.left_column_dock.width() - sizes.left) <= 4
    assert abs(w.log_dock.height() - sizes.log_height) <= 4
    assert w.log_dock.width() > w.output_dock.width()              # the Log is the wide half
    if kind == "laptop":
        assert w._right_panel_collapsed() and w.processing_rail.text() == "Processing ‹"
    else:
        assert not w._right_panel_collapsed() and w.processing_rail.text() == "›"
        assert abs(w.processing_dock.width() - sizes.right) <= 4
        assert w.processing_dock.isVisible()
    # The same arrangement again, by View > Reset layout.
    w.reset_layout()
    qtbot.wait(30)
    assert_frame_geometry(w, size)
    assert w._right_panel_collapsed() is (kind == "laptop")


def test_laptop_with_the_inspector_open_and_the_right_panel_expanded_still_fits(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    w._open_scan_inspector()
    w.set_right_panel_collapsed(False)
    qtbot.wait(50)
    assert not w.scan_inspector.isHidden() and not w._right_panel_collapsed()
    assert_frame_geometry(w, LAPTOP)
    assert w.width() <= LAPTOP[0]


def test_mounts_fit_inside_their_columns_minimum_width(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    assert w.experiment_strip.minimumSizeHint().width() <= mw_mod._LEFT_MIN
    assert w.files_strip.minimumSizeHint().width() <= mw_mod._LEFT_MIN
    assert w.scan_list.minimumSizeHint().width() <= mw_mod._LEFT_MIN
    assert w.scan_inspector.minimumSizeHint().width() <= mw_mod._INSPECTOR_MIN
    # the queue's Fast pass / Deep pass row measures 250 px in offscreen's wide fake font (real fonts need about half)
    assert w.processing_panel.minimumSizeHint().width() <= mw_mod._RIGHT_MIN + 40
    hint = w.output_bar.minimumSizeHint()
    assert hint.width() <= 300 and hint.height() <= 120
    assert w.processing_rail.minimumSizeHint().width() <= 24


def test_the_two_strips_leave_the_scan_list_a_tall_column_at_1108x851(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    strips = w.experiment_strip.height() + w.files_strip.height()
    assert strips <= 260, strips
    assert w.scan_list.height() >= 400, w.scan_list.height()


def test_a_long_experiment_name_is_elided_with_a_tooltip_and_widens_nothing(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    before = w.left_column_dock.width()
    long = Path("C:/data") / ("CsV3Sb5_97K_0p5_0p5_3_" * 8 + "run")
    w.experiment_strip.set_experiment(long)
    w.output_bar.set_experiment(long)
    qtbot.wait(30)
    assert w.left_column_dock.width() == before
    name = w.experiment_strip._name
    assert name.text() != long.name and "…" in name.text() and name.toolTip() == long.name


# ---------------------------------------------------------------------------
# the right panel, the rail, View menu
# ---------------------------------------------------------------------------
def test_the_rail_expands_and_collapses_and_the_view_action_stays_in_step(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    action = w._processing_panel_action
    assert w._right_panel_collapsed() and not action.isChecked()
    assert w.processing_rail.accessibleName() == "Expand the processing panel"

    width_before = w._center_stack.width()
    w.processing_rail.toggled.emit()
    qtbot.wait(30)
    assert not w._right_panel_collapsed() and action.isChecked()
    assert w.processing_rail.text() == "›"
    assert w.processing_rail.accessibleName() == "Collapse the processing panel"
    assert w._center_stack.width() < width_before                # width came from the display
    assert_frame_geometry(w, LAPTOP)

    w.processing_rail.toggled.emit()
    qtbot.wait(30)
    assert w._right_panel_collapsed() and not action.isChecked()
    assert w.processing_rail.text() == "Processing ‹"

    action.trigger()                                             # the View menu drives it too
    assert not w._right_panel_collapsed()
    action.trigger()
    assert w._right_panel_collapsed()


def test_view_panels_shows_an_old_panel_as_a_left_tab_and_parks_it_again(qtbot, monkeypatch, make_ws):
    from PySide6.QtWidgets import QTabBar
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    scans = next(a for a in w._docks_menu.actions() if a.text() == "Scans")
    assert w.dockWidgetArea(w.scans_dock) == Qt.NoDockWidgetArea and not scans.isChecked()

    scans.trigger()                                  # a user click on View > Panels > Scans
    qtbot.wait(30)
    assert w.scans_dock.isVisible() and scans.isChecked()
    assert w.scans_dock in w.tabifiedDockWidgets(w.left_column_dock)
    assert w.rect().contains(rect_in(w, w.scans_dock))      # in front, where the column was

    scans.trigger()                                  # and off again
    qtbot.wait(30)
    assert w.scans_dock.isHidden() and w.dockWidgetArea(w.scans_dock) == Qt.NoDockWidgetArea
    # No tab bar is left behind in the left column once the old panel is parked again.
    assert not [bar for bar in w.findChildren(QTabBar)
                if bar.isVisible() and bar.count() and "Experiment" in bar.tabText(0)
                and bar.count() > 1]


def test_campaign_dock_scrolls_instead_of_forcing_the_right_panel_wide(qtbot, monkeypatch):
    from PySide6.QtWidgets import QScrollArea
    w = _window(qtbot, monkeypatch, LAPTOP)
    assert isinstance(w.campaign_dock.widget(), QScrollArea)
    assert w.campaign_dock.minimumSizeHint().width() <= mw_mod._RIGHT_MIN + 40


def test_compact_mode_hides_and_restores_the_rail_and_the_inspector(qtbot, monkeypatch, make_ws):
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    w._open_scan_inspector()
    qtbot.wait(30)
    assert not w.scan_inspector.isHidden() and not w.processing_rail.isHidden()
    w.toggle_compact_mode()
    assert w.scan_inspector.isHidden() and w.processing_rail.isHidden()
    w.toggle_compact_mode()
    qtbot.wait(30)
    assert not w.scan_inspector.isHidden() and not w.processing_rail.isHidden()


# ---------------------------------------------------------------------------
# persistence and its validity rule
# ---------------------------------------------------------------------------
def _saved_session(w):
    snap = w._snapshot_session()
    return Session(dock_layout_b64=snap.dock_layout_b64, view_state=dict(snap.view_state))


def test_a_saved_arrangement_restores_on_a_screen_it_fits(qtbot, monkeypatch):
    w1 = _window(qtbot, monkeypatch, LAPTOP_125)
    w1.set_right_panel_collapsed(False)
    w1.resizeDocks([w1.left_column_dock], [400], Qt.Horizontal)
    qtbot.wait(50)
    left = w1.left_column_dock.width()
    w1._inspector_width = 333                      # the inspector's remembered width travels too
    session = _saved_session(w1)

    w2 = _window(qtbot, monkeypatch, LAPTOP_125, session=session)
    assert not w2._right_panel_collapsed()
    assert abs(w2.left_column_dock.width() - left) <= 4
    assert w2._inspector_width == 333 and w2.scan_inspector.isHidden()   # it opens closed


def test_a_saved_desktop_arrangement_is_refused_on_a_laptop_with_one_log_line(
        qtbot, monkeypatch, caplog):
    w1 = _window(qtbot, monkeypatch, LARGE)
    assert not w1._right_panel_collapsed()
    session = _saved_session(w1)
    needs = session.view_state["window_layout"]["needs"]
    assert needs[0] > LAPTOP[0]

    with caplog.at_level(logging.INFO, logger="isar_gui.main_window"):
        w2 = _window(qtbot, monkeypatch, LAPTOP, session=session)
    lines = [r.getMessage() for r in caplog.records if "Window layout:" in r.getMessage()]
    assert len(lines) == 1 and "laptop layout" in lines[0] and f"{LAPTOP[0]}×{LAPTOP[1]}" in lines[0]
    assert w2._right_panel_collapsed()
    assert abs(w2.left_column_dock.width() - mw_mod._ARRANGEMENTS["laptop"].left) <= 4
    assert_frame_geometry(w2, LAPTOP)


def test_a_blob_without_its_window_layout_record_is_refused(qtbot, monkeypatch, caplog):
    w1 = _window(qtbot, monkeypatch, LAPTOP)
    session = Session(dock_layout_b64=_saved_session(w1).dock_layout_b64)
    with caplog.at_level(logging.INFO, logger="isar_gui.main_window"):
        _window(qtbot, monkeypatch, LAPTOP, session=session)
    lines = [r.getMessage() for r in caplog.records if "Window layout:" in r.getMessage()]
    assert len(lines) == 1 and "older ISAR or unreadable" in lines[0]


def test_reset_layout_restores_the_default_for_the_screen(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    w.set_right_panel_collapsed(False)
    w.resizeDocks([w.left_column_dock], [450], Qt.Horizontal)
    w._open_scan_inspector()
    qtbot.wait(30)
    w.reset_layout()
    qtbot.wait(30)
    assert w._right_panel_collapsed() and w.scan_inspector.isHidden()
    assert abs(w.left_column_dock.width() - mw_mod._ARRANGEMENTS["laptop"].left) <= 4
    assert w._inspector_width == mw_mod._ARRANGEMENTS["laptop"].inspector


def test_the_arrangement_is_the_same_before_and_after_the_first_data(qtbot, monkeypatch, make_ws):
    """The point of the frame: nothing appears or rearranges when the first data arrives."""
    w = _window(qtbot, monkeypatch, LAPTOP)
    empty = {name: rect_in(w, getattr(w, name)) for name in
             ("left_column_dock", "log_dock", "output_dock", "_center_stack", "processing_rail")}
    assert w.load_workspace(str(_ready_ws(make_ws).root))
    qtbot.wait(50)
    for name, rect in empty.items():
        now = rect_in(w, getattr(w, name))
        assert abs(now.x() - rect.x()) <= 4 and abs(now.y() - rect.y()) <= 4, name
        assert abs(now.width() - rect.width()) <= 4 and abs(now.height() - rect.height()) <= 4, name


# ---------------------------------------------------------------------------
# the Processing panel
# ---------------------------------------------------------------------------
def test_processing_panel_run_queues_the_ready_analyzers_and_says_why_it_is_off(
        qtbot, monkeypatch, bl43_ws):
    starts = []
    monkeypatch.setattr(MainWindow, "start_analysis", lambda self: starts.append(1))
    w = _window(qtbot, monkeypatch, LAPTOP)
    run = w.processing_panel.findChild(QPushButton, "processing_run")
    assert w.processing_panel.findChild(QPushButton, "processing_stop") is None      # the batch Stop is gone
    assert not run.isEnabled() and run.full_text() == "Fast-pass all ready"
    reason = w.processing_panel.findChild(type(w._empty_hint), "processing_reason")
    assert reason.text() == "Open or create an experiment first." and run.toolTip() == reason.text()

    with qtbot.waitSignal(w.intake.worker_changed, timeout=15000):
        assert w.load_workspace(str(bl43_ws))
    qtbot.waitUntil(run.isEnabled, timeout=15000)
    count = w._queue.fast_all()[0]
    assert count and run.full_text() == f"Fast-pass all ready ({count})" and "no current fast pass" in run.toolTip()
    assert not reason.isVisibleTo(w.processing_panel)
    run.click()
    assert starts == [1]

    monkeypatch.setattr(w._queue, "fast_all", lambda: (0, None))
    w._push_run_row()
    assert not run.isEnabled() and run.full_text() == "Fast-pass all ready (0)"
    assert reason.isVisibleTo(w.processing_panel) and reason.text() == run.toolTip()
    assert "current fast pass or is queued" in reason.text()


def test_the_processing_panel_shows_an_ingest_export_or_campaign_in_its_activity_line(qtbot, monkeypatch, make_ws):
    w = _window(qtbot, monkeypatch, LAPTOP, _ready_ws(make_ws))
    line = w.processing_panel.findChild(type(w._empty_hint), "processing_activity")
    assert not line.isVisibleTo(w.processing_panel)
    w._on_operation_activity_changed({"kind": "export", "queued": 0, "cancelling": False})
    assert line.isVisibleTo(w.processing_panel) and line.text() == "Exporting" and line.toolTip()
    w._on_operation_activity_changed({"kind": None, "queued": 0, "cancelling": False})
    assert not line.isVisibleTo(w.processing_panel)


def test_the_experiments_root_seam_points_under_the_test_folder(tmp_path):
    assert Path(os.environ["ISAR_EXPERIMENTS_ROOT"]).is_relative_to(tmp_path)


def test_stub_mounts_have_their_final_signatures():
    for cls, signals in ((ExperimentStrip, ("new_requested", "open_requested", "recent_chosen",
                                            "rename_requested")),
                         (OutputBar, ("save_copy_requested", "export_requested", "open_copy_requested")),
                         (FilesStrip, ("paths_dropped", "log_message")),
                         (ScanInspectorPanel, ("close_requested",)),
                         (ProcessingPanel, ("run_requested",)),
                         (ProcessingRail, ("toggled",)),
                         (ExportController, ("started", "progress", "finished", "failed")),
                         (ScanListViewModel, ("snapshot_changed", "selection_changed", "inspect_requested"))):
        for name in signals:
            assert hasattr(cls, name), (cls.__name__, name)
    assert list(inspect.signature(ScanListPanel.__init__).parameters) == ["self", "vm", "parent"]
    assert list(inspect.signature(ScanInspectorPanel.__init__).parameters) == ["self", "vm", "parent"]


# ---------------------------------------------------------------------------
# the status vocabulary (theme.py)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("column, word, tone", [
    (PARSED, "ok", Tone.OK), (PARSED, "not read", Tone.ERR), (PARSED, "no Q", Tone.WARN),
    (PARSED, "no T, no Q", Tone.WARN),
    (FAST, "resolved", Tone.OK), (FAST, "close_call", Tone.WARN), (FAST, "unresolved", Tone.WARN),
    (FAST, "search_limited", Tone.WARN), (FAST, "held", Tone.WARN), (FAST, "not fitted", Tone.ERR),
    (FAST, "not recorded", Tone.IDLE), (FAST, "not run", Tone.IDLE),
    (DEEP, "resolved", Tone.OK), (DEEP, "close_call", Tone.WARN), (DEEP, "search_limited", Tone.WARN),
    (DEEP, "refused", Tone.ERR), (DEEP, "not run", Tone.IDLE),
    (FAST, "some_new_state", Tone.IDLE), (DEEP, "some_new_state", Tone.IDLE)])
def test_status_tone_table(column, word, tone):
    assert status_tone(column, word) is tone


def test_summary_tone_is_the_worst_and_dot_icons_are_cached(qtbot):
    assert summary_tone([Tone.OK, Tone.IDLE, Tone.WARN]) is Tone.WARN
    assert summary_tone([Tone.OK, Tone.IDLE]) is Tone.IDLE
    assert summary_tone([Tone.WARN, Tone.ERR]) is Tone.ERR
    assert summary_tone([Tone.OK]) is Tone.OK
    assert summary_tone([]) is Tone.IDLE
    for tone in Tone:
        assert dot_icon(tone) is dot_icon(tone) and not dot_icon(tone).isNull()
        assert TONE_COLOR[tone].startswith("#")


def test_state_icons_differ_in_shape_not_only_colour_and_are_cached(qtbot):
    from PySide6.QtGui import QColor
    from isar_gui.theme import state_icon
    assert state_icon(True) is state_icon(True) and state_icon(False) is state_icon(False)
    images = {good: state_icon(good).pixmap(12, 12).toImage() for good in (True, False)}

    def ink(img):
        return {(x, y) for x in range(img.width()) for y in range(img.height())
                if img.pixelColor(x, y).alpha() > 128}
    assert ink(images[True]) != ink(images[False])             # shape first: not one mark in two colours
    for good, tone in ((True, Tone.OK), (False, Tone.ERR)):    # and the colour is the tone's token
        x, y = next(iter(sorted(ink(images[good]))))
        c, want = images[good].pixelColor(x, y), QColor(TONE_COLOR[tone])
        assert (c.red(), c.green(), c.blue()) == (want.red(), want.green(), want.blue())


def test_natural_key_orders_digits_as_numbers_and_ignores_case():
    assert sorted(["a14", "a3", "A04"], key=natural_key) == ["a3", "A04", "a14"]
    assert sorted(["102", "58"], key=natural_key) == ["58", "102"]
    assert natural_key("58") < natural_key("CsV3Sb5_97K")        # numbers sort before text


# ---------------------------------------------------------------------------
# the shared BL43 test workspace
# ---------------------------------------------------------------------------
def test_bl43_fixture_workspace(bl43_ws_readonly):
    ws = Workspace.open(bl43_ws_readonly)
    ids = {s.scan_id for s in ws.scans}
    assert len(ids) == 9
    assert {f"{bl43_fixture.KEY_A}:a03", f"{bl43_fixture.KEY_B}:a06",
            f"{bl43_fixture.KEY_SINGLE}:a06"} <= ids
    files = {Path(f.path).name: f for f in ws.files}
    # three files are not read: the set that fails whole, the member left out of a read set, a note
    assert files["CsV3Sb5_97K_0p5_0p5_3_bad_a03"].error
    assert files["notes.txt"].classification == "unrecognized"
    assert f"{bl43_fixture.KEY_B}:a07" not in ids and files[f"{bl43_fixture.STEM_B}_a07"].n_scans == 1
    # the stamped fits are what the scan list will report
    by_id = {s.scan_id: s for s in ws.scans}
    ledger = json.loads((bl43_ws_readonly / "session.json").read_text(encoding="utf-8"))["scans"]
    stored = ledger[f"{bl43_fixture.KEY_A}:a04"]["detectors"]["a04"]
    assert stored["fast_pass"]["state"] == "close_call" and stored["deep_pass"]["state"] == "resolved"
    assert by_id[f"{bl43_fixture.KEY_B}:a03"].status == "held_for_calibration"
    assert by_id[f"{bl43_fixture.KEY_B}:a04"].status == "excluded"
    assert by_id[f"{bl43_fixture.KEY_A}:a05"].status == "ingested"


def test_bl43_sample_snapshot_describes_the_fixture_workspace(bl43_ws_readonly):
    ws = Workspace.open(bl43_ws_readonly)
    snap = bl43_fixture.sample_snapshot(bl43_ws_readonly)
    assert {sp.key for s in snap.sets for sp in s.spectra} == {s.scan_id for s in ws.scans}
    assert snap.n_spectra == 9 and [s.label for s in snap.sets] == [
        "58", bl43_fixture.STEM_B, bl43_fixture.STEM_A]
    for s in snap.sets:                       # the hashed placeholder number is only ever inside a key
        words = [s.label, *(u.reason for u in s.unread)]
        for sp in s.spectra:
            words += [sp.file_name, sp.file_path, sp.parsed.word, sp.parsed.reason or "",
                      sp.fast.word, sp.fast.reason or "", sp.deep.word, sp.deep.reason or ""]
        assert not any("1842762476" in w or "1126149381" in w for w in words), s.key
    for s in snap.sets:
        assert snap.scan_set(s.key) is s
        for sp in s.spectra:
            assert snap.spectrum(sp.key) is sp and Path(sp.file_path).exists()
            assert sp.set_key == s.key
    assert snap.spectrum("nope") is None and snap.scan_set("nope") is None
    assert [u.name for u in snap.files_not_read] == [
        "CsV3Sb5_97K_0p5_0p5_3_bad_a03", f"{bl43_fixture.STEM_B}_a07", "notes.txt"]


def test_bl43_pooled_workspace_has_one_master(tmp_path):
    root = bl43_fixture.pooled_workspace(tmp_path / "pooled")
    ids = {s.scan_id: s for s in Workspace.open(root).scans}
    assert set(ids) == {"tavi:11:det", "tavi:12:det", "tavi:900:det"}
    assert ids["tavi:900:det"].detectors["det"].pooled is True

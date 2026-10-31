"""The Analyzer array dock inside the real MainWindow (offscreen, real widgets): View > Panels, the first open,
persistence, geometry at the supported window sizes, the composed check, and the scan-switch timing.

A 28-analyzer BL43LXU workspace is generated at test time (hard rule 10). Set ``ISAR_ARRAY_SHOTS`` to a folder to
have the geometry tests also save PNG screenshots there.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QToolButton
from shiboken6 import isValid

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B
from gui.array_helpers import hostile_state, open_dock
from gui.window_frame_checks import assert_apart, assert_elided_have_tooltips, assert_frame_geometry, rect_in
from isar.workspace_api import Workspace
from isar_gui import main_window as mw_mod
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.analyzer_array_dock import ArrayWindow
from isar_gui.views.analyzer_cell import CELL_MIN

LAPTOP = (1108, 851)
LARGE = (2560, 1392)
SHOTS = os.environ.get("ISAR_ARRAY_SHOTS")


def analyzer(set_key, number):
    return f"{set_key}:a{number:02d}"


def _shot(widget, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        widget.grab().save(str(Path(SHOTS) / name))


@pytest.fixture(scope="module")
def ws28(tmp_path_factory):
    return bl43_fixture.build_bl43_workspace(tmp_path_factory.mktemp("bl43_28"), n_analyzers=28)


def _window(qtbot, monkeypatch, size, ws_root=None, session=None):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    ws = Workspace.open(ws_root) if ws_root is not None else None
    w = MainWindow(ws, session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(30)
    return w


def _panels_action(w):
    return next(a for a in w._docks_menu.actions() if a.text() == "Analyzer array")


def _open(w, qtbot):
    _panels_action(w).trigger()
    qtbot.wait(30)
    return w.array_dock


def _saved(w):
    snap = w._snapshot_session()
    return Session(dock_layout_b64=snap.dock_layout_b64, view_state=dict(snap.view_state))


def _select(w, *keys, **kw):
    w.scan_list_vm.selection.set(keys, **kw)
    w.array_dock.flush_redraw()
    QApplication.processEvents()


def _geometry(dock):
    return dock.geometry()


# ---------------------------------------------------------------------------
# View > Panels, the first open, persistence
# ---------------------------------------------------------------------------
def test_panels_lists_it_and_the_first_open_is_its_own_window_inside_the_screen(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    assert "Analyzer array" in {a.text() for a in w._docks_menu.actions()}
    dock = w.array_dock
    win = dock.array_window
    assert dock.isHidden() and not win.isVisible() and not dock.is_open()   # parked until opened
    _open(w, qtbot)
    assert win.isVisible() and dock.is_open() and _panels_action(w).isChecked()
    assert dock.isHidden() and not dock.isFloating()                  # the dock itself stays in the main window
    assert QRect(0, 0, *LAPTOP).contains(win.frameGeometry()) and win.width() >= 800
    centre, placed = w.geometry().center(), win.geometry()
    assert abs(placed.center().x() - centre.x()) <= 2 and abs(placed.center().y() - centre.y()) <= 2
    _panels_action(w).trigger()                                       # hide
    qtbot.wait(30)
    assert not win.isVisible() and not _panels_action(w).isChecked()
    _open(w, qtbot)                                                   # reopen: left where it was
    assert win.isVisible() and win.geometry() == placed
    win.move(placed.x() + 37, placed.y() + 21)                        # the operator moves it; a reopen keeps that
    moved = win.geometry()
    _panels_action(w).trigger()
    _open(w, qtbot)
    assert win.geometry() == moved


def test_the_window_is_a_top_level_window_of_its_own(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    win = _open(w, qtbot).array_window
    assert isinstance(win, ArrayWindow) and win.parent() is None
    assert win.windowType() == Qt.WindowType.Window                  # its own taskbar button, not an owned dock frame
    assert win.windowTitle() == f"Analyzer array — {w._ws.root.name}"
    assert win.dock_button.toolTip() and win.dock_button.text() == "Dock in main window"


def test_a_restart_reopens_the_window_open_where_it_was(qtbot, monkeypatch, ws28):
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    win1 = _open(w1, qtbot).array_window
    # Qt clamps a restored window into the real screen (the offscreen one is 800 x 600), so the
    # operator's arrangement here is one that fits it.
    win1.resize(600, 450)
    win1.move(30, 20)
    qtbot.wait(30)
    w1.array_vm.set_scale("raw")
    session = _saved(w1)
    saved = session.view_state["analyzer_array_window"]
    assert saved["windowed"] is True and saved["open"] is True and saved["geometry"]
    assert session.view_state["analyzer_array"]["scale"] == "raw"
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    win2 = w2.array_dock.array_window
    assert win2.isVisible() and w2.array_dock.is_open() and w2.array_dock.isHidden()
    assert win2.size() == win1.size()                                 # the frame margins move the origin by a few px
    assert (win2.geometry().topLeft() - win1.geometry().topLeft()).manhattanLength() <= 4
    assert w2.array_vm.scale == "raw"


def test_a_window_closed_before_the_restart_stays_closed_and_windowed(qtbot, monkeypatch, ws28):
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w1, qtbot)
    w1.array_dock.array_window.close()
    session = _saved(w1)
    assert session.view_state["analyzer_array_window"]["open"] is False
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    assert not w2.array_dock.array_window.isVisible() and not w2.array_dock.is_open()
    assert _panels_action(w2).isChecked() is False
    _open(w2, qtbot)                                                 # and it opens in the window, as ever
    assert w2.array_dock.array_window.isVisible()


def test_docked_in_the_main_window_survives_a_restart(qtbot, monkeypatch, ws28):
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w1, qtbot)
    w1.array_dock.dock_in()
    session = _saved(w1)
    assert session.view_state["analyzer_array_window"]["windowed"] is False
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    assert w2.array_dock.isVisible() and not w2.array_dock.array_window.isVisible()
    assert w2.array_dock._root.parentWidget() is w2.array_dock


def test_reset_layout_closes_the_window_and_forgets_its_place(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    win = _open(w, qtbot).array_window
    win.move(40, 30)
    w.reset_layout()
    qtbot.wait(30)
    assert w.array_dock.isHidden() and not win.isVisible() and not w.array_dock.is_open()
    assert _saved(w).view_state["analyzer_array_window"]["geometry"] is None
    _open(w, qtbot)                                                  # the next open places it afresh, centred
    assert win.isVisible() and abs(win.geometry().center().x() - w.geometry().center().x()) <= 2
    assert win.geometry().topLeft() != QPoint(40, 30)


def test_a_v4_blob_saved_without_the_dock_restores_and_leaves_it_hidden(qtbot, monkeypatch, ws28):
    """No ``_LAYOUT_VERSION`` bump: a saved window from before this dock existed still restores (plan decision 6)."""
    assert mw_mod._LAYOUT_VERSION == 4
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    w1.set_right_panel_collapsed(False)
    w1.resizeDocks([w1.left_column_dock], [400], Qt.Horizontal)
    qtbot.wait(50)
    left = w1.left_column_dock.width()
    w1.removeDockWidget(w1.array_dock)                                # the arrangement as it was before the dock
    blob = w1.saveState(mw_mod._LAYOUT_VERSION)
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    assert w2.restoreState(blob, mw_mod._LAYOUT_VERSION) is True
    qtbot.wait(50)
    assert w2.array_dock.isHidden() and abs(w2.left_column_dock.width() - left) <= 4
    _open(w2, qtbot)                                                  # and it opens as ever
    assert w2.array_dock.array_window.isVisible() and not w2.array_dock.isFloating()


def _alive(window) -> bool:
    """Whether a window still exists: deleted ones are gone once their deferred delete has run."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return isValid(window)


def test_a_rebind_keeps_it_open_and_compact_mode_hides_and_restores_it(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    old = _open(w, qtbot).array_window
    w.array_vm.set_y_mode("shared")
    w._rebind(Workspace.open(ws28))
    qtbot.wait(50)
    assert not _alive(old) and _alive(w.array_dock.array_window)     # the old window went with its binding
    assert w.array_dock.array_window.isVisible() and w.array_vm.y_mode == "shared"
    w.toggle_compact_mode()
    assert not w.array_dock.array_window.isVisible()
    w.toggle_compact_mode()
    qtbot.wait(50)
    assert w.array_dock.array_window.isVisible()


def test_a_docked_array_is_hidden_in_compact_mode_and_comes_back_docked(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w, qtbot)
    w.array_dock.dock_in()
    w.toggle_compact_mode()
    assert w.array_dock.isHidden()
    w.toggle_compact_mode()
    qtbot.wait(50)
    assert w.array_dock.isVisible() and not w.array_dock.array_window.isVisible()


def test_closing_the_main_window_leaves_no_array_window_alive(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    win = _open(w, qtbot).array_window
    assert _alive(win) and win.isVisible()
    w.close()
    qtbot.wait(30)
    assert not _alive(win)                                            # deleted with the main window, not kept alive
    w._snapshot_session()                                             # an autosave after the close must not touch it
    assert w._session.view_state["analyzer_array_window"]["open"] is True


def test_the_empty_state_has_no_dock(qtbot, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP)
    assert w.array_dock is None and "Analyzer array" not in {a.text() for a in w._docks_menu.actions()}


def test_a_click_on_a_cell_shows_that_spectrum_in_the_main_plot(qtbot, monkeypatch, ws28):
    from PySide6.QtTest import QTest
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = _open(w, qtbot)
    _select(w, analyzer(KEY_A, 6))
    w._center_host.show_view("plane")
    cell = dock.cell_at("a09")
    QTest.mouseClick(cell, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(cell.width() // 2, 4))
    qtbot.wait(30)
    assert w.scan_list_vm.selection.keys == (analyzer(KEY_A, 9),)
    assert w._center_host.current_key == "scan"                       # the main plot follows the click
    assert w.scans_page.vm.selection.keys == (analyzer(KEY_A, 9),)


# ---------------------------------------------------------------------------
# geometry: hostile data on the dock itself, then composed in the real window
# ---------------------------------------------------------------------------
def _children_inside(dock):
    """Every visible widget of the dock lies inside its parent; the scrolled grid host and pyqtgraph's own
    internals are the two things whose parent rectangle is not their bound."""
    bad = []
    for child in dock._root.findChildren(QLabel) + dock._root.findChildren(QToolButton) + \
            dock._root.findChildren(QLineEdit):
        parent = child.parentWidget()
        if child.isVisible() and parent is not None and not parent.rect().contains(child.geometry()):
            bad.append((child.objectName() or type(child).__name__, child.geometry(), parent.rect()))
    return bad


def _unclipped(dock):
    for control in dock._root.findChildren(QToolButton) + [dock.limit_box]:
        if control.isVisible():
            assert control.visibleRegion().boundingRect().size() == control.size(), control.objectName() or control.text()


@pytest.mark.parametrize("size", [(480, 420), (1060, 800), LARGE], ids=["small", "default", "large"])
def test_hostile_labels_nothing_clipped_and_the_grid_scrolls_instead_of_shrinking(
        qtbot, scans_view_ws_readonly, size):
    dock = open_dock(qtbot, scans_view_ws_readonly, size=size).dock
    dock.flush_redraw()                                  # the shown dock settles first; then it is handed the state
    dock.apply(hostile_state())
    QApplication.processEvents()
    assert not _children_inside(dock)
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    for axis in (dock.x_axis, dock.y_axis):                            # custom painted, not ElidedLabel: the tooltip holds it all
        assert axis.full_text() and axis.full_text() in axis.toolTip() and axis.accessibleName() == axis.full_text()
    assert dock.title_label.full_text() in dock.title_label.toolTip()
    assert dock.grid_host.width() >= 7 * CELL_MIN[0]
    for cell in dock.cells.values():
        assert cell.width() >= CELL_MIN[0] and cell.height() >= CELL_MIN[1]
        assert cell.absence_label.text() == "not zeroed" and cell.hkl_label.full_text() in cell.hkl_label.toolTip()
        assert cell.q_label.full_text() in cell.q_label.toolTip()
        assert all(cell.rect().contains(w.geometry()) for w in (cell.name_row, cell.hkl_label, cell.q_label))
    if size == (480, 420):
        assert dock.scroll.horizontalScrollBar().maximum() > 0 and dock.scroll.verticalScrollBar().maximum() > 0
    _shot(dock, f"hostile_{size[0]}x{size[1]}.png")


def _check_cells_paint_inside(dock):
    viewport = dock.scroll.viewport()
    for name, cell in dock.cells.items():
        for child in (cell.name_row, cell.plot if cell.plot.isVisible() else cell.absence_label,
                      cell.hkl_label, cell.q_label):
            assert cell.rect().contains(child.geometry()), (name, child.objectName(), child.geometry(), cell.rect())
        for rect in cell.outline_rects():
            assert QRect(cell.rect()).contains(rect.toRect().adjusted(0, 0, -1, -1)), (name, rect)
        for icon in cell.icons.values():
            if icon.isVisible():
                assert cell.name_row.rect().contains(icon.geometry()), (name, icon.objectName())
        dock.scroll.ensureWidgetVisible(cell, 0, 0)
        QApplication.processEvents()
        spot = QRect(cell.mapTo(viewport, QPoint(0, 0)), cell.size())
        assert viewport.rect().contains(spot), (name, spot, viewport.rect())


def _anchor_and_lock_something(w):
    from gui.inter_fakes import FakeInterState, make_overview
    from gui.array_helpers import inter_row
    inter = FakeInterState()
    a3, a4, a6 = (analyzer(KEY_A, n) for n in (3, 4, 6))
    inter.overview = make_overview(by_key={a3: inter_row(a3, locked=True, locked_fit_id="fast"),
                                           a4: inter_row(a4, locked=True, locked_fit_id="fast")}, anchors=(a4, a6))
    w.array_vm.set_inter(inter)
    w.scan_list_vm.set_inter(inter)


@pytest.mark.parametrize("size", [LAPTOP, LARGE], ids=["laptop", "large"])
def test_composed_in_its_own_window_then_docked_into_the_main_window(qtbot, monkeypatch, ws28, size):
    w = _window(qtbot, monkeypatch, size, ws28)
    _anchor_and_lock_something(w)
    dock = _open(w, qtbot)
    _select(w, analyzer(KEY_A, 6), analyzer(KEY_A, 9), clicked=analyzer(KEY_A, 6))
    assert len(dock.cells) == 28 and dock.cell_at("a41").key is not None      # all 28 analyzers have files here
    assert dock.array_window.isVisible() and dock._root.parentWidget() is dock.array_window
    assert_apart(dock, [dock.x_axis, dock.y_axis, *dock.cells.values()], "the grid and its axes")
    _check_cells_paint_inside(dock)
    assert dock.x_axis.mapTo(dock.grid_host, QPoint(0, 0)).y() >= max(
        c.mapTo(dock.grid_host, QPoint(0, 0)).y() + c.height() for c in dock.cells.values()) - 1
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    _shot(dock._root, f"window_{size[0]}x{size[1]}.png")
    # the dock button puts it back into the main window's right area
    w.set_right_panel_collapsed(True)
    dock.array_window.dock_button.click()
    qtbot.wait(80)
    assert not dock.array_window.isVisible() and dock.isVisible() and not dock.isFloating()
    assert_frame_geometry(w, size)
    assert w.rect().contains(rect_in(w, dock))
    _check_cells_paint_inside(dock)
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    _shot(w, f"docked_{size[0]}x{size[1]}.png")


# ---------------------------------------------------------------------------
# its own window: the round trip, activity, the Panels check, floating, an older layout, the dock's area
# ---------------------------------------------------------------------------
def test_pop_out_and_dock_in_keep_one_content_and_the_same_cells(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = _open(w, qtbot)
    _select(w, analyzer(KEY_A, 6))
    root, cells = dock._root, dict(dock.cells)
    dock.array_window.dock_button.click()
    qtbot.wait(30)
    assert dock._root is root and root.parentWidget() is dock and dock.cells == cells
    assert all(dock.cells[name] is cell for name, cell in cells.items())
    dock.pop_out()
    qtbot.wait(30)
    assert dock._root is root and root.parentWidget() is dock.array_window and dock.cells == cells
    assert dock.array_window.isVisible() and dock.isHidden()


def test_the_array_is_inactive_while_hidden_in_either_host(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = _open(w, qtbot)
    assert w.array_vm.active
    dock.array_window.hide()
    assert not w.array_vm.active
    _open(w, qtbot)
    assert w.array_vm.active
    dock.dock_in()
    assert w.array_vm.active
    dock.hide()
    assert not w.array_vm.active
    dock.show_panel()
    assert w.array_vm.active


def test_the_window_is_its_own_window_and_closing_it_unticks_panels(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = _open(w, qtbot)
    action = _panels_action(w)
    assert action.isChecked()
    dock.array_window.close()                                         # the window's own close button
    assert not action.isChecked() and not dock.is_open()
    _open(w, qtbot)
    dock.array_window.dock_button.click()                             # the dock button leaves it checked
    qtbot.wait(30)
    assert action.isChecked() and dock.isVisible()


def test_dock_in_returns_the_array_to_the_area_the_dock_sits_in(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = w.array_dock
    assert w.dockWidgetArea(dock) == Qt.RightDockWidgetArea          # never docked: the right area
    w.addDockWidget(Qt.BottomDockWidgetArea, dock)                    # where the operator last left the dock
    _open(w, qtbot)
    dock.dock_in()
    qtbot.wait(30)
    assert w.dockWidgetArea(dock) == Qt.BottomDockWidgetArea and dock.isVisible()


def test_floating_the_dock_pops_the_array_out_into_its_window(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = _open(w, qtbot)
    dock.dock_in()
    qtbot.wait(30)
    win = dock.array_window
    dock.setFloating(True)
    dock.setGeometry(QRect(30, 20, 600, 450))
    floated = dock.geometry()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=2000)
    assert not dock.isFloating() and dock.isHidden() and dock.is_open()
    assert dock._root.parentWidget() is win and win.geometry() == floated


def test_a_floating_array_from_an_older_layout_becomes_the_window(qtbot, monkeypatch, ws28):
    """Layouts saved before the window existed float the array as a Qt dock; that floating dock becomes the window."""
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    dock = w1.array_dock
    dock.blockSignals(True)                                           # the older layout: a visible Qt floating dock
    dock.dock_in()
    dock.setFloating(True)
    dock.setGeometry(QRect(30, 20, 600, 450))
    dock.blockSignals(False)
    floated = dock.geometry()
    session = w1._snapshot_session()
    session.view_state.pop("analyzer_array_window")                   # no window state: an existing user's session
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    win = w2.array_dock.array_window
    assert win.isVisible() and w2.array_dock.isHidden() and not w2.array_dock.isFloating()
    assert win.geometry() == floated and w2.array_dock.is_open()


def test_a_docked_array_from_an_older_layout_stays_docked(qtbot, monkeypatch, ws28):
    """Only a floating array converts; one docked in the main window before the upgrade stays where it was."""
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w1, qtbot)
    w1.array_dock.dock_in()
    session = w1._snapshot_session()
    session.view_state.pop("analyzer_array_window")                   # no window state: an existing user's session
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    assert w2.array_dock.isVisible() and not w2.array_dock.array_window.isVisible()
    assert w2.array_dock._root.parentWidget() is w2.array_dock and _panels_action(w2).isChecked()


def test_a_reopened_window_is_shown_after_the_main_window(qtbot, monkeypatch, ws28):
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w1, qtbot)
    session = _saved(w1)
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: LAPTOP)
    w2 = MainWindow(Workspace.open(ws28), session=session, watch=False)
    qtbot.addWidget(w2)
    assert not w2.array_dock.array_window.isVisible()                 # not during construction, before the main window
    w2.show()
    qtbot.waitUntil(lambda: w2.array_dock.array_window.isVisible())

# ---------------------------------------------------------------------------
# responsiveness (a target, my estimate: measured and reported, not a gate)
# ---------------------------------------------------------------------------
def test_scan_switch_time_is_measured(qtbot, monkeypatch, ws28, tmp_path):
    w = _window(qtbot, monkeypatch, LARGE, ws28)
    dock = _open(w, qtbot)
    sel = w.scan_list_vm.selection

    def switch(key):
        t0 = time.perf_counter()
        sel.set((key,), clicked=key)
        dock.flush_redraw()
        t1 = time.perf_counter()
        dock._root.grab()                                              # a full paint of the 28 plots
        return t1 - t0, time.perf_counter() - t1

    first = switch(analyzer(KEY_A, 6))            # first open: files read, 28 cells built
    cold = switch(analyzer(KEY_B, 6))             # a scan not seen yet: files read
    warm_a = switch(analyzer(KEY_A, 6))           # back: every bundle is cached
    warm_b = switch(analyzer(KEY_B, 6))
    lines = [f"{name}: redraw {r:.3f} s + paint {p:.3f} s = {r + p:.3f} s" for name, (r, p) in
             (("first open (cold, builds the 28 cells)", first), ("switch to an unseen scan (cold)", cold),
              ("switch back (warm)", warm_a), ("switch again (warm)", warm_b))]
    (tmp_path / "array_timing.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("\n".join(lines))
    assert sum(warm_a) < 30 and sum(warm_b) < 30                      # a hang guard, not the 0.5 s target
    assert dock.last_redraw is not None and dock.last_redraw.n > 0

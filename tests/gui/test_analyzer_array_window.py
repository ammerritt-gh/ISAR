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

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QToolButton

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B
from gui.array_helpers import hostile_state, open_dock
from gui.window_frame_checks import assert_apart, assert_elided_have_tooltips, assert_frame_geometry, rect_in
from isar.workspace_api import Workspace
from isar_gui import main_window as mw_mod
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
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
def test_panels_lists_it_and_the_first_open_floats_inside_the_screen(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    assert "Analyzer array" in {a.text() for a in w._docks_menu.actions()}
    dock = w.array_dock
    assert dock.isHidden() and dock.isFloating()                      # parked until opened
    assert not w._session.view_state.get("array_dock_placed")
    _open(w, qtbot)
    assert dock.isVisible() and dock.isFloating() and _panels_action(w).isChecked()
    assert QRect(0, 0, *LAPTOP).contains(dock.frameGeometry()) and dock.width() >= 800
    assert w._session.view_state["array_dock_placed"] is True
    placed = _geometry(dock)
    _panels_action(w).trigger()                                       # hide
    qtbot.wait(30)
    assert dock.isHidden()
    _open(w, qtbot)                                                   # reopen: left where it was
    assert dock.isVisible() and _geometry(dock) == placed
    dock.move(placed.x() + 37, placed.y() + 21)                       # the operator moves it; a reopen keeps that
    moved = _geometry(dock)
    _panels_action(w).trigger()
    _open(w, qtbot)
    assert _geometry(dock) == moved


def test_a_restart_restores_it_visible_floating_and_in_place(qtbot, monkeypatch, ws28):
    w1 = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w1, qtbot)
    # Qt clamps a restored floating window into the real screen (the offscreen one is 800 x 600), so the
    # operator's arrangement here is one that fits it.
    w1.array_dock.resize(600, 450)
    w1.array_dock.move(30, 20)
    qtbot.wait(30)
    w1.array_vm.set_scale("raw")
    session = _saved(w1)
    assert session.view_state["array_dock_placed"] is True and session.view_state["analyzer_array"]["scale"] == "raw"
    w2 = _window(qtbot, monkeypatch, LAPTOP, ws28, session=session)
    qtbot.wait(30)
    assert w2.array_dock.isVisible() and w2.array_dock.isFloating()
    assert _geometry(w2.array_dock).topLeft() == _geometry(w1.array_dock).topLeft()
    assert w2.array_vm.scale == "raw"


def test_reset_layout_parks_it_and_clears_the_placed_flag(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w, qtbot)
    w.reset_layout()
    qtbot.wait(30)
    assert w.array_dock.isHidden() and not w._session.view_state.get("array_dock_placed")
    _open(w, qtbot)
    assert w.array_dock.isVisible() and w.array_dock.isFloating()


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
    assert w2.array_dock.isVisible() and w2.array_dock.isFloating()


def test_a_rebind_keeps_it_shown_and_compact_mode_hides_and_restores_it(qtbot, monkeypatch, ws28):
    w = _window(qtbot, monkeypatch, LAPTOP, ws28)
    _open(w, qtbot)
    w.array_vm.set_y_mode("shared")
    w._rebind(Workspace.open(ws28))
    qtbot.wait(50)
    assert w.array_dock.isVisible() and w.array_dock.isFloating() and w.array_vm.y_mode == "shared"
    w.toggle_compact_mode()
    assert w.array_dock.isHidden()
    w.toggle_compact_mode()
    qtbot.wait(50)
    assert w.array_dock.isVisible()


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
    for child in dock.widget().findChildren(QLabel) + dock.widget().findChildren(QToolButton) + \
            dock.widget().findChildren(QLineEdit):
        parent = child.parentWidget()
        if child.isVisible() and parent is not None and not parent.rect().contains(child.geometry()):
            bad.append((child.objectName() or type(child).__name__, child.geometry(), parent.rect()))
    return bad


def _unclipped(dock):
    for control in dock.widget().findChildren(QToolButton) + [dock.limit_box]:
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
def test_composed_in_the_real_window_floating_then_docked(qtbot, monkeypatch, ws28, size):
    w = _window(qtbot, monkeypatch, size, ws28)
    _anchor_and_lock_something(w)
    dock = _open(w, qtbot)
    _select(w, analyzer(KEY_A, 6), analyzer(KEY_A, 9), clicked=analyzer(KEY_A, 6))
    assert len(dock.cells) == 28 and dock.cell_at("a41").key is not None      # all 28 analyzers have files here
    assert_frame_geometry(w, size)
    assert_apart(dock, [dock.x_axis, dock.y_axis, *dock.cells.values()], "the grid and its axes")
    _check_cells_paint_inside(dock)
    assert dock.x_axis.mapTo(dock.grid_host, QPoint(0, 0)).y() >= max(
        c.mapTo(dock.grid_host, QPoint(0, 0)).y() + c.height() for c in dock.cells.values()) - 1
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    _shot(dock, f"floating_{size[0]}x{size[1]}.png")
    # docked into the window's right area
    w.set_right_panel_collapsed(True)
    dock.setFloating(False)
    w.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.show()
    qtbot.wait(80)
    assert not dock.isFloating() and dock.isVisible()
    assert_frame_geometry(w, size)
    assert w.rect().contains(rect_in(w, dock))
    _check_cells_paint_inside(dock)
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    _shot(w, f"docked_{size[0]}x{size[1]}.png")


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
        dock.grab()                                                    # a full paint of the 28 plots
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

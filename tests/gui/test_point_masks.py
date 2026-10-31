"""Real offscreen widgets: WYSIWYG masks, restore, sizing and stale explanations."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt

from scans_view_fixture import ONE_MODE, TWO_MODE
from isar.intake.point_masks import save_selection
from isar.workspace_api import Workspace
from isar.workspace_api.spectra import spectrum_bundles
from isar_gui.views.scans_page import ScansPage
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.viewmodels.analyzer_array import AnalyzerArrayViewModel, CellPlot


@pytest.fixture
def page(qtbot, scans_view_ws):
    ws = Workspace.open(scans_view_ws)
    page = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(page)
    page.resize(850, 700)
    page.show()
    page.selection.set((ONE_MODE, TWO_MODE))
    page.flush_redraw()
    return page


def test_box_masks_all_displayed_and_restores_mixed_selection_logically(page):
    page.vm.set_layout('overlay')
    page.flush_redraw()
    # A small energy slice, all intensities; both displayed analyzers participate.
    point = page.masks.points[12]
    rect = QRectF(point[2] - .01, -1e9, .02, 2e9)
    selected = page.masks._selection(page.masks._inside(rect))
    assert set(selected) == {ONE_MODE, TWO_MODE}
    assert save_selection(page.ws.root, {ONE_MODE: selected[ONE_MODE]}).ok
    page.ws.refresh()
    page.scan_list_vm.on_workspace_changed()
    page.flush_redraw()
    page.masks.preview_box(rect)
    assert 'Mask 2 points' in page.hint.full_text() and page.hint.isVisible()
    page.masks.select_box(rect)
    page.flush_redraw()
    assert len(page.masks.rows) == 2
    page.masks.select_box(rect)
    page.flush_redraw()
    assert page.masks.rows == []


def test_waterfall_rectangle_uses_offsets_and_duplicate_copies_toggle_once(page):
    page.vm.set_step(5000)
    page.flush_redraw()
    page.masks.vb.updateAutoRange()
    p = page.masks.points[10]
    state = page._drawn_state
    assert p[3] == pytest.approx(state.traces[0].y[10] + 5000)
    rect = QRectF(p[2] - .001, p[3] - .001, .002, .002)
    assert set(page.masks._selection(page.masks._inside(rect))) == {state.keys[0]}
    page.masks.select_box(rect)
    page.flush_redraw()
    assert len(page.masks.rows) == 1
    # One source spectrum repeated in a fit waterfall: restoring both copies acts once.
    trace = page._drawn_state.traces[0]
    repeated = replace(state, keys=(state.keys[0], state.keys[0]), traces=(trace, trace), step=100)
    page.masks.apply(repeated)
    selected = page.masks._selection(page.masks.points)
    assert len(selected[state.keys[0]]) == len(trace.x)
    assert len(page.masks.rows) == 1


def test_point_click_is_pixel_based_and_mask_survives_scale_changes(page, qtbot):
    page.selection.set((ONE_MODE,))
    page.flush_redraw()
    qtbot.wait(20)
    point = page.masks.points[-1]
    click = page.masks.vb.mapViewToScene(QPointF(point[2], point[3]))
    page.masks.mode.setChecked(True)
    qtbot.mouseClick(page._glw.viewport(), Qt.MouseButton.LeftButton,
                     pos=page._glw.mapFromScene(click))
    page.flush_redraw()
    assert len(page.masks.rows) == 1
    point_id = page.masks.rows[0][1]
    page.vm.set_scale('raw')
    page.flush_redraw()
    assert page.masks.rows[0][1] == point_id
    page.masks.show_list()
    table = page.masks.dialog.table
    assert table.rowCount() == 1
    table.selectRow(0)
    page.masks.dialog.restore_selected()
    page.flush_redraw()
    assert page.masks.rows == []


def test_mask_changes_fit_status_hover_and_data_bounds(page, qtbot):
    page.selection.set((ONE_MODE,))
    page.flush_redraw()
    before = page.ws.unit_data(ONE_MODE)
    page.masks.save({ONE_MODE: [before.point_ids[-1]]})
    page.flush_redraw()
    qtbot.wait(20)
    after = page.ws.unit_data(ONE_MODE)
    np.testing.assert_array_equal(before.counts, after.counts)
    assert not after.mask[-1] and after.manual_mask[-1]
    row = page.scan_list_vm.snapshot.spectrum(ONE_MODE)
    assert row.fast.current is False
    assert any(name == 'manual point mask' for name, _, _ in row.fast.changed)
    trace = page._drawn_state.traces[0]
    assert 'stale' in trace.note
    assert 'manual point mask' in trace.tooltip
    legend = page._main_plot._isar_rendered.legend
    assert any('manual point mask' in label.toolTip() for _, label in legend.items)
    # Old fit grids and masked energies cannot enter the bounds of the analyzer grid.
    plot = CellPlot((), np.array([0., 1., 80.]), np.array([2., 3., 999.]),
                    np.array([True, True, False]), np.array([-20., 90.]), np.array([1., 1.]), ())
    lo, hi = AnalyzerArrayViewModel._x_range([plot])
    assert lo > -1 and hi < 2
    page._main_plot.setRange(xRange=(trace.x[0], trace.x[-2]), yRange=(0, 10000), padding=0)
    assert page.masks.edges.counts['right'][1] == 1
    page.masks.show_all()
    assert 'right' not in page.masks.edges.counts


def test_all_masked_stays_drawable_and_restorable(page):
    page.selection.set((ONE_MODE,))
    page.flush_redraw()
    page.masks.save(page.masks._selection(page.masks.points))
    page.flush_redraw()
    assert page._drawn_state.traces
    assert not page._drawn_state.traces[0].kept.any()
    assert page.masks.rows
    page.masks.show_list()
    page.masks.dialog.restore_all()
    page.flush_redraw()
    assert page._drawn_state.traces[0].kept.any()
    assert not page.masks.rows


def test_mouse_drag_selects_visible_waterfall_points(page, qtbot):
    page.vm.set_step(5000)
    page.flush_redraw()
    page.masks.vb.updateAutoRange()
    page.masks.mode.setChecked(True)
    qtbot.wait(20)
    point = page.masks.points[10]
    center = page._glw.mapFromScene(page.masks.vb.mapViewToScene(QPointF(point[2], point[3])))
    start = center + QPointF(-10, -10).toPoint()
    end = center + QPointF(10, 10).toPoint()
    viewport = page._glw.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, center, delay=30)
    qtbot.mouseMove(viewport, end, delay=30)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    page.flush_redraw()
    assert any(row[:2] == point[:2] for row in page.masks.rows)
    assert all(row[0] == point[0] for row in page.masks.rows)


def test_saved_edge_indicator_option_reaches_both_scan_views(qtbot, scans_view_ws):
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    session = Session()
    session.view_state['scans_page'] = {'out_of_range': False}
    window = MainWindow(Workspace.open(scans_view_ws), session=session, watch=False)
    qtbot.addWidget(window)
    window.show()
    window.scans_page.selection.set((ONE_MODE,))
    window.scans_page.flush_redraw()
    assert not window.scans_page.masks.edges.enabled
    assert not window.array_dock.out_of_range
    window._scans_display_actions['out_of_range'].trigger()
    window.scans_page.flush_redraw()
    assert window.scans_page.masks.edges.enabled and window.array_dock.out_of_range
    assert session.view_state['scans_page']['out_of_range'] is True

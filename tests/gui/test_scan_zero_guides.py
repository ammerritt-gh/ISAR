"""Full-span Scan(s) guides on real widgets, including redraw, pan/zoom and saved configuration."""
import numpy as np
import pytest
import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from isar.plotspec.spec import GuideLine, PlotSpec, Series
from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.plotspec_pg import render_into
from isar_gui.session import Session
from scans_view_fixture import ONE_MODE, TWO_MODE


def _assert_full_span(plot):
    rect = plot.getViewBox().viewRect()
    lines = [item for item in plot.items if isinstance(item, pg.InfiniteLine)]
    assert lines
    for line in lines:
        bounds = line.mapRectToParent(line.boundingRect())
        if line.angle == 90:
            assert bounds.top() <= rect.top() and bounds.bottom() >= rect.bottom()
            assert line.value() == 0
        else:
            assert bounds.left() <= rect.left() and bounds.right() >= rect.right()
    return lines


@pytest.mark.parametrize('size', [(1108, 851), (1920, 1040)])
def test_config_energy_zero_and_scan_zeros_span_the_view_and_survive_redraw(qtbot, scans_view_ws_readonly, tmp_path, size):
    session = Session()
    window = MainWindow(Workspace.open(scans_view_ws_readonly), session=session, watch=False)
    qtbot.addWidget(window)
    window.resize(*size)
    window.show()
    page = window.scans_page
    page.vm.set_fit('fast')
    page.selection.set((ONE_MODE, TWO_MODE))
    page.vm.set_step(100)
    page.flush_redraw()
    window._sync_scans_display()
    action = window._scans_display_actions['energy_zero_line']
    assert not action.isChecked()
    page._main_plot.setRange(xRange=(-1000, 1000), yRange=(-100, 1000), padding=0)
    before = page.drawn().view_range
    action.trigger()
    page.flush_redraw()
    QApplication.processEvents()
    assert page.drawn().view_range == before
    assert session.view_state['scans_page']['energy_zero_line'] is True
    lines = _assert_full_span(page._main_plot)
    assert sorted((line.angle, line.value()) for line in lines) == [(0, 0), (0, 100), (90, 0)]
    # Full-span geometry follows a second, much wider view.
    page._main_plot.setRange(xRange=(-1e6, 2e6), yRange=(-1e6, 2e6), padding=0)
    QApplication.processEvents()
    assert len(_assert_full_span(page._main_plot)) == 3
    assert window.grab().save(str(tmp_path / f'zero-guides-{size[0]}.png'))
    page.vm.set_display_option('energy_zero_line', False)
    page.vm.load_view_state(session)
    assert page.vm.energy_zero_line is True
    page.selection.set((ONE_MODE,))
    page.flush_redraw()
    assert [(g.axis, g.value) for g in page._drawn_state.residual_spec.guides] == [('x', 0)]
    action.trigger()
    page.flush_redraw()
    assert not page._drawn_state.main_spec.guides and not page._drawn_state.residual_spec.guides
    assert not any(isinstance(item, pg.InfiniteLine) for item in page._main_plot.items)


@pytest.mark.parametrize('include_baseline', [False, True])
def test_guides_preserve_data_autorange_and_baseline_visibility(qtbot, include_baseline):
    widget = pg.GraphicsLayoutWidget()
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    plot = widget.addPlot()
    spec = PlotSpec('', 'x', 'y', series=[Series(np.array([1000., 1001.]), np.array([1e6, 1e6 + 1]))],
                    guides=[GuideLine('x', 0), GuideLine('y', 0, include_in_bounds=include_baseline)])
    render_into(plot, spec)
    widget.show()
    plot.autoRange()
    QApplication.processEvents()
    x, y = plot.viewRange()
    assert x[0] > 999
    assert y[0] <= 0 if include_baseline else y[0] > 1e6 - 1
    render_into(plot, spec)
    assert len([item for item in plot.items if isinstance(item, pg.InfiniteLine)]) == 2

"""Exercise the Config menu against real Scan(s) widgets and synthetic stored fits."""
import numpy as np
from PySide6.QtCore import Qt
import pyqtgraph as pg

from isar.plotspec.spec import Role, CURVE_COLORS
from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from scans_view_fixture import ONE_MODE, TWO_MODE


def test_config_actions_redraw_and_save_scan_appearance(qtbot, scans_view_ws_readonly):
    session = Session()
    window = MainWindow(Workspace.open(scans_view_ws_readonly), session=session, watch=False)
    qtbot.addWidget(window)
    window.show()
    page = window.scans_page
    page.vm.set_fit('fast')
    page.selection.set((ONE_MODE, TWO_MODE))
    page.vm.set_step(100)
    page.flush_redraw()
    window._sync_scans_display()
    actions = window._scans_display_actions
    assert all(a.toolTip() for a in actions.values())
    assert actions['zero_lines'].isChecked() and actions['peak_lines'].isChecked()
    assert not actions['filled_symbols'].isChecked()
    assert not actions['energy_zero_line'].isChecked()
    spec = page._drawn_state.main_spec
    guides = [s for s in spec.series if s.legend_group is None]
    assert [(g.axis, g.value) for g in spec.guides] == [('y', 100), ('y', 0)]
    peaks = [s for s in guides if s.style != 'dotted']
    assert len(peaks) == 5  # one of the six centres is outside the recorded energy range
    assert all(s.x[0] == s.x[1] and s.y[1] >= s.y[0] for s in peaks)
    assert {s.color for s in peaks} == {CURVE_COLORS['mode1'], CURVE_COLORS['mode2']}
    assert any(s.x[0] < 0 for s in peaks) and any(s.x[0] > 0 for s in peaks)
    assert [a.xy[1] for a in spec.annotations] == [100, 0]
    assert all(a.anchor[1] < 0 for a in spec.annotations)
    assert all('heavy' in s.style for s in spec.series if (s.label or '').startswith('Mode'))
    data = [s for s in spec.series if s.role is Role.DATA]
    assert all(s.style == 'hollow' for s in data)
    points = [i for i in page._main_plot.listDataItems() if i.opts['symbol'] is not None]
    assert points and all(pg.mkBrush(i.opts['symbolBrush']).style() == Qt.BrushStyle.NoBrush
                          for i in points)
    page._main_plot.setRange(xRange=(-8, 8), yRange=(-10, 250), padding=0)
    before = page.drawn().view_range
    for name in ('zero_lines', 'peak_lines', 'filled_symbols'):
        actions[name].trigger()
    page.flush_redraw()
    assert page.drawn().view_range == before
    assert not any(s.legend_group is None for s in page._drawn_state.main_spec.series)
    assert not page._drawn_state.main_spec.guides
    backgrounds = [s for s in page._drawn_state.main_spec.series
                   if s.label is None and s.color == CURVE_COLORS['background']]
    assert len(backgrounds) == 2 and all(s.style == 'dotted' for s in backgrounds)
    points = [i for i in page._main_plot.listDataItems() if i.opts['symbol'] is not None]
    assert all(pg.mkBrush(i.opts['symbolBrush']).style() == Qt.BrushStyle.SolidPattern for i in points)
    for old, new in zip(data, [s for s in page._drawn_state.main_spec.series if s.role is Role.DATA]):
        np.testing.assert_array_equal(old.y, new.y)
    assert session.view_state['scans_page']['filled_symbols'] is True
    page.vm.load_view_state(session)
    assert not page.vm.zero_lines and not page.vm.peak_lines and page.vm.filled_symbols

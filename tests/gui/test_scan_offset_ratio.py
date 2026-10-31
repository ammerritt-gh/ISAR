"""Linked offset inputs, ratio stepping, saved choices and compact labelled geometry on real Qt widgets."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from isar.workspace_api import Workspace
from isar_gui.gui_store import GuiStore
from isar_gui.session import Session
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scans_page import ScansPage
from scans_view_fixture import ONE_MODE, TWO_MODE, NO_Q


@pytest.fixture
def page(qtbot, scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    widget = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(widget)
    widget.set_store(GuiStore(scans_view_ws_readonly))
    widget.resize(600, 600)
    widget.show()
    widget.selection.set((ONE_MODE, TWO_MODE))
    widget.flush_redraw()
    return widget


def _range(page):
    return max(float(np.ptp(t.y[t.kept])) for t in page._drawn_state.traces)


def _type(qtbot, box, value):
    box.setFocus()
    qtbot.keyClick(box, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(box, value)
    qtbot.keyClick(box, Qt.Key.Key_Return)


def test_edit_either_input_and_step_ratio_without_losing_presses(page, qtbot):
    base = _range(page)
    assert page.ratio_box.value() == 1.1 and page._drawn_state.step_auto
    assert page._drawn_state.step == pytest.approx(base * 1.1)
    _type(qtbot, page.ratio_box, '1.35')
    page.flush_redraw()
    assert page._drawn_state.step == pytest.approx(base * 1.35)
    assert page.offset_box.value() == pytest.approx(base * 1.35, abs=0.005)
    _type(qtbot, page.offset_box, f'{base * 1.75:.2f}')
    page.flush_redraw()
    assert not page._drawn_state.step_auto and page.ratio_box.value() == pytest.approx(1.75, abs=0.01)
    qtbot.mouseClick(page.ratio_minus, Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page.ratio_box.value() == 1.70 and page._drawn_state.step_auto
    for _ in range(3):
        qtbot.mouseClick(page.ratio_plus, Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page.ratio_box.value() == 1.85
    assert page._drawn_state.step == pytest.approx(base * 1.85)
    page.ratio_box.setValue(0)
    qtbot.mouseClick(page.ratio_minus, Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page.ratio_box.value() == 0 and page._drawn_state.step == 0
    assert not page.auto_button.isCheckable()
    for _ in range(2):
        qtbot.mouseClick(page.auto_button, Qt.MouseButton.LeftButton)
        page.flush_redraw()
        assert page.ratio_box.text() == '×1.10'
        assert page._drawn_state.step == pytest.approx(base * 1.1)
    assert page.offset_box.decimals() == page.ratio_box.decimals() == 2


def test_relative_and_absolute_modes_follow_their_own_values_on_selection_and_scale_changes(page):
    page.ratio_box.setValue(1.4)
    page.selection.set((ONE_MODE, NO_Q))
    page.flush_redraw()
    assert page._drawn_state.step == pytest.approx(_range(page) * 1.4)
    page.offset_box.setValue(50)
    page.flush_redraw()
    page.selection.set((ONE_MODE, TWO_MODE))
    page.flush_redraw()
    assert page._drawn_state.step == 50
    assert page._drawn_state.step_ratio == pytest.approx(50 / _range(page))
    ratio = page._drawn_state.step_ratio
    page.vm.set_scale('raw')
    page.flush_redraw()
    assert page._drawn_state.step_auto
    assert page._drawn_state.step == pytest.approx(_range(page) * ratio)


def test_ratio_and_absolute_choice_roundtrip_and_older_sessions_start_at_one_point_one(page):
    session = Session()
    page.ratio_box.setValue(1.35)
    page.flush_redraw()
    page.save_view_state(session)
    page.vm.set_ratio(2)
    page.load_view_state(session)
    page.flush_redraw()
    assert page.ratio_box.value() == 1.35 and page._drawn_state.step_auto
    page.offset_box.setValue(75)
    page.flush_redraw()
    page.save_view_state(session)
    page.vm.set_ratio(2)
    page.load_view_state(session)
    page.flush_redraw()
    assert page.offset_box.value() == 75 and not page._drawn_state.step_auto
    page.load_view_state(Session())
    page.flush_redraw()
    assert page.ratio_box.value() == 1.1 and page._drawn_state.step_auto


@pytest.mark.parametrize('width', [300, 600])
def test_offset_boxes_have_visible_labels_and_fit_the_narrow_panel(page, width, tmp_path):
    page.resize(width, 600)
    QApplication.processEvents()
    assert page.minimumSizeHint().width() <= 300
    assert page.offset_label.text() == 'Absolute offset' and page.offset_label.buddy() is page.offset_box
    assert page.ratio_label.text() == 'Offset ratio' and page.ratio_label.buddy() is page.ratio_box
    assert page.ratio_box.prefix() == '×'
    widgets = (page.offset_label, page.offset_box, page.ratio_label, page.ratio_box,
               page.ratio_minus, page.ratio_plus, page.auto_button)
    for widget in widgets:
        assert page.rect().contains(widget.mapTo(page, widget.rect().topLeft()))
        assert page.rect().contains(widget.mapTo(page, widget.rect().bottomRight()))
        assert not widget.isHidden()
    for widget in widgets[1:]:
        if widget is not page.ratio_label:
            assert widget.accessibleName() and widget.toolTip()
    assert page.grab().save(str(tmp_path / f'offset-ratio-{width}.png'))
    # A flat scan has no meaningful ratio; the explicit absolute control still works.
    page._controls(replace(page._drawn_state, step_ratio=None))
    assert page.offset_box.isEnabled() and not page.ratio_box.isEnabled()
    assert not page.ratio_minus.isEnabled() and not page.ratio_plus.isEnabled()
    assert 'No nonzero data range' in page.ratio_box.toolTip()

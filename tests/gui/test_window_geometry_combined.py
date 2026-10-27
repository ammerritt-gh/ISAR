"""The hardest laptop combination, with real content, in one window.

Offscreen Qt with the real widgets over the 56-spectrum BL43LXU-shaped workspace generated at test
time, at the laptop size. The combination: the scan inspector open inside the left panel, every
optional column on, and the Processing panel in one of two states, set explicitly here (the
auto-collapse on opening the inspector is an S1U7 repair with its own test):

* collapsed to its rail: the fit page's plot keeps at least 240 x 250, a floor that catches the
  86 px squeeze an expanded Processing panel leaves (collapsed it measures 253 px);
* expanded: only the display (CenterHost) keeps its 300 x 250 minimum; a narrower plot is accepted.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import bl43_fixture
from gui.window_frame_checks import assert_frame_geometry, rect_in
from isar_gui.main_window import MainWindow
from isar_gui.session import Session

LAPTOP = (1108, 851)
A03 = f"{bl43_fixture.KEY_A}:a03"          # a fitted analyzer of set A: its display is the fit page
DISPLAY_MIN = (300, 250)
PLOT_MIN = (240, 250)


def _open_hardest_combination(qtbot, monkeypatch, tmp_path, *, processing_collapsed: bool):
    ws_root = bl43_fixture.build_bl43_workspace(tmp_path, n_analyzers=28)
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: LAPTOP)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*LAPTOP)
    w.show()
    qtbot.waitExposed(w)
    assert w.load_workspace(str(ws_root))
    panel = w.scan_list
    for column in ("fast", "deep", "slit", "file"):
        panel._column_actions[column].trigger()
    w.set_right_panel_collapsed(processing_collapsed)
    w.scan_list_vm.inspect(A03)
    panel._view.expandAll()
    qtbot.wait(50)
    return w, panel


def _assert_usable(w, panel, processing_collapsed: bool):
    assert_frame_geometry(w, LAPTOP)
    assert w.scan_list_vm.current_key == A03
    assert w._center_host.current_key == "scan"

    # the inspector is part of the left panel; the scan list keeps its minimum beside it
    assert w.left_column_dock.isAncestorOf(w.scan_inspector) and not w.scan_inspector.isHidden()
    assert panel.width() >= 280, panel.width()
    assert rect_in(w, w.log_dock).left() > rect_in(w, w.scan_inspector).right()

    # a usable number of rows: at least ten analyzer or set rows fit in the tree
    tree = panel._view
    assert tree.viewport().height() // tree.sizeHintForRow(0) >= 10

    # every control is reachable: visible, not zero-width, and inside the window
    processing = w.processing_rail if processing_collapsed else w.processing_panel._run
    controls = (w.experiment_strip._new, w.experiment_strip._open, w.experiment_strip._recent,
                w.experiment_strip._rename, w.output_bar._save_copy, w.output_bar._export,
                processing, w.scan_inspector._close, panel._filter,
                panel._columns_button, panel._sort_button, panel._inspect)
    for control in controls:
        rect = rect_in(w, control)
        assert control.isVisible() and rect.width() > 0 and w.rect().contains(rect), (
            control.objectName(), rect)


def test_hardest_laptop_combination_plot_keeps_its_minimum_when_processing_is_collapsed(
        qtbot, monkeypatch, tmp_path):
    w, panel = _open_hardest_combination(qtbot, monkeypatch, tmp_path, processing_collapsed=True)
    _assert_usable(w, panel, processing_collapsed=True)

    plot = w._center_host.glw_for("scan")
    assert plot is not None and plot.isVisible()
    assert plot.width() >= PLOT_MIN[0] and plot.height() >= PLOT_MIN[1], (plot.width(), plot.height())


def test_hardest_laptop_combination_display_keeps_its_minimum_when_processing_is_expanded(
        qtbot, monkeypatch, tmp_path):
    w, panel = _open_hardest_combination(qtbot, monkeypatch, tmp_path, processing_collapsed=False)
    _assert_usable(w, panel, processing_collapsed=False)

    host = w._center_host
    assert host.isVisible()
    assert host.width() >= DISPLAY_MIN[0] and host.height() >= DISPLAY_MIN[1], (host.width(), host.height())

"""The Analyzer array dock on real widgets (offscreen): where each cell sits, what it says, what a click does.

Real workspace generated at test time (the small scans-view fixture: seven analyzers a03-a09 with real fits); the
read layer's 4 x 7 grid is the card's own, so the other 21 cells read "no file". Delta-hkl words and the 28-cell
cases use the hand-made view from ``array_helpers``.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bl43_fixture import KEY_A, KEY_B
from gui.array_helpers import inter_row, make_array_view, open_dock
from gui.inter_fakes import FakeInterState, make_overview
from gui.inter_helpers import assert_described
from gui.window_frame_checks import assert_elided_have_tooltips
from isar_gui.theme import marker_icon
from isar_gui.viewmodels import analyzer_array as vm_mod
from isar_gui.views.analyzer_cell import CELL_MIN
from scans_view_fixture import SPECTRA_A, SPECTRA_B

A3, A4, A5, A6, A7, A8, A9 = SPECTRA_A


@pytest.fixture
def env(qtbot, scans_view_ws_readonly):
    return open_dock(qtbot, scans_view_ws_readonly)


def _show(env, *keys, **kw):
    env.selection.set(keys, **kw)
    env.dock.flush_redraw()
    QApplication.processEvents()


def _pos(env, name):
    cell = env.dock.cell_at(name)
    return cell.mapTo(env.dock.grid_host, QPoint(0, 0))


def _click(cell, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(cell, Qt.MouseButton.LeftButton, modifier, QPoint(cell.width() // 2, 4))


def test_positions_names_and_labels(env):
    _show(env, A6)
    d = env.dock
    assert len(d.cells) == 28
    a03, a09, a41, a06 = (_pos(env, n) for n in ("a03", "a09", "a41", "a06"))
    assert a03.y() == a09.y() and a03.x() > a09.x()                   # a03 bottom right, a09 bottom left
    assert a41.x() == a09.x() and a41.y() < a09.y()                   # a41 top left
    assert a06.y() == a03.y() and a09.x() < a06.x() < a03.x()        # a06 in the scattering plane, centre
    assert max(_pos(env, n).x() for n in d.cells) == a03.x() and min(_pos(env, n).y() for n in d.cells) == a41.y()
    cell = d.cell_at("a06")
    assert cell.internal_label.text() == "A4" and cell.beamline_label.text() == "a06"
    assert d.cell_at("a35").internal_label.text() == "D1" and d.cell_at("a41").internal_label.text() == "D7"
    assert cell.q_label.full_text().startswith("|Q| ") and cell.q_label.full_text().endswith("nm⁻¹")
    assert cell.hkl_label.full_text().startswith("hkl ")
    assert d.title_label.full_text().startswith("scan CsV3Sb5_97K_0p5_0p5_3_sum")
    assert d.layout_label.full_text() == "Layout BL43LXU-7x4 (the only BL43LXU layout)"
    assert d.note_label.isHidden()
    assert d.body.currentWidget() is d.scroll
    assert d.grid_minimum()[0] >= 7 * CELL_MIN[0] and d.scroll.widget().minimumSizeHint().width() >= 7 * CELL_MIN[0]


def test_nothing_selected_and_the_axis_words(env, monkeypatch):
    d = env.dock
    d.flush_redraw()
    assert d.body.currentWidget() is d.message
    assert d.message.text() == "Select a scan or a spectrum in the scan list."
    view = make_array_view(KEY_A)
    monkeypatch.setattr(vm_mod, "array_view", lambda snapshot, key, ws: view)
    _show(env, A6)
    assert d.x_axis.full_text() == "In plane — 2θ increases ←   Δhkl ≈ (0.100, 0.000, 0.000) per analyzer"
    assert d.y_axis.full_text() == "Out of plane ↑   Δhkl ≈ (0.000, 0.100, 0.000) per row"
    assert "A1 → A2  (0.1, 0, 0)" in d.x_axis.toolTip() and "A1 → B1  (0, 0.1, 0)" in d.y_axis.toolTip()
    # the axis labels sit outside the grid, below and left of it
    assert d.x_axis.mapTo(d.grid_host, QPoint(0, 0)).y() > _pos(env, "a03").y()
    assert d.y_axis.mapTo(d.grid_host, QPoint(0, 0)).x() < _pos(env, "a09").x()


def test_a_scan_with_no_layout_says_so(qtbot, tmp_path):
    from bl43_fixture import pooled_workspace
    tavi = open_dock(qtbot, pooled_workspace(tmp_path / "tavi"))
    _show(tavi, "tavi:11:det")
    assert tavi.dock.body.currentWidget() is tavi.dock.message
    assert "no analyzer layout" in tavi.dock.message.text().lower() and "BL43LXU for now" in tavi.dock.message.text()
    assert "scan 11" in tavi.dock.title_label.text()
    assert tavi.dock.cells == {}


def test_both_scale_modes_and_the_typed_limit_on_the_real_viewboxes(env):
    _show(env, A6)
    d = env.dock

    def y_range(name):
        return d.cell_at(name).plot.getPlotItem().vb.viewRange()[1]

    auto = {n: y_range(n) for n in ("a03", "a06", "a09")}
    assert all(abs(lo) < 1e-9 for lo, _ in auto.values()) and len({round(hi, 3) for _, hi in auto.values()}) > 1
    d.mode_buttons["shared"].click()
    d.flush_redraw()
    shared = {y_range(n)[1] for n in ("a03", "a06", "a09")}
    assert len({round(v, 6) for v in shared}) == 1 and d.limit_box.isEnabled()
    assert d.limit_box.placeholderText().startswith("largest shown (")
    d.scale_buttons["raw"].click()
    d.limit_box.setText("250")
    d.limit_box.editingFinished.emit()
    d.flush_redraw()
    assert all(abs(y_range(n)[1] - 250.0) < 1e-6 and abs(y_range(n)[0]) < 1e-6 for n in ("a03", "a06", "a09"))
    assert d.limit_unit.full_text() == "Counts" and d.scale_buttons["raw"].isChecked()
    d.scale_buttons["normalised"].click()
    d.flush_redraw()
    assert d.limit_box.text() == "" and d.vm.limits["raw"] == 250.0          # the Raw limit is kept apart
    d.limit_box.setText("abc")                                               # refused: the box goes back, the reason shows
    d.limit_box.editingFinished.emit()
    d.flush_redraw()
    assert d.limit_box.text() == "" and "was not used" in d.limit_note.full_text() and d.limit_note.isVisible()
    d.mode_buttons["auto"].click()
    d.flush_redraw()
    assert not d.limit_box.isEnabled() and "Used when all cells share one y limit" in d.limit_box.toolTip()
    assert all(x == y_range("a06")[0] for x in (y_range("a06")[0],))
    xs = {tuple(round(v, 6) for v in d.cell_at(n).plot.getPlotItem().vb.viewRange()[0]) for n in d.cells}
    assert len(xs) == 1                                                       # one energy axis in every cell


def test_selection_round_trip_with_the_scan_list(env):
    _show(env, A6)
    d = env.dock
    _click(d.cell_at("a04"))
    assert env.selection.keys == (A4,) and env.selection.active == A4
    d.flush_redraw()
    assert [n for n, c in d.cells.items() if c.selected] == ["a04"]
    _click(d.cell_at("a07"), Qt.KeyboardModifier.ControlModifier)
    assert set(env.selection.keys) == {A4, A7} and env.selection.picks[0] == A4
    _click(d.cell_at("a04"), Qt.KeyboardModifier.ControlModifier)
    assert env.selection.keys == (A7,)
    _click(d.cell_at("a41"))                                                  # no file: nothing happens
    _click(d.cell_at("a41"), Qt.KeyboardModifier.ControlModifier)
    assert env.selection.keys == (A7,) and d.cell_at("a41").key is None
    _show(env, A3, A5, A8)                                                    # the list selects: those cells light up
    assert {n for n, c in d.cells.items() if c.selected} == {"a03", "a05", "a08"}
    d.cell_at("a05").setFocus()
    QTest.keyClick(d.cell_at("a05"), Qt.Key.Key_Space)
    assert env.selection.keys == (A5,)
    QTest.keyClick(d.cell_at("a05"), Qt.Key.Key_Space)
    assert env.selection.keys == (A5,)
    d.cell_at("a06").setFocus()
    QTest.keyClick(d.cell_at("a06"), Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    assert set(env.selection.keys) == {A5, A6}


def test_several_scans_selected_shows_the_first_picked_in_words(env):
    _show(env, A6, SPECTRA_B[0], A4, clicked=SPECTRA_B[0], first=SPECTRA_B[0])
    d = env.dock
    assert "_rep_sum" in d.title_label.full_text()
    assert d.note_label.isVisible() and d.note_label.full_text().endswith("— 2 scans selected, only one is shown")
    _show(env, A6, SPECTRA_B[0], A4, clicked=A6, first=A6)                    # the same selection, the other first
    assert "_rep_sum" not in d.title_label.full_text() and "_sum" in d.title_label.full_text()


def test_anchors_and_locks_show_outlines_and_icons(qtbot, scans_view_ws_readonly):
    inter = FakeInterState()
    inter.overview = make_overview(by_key={A3: inter_row(A3, locked=True, locked_fit_id="fast"),
                                           A4: inter_row(A4, locked=True, locked_fit_id="fast"),
                                           A6: inter_row(A6)}, anchors=(A4, A6))
    env = open_dock(qtbot, scans_view_ws_readonly, inter=inter)
    _show(env, A5)
    cells = env.dock.cells
    assert [cells[n].outline_kind for n in ("a03", "a04", "a06", "a07")] == ["lock", "both", "anchor", "none"]
    assert cells["a06"].icons["anchor"].isVisible() and not cells["a06"].icons["lock"].isVisible()
    assert cells["a03"].icons["lock"].isVisible() and cells["a04"].icons["anchor"].isVisible()
    assert not cells["a07"].icons["anchor"].isVisible() and not cells["a07"].icons["lock"].isVisible()
    assert marker_icon("anchor").pixmap(12, 12).toImage() != marker_icon("lock").pixmap(12, 12).toImage()
    # the outlines differ in the pixels too: solid anchor, dashed lock (never colour alone)
    solid, dashed = cells["a06"].grab().toImage(), cells["a03"].grab().toImage()
    edge = lambda image: [image.pixelColor(x, 1) for x in range(8, image.width() - 8)]
    assert len({c.name() for c in edge(solid)}) == 1 and len({c.name() for c in edge(dashed)}) > 1
    assert "anchor" in cells["a06"].accessibleDescription() and "locked" in cells["a03"].accessibleDescription()
    assert env.selection.keys == (A5,)                                        # selection is a separate channel
    _click(cells["a06"])
    env.dock.flush_redraw()
    assert cells["a06"].selected and cells["a06"].outline_kind == "anchor"   # a selected anchor reads as both


def test_the_three_absences_are_words_on_the_cells(env, monkeypatch):
    _show(env, A6)
    d = env.dock
    assert d.cell_at("a41").absence_label.isVisible() and d.cell_at("a41").absence_label.text() == "no file"
    assert not d.cell_at("a41").plot.isVisible()                              # no empty axes where nothing is drawn
    assert d.cell_at("a09").absence_label.text() == "no fit" and d.cell_at("a09").plot.isVisible()
    assert d.cell_at("a06").absence_label.isHidden()
    assert "no file" in d.cell_at("a41").toolTip().lower() or d.cell_at("a41").absence_label.toolTip()
    from dataclasses import replace
    from isar.workspace_api.spectra import ZERO_NONE
    real = vm_mod.spectrum_bundles
    monkeypatch.setattr(vm_mod, "spectrum_bundles", lambda ws, rows, **kw: tuple(
        replace(b, zero=ZERO_NONE) if b.key == A5 else b for b in real(ws, rows, **kw)))
    env.vm.refreshed.emit()
    d.flush_redraw()
    assert d.cell_at("a05").absence_label.text() == "not zeroed" and not d.cell_at("a05").plot.isVisible()


def test_a_word_beside_drawn_data_sits_under_the_plot_not_over_it(env):
    _show(env, A6)
    cell = env.dock.cell_at("a09")                                     # "no fit", with its data drawn
    assert cell.absence_label.isVisible() and cell.plot.isVisible()
    word = QRect(cell.absence_label.mapTo(cell, QPoint(0, 0)), cell.absence_label.size())
    assert not cell.plot_box.geometry().intersects(word)


def test_switching_scans_keeps_the_widgets_and_redraws_only_what_changed(env):
    _show(env, A6)
    d = env.dock
    before_widgets = dict(d.cells)
    redraws = {n: c.redraws for n, c in d.cells.items()}
    _show(env, A4)                                                            # same scan, other selection
    assert d.cells == before_widgets and {n: c.redraws for n, c in d.cells.items()} == redraws
    _show(env, SPECTRA_B[1])                                                  # the other scan
    assert d.cells == before_widgets
    changed = {n for n, c in d.cells.items() if c.redraws != redraws[n]}
    assert {"a03", "a04", "a05", "a06", "a08", "a09"} <= changed             # set B's analyzers redrew (a07 is not read)
    assert "_rep_sum" in d.title_label.full_text()
    now = {n: c.redraws for n, c in d.cells.items()}
    d.scale_buttons["raw"].click()
    d.flush_redraw()
    assert {n for n, c in d.cells.items() if c.redraws != now[n]} >= {"a03", "a04"}


def test_a_hidden_dock_does_no_work(env):
    _show(env, A6)
    d = env.dock
    drawn = {n: c.redraws for n, c in d.cells.items()}
    d.hide()
    assert not env.vm.active
    env.selection.set((SPECTRA_B[1],))
    d.flush_redraw()
    QApplication.processEvents()
    assert {n: c.redraws for n, c in d.cells.items()} == drawn and "_rep_sum" not in d.title_label.full_text()
    d.show()
    QApplication.processEvents()
    d.flush_redraw()
    assert "_rep_sum" in d.title_label.full_text()


def test_every_control_and_cell_is_named_and_described(env):
    _show(env, A6)
    d = env.dock
    assert_described(d.widget())          # the dock frame's own float and close buttons are Qt's
    for name, cell in d.cells.items():
        assert cell.accessibleName() == f"Analyzer {cell.internal}, beamline {name}"
        assert cell.accessibleDescription() and cell.toolTip()
    assert d.cell_at("a06").focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert d.cell_at("a41").focusPolicy() == Qt.FocusPolicy.NoFocus
    assert "affects every cell's intensity" in d.scale_buttons["raw"].toolTip().lower() or \
        "every cell's intensity" in d.scale_buttons["raw"].toolTip()
    assert "every cell's y axis" in d.mode_buttons["shared"].toolTip()
    assert "kept separately for normalised and raw" in d.limit_box.toolTip().lower()
    assert d.x_axis.accessibleName() and d.y_axis.accessibleName()
    assert_elided_have_tooltips(d)

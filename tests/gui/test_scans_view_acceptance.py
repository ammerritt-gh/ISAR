"""Whole-window Scan(s) acceptance through the real MainWindow and Qt widgets."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu

import bl43_fixture
from gui.window_frame_checks import (assert_elided_have_tooltips, assert_frame_geometry,
                                    assert_reachable, rect_in)
from isar.analyze.resolution import file_stamp
from isar.session import ingest, unit_data
from isar.workspace_api import Workspace
from isar.workspace_api.spectra import OUTDATED_TEXT
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.scan_list import GUTTER
from scans_view_fixture import (DEEP_OUTDATED, DEEP_TRUSTED, FITTED, KEY_A, KEY_B,
                                NO_MONITOR, NO_ZERO, ONE_MODE, SPECTRA_A, SPECTRA_B, STEM_B)

LAPTOP = (1108, 851)
DESKTOP = (1920, 1040)
PLOT_MIN = (240, 250)
CURVES = Path(__file__).resolve().parents[2] / "resolution_curves" / "SP8 BL43LXU"


@pytest.fixture(autouse=True)
def _windows_font(qapp):
    previous = qapp.font()
    qapp.setFont(QFont("Segoe UI", 9))
    yield
    qapp.setFont(previous)


def _window(qtbot, monkeypatch, ws_root, size=LAPTOP):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    window = MainWindow(Workspace.open(ws_root), session=Session(), watch=False)
    qtbot.addWidget(window)
    window.resize(*size)
    window.show()
    window.activateWindow()
    qtbot.waitExposed(window)
    QApplication.processEvents()
    return window


def _page(window):
    return window._center_host.pane("scan")


def _index(window, node_id):
    panel = window.scan_list
    assert node_id in panel._by_id, (node_id, tuple(panel._by_id))
    return panel._model.indexFromItem(panel._by_id[node_id])


def _expand_set(window, key):
    panel = window.scan_list
    index = _index(window, f"set:{key}")
    if not panel._view.isExpanded(index):
        panel._view.setExpanded(index, True)
        QApplication.processEvents()


def _click_spectrum(window, key, modifiers=Qt.KeyboardModifier.NoModifier):
    panel = window.scan_list
    index = panel.index_for_key(key)
    assert index.isValid(), key
    _expand_set(window, key.rsplit(":", 1)[0])
    panel._view.scrollTo(index)
    QApplication.processEvents()
    row = panel._view.visualRect(index)
    position = QPoint(row.left() + GUTTER + 8, row.center().y())
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton, modifiers, position)


def _click_set(window, key, modifiers=Qt.KeyboardModifier.NoModifier):
    panel = window.scan_list
    index = _index(window, f"set:{key}")
    panel._view.scrollTo(index)
    QApplication.processEvents()
    row = panel._view.visualRect(index)
    position = QPoint(row.left() + GUTTER + 8, row.center().y())
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton, modifiers, position)


def _click_gutter(window, key):
    panel = window.scan_list
    index = _index(window, f"set:{key}")
    if panel._view.isExpanded(index):
        return
    panel._view.scrollTo(index)
    QApplication.processEvents()
    row = panel._view.visualRect(index)
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(row.left() + 4, row.center().y()))
    QApplication.processEvents()
    assert panel._view.isExpanded(index)


def _wait_drawn(qtbot, page, predicate):
    qtbot.waitUntil(lambda: predicate(page.drawn()), timeout=15000)
    return page.drawn()


def _legend(page, key):
    return next(line for line in page.drawn().legend if line.entry == key)


def _fit_cell(inspector, name):
    cell = inspector.findChild(QLabel, name)
    assert cell is not None, name
    return cell


def _fit_texts(inspector):
    return {label.objectName(): label.text()
            for label in inspector._fit_section.findChildren(QLabel)
            if label.objectName().startswith("fit_")}


def _capture(window, tmp_path, name):
    path = tmp_path / name
    assert window.grab().save(str(path)), path
    print(f"Scan(s) screenshot: {path}")


def _write_changed_unit(ws_root, key):
    from isar.session.workspace import Workspace as SessionWorkspace

    detector = key.rsplit(":", 1)[1]
    incoming = ws_root.parent / "incoming"
    card, _session = SessionWorkspace.open(ws_root).load()
    paths = [incoming / f"{STEM_B}_{detector}", incoming / f"{STEM_B}.var"]
    records = ingest.parse_all(ingest.discover(paths, card), card)
    scan_key, _ = key.rsplit(":", 1)
    experiment, scan_number = scan_key.rsplit(":", 1)
    record = next(rec for rec in records if rec.experiment_id == experiment
                  and rec.scan_number == int(scan_number))
    spectrum = record.spectra[detector]
    previous = unit_data.read_unit_data_for_unit(ws_root / "data", experiment,
                                                 int(scan_number), detector)
    assert previous is not None
    spectrum.intensity = np.asarray(previous["counts"], dtype=float) + 37.0
    spectrum.variance = np.asarray(previous["variance"], dtype=float) + 37.0
    record.energy_calibration.raw_abscissa_kind = previous["axis"]["raw_abscissa_kind"]
    record.energy_calibration.status = previous["axis"]["calibration_status"]
    unit_data.write_unit_data(record, detector, ws_root / "data")


def _stamp_tabulated_curves(ws_root, keys):
    path = ws_root / "session.json"
    with path.open("r", encoding="utf-8", newline="") as source:
        session = json.load(source)
    for key in keys:
        detector = key.rsplit(":", 1)[1]
        curve = CURVES / f"sarf_24Nov10_s50_40_40_{detector}"
        session["scans"][key]["detectors"][detector]["calib"]["tabulated"] = {
            "folder": str(curve.parent), "file": curve.name, "stamp": file_stamp(curve),
            "detector": detector, "setting": "s50_40_40",
        }
    with path.open("w", encoding="utf-8", newline="") as target:
        json.dump(session, target, indent=2)
        target.write("\n")


def _time_action(qtbot, page, action, condition):
    started = time.perf_counter()
    action()
    QApplication.processEvents()
    qtbot.waitUntil(lambda: condition(page.drawn()), timeout=15000)
    page._glw.viewport().repaint()
    return time.perf_counter() - started


def test_first_use_walkthrough_uses_real_list_clicks_and_keyboard(qtbot, monkeypatch,
                                                                   scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    page = _page(window)
    _click_spectrum(window, ONE_MODE)
    drawn = _wait_drawn(qtbot, page, lambda state: state.keys == (ONE_MODE,))
    assert window.selection.keys == (ONE_MODE,)
    assert drawn.curves and drawn.scale == "normalised"    # a fresh page opens with Fit on Fast
    assert drawn.ylabel == "Counts per 10⁵ monitor"

    qtbot.mouseClick(page.fit_buttons["deep"], Qt.MouseButton.LeftButton)
    drawn = _wait_drawn(qtbot, page, lambda state: state.fit == "deep")
    assert any(curve.key == ONE_MODE and curve.style == "dashed" for curve in drawn.curves)
    assert "Deep (untrusted)" in drawn.curves_header

    for key in (SPECTRA_A[1], SPECTRA_A[2], SPECTRA_A[4]):
        _click_spectrum(window, key, Qt.KeyboardModifier.ControlModifier)
    drawn = _wait_drawn(qtbot, page, lambda state: len(state.keys) == 4)
    selected = (SPECTRA_A[0], SPECTRA_A[1], SPECTRA_A[2], SPECTRA_A[4])
    assert window.selection.keys == selected
    assert drawn.layout == "offset" and len(page._drawn_state.traces) == 4

    page._glw.setFocus()
    QTest.keyClick(page._glw, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert page.store.slot_group(1).members == list(selected)
    assert page.groups.chip(1).text() == "1"

    qtbot.mouseClick(window.scan_list._inspect, Qt.MouseButton.LeftButton)
    inspector = window.scan_inspector
    assert not inspector.isHidden()
    assert "a07" in inspector._title.full_text()
    assert _fit_cell(inspector, "fit_deep_m1_energy").text() != "—"


def test_expert_group_recall_and_header_switches_need_no_menu(qtbot, monkeypatch,
                                                               scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    page = _page(window)
    page.store.save_slot(2, SPECTRA_B[:3])
    page.groups.reload()
    calls = []
    original_exec, original_popup = QMenu.exec, QMenu.popup

    def exec_spy(menu, *args, **kwargs):
        calls.append("exec")
        return original_exec(menu, *args, **kwargs)

    def popup_spy(menu, *args, **kwargs):
        calls.append("popup")
        return original_popup(menu, *args, **kwargs)

    monkeypatch.setattr(QMenu, "exec", exec_spy)
    monkeypatch.setattr(QMenu, "popup", popup_spy)
    page._glw.setFocus()
    QApplication.processEvents()
    QTest.keyClick(page._glw, Qt.Key.Key_2)
    drawn = _wait_drawn(qtbot, page, lambda state: state.keys == SPECTRA_B[:3])
    assert window.selection.keys == SPECTRA_B[:3]
    assert page.groups.chip(2).text() == "2"

    qtbot.mouseClick(page.fit_buttons["fast"], Qt.MouseButton.LeftButton)
    _wait_drawn(qtbot, page, lambda state: state.fit == "fast")
    qtbot.mouseClick(page.layout_buttons["overlay"], Qt.MouseButton.LeftButton)
    drawn = _wait_drawn(qtbot, page, lambda state: state.layout == "overlay")
    assert not drawn.curves

    window.selection.clear()
    _click_set(window, KEY_A)
    qtbot.waitUntil(lambda: window.selection.keys == SPECTRA_A, timeout=5000)
    drawn = _wait_drawn(qtbot, page, lambda state: len(state.keys) == len(SPECTRA_A))
    assert window.selection.keys == SPECTRA_A
    assert NO_ZERO in drawn.keys and not drawn.not_drawn    # its beamline zero: drawn before any fast pass
    assert calls == []


def test_recalling_five_then_stepping_changes_only_the_active_display(qtbot, monkeypatch,
                                                                      scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws, DESKTOP)
    page = _page(window)
    keys = SPECTRA_A[:5]
    page.store.save_slot(3, keys)
    page.groups.reload()
    qtbot.mouseClick(page.groups.chip(3), Qt.MouseButton.LeftButton)
    drawn = _wait_drawn(qtbot, page, lambda state: state.keys == keys)
    qtbot.mouseClick(window.scan_list._inspect, Qt.MouseButton.LeftButton)
    inspector = window.scan_inspector
    qtbot.waitUntil(lambda: inspector._position.text() == "1 of 5 selected", timeout=15000)

    before_keys = window.selection.keys
    before_drawn = page.drawn()
    before_bold = next(line.entry for line in before_drawn.legend if line.bold)
    qtbot.mouseClick(inspector._next, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(inspector._next, Qt.MouseButton.LeftButton)
    assert window.selection.keys == before_keys
    assert page.drawn().keys == before_drawn.keys
    assert inspector.current_bundle.key == keys[2]
    assert inspector._position.text() == "3 of 5 selected"
    assert inspector._title.full_text() != ""
    qtbot.waitUntil(lambda: any(line.entry == keys[2] and line.bold
                                for line in page.drawn().legend), timeout=5000)
    after_bold = next(line.entry for line in page.drawn().legend if line.bold)
    assert before_bold == keys[0] and after_bold == keys[2]
    assert [(line.entry, line.text) for line in page.drawn().legend] == [
        (line.entry, line.text) for line in before_drawn.legend]
    before_fit = _fit_texts(inspector)

    for button, state in ((page.fit_buttons["fast"], "fast"),
                          (page.fit_buttons["deep"], "deep"),
                          (page.fit_buttons["none"], "none")):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        _wait_drawn(qtbot, page, lambda drawn, expected=state: drawn.fit == expected)
        assert _fit_texts(inspector) == before_fit
    for button, state in ((page.scale_buttons["raw"], "raw"),
                          (page.scale_buttons["normalised"], "normalised")):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        _wait_drawn(qtbot, page, lambda drawn, expected=state: drawn.scale == expected)
        assert _fit_texts(inspector) == before_fit


def test_mouse_edit_replaces_group_three_when_all_slots_are_used(qtbot, monkeypatch,
                                                                 scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws, DESKTOP)
    page = _page(window)
    for slot in range(1, 11):
        page.store.save_slot(slot, SPECTRA_A[:5])
    page.groups.reload()
    qtbot.mouseClick(page.groups.chip(3), Qt.MouseButton.LeftButton)
    _wait_drawn(qtbot, page, lambda state: state.keys == SPECTRA_A[:5])

    _click_spectrum(window, SPECTRA_B[0], Qt.KeyboardModifier.ControlModifier)
    edited = window.selection.keys
    chip = page.groups.chip(3)
    save = page.groups.save_button(3)
    assert "edited" in chip.accessibleName() and save is not None and save.isVisible()
    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert page.store.slot_group(3).members == list(edited)
    assert page.store.free_slot() is None
    assert page.groups._notice.full_text() == "Group 3 replaced"
    assert page.groups._clear_button.isEnabled()


def test_live_counts_mark_old_fits_and_keep_selection_zoom_and_list_state(qtbot, monkeypatch,
                                                                           scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws, (1108, 620))
    page = _page(window)
    _click_gutter(window, KEY_A)
    _click_gutter(window, KEY_B)
    window.selection.set((DEEP_TRUSTED, ONE_MODE), clicked=DEEP_TRUSTED)
    _wait_drawn(qtbot, page, lambda state: len(state.keys) == 2)
    qtbot.mouseClick(page.fit_buttons["fast"], Qt.MouseButton.LeftButton)
    _wait_drawn(qtbot, page, lambda state: state.fit == "fast" and len(state.keys) == 2)
    qtbot.mouseClick(window.scan_list._inspect, Qt.MouseButton.LeftButton)

    page._main_plot.setRange(xRange=(1.0, 5.0), yRange=(0.0, 800.0), padding=0)
    old_range = page._main_plot.getViewBox().viewRange()
    old_keys = window.selection.keys
    old_index = page.drawn().keys.index(DEEP_TRUSTED)
    old_y = page._drawn_state.traces[old_index].y.copy()
    scrollbar = window.scan_list._view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    QApplication.processEvents()
    old_scroll = scrollbar.value()
    assert old_scroll > 0
    expanded = {key: window.scan_list._view.isExpanded(_index(window, f"set:{key}"))
                for key in (KEY_A, KEY_B)}
    assert all(expanded.values())

    _write_changed_unit(scans_view_ws, DEEP_TRUSTED)
    from bl43_intake_fixture import write_run
    write_run(scans_view_ws, FITTED, stale={DEEP_TRUSTED: {"data": "earlier revision"}})
    window._ws.refresh()
    window._on_workspace_changed({"snapshot"})
    drawn = _wait_drawn(qtbot, page, lambda state: state.fit == "fast"
                        and any(line.entry == DEEP_TRUSTED
                                and OUTDATED_TEXT not in line.text for line in state.legend)
                        and np.all(page._drawn_state.traces[state.keys.index(DEEP_TRUSTED)].y > old_y))
    assert window.selection.keys == old_keys
    new_y = page._drawn_state.traces[drawn.keys.index(DEEP_TRUSTED)].y
    assert np.all(new_y > old_y), "the redrawn trace did not include the increased counts"
    new_range = page._main_plot.getViewBox().viewRange()
    for before_axis, after_axis in zip(old_range, new_range):
        for before, after in zip(before_axis, after_axis):
            assert math.isclose(before, after, rel_tol=1e-9, abs_tol=0.0)
    assert {key: window.scan_list._view.isExpanded(_index(window, f"set:{key}"))
            for key in (KEY_A, KEY_B)} == expanded
    assert scrollbar.value() == old_scroll
    assert OUTDATED_TEXT in _fit_cell(window.scan_inspector, "fit_fast_inputs").text()

    qtbot.mouseClick(page.fit_buttons["deep"], Qt.MouseButton.LeftButton)
    window.selection.set((DEEP_OUTDATED, DEEP_TRUSTED), clicked=DEEP_OUTDATED)
    drawn = _wait_drawn(qtbot, page, lambda state: state.fit == "deep"
                        and len(state.keys) + len(state.not_drawn) == 2)
    assert set(drawn.keys) == {DEEP_OUTDATED, DEEP_TRUSTED}
    assert drawn.curves
    assert OUTDATED_TEXT not in _legend(page, DEEP_OUTDATED).text
    assert "currentness not recorded" in _legend(page, DEEP_TRUSTED).text
    assert OUTDATED_TEXT in _fit_cell(window.scan_inspector, "fit_deep_inputs").text()


def test_saved_fast_result_redraws_its_scan_on_the_fitted_zero(qtbot, monkeypatch, scans_view_ws):
    """A scan drawn on the file axis before its fast pass moves to its fitted zero once that result is saved."""
    import numpy as np
    from bl43_fixture import _load, _save
    from scans_view_fixture import _outcome

    window = _window(qtbot, monkeypatch, scans_view_ws)
    page = _page(window)
    window.selection.set((NO_ZERO,), clicked=NO_ZERO)
    _wait_drawn(qtbot, page, lambda state: state.keys == (NO_ZERO,))
    before = page._drawn_state.traces
    assert [t.identity.endswith("(beamline zero)") for t in before] == [True]

    ws, card, session = _load(scans_view_ws)
    outcome = _outcome(NO_ZERO)
    outcome["calib"]["zero_source"] = "elastic_fit"
    entry = session.scans[NO_ZERO]
    entry.detectors = {NO_ZERO.rsplit(":", 1)[1]: outcome}
    entry.status, entry.reasons = "fitted", []
    _save(ws, card, session)
    window._on_results_saved((NO_ZERO,))                  # what the queue's writer signals after a save

    _wait_drawn(qtbot, page, lambda state: state.keys == (NO_ZERO,)
                and not any(t.identity.endswith("(beamline zero)") for t in page._drawn_state.traces))
    zero = window.scan_list_vm.snapshot.spectrum(NO_ZERO).fit.energy_zero
    np.testing.assert_allclose(page._drawn_state.traces[0].x, before[0].x - zero)


@pytest.mark.parametrize("size", [LAPTOP, DESKTOP], ids=["laptop", "desktop"])
def test_composed_window_geometry_names_and_elision(qtbot, monkeypatch, scans_view_ws,
                                                    tmp_path, size):
    window = _window(qtbot, monkeypatch, scans_view_ws, size)
    page = _page(window)
    selected = (*SPECTRA_A[:6], NO_MONITOR, *SPECTRA_B[:3])     # one scan with no monitor: a not-drawn line to lay out
    window.selection.set(selected, clicked=selected[0])
    _wait_drawn(qtbot, page, lambda state: len(state.keys) + len(state.not_drawn) == 10)
    qtbot.mouseClick(page.fit_buttons["deep"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(page.layout_buttons["offset"], Qt.MouseButton.LeftButton)
    window.set_right_panel_collapsed(True)
    qtbot.mouseClick(window.scan_list._inspect, Qt.MouseButton.LeftButton)
    drawn = _wait_drawn(qtbot, page, lambda state: state.fit == "deep"
                        and state.keys and len(state.not_drawn) == 1)
    inspector = window.scan_inspector
    assert not inspector.isHidden() and window._right_panel_collapsed()
    assert drawn.keys and NO_MONITOR not in drawn.keys
    assert "1 of 10 selected not drawn" in drawn.not_drawn_line
    assert "Deep (untrusted" in drawn.curves_header
    assert len({line.entry for line in drawn.legend if line.entry in drawn.keys}) == len(drawn.keys)
    assert page.plot_area.width() >= PLOT_MIN[0] and page.plot_area.height() >= PLOT_MIN[1]

    rendered = page._main_plot._isar_rendered
    assert rendered.legend is not None
    assert page._main_plot.sceneBoundingRect().contains(rendered.legend.sceneBoundingRect())
    end_labels = [item for item in rendered._data_items if isinstance(item, pg.TextItem)]
    assert end_labels
    view_rect = page._main_plot.getViewBox().sceneBoundingRect()
    assert all(view_rect.contains(label.sceneBoundingRect()) for label in end_labels)
    for key in drawn.keys:
        assert key in {line.entry for line in drawn.legend}
    assert inspector._title.full_text()

    controls = [*page.fit_buttons.values(), *page.layout_buttons.values(),
                *page.scale_buttons.values(), page.offset_box, page.ratio_box,
                 page.ratio_minus, page.ratio_plus, page.auto_button,
                window.scan_list._inspect]
    for control in controls:
        assert_reachable(window, control)
        name = control.accessibleName()
        if control is window.scan_list._inspect:
            name = name or control.text()
            assert control.toolTip()
        assert name, control.objectName()
    assert_reachable(window, page.not_drawn)
    for control in (inspector._title, inspector._position, inspector._previous,
                    inspector._next, inspector._close):
        assert_reachable(window, control, enabled=control.isEnabled())
    assert inspector._previous.accessibleName() and inspector._next.accessibleName()

    for button in page.fit_buttons.values():
        assert "every Scan(s) plot" in button.toolTip()
    for button in page.scale_buttons.values():
        assert "every Scan(s) plot" in button.toolTip()
    assert page.layout_buttons['offset'].toolTip().startswith('Waterfall: each curve offset')
    assert page.layout_buttons['overlay'].toolTip().startswith('Curves plotted with no offset, no fits.')
    set_a = window.scan_list._by_id[f"set:{KEY_A}"]
    set_b = window.scan_list._by_id[f"set:{KEY_B}"]
    assert "opens or closes" in set_a.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    assert "selects" in set_a.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    partial_text = set_b.data(Qt.ItemDataRole.AccessibleTextRole)
    assert partial_text and "of" in partial_text

    if size == DESKTOP:
        assert page.groups.chip(10).isVisible()
        page.store.save_slot(1, selected)
        page.groups.reload()
        chip = page.groups.chip(1)
        assert chip is not None
        assert_reachable(window, chip)
        assert chip.accessibleName() and "scan-list selection" in chip.toolTip()
        _capture(window, tmp_path, "scan-view-desktop-10-spectra.png")
    else:
        page.store.save_slot(1, selected)
        page.groups.reload()
        QApplication.processEvents()
        _capture(window, tmp_path, "scan-view-laptop-10-spectra.png")
        for slot in range(2, 11):
            page.store.save_slot(slot, selected)
        page.groups.reload()
        QApplication.processEvents()
        for slot in range(1, 11):
            chip = page.groups.chip(slot)
            assert chip is not None
            assert_reachable(window, chip)
            assert chip.accessibleName()
            assert "scan-list selection" in chip.toolTip()
        _capture(window, tmp_path, "scan-view-laptop-10-spectra-9-groups.png")

    for section in inspector._fit_tables._sections.values():
        assert section.header.accessibleName()
    for name in ("fit_deep_modes", "fit_deep_state", "fit_deep_m1_energy"):
        cell = _fit_cell(inspector, name)
        assert_reachable(window, cell, inspector._scroll)
    assert_elided_have_tooltips(page)
    for label in inspector.findChildren(ElidedLabel):
        if label.isVisible() and label.text() != label.full_text():
            assert label.toolTip(), label.objectName() or label.full_text()
    assert_frame_geometry(window, size)


@pytest.mark.parametrize("resolution", ["voigt", "tabulated"])
def test_redraw_timing_report_for_ten_spectra(qtbot, monkeypatch, scans_view_ws, resolution):
    keys = FITTED[:10]
    if resolution == "tabulated":
        _stamp_tabulated_curves(scans_view_ws, FITTED[:11])
    window = _window(qtbot, monkeypatch, scans_view_ws, LAPTOP)
    page = _page(window)
    window.selection.set(keys, clicked=keys[0])
    _wait_drawn(qtbot, page, lambda state: len(state.keys) == 10)
    qtbot.mouseClick(page.fit_buttons["deep"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(page.layout_buttons["offset"], Qt.MouseButton.LeftButton)
    initial = _wait_drawn(qtbot, page, lambda state: state.fit == "deep"
                          and state.layout == "offset" and len(state.keys) == 10)
    assert set(initial.keys) == set(keys)

    changed = (*keys[1:], FITTED[10])
    selection_time = _time_action(
        qtbot, page, lambda: window.selection.set(changed, clicked=changed[0]),
        lambda state: set(state.keys) == set(changed))
    pass_time = _time_action(
        qtbot, page, lambda: QTest.mouseClick(page.fit_buttons["fast"], Qt.MouseButton.LeftButton),
        lambda state: state.fit == "fast" and set(state.keys) == set(changed))
    assert page.drawn().curves
    layout_time = _time_action(
        qtbot, page, lambda: QTest.mouseClick(page.layout_buttons["overlay"], Qt.MouseButton.LeftButton),
        lambda state: state.layout == "overlay" and set(state.keys) == set(changed))
    assert not page.drawn().curves
    print(f"redraw {resolution}: selection {selection_time:.3f} s, "
          f"pass {pass_time:.3f} s, layout {layout_time:.3f} s")

"""Scan-list selection gestures use real widgets and one authoritative selection."""
from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B, KEY_SINGLE
from isar_gui.selection import ORIGIN_GROUP, ORIGIN_LIST
from isar_gui.views.scan_list import GUTTER, _KEY_ROLE
from gui.test_scan_list_panel import A, B, _index, _make


@pytest.fixture
def panel_vm(qtbot, monkeypatch, bl43_ws_readonly):
    snap = bl43_fixture.sample_snapshot(bl43_ws_readonly)
    panel, vm, box = _make(qtbot, monkeypatch, snap, size=(600, 700))
    return panel, vm, box


def _mouse(panel, ident, modifiers=Qt.KeyboardModifier.NoModifier, *, gutter=False):
    index = _index(panel, ident)
    panel._view.scrollTo(index)
    QApplication.processEvents()
    rect = panel._view.visualRect(index)
    point = QPoint(rect.left() + 4 if gutter else rect.left() + GUTTER + 10, rect.center().y())
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton, modifiers, point)


def _open(panel, *keys):
    for key in keys:
        panel._view.expand(_index(panel, "set:" + key))
    QApplication.processEvents()


def _selected_rows(panel):
    return {index.data(_KEY_ROLE) for index in panel._view.selectionModel().selectedRows()
            if index.data(_KEY_ROLE)}


def test_mouse_selection_ranges_and_whole_set_toggle(panel_vm):
    panel, vm, _ = panel_vm
    _open(panel, KEY_A, KEY_B)
    ctrl, shift = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier
    _mouse(panel, f"spec:{B[4]}")
    assert vm.selection.keys == (B[4],)
    _mouse(panel, f"spec:{A[4]}", ctrl)
    assert vm.selection.keys == (B[4], A[4]) and vm.selection.active == A[4]
    _mouse(panel, f"spec:{B[4]}", ctrl)
    assert vm.selection.keys == (A[4],) and vm.selection.active == A[4]
    _mouse(panel, f"spec:{A[4]}")
    _mouse(panel, f"spec:{B[5]}", shift)
    assert vm.selection.keys == (B[5], B[6], A[3], A[4])
    assert vm.selection.active == B[5]
    _mouse(panel, f"spec:{B[5]}")
    _mouse(panel, f"set:{KEY_A}", gutter=True)
    _mouse(panel, f"set:{KEY_A}", shift)
    assert vm.selection.keys == (B[5], B[6])
    assert not panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
    _mouse(panel, f"set:{KEY_A}")
    assert vm.selection.keys == tuple(A.values()) and vm.selection.active == A[3]
    _mouse(panel, f"set:{KEY_B}", ctrl)
    assert vm.selection.keys == (*B.values(), *A.values())
    _mouse(panel, f"set:{KEY_A}", ctrl)
    assert vm.selection.keys == tuple(B.values())
    vm.select(A[4])
    _mouse(panel, f"set:{KEY_A}", ctrl)
    assert vm.selection.keys == tuple(A.values()) and vm.selection.active == A[3]
    assert _selected_rows(panel) == set(A.values())


def test_visible_range_ctrl_range_hidden_anchor_and_select_all(panel_vm):
    panel, vm, _ = panel_vm
    _open(panel, KEY_A, KEY_B)
    ctrl, shift = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier
    _mouse(panel, f"spec:{A[4]}")
    _mouse(panel, f"spec:{B[3]}", ctrl)
    _mouse(panel, f"spec:{B[3]}", ctrl)  # anchor remains B3, membership only A4
    _mouse(panel, f"spec:{B[4]}", ctrl | shift)
    assert vm.selection.keys == (B[3], B[4], A[4]) and vm.selection.active == B[4]
    _mouse(panel, f"spec:{B[5]}")
    _mouse(panel, f"set:{KEY_B}", gutter=True)
    single = vm.snapshot.scan_set(KEY_SINGLE).spectra[0].key
    _mouse(panel, f"spec:{single}", shift)
    assert vm.selection.keys == (single,)
    _open(panel, KEY_B)
    panel._view.collapse(_index(panel, f"set:{KEY_A}"))
    panel._view.setFocus()
    QTest.keyClick(panel._view, Qt.Key.Key_A, ctrl)
    assert vm.selection.keys == (single, *B.values())


def test_gutter_file_and_blank_clicks_and_hover(panel_vm):
    panel, vm, _ = panel_vm
    vm.select(A[4])
    changes = []
    vm.selection.changed.connect(changes.append)
    for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                      Qt.KeyboardModifier.ShiftModifier):
        old = panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
        _mouse(panel, f"set:{KEY_A}", modifiers, gutter=True)
        assert panel._view.isExpanded(_index(panel, f"set:{KEY_A}")) is not old
        assert vm.selection.keys == (A[4],)
    _mouse(panel, "section:not_read")
    child = panel._by_id["section:not_read"].child(0, 0)
    _mouse(panel, child.data(Qt.ItemDataRole.UserRole))
    assert changes == []
    index = _index(panel, f"set:{KEY_A}")
    rect = panel._view.visualRect(index)
    QTest.mouseMove(panel._view.viewport(), QPoint(rect.left() + 4, rect.center().y()))
    assert panel._view.hover == (f"set:{KEY_A}", "open")
    QTest.mouseMove(panel._view.viewport(), QPoint(rect.left() + GUTTER + 6, rect.center().y()))
    assert panel._view.hover == (f"set:{KEY_A}", "select")
    blank = QPoint(10, panel._view.viewport().height() - 3)
    assert not panel._view.indexAt(blank).isValid()
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert vm.selection.keys == () and vm.selection.active is None


def test_keyboard_focus_ranges_and_container_space(panel_vm):
    panel, vm, _ = panel_vm
    _open(panel, KEY_A, KEY_B)
    ctrl, shift = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier
    _mouse(panel, f"spec:{B[3]}")
    QTest.keyClick(panel._view, Qt.Key.Key_Down)
    assert vm.selection.keys == (B[4],)
    QTest.keyClick(panel._view, Qt.Key.Key_Down, shift)
    assert vm.selection.keys == (B[4], B[5])
    QTest.keyClick(panel._view, Qt.Key.Key_Down, ctrl)
    assert vm.selection.keys == (B[4], B[5])
    QTest.keyClick(panel._view, Qt.Key.Key_Space, ctrl)
    assert vm.selection.keys == (B[4], B[5], B[6])
    _mouse(panel, f"spec:{B[6]}")
    QTest.keyClick(panel._view, Qt.Key.Key_Down)
    assert vm.selection.keys == (B[6],)
    QTest.keyClick(panel._view, Qt.Key.Key_Space, ctrl)
    assert vm.selection.keys == (B[6], *A.values()) and vm.selection.active == B[6]
    QTest.keyClick(panel._view, Qt.Key.Key_Space)
    assert vm.selection.keys == tuple(A.values())


def test_group_recall_marks_reveal_and_refresh_continuity(panel_vm):
    panel, vm, box = panel_vm
    vm.selection.set((A[6], B[3]), origin=ORIGIN_GROUP)
    assert vm.selection.keys == (B[3], A[6])
    for key in (KEY_A, KEY_B):
        assert panel._view.isExpanded(_index(panel, f"set:{key}"))
        assert f"set:{key}" in panel._expanded
    assert panel._view.viewport().rect().intersects(panel._view.visualRect(panel.index_for_key(B[3])))
    assert "1 of 4 selected" in panel._by_id[f"set:{KEY_A}"].text()
    assert "1 of 4 selected" in panel._by_id[f"set:{KEY_A}"].data(Qt.ItemDataRole.AccessibleTextRole)
    panel._view.collapse(_index(panel, f"set:{KEY_A}"))
    assert "1 of 4 selected" in panel._by_id[f"set:{KEY_A}"].text()
    changes = []
    vm.selection.changed.connect(changes.append)
    vm.on_workspace_changed()
    assert changes == [] and vm.selection.keys == (B[3], A[6])
    assert not panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
    vm.selection.set(vm.selection.keys, origin=ORIGIN_GROUP)
    assert panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
    b = box["snap"].scan_set(KEY_B)
    reduced = dataclasses.replace(b, spectra=tuple(sp for sp in b.spectra if sp.key != B[3]))
    box["snap"] = dataclasses.replace(box["snap"], sets=tuple(
        reduced if ss.key == KEY_B else ss for ss in box["snap"].sets))
    changes.clear()
    vm.on_workspace_changed()
    assert vm.selection.keys == (A[6],) and len(changes) == 1


def test_order_ignores_filter_and_analyzer_containers_select_all(panel_vm):
    panel, vm, _ = panel_vm
    vm.selection.set((A[4], B[3], A[3], "no:such:key"))
    assert vm.selection.keys == (B[3], A[3], A[4])
    vm.set_sort("analyzer")
    assert vm.selection.keys == (B[3], A[3], A[4])
    vm.set_filter("a05")
    vm.click(f"set:{KEY_A}")
    assert vm.selection.keys == tuple(A.values())
    assert vm.outside_filter() == "3 selected spectra are outside the filter"
    vm.set_filter("")
    vm.select(A[4])
    assert panel._by_id["ana:a04"].text() == "a04 (1 of 2 selected)"
    vm.click("ana:a04", ctrl=True)
    assert vm.selection.keys == (B[4], A[4])


def test_double_click_inspects_once_and_tree_does_not_eat_digit(panel_vm):
    panel, vm, _ = panel_vm
    _open(panel, KEY_A)
    vm.click(f"set:{KEY_A}")
    inspected = []
    vm.inspect_requested.connect(inspected.append)
    _mouse(panel, f"spec:{A[6]}")
    rect = panel._view.visualRect(panel.index_for_key(A[6]))
    QTest.mouseDClick(panel._view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rect.left() + GUTTER + 4, rect.center().y()))
    assert vm.selection.keys == (A[6],) and inspected == [A[6]]
    recalled = []
    shortcut = QShortcut(QKeySequence("3"), panel)
    shortcut.activated.connect(lambda: recalled.append(True))
    panel.activateWindow()
    panel._view.setFocus()
    QApplication.processEvents()
    QTest.keyClick(panel._view, Qt.Key.Key_3)
    assert recalled == [True]

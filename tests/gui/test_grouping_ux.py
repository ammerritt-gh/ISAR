"""Grouping-UX tests (Pass 2b, Phase 5).

Covers the VM's name-suggestion precedence, create-from-selection, and auto-group freeze, plus the
dock's menu-driven "New group from selection…" (dialog mocked — a live modal hangs the offscreen
suite), the group-header plain-click member select / ctrl-extend (only when the press didn't become a
drag), the auto-header "Make this a manual group", and the always-present Manual-tab hint.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QInputDialog

from isar_gui.gui_store import GuiStore
from isar_gui.mime import MIME_SCAN_IDS, decode_scan_ids
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock, _GroupBox


def _sv(exp, num, det, *, T=300.0, hkl=(1.0, 0.0, 0.0), q_target=None, B=None):
    prov = {}
    if q_target is not None:
        prov["q_target"] = list(q_target)
    if B is not None:
        prov["B"] = B
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type="measurement",
        status="ingested", source_file=r"D:\d\scan.dat", T=T,
        hkl=list(hkl) if hkl is not None else None, provenance=prov,
        detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def _nine(exp="s30", num=12, T=300.0):
    return [_sv(exp, num, f"ana{i}", T=T, hkl=(1.5 + 0.01 * i, 0, 0), q_target=(1.5, 0, 0))
            for i in range(1, 10)]


def _box(dock, box_id):
    return next(b for b in dock._boxes if b.box_id == box_id)


def _tab_names(dock):
    # Address tabs by stable page id (the display label "Manual" now reads "Home").
    return list(dock._pages)


def _show_tab(dock, name):
    dock._tabs.setCurrentIndex(_tab_names(dock).index(name))


# ---------------------------------------------------------------------------
# VM: suggest_group_name precedence
# ---------------------------------------------------------------------------
def test_suggest_name_shared_temperature():
    vm = ScansViewModel(_ws(_nine(T=300.0)))
    assert vm.suggest_group_name(["s30:12:ana1", "s30:12:ana2"]) == "T = 300 K"


def test_suggest_name_shared_q_when_temperature_differs():
    # Same q, different T -> T-bin is not shared, q-bin is.
    vm = ScansViewModel(_ws([_sv("s30", 1, "a", T=300.0, hkl=(1.5, 0, 0)),
                             _sv("s30", 2, "b", T=310.0, hkl=(1.5, 0, 0))]))
    name = vm.suggest_group_name(["s30:1:a", "s30:2:b"])
    assert name.startswith("q = ") and "1.5" in name


def test_suggest_name_shared_analyzer():
    # Different scan / T / q, same analyzer id.
    vm = ScansViewModel(_ws([_sv("in8", 1, "det", T=300.0, hkl=(1, 0, 0)),
                             _sv("in8", 2, "det", T=310.0, hkl=(2, 0, 0))]))
    assert vm.suggest_group_name(["in8:1:det", "in8:2:det"]) == "analyzer det"


def test_suggest_name_shared_scan_when_dims_differ():
    # Nine analyzers of one scan, T stripped so only the source scan is shared.
    nine = [_sv("s30", 12, f"ana{i}", T=None, hkl=(1.5 + 0.1 * i, 0, 0), q_target=(1.5, 0, 0))
            for i in range(1, 10)]
    vm = ScansViewModel(_ws(nine))
    name = vm.suggest_group_name([u.scan_id for u in nine])
    assert name.startswith("scan 12")


def test_suggest_name_fallback_group_of_n():
    vm = ScansViewModel(_ws([_sv("in8", 1, "a", T=300.0, hkl=(1, 0, 0)),
                             _sv("in8", 2, "b", T=310.0, hkl=(2, 0, 0))]))
    assert vm.suggest_group_name(["in8:1:a", "in8:2:b"]) == "Group of 2"


def test_suggest_name_unknown_keys():
    vm = ScansViewModel(_ws(_nine()))
    assert vm.suggest_group_name(["nope:9:x"]) == "Group of 1"


# ---------------------------------------------------------------------------
# VM: create_group_from + freeze_auto_group
# ---------------------------------------------------------------------------
def test_create_group_from_populates_members(tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    changed = []
    vm.groups_changed.connect(lambda: changed.append(1))
    gid = vm.create_group_from("Cold", ["s30:12:ana1", "s30:12:ana2"])
    assert gid is not None
    g = store.groups()[0]
    assert g.name == "Cold" and g.members == ["s30:12:ana1", "s30:12:ana2"]
    assert changed  # structural signal fired -> dock rebuilds


def test_create_group_from_without_store_is_noop():
    vm = ScansViewModel(_ws(_nine()))  # no store (empty/pending)
    assert vm.create_group_from("X", ["s30:12:ana1"]) is None


def test_freeze_auto_group_copies_members_and_label(tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    before = len(vm.auto_groups())
    gid = vm.freeze_auto_group("auto:scan:s30:12")
    assert gid is not None
    g = store.groups()[0]
    assert g.members == [f"s30:12:ana{i}" for i in range(1, 10)]  # full membership copied
    assert g.name.startswith("scan 12")                          # auto label copied
    # The auto-group stays derived (freeze is a fork, not a move).
    assert len(vm.auto_groups()) == before
    assert any(a.id == "auto:scan:s30:12" for a in vm.auto_groups())


def test_freeze_unknown_auto_id_is_noop(tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    assert vm.freeze_auto_group("auto:scan:does:not:exist") is None
    assert store.groups() == []


# ---------------------------------------------------------------------------
# Dock: "New group from selection…" physical-child action (dialog mocked)
#
# QMenu.exec is a real modal that ignores Python-level patching (it hangs the offscreen suite), so the
# context-menu tests drive the action-dispatch seam (``_apply_unit_action`` / ``_apply_header_action``)
# that the menu triggers, exactly as clicking that entry would — only the QInputDialog is mocked.
# ---------------------------------------------------------------------------
def test_chip_menu_new_group_from_selection(qtbot, tmp_path, monkeypatch):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    vm.select_keys(["s30:12:ana1", "s30:12:ana2"], clear=True)
    seen = {}

    def _fake_get_text(parent, title, label, *a, **k):
        seen["text"] = k.get("text")
        return "My cold set", True

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(_fake_get_text))
    dock._pool_tree._apply_unit_action("new_group", "s30:12:ana1")
    assert seen["text"] == "T = 300 K"                 # dialog pre-filled from the suggestion
    g = store.groups()[0]
    assert g.name == "My cold set"
    assert g.members == ["s30:12:ana1", "s30:12:ana2"]  # the whole selection, not just the chip


def test_chip_menu_new_group_cancelled_creates_nothing(qtbot, tmp_path, monkeypatch):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("", False)))   # user hit Cancel
    dock._pool_tree._apply_unit_action("new_group", "s30:12:ana1")
    assert store.groups() == []


def test_chip_new_group_from_single_chip_when_unselected(qtbot, tmp_path, monkeypatch):
    # Right-clicking a chip that is NOT part of the current selection groups just that chip.
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Solo", True)))
    dock._pool_tree._apply_unit_action("new_group", "s30:12:ana5")
    assert store.groups()[0].members == ["s30:12:ana5"]


# ---------------------------------------------------------------------------
# Dock: auto-header freeze via context menu
# ---------------------------------------------------------------------------
def test_auto_header_make_manual_group(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    _box(dock, "auto:scan:s30:12")._apply_header_action("freeze")
    g = store.groups()[0]
    assert g.members == [f"s30:12:ana{i}" for i in range(1, 10)]
    assert g.name.startswith("scan 12")


def test_manual_header_delete_action(qtbot, tmp_path, monkeypatch):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    gid = vm.create_group_from("Cold", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    _box(dock, gid)._apply_header_action("delete")
    assert store.groups() == []


# ---------------------------------------------------------------------------
# Dock: group-header plain-click selects members / ctrl extends
# ---------------------------------------------------------------------------
def _press(box):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    box.mousePressEvent(ev)


def _release(box, mods=Qt.KeyboardModifier.NoModifier):
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(5, 5), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.NoButton, mods)
    box.mouseReleaseEvent(ev)


def test_header_click_selects_all_members(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    box = _box(dock, "auto:scan:s30:12")
    _press(box)
    _release(box)
    assert set(vm.selected_keys()) == {f"s30:12:ana{i}" for i in range(1, 10)}


def test_header_ctrl_click_extends_selection(qtbot, tmp_path):
    # Two source scans; header-click one, ctrl-header-click the other -> union selected.
    views = _nine("s30", 12) + [_sv("s30", 13, "ana1", hkl=(2.0, 0, 0), q_target=(2, 0, 0))]
    vm = ScansViewModel(_ws(views), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    box12 = _box(dock, "auto:scan:s30:12")
    box13 = _box(dock, "auto:scan:s30:13")
    _press(box12)
    _release(box12)
    _press(box13)
    _release(box13, mods=Qt.KeyboardModifier.ControlModifier)
    selected = set(vm.selected_keys())
    assert "s30:13:ana1" in selected
    assert {f"s30:12:ana{i}" for i in range(1, 10)} <= selected   # first box's members kept


def test_header_press_that_became_a_drag_does_not_select(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    box = _box(dock, "auto:scan:s30:12")
    _press(box)
    box._drag_start = None            # mouseMoveEvent clears this once a real drag begins
    _release(box)
    assert vm.selected_keys() == []   # a completed drag must not also select the members


def test_filtered_manual_header_drag_carries_full_persisted_membership(
        qtbot, tmp_path, monkeypatch):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    members = ["s30:12:ana1", "s30:12:ana2", "s30:12:ana3"]
    gid = vm.create_group_from("Filtered", members)
    vm.apply_query("ana:ana1")
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    box = _box(dock, gid)
    assert [chip.key for chip in box.chips] == ["s30:12:ana1"]

    payloads = []

    class _Drag:
        def __init__(self, _parent):
            self._md = None

        def setMimeData(self, md):
            self._md = md

        def exec(self, _action):
            payloads.append(decode_scan_ids(self._md.data(MIME_SCAN_IDS).data()))

    monkeypatch.setattr("isar_gui.views.scans_dock.QDrag", _Drag)
    box._drag_start = QPoint(0, 0)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(30, 0), Qt.MouseButton.NoButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    box.mouseMoveEvent(move)
    assert set(payloads[0]) == set(members)


def test_auto_header_drag_carries_visible_members_only(qtbot, tmp_path, monkeypatch):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    box = _GroupBox("auto:test", "computed", vm, kind="auto")
    qtbot.addWidget(box)
    box._chips = [SimpleNamespace(key="s30:12:ana1")]

    payloads = []

    class _Drag:
        def __init__(self, _parent):
            self._md = None

        def setMimeData(self, md):
            self._md = md

        def exec(self, _action):
            payloads.append(decode_scan_ids(self._md.data(MIME_SCAN_IDS).data()))

    monkeypatch.setattr("isar_gui.views.scans_dock.QDrag", _Drag)
    box._drag_start = QPoint(0, 0)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(30, 0), Qt.MouseButton.NoButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    box.mouseMoveEvent(move)
    assert payloads == [["s30:12:ana1"]]


# ---------------------------------------------------------------------------
# Dock: Manual-tab hint is always present (discoverability)
# ---------------------------------------------------------------------------
def test_home_new_box_tile_present_even_with_groups(qtbot, tmp_path):
    # Grouping stays discoverable graphically (the "+ new box" tile), not via an instructional hint.
    from isar_gui.views.scans_dock import _NewBoxTile
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    vm.create_group_from("Cold", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    # The Home page is now a splitter host widget (not a QScrollArea) — search it directly.
    assert dock._pages["Manual"].findChildren(_NewBoxTile)

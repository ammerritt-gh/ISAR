"""ScansDock interaction tests (Pass 2b — dimension tabs + sibling cues).

The dock renders a QTabWidget of dimension pages (All | Scans | T | q | Analyzer | B | Manual) built
lazily from a fake ws + a real GuiStore. Covers: per-dimension filtering, the Analyzer tab hidden on a
single-detector workspace, cross-tab selection lighting, drops writing new-form unit keys onto a
MANUAL box (and auto boxes rejecting the same drop), the active tab persisted as a ui-scrap, and the
sibling soft-highlight on hover.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QMimeData
from PySide6.QtWidgets import QLabel

from isar_gui.gui_store import GuiStore
from isar_gui.mime import MIME_SCAN_IDS, encode_scan_ids
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock


def _sv(exp, num, det, *, T=300.0, hkl=(1.0, 0.0, 0.0), q_target=None, B=None):
    prov = {}
    if q_target is not None:
        prov["q_target"] = list(q_target)
    if B is not None:
        prov["B"] = B
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type="measurement",
        status="ingested", source_file=r"D:\d\scan.dat", T=T, hkl=list(hkl), provenance=prov,
        detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def _nine(exp="s30", num=12):
    return [_sv(exp, num, f"ana{i}", hkl=(1.5 + 0.01 * i, 0, 0), q_target=(1.5, 0, 0))
            for i in range(1, 10)]


def _tab_names(dock):
    # Tabs are keyed by a stable id (page dict), independent of the display label ("Manual"->"Home").
    return list(dock._pages)


def _show_tab(dock, name):
    dock._tabs.setCurrentIndex(_tab_names(dock).index(name))


def _box(dock, box_id):
    return next(b for b in dock._boxes if b.box_id == box_id)


class _Drop:
    """Minimal drop-event stand-in carrying a real QMimeData."""

    def __init__(self, keys):
        md = QMimeData()
        md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


def test_tabs_present_for_multi_detector(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    names = _tab_names(dock)
    # Home (id "Manual") leads; the flat "All" tab is retired (the Pool subsumes it). No B (no field).
    assert names == ["Manual", "Scans", "T", "q", "Analyzer"]


def test_plain_chip_click_routes_to_inspector(qtbot, tmp_path):
    # A plain (unmodified) single click both selects the unit AND asks the inspector to open it.
    vm = ScansViewModel(_ws([_sv("in8", 1, "det")]), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    got = []
    vm.inspect_requested.connect(got.append)
    chip = dock._chips_by_key["in8:1:det"][0]
    chip.clicked.emit("in8:1:det", Qt.KeyboardModifier.NoModifier)
    assert got == ["in8:1:det"]                       # ingested -> routes the unit key


def test_modified_chip_click_does_not_route_to_inspector(qtbot, tmp_path):
    # Ctrl/shift multi-select builds a set — it must NOT steal the inspector focus.
    vm = ScansViewModel(_ws([_sv("in8", 1, "det"), _sv("in8", 2, "det")]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    got = []
    vm.inspect_requested.connect(got.append)
    chip = dock._chips_by_key["in8:1:det"][0]
    chip.clicked.emit("in8:1:det", Qt.KeyboardModifier.ControlModifier)
    chip.clicked.emit("in8:1:det", Qt.KeyboardModifier.ShiftModifier)
    assert got == []


def test_analyzer_tab_hidden_for_single_detector(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("in8", 1, "det"), _sv("in8", 2, "det", T=310.0)]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    assert "Analyzer" not in _tab_names(dock)


def test_b_tab_present_only_with_field_data(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("s30", 1, "ana1", B=2.0), _sv("s30", 1, "ana2", B=2.0)]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    assert "B" in _tab_names(dock)


def test_all_tab_lists_every_unit(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    all_box = _box(dock, "ungrouped")
    assert len(all_box.chips) == 9


def test_scans_tab_groups_by_source_scan(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    scan_box = _box(dock, "auto:scan:s30:12")
    assert len(scan_box.chips) == 9
    assert "scan 12" in scan_box._title.text() and "#Q" in scan_box._title.text()


def test_analyzer_tab_one_box_per_analyzer(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Analyzer")
    ana_boxes = [b for b in dock._boxes if str(b.box_id).startswith("auto:ana:")]
    assert len(ana_boxes) == 9
    assert all(len(b.chips) == 1 for b in ana_boxes)


def test_drop_on_manual_box_writes_unit_keys(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    gid = vm.create_group("Cold")          # rebuilds; switches to Manual tab
    _show_tab(dock, "Manual")
    manual = _box(dock, gid)
    manual.dropEvent(_Drop(["s30:12:ana1", "s30:12:ana2"]))
    assert store.groups()[0].members == ["s30:12:ana1", "s30:12:ana2"]   # new-form unit keys
    assert {c.key for c in _box(dock, gid).chips} == {"s30:12:ana1", "s30:12:ana2"}


def test_auto_box_rejects_the_same_drop(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    auto = _box(dock, "auto:scan:s30:12")
    auto.dropEvent(_Drop(["s30:12:ana1"]))
    assert store.groups() == []            # dropping on a derived box mutates nothing


def test_cross_tab_selection_lights_every_chip(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    # Build two pages so the unit appears in more than one box.
    _show_tab(dock, "Manual")
    _show_tab(dock, "Analyzer")
    chips = dock._chips_by_key["s30:12:ana1"]
    assert len(chips) >= 2                  # one on All, one on Analyzer
    vm.select_keys(["s30:12:ana1"], clear=True)
    assert all(c._selected for c in chips)


def test_active_chip_is_distinct_from_selected_group(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    # Group/multi-selection is intentionally independent from the one detector in the inspector.
    vm.select_keys(["s30:12:ana1", "s30:12:ana2"], clear=True)
    dock.set_active_key("s30:12:ana1")
    first = dock._chips_by_key["s30:12:ana1"][0]
    second = dock._chips_by_key["s30:12:ana2"][0]
    assert first._selected and second._selected
    assert first._active and not second._active
    assert not first._active_label.isHidden()
    assert first._active_label.text() == "Inspecting"


def test_chip_status_is_not_colour_only(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("in8", 1, "det")]), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    chip = dock._chips_by_key["in8:1:det"][0]
    assert "parsed" in chip._sub_label.text()
    assert "status: parsed" in chip.toolTip()


def test_chip_click_updates_shared_selection(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    chip = dock._chips_by_key["s30:12:ana1"][0]
    chip.clicked.emit("s30:12:ana1", Qt.KeyboardModifier.NoModifier)
    assert vm.selected_keys() == ["s30:12:ana1"]


def test_hover_soft_highlights_siblings(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    dock._on_chip_hovered("s30:12:ana1", True)
    # Every sibling chip is soft-highlighted while hovering one.
    assert all(dock._chips_by_key[f"s30:12:ana{i}"][0]._sibling for i in range(1, 10))
    dock._on_chip_hovered("s30:12:ana1", False)
    assert not any(dock._chips_by_key[f"s30:12:ana{i}"][0]._sibling for i in range(1, 10))


def test_active_tab_persisted_as_scrap(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "T")
    assert store.scans_tab() == "T"
    # A fresh dock over the same store restores the persisted tab.
    dock2 = ScansDock(vm)
    qtbot.addWidget(dock2)
    assert _tab_names(dock2)[dock2._tabs.currentIndex()] == "T"


def test_multi_detector_chip_shows_analyzer_suffix(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    chip = dock._chips_by_key["s30:12:ana3"][0]
    assert chip._num_label.text() == "12·ana3"


def test_single_detector_chip_shows_scan_only(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("in8", 5, "det")]), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    chip = dock._chips_by_key["in8:5:det"][0]
    assert chip._num_label.text() == "5"


def test_home_has_pool_and_new_box_tile(qtbot, tmp_path):
    # The Home tab is the Pool (all scans) + the always-visible "+ new box" affordance (R9/R11);
    # the old paragraph hint is gone (grouping is shown graphically, not spelled out).
    from isar_gui.views.scans_dock import _NewBoxTile
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    pool = _box(dock, "ungrouped")
    assert pool._title.text() == "Pool" and len(pool.chips) == 9   # every scan is in the pool
    assert dock.findChildren(_NewBoxTile)                          # the make-a-group tile exists
    assert not dock.findChildren(QLabel, "manualHint")             # no instructional paragraph

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
from isar_gui.views import scans_dock
from isar_gui.views.scans_dock import ScansDock, _q_compact


def test_q_compact_preserves_close_reciprocal_coordinates_and_suppresses_zeroes():
    assert _q_compact((2.10, 0.0, 0.0)) == "[2.1 0 0]"
    assert _q_compact((2.15, 0.0, 0.0)) == "[2.15 0 0]"
    assert _q_compact((2.15001, 2.15002, 0.0)) == "[2.15001 2.15002 0]"


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
    dock._build_page(name)


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
    child = dock._pool_tree.topLevelItem(0).child(0)
    dock._pool_tree._activate(child, 0)
    assert got == ["in8:1:det"]                       # ingested -> routes the unit key


def test_master_tree_preserves_multi_selection(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("in8", 1, "det"), _sv("in8", 2, "det")]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    top = dock._pool_tree.topLevelItem(0)
    top.child(0).setSelected(True)
    top.child(1).setSelected(True)
    dock._pool_tree._activate(top.child(1), 0)
    assert set(vm.selected_keys()) == {"in8:1:det", "in8:2:det"}


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
    _show_tab(dock, "Scans")
    assert dock._pool_tree.topLevelItemCount() == 1
    assert dock._pool_tree.topLevelItem(0).childCount() == 9


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


def test_manual_tab_hides_scan_page_slot_groups(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    ordinary_id = vm.create_group("Manual")
    store.save_slot(3, ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)

    _show_tab(dock, "Manual")

    assert [box.box_id for box in dock._boxes] == [ordinary_id]
    assert len(store.groups()) == 2
    assert store.slot_group(3).members == ["s30:12:ana1"]


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
    _show_tab(dock, "Scans")
    _show_tab(dock, "Analyzer")
    chips = dock._chips_by_key["s30:12:ana1"]
    assert len(chips) >= 2                  # one on Scans, one on Analyzer
    vm.select_keys(["s30:12:ana1"], clear=True)
    assert all(c._selected for c in chips)


def test_active_chip_is_distinct_from_selected_group(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
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
    child = dock._pool_tree.topLevelItem(0).child(0)
    assert child.text(6) == "parsed"


def test_chip_click_updates_shared_selection(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
    chip = dock._chips_by_key["s30:12:ana1"][0]
    chip.clicked.emit("s30:12:ana1", Qt.KeyboardModifier.NoModifier)
    assert vm.selected_keys() == ["s30:12:ana1"]


def test_hover_soft_highlights_siblings(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Scans")
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
    _show_tab(dock, "Scans")
    chip = dock._chips_by_key["s30:12:ana3"][0]
    assert chip._num_label.text() == "12·ana3"


def test_single_detector_chip_shows_scan_only(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("in8", 5, "det")]), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    child = dock._pool_tree.topLevelItem(0).child(0)
    assert child.text(0) == "scan 5 · det"


def test_home_has_pool_and_new_box_tile(qtbot, tmp_path):
    # The Home tab is the Pool (all scans) + the always-visible "+ new box" affordance (R9/R11);
    # the old paragraph hint is gone (grouping is shown graphically, not spelled out).
    from isar_gui.views.scans_dock import _NewBoxTile
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _show_tab(dock, "Manual")
    assert dock._pool_tree.topLevelItem(0).text(0) == "Unpooled scans"
    assert dock._pool_tree.topLevelItem(0).childCount() == 9
    assert dock.findChildren(_NewBoxTile)                          # the make-a-group tile exists
    assert not dock.findChildren(QLabel, "manualHint")             # no instructional paragraph


def test_master_browser_is_hierarchical_scrollable_and_only_children_drag(qtbot, tmp_path):
    scans = [_sv("tavi", index, "det", hkl=(2.0 + index / 100, 0, 0))
             for index in range(1, 61)]
    groups = []
    for index, scan in enumerate(scans, start=1):
        member = SimpleNamespace(unit_id=scan.scan_id, branch_id="optic", omega0=6.0)
        master = SimpleNamespace(
            q=scan.hkl, T=300.0, branch_id="optic", track_state="confirmed",
            interval=[5.8, 6.2], omega0=6.0, interval_reliable=True,
            disposition="resolved", n_members=1, reasons=())
        groups.append(SimpleNamespace(master=master, members=(member,)))
    ws = SimpleNamespace(scans=scans, master_groups=groups)
    vm = ScansViewModel(ws, store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    dock.resize(480, 320)
    qtbot.addWidget(dock)
    dock.show()
    qtbot.wait(20)

    tree = dock._pool_tree
    assert tree.topLevelItemCount() == 60
    top = tree.topLevelItem(0)
    child = top.child(0)
    assert top.text(0) == "Master" and child.text(0).startswith("scan ")
    assert not (top.flags() & Qt.ItemFlag.ItemIsDragEnabled)
    assert child.flags() & Qt.ItemFlag.ItemIsDragEnabled
    assert tree.verticalScrollBar().maximum() > 0


def test_deferred_scrollbar_restore_is_bound_to_the_tree(qtbot, tmp_path, monkeypatch):
    """The zero-delay scrollbar restore must pass the tree as its Qt CONTEXT object.

    Without a context object Qt keeps the timer armed after the tree is destroyed, and the callback
    then calls setValue() on scrollbars whose C++ objects are gone — a hard access violation, not a
    catchable RuntimeError (PySide6 does not invalidate wrappers for C++-owned children reaped by a
    parent's destructor). MainWindow.load_workspace destroys this dock as part of its rebind, so a
    rebind landing before the event loop services the timer hits exactly that case; it is how
    tests/gui/test_browser.py's menu-leak guard used to take the whole suite down.

    Asserted structurally rather than by provoking the crash on purpose: a regression here kills the
    interpreter, and a segfaulting test reports nothing — it just destroys the run.
    """
    real_qtimer = scans_dock.QTimer
    contexts = []

    class _SpyQTimer(real_qtimer):
        @staticmethod
        def singleShot(msec, *rest):
            contexts.append(rest)
            real_qtimer.singleShot(msec, *rest)

    scans = [_sv("tavi", index, "det", hkl=(2.0 + index / 100, 0, 0)) for index in range(1, 11)]

    def group_for(scan):
        member = SimpleNamespace(unit_id=scan.scan_id, branch_id="optic", omega0=6.0)
        master = SimpleNamespace(
            q=scan.hkl, T=300.0, branch_id="optic", track_state="confirmed",
            interval=[5.8, 6.2], omega0=6.0, interval_reliable=True,
            disposition="resolved", n_members=1, reasons=())
        return SimpleNamespace(master=master, members=(member,))

    ws = SimpleNamespace(scans=scans, master_groups=[group_for(scan) for scan in scans])
    vm = ScansViewModel(ws, store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)

    # Patch only around the refresh, so the initial build's timers are not counted. Use a
    # scoped context rather than monkeypatch.setattr(...)/monkeypatch.undo(): pytest shares one
    # MonkeyPatch instance per test node, so undo() also reverts the autouse isolation fixtures.
    with monkeypatch.context() as m:
        m.setattr(scans_dock, "QTimer", _SpyQTimer)
        vm.refresh()

    assert contexts, "the live refresh no longer schedules the deferred scrollbar restore"
    for rest in contexts:
        assert len(rest) == 2, (
            "QTimer.singleShot was called without a context object — the callback would outlive the "
            f"widgets it touches; args after the interval were {rest!r}")
        context, _callback = rest
        assert context is dock._pool_tree


def test_live_refresh_preserves_master_browser_view_state(qtbot, tmp_path):
    scans = [_sv("tavi", index, "det", hkl=(2.0 + index / 100, 0, 0))
             for index in range(1, 61)]

    def group_for(scan):
        member = SimpleNamespace(unit_id=scan.scan_id, branch_id="optic", omega0=6.0)
        master = SimpleNamespace(
            q=scan.hkl, T=300.0, branch_id="optic", track_state="confirmed",
            interval=[5.8, 6.2], omega0=6.0, interval_reliable=True,
            disposition="resolved", n_members=1, reasons=())
        return SimpleNamespace(master=master, members=(member,))

    ws = SimpleNamespace(scans=scans, master_groups=[group_for(scan) for scan in scans])
    vm = ScansViewModel(ws, store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    dock.resize(480, 320)
    qtbot.addWidget(dock)
    dock.show()
    qtbot.waitUntil(lambda: dock._pool_tree.verticalScrollBar().maximum() > 0)

    old_tree = dock._pool_tree
    old_tree.topLevelItem(0).setExpanded(False)
    selected = old_tree.topLevelItem(35)
    selected.setSelected(True)
    old_tree.setCurrentItem(selected)
    selected_q = selected.text(1)
    bar = old_tree.verticalScrollBar()
    bar.setValue(max(1, bar.maximum() // 2))
    old_scroll = bar.value()
    old_splitter_sizes = dock._home_splitter.sizes()

    arriving = _sv("tavi", 61, "det", hkl=(2.61, 0, 0))
    ws.scans.append(arriving)
    ws.master_groups.append(group_for(arriving))
    vm.refresh()
    # A second notification can arrive before Qt services the deferred scrollbar restore.
    arriving_again = _sv("tavi", 62, "det", hkl=(2.62, 0, 0))
    ws.scans.append(arriving_again)
    ws.master_groups.append(group_for(arriving_again))
    vm.refresh()

    qtbot.waitUntil(lambda: dock._pool_tree is not old_tree)
    qtbot.waitUntil(lambda: dock._pool_tree.verticalScrollBar().value() == old_scroll)
    tree = dock._pool_tree
    assert tree.topLevelItemCount() == 62
    assert not tree.topLevelItem(0).isExpanded()
    assert tree.currentItem() is not None
    assert tree.currentItem().text(1) == selected_q
    assert tree.currentItem().isSelected()
    new_splitter_sizes = dock._home_splitter.sizes()
    old_ratio = old_splitter_sizes[0] / sum(old_splitter_sizes)
    new_ratio = new_splitter_sizes[0] / sum(new_splitter_sizes)
    assert new_ratio == pytest.approx(old_ratio, abs=0.05)

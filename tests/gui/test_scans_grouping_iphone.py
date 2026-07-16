"""Part-1 (GUI Pass 2) iPhone-grouping tests: chip-onto-chip merge with a dwell gate, source-aware
move vs copy on overlapping groups, the ambient lens dim + precedence, inline birth rename, full
(filter-independent) group payload, and the folder-order round-trip.

Drops/dwell are driven through the model seams (`_on_chip_merge`, `_arm_merge`, box `dropEvent` with a
real QMimeData) rather than a live `QDrag`, which hangs the offscreen suite — the same discipline the
existing dock tests use for menu actions.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from isar_gui.gui_store import GuiStore
from isar_gui.mime import (
    MIME_DRAG_ORIGIN,
    MIME_GROUP_REF,
    MIME_SCAN_IDS,
    decode_drag_origin,
    encode_drag_origin,
    encode_group_ref,
    encode_scan_ids,
)
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock


# --------------------------------------------------------------------------- fixtures
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


def _box(dock, box_id):
    return next(b for b in dock._boxes if b.box_id == box_id)


def _home(dock):
    dock._tabs.setCurrentIndex(list(dock._pages).index("Manual"))


class _Drop:
    """Drop-event stand-in with a real QMimeData; optional drag-origin and group-ref payloads."""

    def __init__(self, keys, *, origin=None, group_ref=None):
        md = QMimeData()
        md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        if origin is not None:
            md.setData(MIME_DRAG_ORIGIN, encode_drag_origin(origin))
        if group_ref is not None:
            md.setData(MIME_GROUP_REF, encode_group_ref("manual", group_ref, group_ref))
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


# --------------------------------------------------------------------------- mime codec
def test_drag_origin_codec_roundtrip_and_tolerant():
    assert decode_drag_origin(encode_drag_origin("abc123")) == "abc123"
    assert decode_drag_origin(encode_drag_origin("")) == ""
    assert decode_drag_origin(encode_drag_origin(None)) == ""
    assert decode_drag_origin(b"\xff\xfe") == ""   # invalid utf-8 -> tolerant empty, never raises


# --------------------------------------------------------------------------- VM methods
def test_create_group_from_merge_builds_named_group(tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from_merge("s30:12:ana1", ["s30:12:ana2", "s30:12:ana1"])  # dup target
    assert gid is not None
    members = set(vm.member_keys(gid))
    assert members == {"s30:12:ana1", "s30:12:ana2"}


def test_create_group_from_merge_self_only_is_noop(tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    assert vm.create_group_from_merge("s30:12:ana1", []) is None
    assert vm.create_group_from_merge("s30:12:ana1", ["s30:12:ana1"]) is None
    assert vm.manual_groups() == []


def test_member_keys_is_full_under_active_filter(tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("g", ["s30:12:ana1", "s30:12:ana2", "s30:12:ana3"])
    # Narrow the view so only ana1 survives; the carried membership must still be all three.
    vm.apply_query("ana:ana1")
    assert len(vm.rows()) == 1
    assert set(vm.member_keys(gid)) == {"s30:12:ana1", "s30:12:ana2", "s30:12:ana3"}


def test_lens_bins_match_auto_groups(tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    bins = dict(vm.lens_bins("ana"))
    autos = {g.label: set(g.scan_keys) for g in vm.auto_groups("ana")}
    assert bins == autos and bins   # non-empty, identical to the derived auto-groups


# --------------------------------------------------------------------------- dock merge seam
def test_merge_on_loose_chip_births_folder_and_opens_rename(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._on_chip_merge("s30:12:ana1", ["s30:12:ana2"], "")
    groups = vm.manual_groups()
    assert len(groups) == 1
    assert set(vm.member_keys(groups[0].id)) == {"s30:12:ana1", "s30:12:ana2"}
    # the freshly-born folder opened inline for renaming
    box = _box(dock, groups[0].id)
    assert box._name_edit is not None


def test_merge_onto_foldered_chip_adds_to_that_folder(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("g", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._on_chip_merge("s30:12:ana1", ["s30:12:ana5"], gid)   # target already in folder gid
    assert vm.manual_groups() == vm.manual_groups()            # no new group
    assert len(vm.manual_groups()) == 1
    assert set(vm.member_keys(gid)) == {"s30:12:ana1", "s30:12:ana5"}


def test_chip_drop_gated_by_dwell(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    target = dock._chips_by_key["s30:12:ana1"][0]
    fired = []
    target.merge_requested.connect(lambda *a: fired.append(a))

    # Unarmed drop does nothing (early release before the dwell).
    target.dropEvent(_Drop(["s30:12:ana2"]))
    assert fired == []
    # Arm (the dwell timeout seam) then drop -> merge requested with the other key only.
    target._arm_merge()
    target.dropEvent(_Drop(["s30:12:ana2", "s30:12:ana1"]))
    assert fired == [("s30:12:ana1", ["s30:12:ana2"], "")]


def test_self_only_drag_never_arms(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    target = dock._chips_by_key["s30:12:ana1"][0]
    assert _ScanChip_dragged_others(target, ["s30:12:ana1"]) == []


def _ScanChip_dragged_others(chip, keys):
    md = QMimeData()
    md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
    return chip._dragged_others(md, chip._key)


def test_folder_header_drag_never_merges_a_chip(qtbot, tmp_path):
    # A folder-header drag carries a group ref; a chip must not treat it as a merge.
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    target = dock._chips_by_key["s30:12:ana1"][0]
    md = QMimeData()
    md.setData(MIME_SCAN_IDS, encode_scan_ids(["s30:12:ana2"]))
    md.setData(MIME_GROUP_REF, encode_group_ref("manual", "g", "g"))
    assert target._dragged_others(md, target._key) == []


# --------------------------------------------------------------------------- drag-out remove (R2)
def test_drag_out_removes_from_origin_only_with_overlap(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    a = vm.create_group_from("A", ["s30:12:ana1", "s30:12:ana2"])
    b = vm.create_group_from("B", ["s30:12:ana1", "s30:12:ana3"])   # ana1 is in BOTH
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    ung = _box(dock, "ungrouped")
    ung.dropEvent(_Drop(["s30:12:ana1"], origin=a))   # drag ana1 OUT of group A
    assert "s30:12:ana1" not in set(vm.member_keys(a))
    assert "s30:12:ana1" in set(vm.member_keys(b))    # untouched in B


def test_drag_out_of_loose_chip_is_noop(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("A", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    ung = _box(dock, "ungrouped")
    ung.dropEvent(_Drop(["s30:12:ana5"], origin=""))   # loose chip -> nothing to leave
    assert set(vm.member_keys(gid)) == {"s30:12:ana1"}


def test_manual_box_ignores_folder_ref_drop(qtbot, tmp_path):
    # Dropping a folder (group-ref) onto a manual box must NOT silently merge its members.
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("A", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    box = _box(dock, gid)
    box.dropEvent(_Drop(["s30:12:ana2"], group_ref="other"))
    assert set(vm.member_keys(gid)) == {"s30:12:ana1"}   # unchanged


# --------------------------------------------------------------------------- ambient lens (R7)
def test_lens_dims_non_matching_and_respects_precedence(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    label, keys = vm.lens_bins("ana")[0]     # a single-analyzer bin
    in_key = next(iter(keys))
    out_key = next(u.key for u in vm.rows() if u.key not in keys)
    dock._lens_dim, dock._lens_bin = "ana", label
    dock._sync_lens()
    assert dock._chips_by_key[in_key][0]._dimmed is False
    assert dock._chips_by_key[out_key][0]._dimmed is True
    # Precedence: an inspected (active) chip is never *painted* dimmed even when outside the bin —
    # the greyed text colour is applied only on the (dimmed and not active) branch of _apply_style.
    out_chip = dock._chips_by_key[out_key][0]
    out_chip.set_active(True)
    dock._sync_lens()
    assert "#b0b0b0" not in out_chip.styleSheet()   # active border wins over the lens dim


def test_lens_resets_on_data_refresh(qtbot, tmp_path):
    # A data refresh (ingest tick) must clear the lens so new scans are not silently dimmed (R7).
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    label, _keys = vm.lens_bins("ana")[0]
    dock._lens_dim, dock._lens_bin = "ana", label
    dock._sync_lens()
    vm.refresh()                       # emits refreshed -> lens reset + rebuild
    assert dock._lens_dim is None and dock._lens_bin is None
    assert all(not c._dimmed for chips in dock._chips_by_key.values() for c in chips)


# --------------------------------------------------------------------------- inline rename (R4/R5)
def test_chip_click_in_folder_selects_only_that_chip(qtbot, tmp_path):
    # Regression: a chip click must be CONSUMED so it never falls through to the folder box's
    # header-click (which selects every member). Previously clicking one scan selected the whole
    # group, and right-click "remove from group" then removed all of them.
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("g", ["s30:12:ana1", "s30:12:ana2", "s30:12:ana3"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    box = next(b for b in dock._boxes if b.box_id == gid)
    chip = next(c for c in box.chips if c.key == "s30:12:ana2")
    center = QPointF(chip.rect().center())
    press = QMouseEvent(QEvent.Type.MouseButtonPress, center, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    chip.mousePressEvent(press)
    assert press.isAccepted()                       # consumed, not propagated to the folder box
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, center, Qt.MouseButton.LeftButton,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    chip.mouseReleaseEvent(release)
    assert release.isAccepted()
    assert vm.selected_keys() == ["s30:12:ana2"]    # exactly one scan, not the whole group


def test_inline_rename_commits_new_name(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("old", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    box = _box(dock, gid)
    box.begin_inline_rename()
    assert box._name_edit is not None
    box._name_edit.setText("new name")
    box._commit_inline_rename()
    assert next(g.name for g in vm.manual_groups() if g.id == gid) == "new name"


# --------------------------------------------------------------------------- folder order round-trip (R6)
def test_folder_order_round_trips(tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    a = vm.create_group_from("A", ["s30:12:ana1"])
    b = vm.create_group_from("B", ["s30:12:ana2"])
    c = vm.create_group_from("C", ["s30:12:ana3"])
    vm.set_group_order([c, a, b])
    assert [g.id for g in vm.manual_groups()] == [c, a, b]
    store.save()
    reloaded = GuiStore.load(tmp_path)
    vm2 = ScansViewModel(_ws(_nine()), store=reloaded)
    assert [g.id for g in vm2.manual_groups()] == [c, a, b]


# --------------------------------------------------------------------------- tab id persistence (R1)
def test_home_tab_label_is_home_but_id_is_manual(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    labels = [dock._tabs.tabText(i) for i in range(dock._tabs.count())]
    assert "Home" in labels and "Manual" not in labels   # display label renamed
    assert "Manual" in list(dock._pages)                 # persisted id kept


# --------------------------------------------------------------------------- Part 3: Pool + trays
def test_membership_count(tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    assert vm.membership_count("s30:12:ana1") == 0
    vm.create_group_from("A", ["s30:12:ana1"])
    vm.create_group_from("B", ["s30:12:ana1", "s30:12:ana2"])
    assert vm.membership_count("s30:12:ana1") == 2
    assert vm.membership_count("s30:12:ana2") == 1


def test_pool_lists_all_scans_including_boxed_ones(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    vm.create_group_from("A", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    pool = next(b for b in dock._boxes if b.box_id == "ungrouped")
    assert {c.key for c in pool.chips} == {f"s30:12:ana{i}" for i in range(1, 10)}  # all 9
    # the boxed scan's POOL chip carries a membership badge; a loose one does not
    boxed = next(c for c in pool.chips if c.key == "s30:12:ana1")
    loose = next(c for c in pool.chips if c.key == "s30:12:ana2")
    assert boxed._badge == 1 and not boxed._badge_label.isHidden()
    assert loose._badge == 0 and loose._badge_label.isHidden()


def test_new_box_tile_drop_creates_group_and_opens_rename(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    dock._create_box(["s30:12:ana1", "s30:12:ana2"])   # the tile's create_requested handler
    groups = vm.manual_groups()
    assert len(groups) == 1
    assert set(vm.member_keys(groups[0].id)) == {"s30:12:ana1", "s30:12:ana2"}
    box = next(b for b in dock._boxes if b.box_id == groups[0].id)
    assert box._name_edit is not None                  # opened inline for naming


def test_new_box_tile_click_creates_empty_group(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    dock._create_box([])                               # click path -> empty group
    assert len(vm.manual_groups()) == 1
    assert vm.member_keys(vm.manual_groups()[0].id) == []


def test_drag_session_arms_trays_and_tile_and_reemits(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    gid = vm.create_group_from("A", ["s30:12:ana1"])
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    seen = []
    dock.scan_drag_active.connect(lambda active, keys: seen.append((active, list(keys))))
    box = next(b for b in dock._boxes if b.box_id == gid)
    dock._on_drag_session(True, ["s30:12:ana2"])
    assert box._armed and dock._new_box_tile._armed
    assert seen[-1] == (True, ["s30:12:ana2"])         # re-emitted for the window to arm plots
    dock._on_drag_session(False, ["s30:12:ana2"])
    assert not box._armed and not dock._new_box_tile._armed
    assert seen[-1][0] is False


def test_preset_boxes_never_arm(qtbot, tmp_path):
    # A read-only preset (auto) box stays inert during a drag — it is a lens, not a drop target (R10).
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._tabs.setCurrentIndex(list(dock._pages).index("Scans"))
    auto = next(b for b in dock._boxes if str(b.box_id).startswith("auto:"))
    auto.set_armed(True)
    assert auto._armed is False                        # set_armed is a no-op on non-manual boxes


def test_home_is_pool_workbench_splitter(qtbot, tmp_path):
    # R15: Home is a vertical splitter (Pool pane + Workbench pane) so the + tile / trays are always
    # visible; the Pool holds all scans and the + tile lives in the (pinned) workbench.
    from PySide6.QtWidgets import QSplitter

    from isar_gui.views.scans_dock import _NewBoxTile
    vm = ScansViewModel(_ws(_nine()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    _home(dock)
    host = dock._pages["Manual"]
    splitters = host.findChildren(QSplitter)
    assert splitters and splitters[0].count() == 2          # Pool pane + Workbench pane
    assert host.findChildren(_NewBoxTile)                    # + tile present in the workbench
    pool = next(b for b in dock._boxes if b.box_id == "ungrouped")
    assert len(pool.chips) == 9                              # pool = all scans


def test_active_home_tab_persists_by_id(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws(_nine()), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    # Home is the default tab, so switch away and back to fire the persist (by id, not the label).
    dock._tabs.setCurrentIndex(list(dock._pages).index("T"))
    _home(dock)
    assert store.scans_tab() == "Manual"                 # id, not "Home"

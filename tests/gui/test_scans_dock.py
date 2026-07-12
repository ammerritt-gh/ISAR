"""ScansDock interaction tests (Phase E).

The physical dock renders boxes (ungrouped / auto / manual) from a fake ws + a real GuiStore, mutates
the store when scans are dropped on a MANUAL box, IGNORES the identical drop on an AUTO box (derived
membership), and keeps chip selection in lockstep with the shared selection model — a scan selected
anywhere lights up in every box it appears in.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QMimeData

from isar_gui.gui_store import GuiStore
from isar_gui.mime import MIME_SCAN_IDS, encode_scan_ids
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock


def _sv(exp, num, det, *, T=300.0, hkl=(1.0, 0.0, 0.0)):
    return SimpleNamespace(
        experiment_id=exp, scan_number=num, scan_type="measurement", status="ingested",
        source_file=r"D:\d\scan.dat", T=T, hkl=list(hkl),
        detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


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


def _box(dock, box_id):
    return next(b for b in dock._boxes if b.box_id == box_id)


def test_boxes_render_ungrouped_and_auto(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1", T=300.0), _sv("tavi", 2, "d1", T=310.0)]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    ids = {b.box_id for b in dock._boxes}
    assert "ungrouped" in ids
    # Two distinct T-bins -> two auto T-groups.
    assert any(str(b.box_id).startswith("auto:T:") for b in dock._boxes)
    # Every scan appears in Ungrouped (nothing manually grouped yet).
    assert len(_box(dock, "ungrouped").chips) == 2


def test_drop_on_manual_box_calls_add_to_group(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1")]), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    gid = vm.create_group("Cold")           # emits groups_changed -> dock rebuilds
    manual = _box(dock, gid)

    manual.dropEvent(_Drop(["tavi:1"]))
    assert store.memberships_of("tavi:1") == [gid]
    # And the scan now also shows in the manual box after the rebuild.
    assert any(c.key == "tavi:1" for c in _box(dock, gid).chips)


def test_auto_box_rejects_the_same_drop(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1", T=300.0)]), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    auto = next(b for b in dock._boxes if str(b.box_id).startswith("auto:"))

    auto.dropEvent(_Drop(["tavi:1"]))
    # No manual group was created or mutated by dropping on a derived box.
    assert store.groups() == []


def test_chip_click_updates_shared_selection(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1"), _sv("tavi", 2, "d1")]),
                        store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    chip = dock._chips_by_key["tavi:1"][0]

    chip.clicked.emit("tavi:1", Qt.KeyboardModifier.NoModifier)
    assert vm.selected_keys() == ["tavi:1"]


def test_selection_lights_chip_in_every_box(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1", T=300.0)]), store=store)
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    # tavi:1 shows in Ungrouped AND its auto T-group -> at least two chip instances.
    chips = dock._chips_by_key["tavi:1"]
    assert len(chips) >= 2

    vm.select_keys(["tavi:1"], clear=True)
    assert all(c._selected for c in chips)   # every box's chip lit

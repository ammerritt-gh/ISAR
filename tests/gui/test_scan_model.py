"""ScanTableModel / ScansViewModel tests (Pass 2b — atomic scan-units).

Covers the atomic-unit rows (one per ledger entry, no scan-level collapse), each unit carrying its
OWN analyzer q (not the first detector's) plus the source scan's target #Q, the Analyzer column, the
``multi_detector`` predicate that gates the detector suffix / Analyzer tab, and the legacy-membership
sibling-preserving removal semantics.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.scans import (
    COL_ANA,
    COL_FILE,
    COL_GROUPS,
    COL_NUM,
    COL_Q,
    COL_STATUS,
    ScansViewModel,
    unit_rows,
)


def _sv(exp, num, det, *, status="ingested", T=300.0, hkl=(1.0, 0.0, 0.0),
        q_target=None, source=r"D:\d\scan.dat", scan_type="measurement", B=None, command=None):
    prov = {}
    if q_target is not None:
        prov["q_target"] = list(q_target)
    if B is not None:
        prov["B"] = B
    if command is not None:
        prov["command"] = command
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type=scan_type,
        status=status, source_file=source, T=T, hkl=list(hkl) if hkl is not None else None,
        provenance=prov, detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def _nine_analyzers(exp="s30", num=12, T=300.0, q_target=(1.5, 0.0, 0.0)):
    # Nine analyzers, each with a DIFFERENT own q — the S30 #V lines.
    return [_sv(exp, num, f"ana{i}", T=T, hkl=(1.5 + 0.01 * i, 0.0, 0.0), q_target=q_target)
            for i in range(1, 10)]


def test_nine_analyzer_entries_make_nine_units():
    rows = unit_rows(_nine_analyzers())
    assert len(rows) == 9
    assert [r.key for r in rows] == [f"s30:12:ana{i}" for i in range(1, 10)]
    assert all(r.scan_number == 12 for r in rows)


def test_each_unit_keeps_its_own_analyzer_q_not_the_first():
    rows = unit_rows(_nine_analyzers())
    # Every unit's q is distinct (its own #V line), NOT ana1's collapsed value.
    qs = [tuple(r.q) for r in rows]
    assert len(set(qs)) == 9
    assert rows[0].q[0] != rows[8].q[0]


def test_units_carry_scan_target_q():
    rows = unit_rows(_nine_analyzers(q_target=(1.5, 0.0, 0.0)))
    assert all(r.q_target == [1.5, 0.0, 0.0] for r in rows)


def test_q_column_shows_own_q_target_rides_tooltip(qtbot):
    vm = ScansViewModel(_ws([_sv("s30", 12, "ana3", hkl=(0.7, 0.0, 0.0), q_target=(1.5, 0.0, 0.0))]))
    m = vm.model
    assert m.index(0, COL_Q).data(Qt.DisplayRole) == "[0.7, 0, 0]"
    assert "target #Q" in m.index(0, COL_Q).data(Qt.ToolTipRole)
    assert m.index(0, COL_ANA).data(Qt.DisplayRole) == "ana3"


def test_userrole_serves_unit_key(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 470, "d1")]))
    assert vm.model.index(0, 0).data(Qt.UserRole) == "tavi:470:d1"
    assert vm.model.index(0, COL_FILE).data(Qt.ToolTipRole) == r"D:\d\scan.dat"


def test_status_parsed_mapping(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 469, "d1", status="ingested")]))
    m = vm.model
    assert m.index(0, COL_NUM).data(Qt.DisplayRole) == "469"
    assert m.index(0, COL_STATUS).data(Qt.DisplayRole) == "parsed"
    assert "not fitted" in m.index(0, COL_STATUS).data(Qt.ToolTipRole)


def test_multi_detector_true_for_many_analyzers(qtbot):
    vm = ScansViewModel(_ws(_nine_analyzers()))
    assert vm.multi_detector is True


def test_single_detector_workspace_is_not_multi(qtbot):
    vm = ScansViewModel(_ws([_sv("in8", 1, "det"), _sv("in8", 2, "det")]))
    assert vm.multi_detector is False
    # Single-detector 1-unit scans get no scan-dimension auto-group (the scan == the unit).
    assert vm.auto_groups("scan") == []


def test_scan_group_label_carries_target_q(qtbot):
    vm = ScansViewModel(_ws(_nine_analyzers(num=12, q_target=(1.5, 0.0, 0.0))))
    scan_groups = vm.auto_groups("scan")
    assert len(scan_groups) == 1
    assert "scan 12" in scan_groups[0].label
    assert "#Q" in scan_groups[0].label
    # All nine analyzer units are members of the one source-scan group.
    assert len(scan_groups[0].scan_keys) == 9


def test_per_unit_q_groups(qtbot):
    # Two analyzers at genuinely different q -> two q-dimension auto-groups (per-unit q, not target).
    vm = ScansViewModel(_ws([_sv("s30", 1, "ana1", hkl=(0.5, 0, 0)),
                             _sv("s30", 1, "ana2", hkl=(1.5, 0, 0))]))
    assert len(vm.auto_groups("q")) == 2


def test_analyzer_and_b_dimensions(qtbot):
    vm = ScansViewModel(_ws([_sv("s30", 1, "ana1", B=2.0), _sv("s30", 1, "ana2", B=2.0)]))
    assert len(vm.auto_groups("ana")) == 2
    assert vm.has_field() is True
    assert len(vm.auto_groups("B")) == 1


def test_groups_column_shows_auto_label(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1", T=300.0)]))
    text = vm.model.index(0, COL_GROUPS).data(Qt.DisplayRole)
    assert "T = 300 K" in text


def test_selection_survives_refresh(qtbot):
    ws = _ws([_sv("tavi", 1, "d1"), _sv("tavi", 2, "d1")])
    vm = ScansViewModel(ws)
    vm.select_keys(["tavi:1:d1"])
    assert vm.selected_keys() == ["tavi:1:d1"]
    ws.scans.append(_sv("tavi", 3, "d1"))
    vm.refresh()
    assert vm.selected_keys() == ["tavi:1:d1"]


def test_selection_of_vanished_key_is_not_restored(qtbot):
    ws = _ws([_sv("tavi", 1, "d1"), _sv("tavi", 2, "d1")])
    vm = ScansViewModel(ws)
    vm.select_keys(["tavi:2:d1"])
    ws.scans[:] = [_sv("tavi", 1, "d1")]
    vm.refresh()
    assert vm.selected_keys() == []


def test_new_key_highlight_diff_on_refresh(qtbot):
    ws = _ws([_sv("tavi", 1, "d1")])
    vm = ScansViewModel(ws)
    assert vm.model.highlighted_keys() == {"tavi:1:d1"}
    ws.scans.append(_sv("tavi", 2, "d1"))
    vm.refresh()
    assert vm.model.highlighted_keys() == {"tavi:2:d1"}


def test_select_siblings_selects_all_analyzers(qtbot):
    vm = ScansViewModel(_ws(_nine_analyzers()))
    vm.select_siblings("s30:12:ana3")
    assert set(vm.selected_keys()) == {f"s30:12:ana{i}" for i in range(1, 10)}


# ---------------------------------------------------------------------------
# Legacy-membership sibling-preserving removal (the subtle Pass-2b contract)
# ---------------------------------------------------------------------------
def test_remove_unit_from_legacy_membership_preserves_siblings(qtbot, tmp_path):
    """A v1 store holds a scan-level membership ``s30:12`` covering all nine analyzer units. Removing
    ONE unit must expand the other eight under new-form keys, not silently drop the whole scan."""
    store = GuiStore.load(tmp_path)
    gid = store.create_group("Cold")
    store.add_member(gid, "s30:12")   # legacy scan-level membership (covers every analyzer)
    vm = ScansViewModel(_ws(_nine_analyzers()), store=store)

    # Every analyzer is covered by the legacy membership.
    assert all(vm.is_unit_grouped(f"s30:12:ana{i}") for i in range(1, 10))

    vm.remove_from_group(gid, ["s30:12:ana3"])

    # ana3 gone; the other eight survive as explicit unit memberships.
    assert vm.is_unit_grouped("s30:12:ana3") is False
    for i in range(1, 10):
        if i == 3:
            continue
        assert vm.is_unit_grouped(f"s30:12:ana{i}") is True
    members = store.groups()[0].members
    assert "s30:12" not in members            # the covering legacy key was expanded away
    assert "s30:12:ana3" not in members
    assert "s30:12:ana8" in members


def test_remove_direct_unit_membership(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    gid = store.create_group("G")
    vm = ScansViewModel(_ws(_nine_analyzers()), store=store)
    vm.add_to_group(gid, ["s30:12:ana1", "s30:12:ana2"])
    vm.remove_from_group(gid, ["s30:12:ana1"])
    assert store.groups()[0].members == ["s30:12:ana2"]

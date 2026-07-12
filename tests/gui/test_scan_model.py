"""ScanTableModel / ScansViewModel tests (Phase E).

Covers the physical-scan collapse (per-detector ledger entries -> one row), column rendering incl the
``ingested`` -> ``parsed`` status mapping, the ``UserRole`` scan-key contract the chips rely on, and
the new-key highlight diff set across a refresh.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar_gui.viewmodels.scans import (
    COL_FILE,
    COL_GROUPS,
    COL_NUM,
    COL_STATUS,
    collapse_scans,
)
from isar_gui.viewmodels.scans import ScansViewModel


def _sv(exp, num, det, *, status="ingested", T=300.0, hkl=(1.0, 0.0, 0.0),
        source=r"D:\d\scan.dat", scan_type="measurement"):
    return SimpleNamespace(
        experiment_id=exp, scan_number=num, scan_type=scan_type, status=status,
        source_file=source, T=T, hkl=list(hkl) if hkl is not None else None,
        detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def test_three_detector_entries_collapse_to_one_row():
    views = [_sv("tavi", 469, "d1"), _sv("tavi", 469, "d2"), _sv("tavi", 469, "d3")]
    rows = collapse_scans(views)
    assert len(rows) == 1
    assert rows[0].key == "tavi:469"
    assert rows[0].detectors == ["d1", "d2", "d3"]


def test_status_precedence_picks_fitted():
    views = [_sv("tavi", 1, "d1", status="ingested"),
             _sv("tavi", 1, "d2", status="fitted")]
    rows = collapse_scans(views)
    assert rows[0].status == "fitted"


def test_model_columns_and_parsed_status_mapping(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 469, "d1", status="ingested")]))
    m = vm.model
    assert m.rowCount() == 1
    assert m.index(0, COL_NUM).data(Qt.DisplayRole) == "469"
    assert m.index(0, COL_FILE).data(Qt.DisplayRole) == "scan.dat"
    # ingested renders as "parsed" with the not-fitted tooltip.
    assert m.index(0, COL_STATUS).data(Qt.DisplayRole) == "parsed"
    assert "not fitted" in m.index(0, COL_STATUS).data(Qt.ToolTipRole)


def test_userrole_serves_scan_key(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 470, "d1")]))
    assert vm.model.index(0, 0).data(Qt.UserRole) == "tavi:470"
    # The full file path rides the File tooltip.
    assert vm.model.index(0, COL_FILE).data(Qt.ToolTipRole) == r"D:\d\scan.dat"


def test_groups_column_shows_auto_label(qtbot):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1", T=300.0)]))
    text = vm.model.index(0, COL_GROUPS).data(Qt.DisplayRole)
    assert "T = 300 K" in text


def test_new_key_highlight_diff_on_refresh(qtbot):
    views = [_sv("tavi", 1, "d1")]
    ws = _ws(views)
    vm = ScansViewModel(ws)
    # First build highlights everything (prev set was empty).
    assert vm.model.highlighted_keys() == {"tavi:1"}
    # Add a scan and refresh: only the newly-appeared key is highlighted.
    ws.scans.append(_sv("tavi", 2, "d1"))
    vm.refresh()
    assert vm.model.highlighted_keys() == {"tavi:2"}

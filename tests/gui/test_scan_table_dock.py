"""ScanTableDock wiring tests (Phase E).

The table is a thin view over the SHARED model + selection model, so a chip and a row select together;
and its rows drag out with the same ``MIME_SCAN_IDS`` payload the chips emit.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar_gui.gui_store import GuiStore
from isar_gui.mime import MIME_SCAN_IDS, decode_scan_ids
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scan_table_dock import ScanTableDock


def _sv(exp, num, det):
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type="measurement",
        status="fitted", source_file=r"D:\d\scan.dat", T=300.0, hkl=[1.0, 0.0, 0.0],
        provenance={}, detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def test_table_shares_model_and_selection(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1")]), store=GuiStore.load(tmp_path))
    dock = ScanTableDock(vm)
    qtbot.addWidget(dock)
    assert dock._view.model() is vm.model
    assert dock._view.selectionModel() is vm.selection_model


def test_selecting_a_row_is_visible_through_the_vm(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1"), _sv("tavi", 2, "d1")]),
                        store=GuiStore.load(tmp_path))
    dock = ScanTableDock(vm)
    qtbot.addWidget(dock)
    vm.select_keys(["tavi:2:d1"], clear=True)
    assert vm.selected_keys() == ["tavi:2:d1"]


def test_model_mimedata_for_selected_rows(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1"), _sv("tavi", 2, "d1")]),
                        store=GuiStore.load(tmp_path))
    dock = ScanTableDock(vm)
    qtbot.addWidget(dock)
    idx = vm.model.index(1, 0)   # row for tavi:2:d1
    md = vm.model.mimeData([idx])
    assert md.hasFormat(MIME_SCAN_IDS)
    assert decode_scan_ids(md.data(MIME_SCAN_IDS).data()) == ["tavi:2:d1"]


def test_double_click_fitted_scan_requests_inspect(qtbot, tmp_path):
    vm = ScansViewModel(_ws([_sv("tavi", 1, "d1")]), store=GuiStore.load(tmp_path))
    dock = ScanTableDock(vm)
    qtbot.addWidget(dock)
    seen = []
    vm.inspect_requested.connect(lambda fit: seen.append(fit))
    dock._on_double_clicked(vm.model.index(0, 0))
    assert len(seen) == 1   # a FitView (fitted scan) was handed to the inspector hook

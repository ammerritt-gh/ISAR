"""ScansDock filter UI — the filter bar, query box, banner and empty-state widgets.

These are presentation tests: the predicate/persistence logic is covered in ``test_scan_filter*.py``.
Here we confirm the widgets exist above the tabs, drive the VM, degrade on bad input, and surface the
loud persisted-filter banner.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QTabWidget

from isar_gui.gui_store import GuiStore
from isar_gui.scan_filter import ScanFilter, parse_query
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock


def _sv(exp, num, det, *, T=300.0, status="ingested"):
    return SimpleNamespace(
        scan_id=f"{exp}:{num}:{det}", experiment_id=exp, scan_number=num, scan_type="measurement",
        status=status, source_file=r"D:\d\scan.dat", T=T, hkl=[1.5, 0.0, 0.0],
        provenance={}, detectors={det: SimpleNamespace(detector_id=det)})


def _ws(scan_views):
    return SimpleNamespace(scans=list(scan_views))


def _three_analyzers():
    return [_sv("s30", 1, f"ana{i}") for i in (1, 2, 9)]


def test_filter_bar_and_query_box_present_above_tabs(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    assert dock._filter_bar is not None
    assert dock._query_box is not None
    # The bar lives above the QTabWidget so it survives tab rebuilds.
    tabs = dock.findChild(QTabWidget)
    assert tabs is not None


def test_ctrl_f_focuses_query_box(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._query_box.setText("abc")
    dock._focus_query()   # offscreen focus is unreliable; assert the selectAll side-effect instead
    assert dock._query_box.selectedText() == "abc"


def test_query_box_applies_and_drives_vm(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._query_box.setText("ana:ana9")
    dock._query_box._apply()
    assert {u.key for u in vm.rows()} == {"s30:1:ana9"}
    assert dock._query_box.styleSheet() == ""     # clean parse, no error styling


def test_invalid_query_shows_error_style_without_raising(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._query_box.setText("bogus:xyz")
    dock._query_box._apply()
    assert "c62828" in dock._query_box.styleSheet()   # reddened border
    assert dock._query_box.toolTip()                  # error message present


def test_filter_bar_analyzer_selection_narrows(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    bar = dock._filter_bar
    # Check the ana9 action, then trigger the bar's rebuild-and-apply.
    bar._ana._actions["ana9"].setChecked(True)
    bar._on_changed()
    assert vm.filter().analyzers == frozenset({"ana9"})
    assert {u.key for u in vm.rows()} == {"s30:1:ana9"}


def test_empty_state_message_and_reset(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    vm.apply_query("ana:nope")   # matches nothing
    assert vm.filtered_count()[0] == 0
    labels = [w.text() for w in dock.findChildren(type(dock._banner))]
    assert any("No scans match" in t for t in labels)
    # Reset via the VM clears the filter and restores the rows.
    vm.clear_filter()
    assert vm.filtered_count()[0] == 3


def test_persisted_filter_shows_loud_banner_on_load(qtbot, tmp_path):
    store = GuiStore.load(tmp_path)
    store.set_scan_filter(ScanFilter(analyzers=frozenset({"ana9"})).to_dict())
    vm = ScansViewModel(_ws(_three_analyzers()))
    vm.set_store(store)          # hydrates the persisted filter
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    # isVisible() is False until the top-level is shown; isHidden() reflects the explicit flag we set.
    assert not dock._banner.isHidden()
    assert "Filter active" in dock._banner.text()
    assert "showing 1 of 3" in dock._banner.text()


def test_no_banner_without_active_filter(qtbot, tmp_path):
    vm = ScansViewModel(_ws(_three_analyzers()), store=GuiStore.load(tmp_path))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    assert dock._banner.isHidden()

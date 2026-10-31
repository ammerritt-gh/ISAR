"""Analyzer labels change through Config without changing spectrum identity."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMenu

import bl43_fixture
from bl43_fixture import KEY_A
from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scan_list import _ID_ROLE


KEY = f"{KEY_A}:a06"
LAYOUT = {"id": "test-row", "rows": [[f"a{n:02d}" for n in range(3, 10)]]}


@pytest.fixture
def numbered(qtbot, monkeypatch, bl43_ws_readonly):
    snapshot = bl43_fixture.sample_snapshot(bl43_ws_readonly)
    snapshot = replace(snapshot, sets=tuple(replace(ss, spectra=tuple(
        replace(sp, fit=None) for sp in ss.spectra)) for ss in snapshot.sets))
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **reads: snapshot)
    window = MainWindow(Workspace.open(bl43_ws_readonly), session=Session(), watch=False)
    qtbot.addWidget(window)
    window.resize(1108, 851)
    window.show()
    QApplication.processEvents()
    return window


def test_config_numbering_changes_labels_and_preserves_selection(numbered, tmp_path):
    window = numbered
    vm, panel = window.scan_list_vm, window.scan_list
    actions = window._scan_numbering_actions
    assert {a.text() for a in actions.values()} == {
        "Facility (a06)", "ISAR (A4)", "Both (A4 • a06)"}
    assert all(a.toolTip() for a in actions.values())
    assert actions["facility"].isChecked()
    menus = window.menuBar().findChildren(QMenu)
    config = next(m for m in menus if m.title() == "&Config")
    scans_list = next(m for m in menus if m.title() == "Scans list")
    numbering = next(m for m in menus if m.title() == "Analyzer numbering")
    assert scans_list.menuAction() in config.actions()
    assert numbering.menuAction() in scans_list.actions()
    assert set(numbering.actions()) == set(actions.values())
    vm.select(KEY)
    snapshot = vm.snapshot
    index = panel.index_for_key(KEY)
    assert index.data() == "a06"
    panel._view.expand(index.parent())
    before_scroll = panel._view.verticalScrollBar().value()
    for mode, label in (("isar", "A4"), ("both", "A4 • a06"), ("facility", "a06")):
        actions[mode].trigger()
        QApplication.processEvents()
        index = panel.index_for_key(KEY)
        assert index.data() == label
        assert "a06" in index.data(Qt.ItemDataRole.ToolTipRole)
        assert "A4" in index.data(Qt.ItemDataRole.ToolTipRole)
        assert vm.selection.keys == (KEY,) and vm.selection.active == KEY
        assert vm.snapshot is snapshot
        assert panel._view.isExpanded(index.parent())
        assert panel._view.verticalScrollBar().value() == before_scroll
        assert [name for name, action in actions.items() if action.isChecked()] == [mode]
        assert window._session.view_state["scan_list"]["analyzer_numbering"] == mode
    actions["both"].trigger()
    vm.set_sort("analyzer")
    QApplication.processEvents()
    assert panel._by_id["ana:a06"].text().startswith("A4 • a06 (")
    assert panel._by_id["ana:a06"].data(_ID_ROLE) == "ana:a06"
    saved = Session.from_dict(window._session.to_dict())
    restored = ScanListViewModel(vm.workspace)
    restored.load_view_state(saved)
    assert restored.analyzer_numbering == "both"
    restored.set_sort("analyzer")
    assert next(n for n in restored.tree() if n.id == "ana:a06").cells["scan"].text.startswith("A4 • a06 (")
    assert window.grab().save(str(tmp_path / "numbering-window.png"))


def test_numbering_filter_and_workspace_layout_refresh(numbered):
    vm = numbered.scan_list_vm
    vm.set_analyzer_numbering("both")
    for text in ("A4", "a06"):
        vm.set_filter(text)
        assert {sp.analyzer for _, spectra in vm._visible()[0] for sp in spectra} == {"a06"}
    vm.set_filter("")
    vm.set_workspace(SimpleNamespace(card=SimpleNamespace(policy=SimpleNamespace(
        instrument="Test", analyzer_layouts=[{"id": "reverse", "rows": [list(reversed(LAYOUT["rows"][0]))]}]))))
    assert vm._analyzer_cell("a06").text == "A4 • a06"
    assert vm._analyzer_cell("a03").text == "A7 • a03"


@pytest.mark.parametrize("layouts, analyzer", [([], "a06"), ([LAYOUT, LAYOUT], "a06"), ([LAYOUT], "a99")])
def test_unmapped_analyzer_keeps_facility_name(qtbot, monkeypatch, bl43_ws_readonly, layouts, analyzer):
    snapshot = bl43_fixture.sample_snapshot(bl43_ws_readonly)
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **reads: snapshot)
    ws = SimpleNamespace(card=SimpleNamespace(policy=SimpleNamespace(instrument="Test", analyzer_layouts=layouts)))
    vm = ScanListViewModel(ws)
    for mode in ("isar", "both"):
        vm.set_analyzer_numbering(mode)
        cell = vm._analyzer_cell(analyzer)
        assert cell.text == analyzer
        assert "ISAR numbering unavailable" in cell.tooltip


def test_older_or_invalid_saved_numbering_defaults_to_facility(numbered):
    vm = numbered.scan_list_vm
    for state in ({}, {"analyzer_numbering": "unknown"}):
        restored = ScanListViewModel(vm.workspace)
        restored.load_view_state(Session(view_state={"scan_list": state}))
        assert restored.analyzer_numbering == "facility"

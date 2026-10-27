"""Amendment 7: the Fast pass and Deep pass columns switch on by themselves when the first result of that pass
exists, and a hand choice is remembered and no longer overridden. Generated data (the BL43 fixture), offscreen Qt."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

import bl43_fixture as fx
from isar.workspace_api import Workspace
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scan_list import ScanListPanel

A03, A04, A06 = (f"{fx.KEY_A}:{d}" for d in ("a03", "a04", "a06"))


def _vm(root) -> ScanListViewModel:
    return ScanListViewModel(Workspace.open(root))


def _after(vm, root) -> None:
    """The window's re-read after the engine wrote results."""
    vm.set_workspace(Workspace.open(root))


@pytest.fixture
def bare(tmp_path):
    """An experiment whose spectra were read but never fitted."""
    return fx.build_bl43_workspace(tmp_path, fits=False)


def test_result_columns_are_off_without_results_and_on_once_a_result_of_that_pass_exists(qtbot, bare):
    vm = _vm(bare)
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    assert "fast" not in vm.visible_columns() and "deep" not in vm.visible_columns()
    fx.stamp_fit(bare, A03, "resolved")                           # a fast-pass record, no deep pass
    _after(vm, bare)
    assert "fast" in vm.visible_columns() and "deep" not in vm.visible_columns()
    assert panel._column_actions["fast"].isChecked() and not panel._column_actions["deep"].isChecked()
    fx.stamp_fit(bare, A04, "close_call", deep=fx._DEEP_RESOLVED)
    _after(vm, bare)
    assert "deep" in vm.visible_columns()
    assert "slit" not in vm.visible_columns() and "file" not in vm.visible_columns()   # only the result columns


def test_a_column_the_user_switched_off_stays_off_when_another_result_arrives_and_across_sessions(qtbot, bare):
    fx.stamp_fit(bare, A03, "resolved")
    vm = _vm(bare)
    assert "fast" in vm.visible_columns()
    vm.set_column_visible("fast", False)                          # by hand
    assert "fast" not in vm.visible_columns()
    fx.stamp_fit(bare, A06, "search_limited")                     # another result arrives
    _after(vm, bare)
    assert "fast" not in vm.visible_columns()
    session = SimpleNamespace(view_state={})
    vm.save_view_state(session)
    again = _vm(bare)
    again.load_view_state(session)
    assert "fast" not in again.visible_columns()                  # remembered: the auto rule does not override it
    again.set_column_visible("fast", True)                        # and switching it on by hand works
    assert "fast" in again.visible_columns()
    fresh = _vm(bare)                                             # an auto-on column is not itself persisted
    auto = SimpleNamespace(view_state={})
    _vm(bare).save_view_state(auto)
    fresh.load_view_state(auto)
    assert auto.view_state["scan_list"]["columns"] == [] and "fast" in fresh.visible_columns()

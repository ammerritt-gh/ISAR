"""Browser tests: BrowserViewModel derives the group/member tree; BrowserDock renders it and forwards
selection; MainWindow wires it in and can swap the whole workspace via load_workspace."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.browser import BrowserViewModel
from isar_gui.views.browser_dock import BrowserDock

_MVP16 = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")

_ROWS = [
    {"scan": 1, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0,
     "status": "fitted", "verdict": "resolved", "omega0": 5.0, "interval_lo": 4.5,
     "interval_hi": 5.5, "interval_reliable": True},
    {"scan": 2, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0,
     "status": "fitted", "verdict": "resolved", "omega0": 5.1, "interval_lo": 4.6,
     "interval_hi": 5.6, "interval_reliable": False},
    {"scan": 3, "detector": "det", "sample": "tavi", "T": 300.0, "h": 3.0, "k": 0.0, "l": 0.0,
     "status": "fitted", "verdict": "resolved", "omega0": 6.0, "interval_lo": 5.5,
     "interval_hi": 6.5, "interval_reliable": True},
]


def _open(make_ws, rows=_ROWS):
    root = make_ws(rows=rows)
    return Workspace.open(root), root


# ---- BrowserViewModel -------------------------------------------------------
def test_vm_group_rows_match_ws_groups(make_ws):
    ws, _ = _open(make_ws)
    vm = BrowserViewModel(ws)
    rows = vm.group_rows
    assert len(rows) == len(ws.groups)
    total_members = sum(len(r.members) for r in rows)
    assert total_members == sum(len(g.members) for g in ws.groups)
    # two scans share (tavi, q=(2,0,0), T=200) -> one group with 2 members
    two_member = [r for r in rows if len(r.members) == 2]
    assert len(two_member) == 1


def test_vm_card_summary_fields(make_ws):
    ws, root = _open(make_ws)
    vm = BrowserViewModel(ws)
    summary = vm.card_summary
    assert summary["facility"] == "TAVI"
    assert summary["instrument"] == "PUMA"
    assert summary["plugin"] == "tavi"
    assert summary["counting_mode"] == "monitor"
    assert summary["collation_keys"] == ["sample", "q", "T"]
    assert summary["schema_version"] == ws.schema_version
    assert summary["lattice_epochs"] == 0
    assert summary["workspace_path"] == str(root)


def test_vm_selection_changed_emits_fitview(qtbot, make_ws):
    ws, _ = _open(make_ws)
    vm = BrowserViewModel(ws)
    target = ws.results[0]
    with qtbot.waitSignal(vm.selection_changed, timeout=1000) as blocker:
        vm.select(target)
    assert blocker.args == [target]


# ---- BrowserDock ------------------------------------------------------------
def test_dock_renders_groups_and_children(qtbot, make_ws):
    ws, _ = _open(make_ws)
    vm = BrowserViewModel(ws)
    dock = BrowserDock(vm)
    qtbot.addWidget(dock)
    tree = dock._tree
    assert tree.topLevelItemCount() == len(vm.group_rows)
    total_children = sum(tree.topLevelItem(i).childCount() for i in range(tree.topLevelItemCount()))
    assert total_children == len(ws.results)


def test_dock_selecting_child_fires_vm_signal(qtbot, make_ws):
    ws, _ = _open(make_ws)
    vm = BrowserViewModel(ws)
    dock = BrowserDock(vm)
    qtbot.addWidget(dock)
    tree = dock._tree
    group_item = tree.topLevelItem(0)
    assert group_item.childCount() > 0
    child = group_item.child(0)
    with qtbot.waitSignal(vm.selection_changed, timeout=1000):
        tree.setCurrentItem(child)


def test_dock_unreliable_member_shows_suffix_no_numeric_interval(qtbot, make_ws):
    ws, _ = _open(make_ws)
    vm = BrowserViewModel(ws)
    dock = BrowserDock(vm)
    qtbot.addWidget(dock)
    tree = dock._tree

    found_unreliable = False
    for i in range(tree.topLevelItemCount()):
        group_item = tree.topLevelItem(i)
        for j in range(group_item.childCount()):
            child = group_item.child(j)
            fv = child.data(0, Qt.ItemDataRole.UserRole)
            texts = [child.text(c) for c in range(child.columnCount())]
            if fv.interval_reliable is False:
                found_unreliable = True
                assert "(unreliable)" in texts[2]
            # never render a numeric interval bound anywhere in the row
            for bad in ("4.5", "5.5", "4.6", "5.6"):
                assert all(bad not in t for t in texts)
    assert found_unreliable


# ---- MainWindow wiring -------------------------------------------------------
def test_main_window_registers_browser_dock(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert "Browser" in w._docks
    assert w._docks["Browser"] is w.browser_dock
    assert w.browser_vm is not None
    w.show()
    assert w.browser_dock.isVisible()
    assert "select a scan in the browser" in w.centralWidget().text().lower()


def test_main_window_browser_selection_available(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    target = ws.results[0]
    with qtbot.waitSignal(w.browser_vm.selection_changed, timeout=1000):
        w.browser_vm.select(target)


# ---- load_workspace ----------------------------------------------------------
def test_load_workspace_swaps_tree(qtbot, make_ws):
    ws1, root1 = _open(make_ws, rows=_ROWS[:1])
    w = MainWindow(ws1, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert sum(w.browser_dock._tree.topLevelItem(i).childCount()
               for i in range(w.browser_dock._tree.topLevelItemCount())) == 1

    root2 = make_ws(rows=_ROWS)
    ok = w.load_workspace(root2)
    assert ok is True
    assert w.windowTitle() == f"ISAR — {root2.name}"
    assert sum(w.browser_dock._tree.topLevelItem(i).childCount()
               for i in range(w.browser_dock._tree.topLevelItemCount())) == len(_ROWS)
    assert str(root2) in w._session.recent_workspaces


def test_load_workspace_twice_no_menu_leak(qtbot, make_ws):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QMenu

    ws1, _ = _open(make_ws, rows=_ROWS[:1])
    w = MainWindow(ws1, session=Session(), watch=False)
    qtbot.addWidget(w)

    def _menu_count() -> int:
        # Flush deferred deletes (deleteLater) so torn-down menus are actually gone before counting.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return len(w.menuBar().findChildren(QMenu))

    baseline = _menu_count()  # File + Docks
    w.load_workspace(make_ws(rows=_ROWS))
    after_first = _menu_count()
    w.load_workspace(make_ws(rows=_ROWS[:2]))
    after_second = _menu_count()

    # A leak would grow the QMenu count by one per load; the count must be stable instead.
    assert after_first == baseline
    assert after_second == baseline
    # And the tree reflects the LAST workspace (2 rows -> 2 members across its groups).
    assert sum(w.browser_dock._tree.topLevelItem(i).childCount()
               for i in range(w.browser_dock._tree.topLevelItemCount())) == 2


def test_load_workspace_invalid_path_keeps_old_and_reports_status(qtbot, make_ws, tmp_path):
    ws1, root1 = _open(make_ws, rows=_ROWS[:1])
    w = MainWindow(ws1, session=Session(), watch=False)
    qtbot.addWidget(w)

    bad = tmp_path / "not_a_workspace"
    bad.mkdir()
    ok = w.load_workspace(bad)
    assert ok is False
    assert w._ws is ws1
    assert w.windowTitle() == f"ISAR — {root1.name}"
    assert "not" in w.statusBar().currentMessage().lower()


# ---- MVP-16 smoke (read-only) -------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen baseline not present")
def test_mvp16_browser_smoke():
    ws = Workspace.open(_MVP16)
    vm = BrowserViewModel(ws)
    rows = vm.group_rows
    assert len(rows) > 0
    assert sum(len(r.members) for r in rows) > 0

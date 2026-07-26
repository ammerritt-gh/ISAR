"""Browser tests: BrowserViewModel derives the group/member tree; BrowserDock renders it and forwards
selection; MainWindow wires it in and can swap the whole workspace via load_workspace."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar.workspace_api import Workspace
from isar_gui.commands import GuiIngestReport
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.browser import BrowserViewModel
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.browser_dock import BrowserDock
from synthetic_ws import scan_entry, write_session

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


def test_pooled_master_is_derived_child_not_physical_member(qtbot, make_ws):
    rows = [dict(_ROWS[0]), dict(_ROWS[1]), {
        "scan": 9_000_000, "detector": "det", "sample": "tavi", "T": 200.0,
        "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "verdict": "resolved",
        "omega0": 5.05, "interval_lo": 4.8, "interval_hi": 5.3,
        "interval_reliable": True, "pooled": True, "n_members": 2,
        "members": "[1, 2]",
    }]
    ws, _ = _open(make_ws, rows=rows)
    scan_views = [
        SimpleNamespace(scan_id=f"tavi:{n}:det", experiment_id="tavi", scan_number=n,
                        scan_type="measurement", status="fitted", source_file=None, T=200.0,
                        hkl=[2, 0, 0], provenance={},
                        detectors={"det": SimpleNamespace(pooled=pooled)})
        for n, pooled in ((1, False), (2, False), (9_000_000, True))
    ]
    scan_vm = ScansViewModel(SimpleNamespace(scans=scan_views))
    assert [r.scan_number for r in scan_vm.rows()] == [1, 2]
    physical_results = [fv for fv in ws.results if not fv.pooled]
    pooled = SimpleNamespace(
        pooled=True, members=[["tavi", 1, "det"], ["tavi", 2, "det"]],
        scan_number=9_000_000, detector_id="det", verdict="resolved", omega0=5.05,
        interval_reliable=True, interval=(4.8, 5.3), disposition="resolved",
        q=[2.0, 0.0, 0.0], T=200.0,
    )
    browser_ws = SimpleNamespace(results=physical_results + [pooled], groups=ws.groups,
                                 warnings=[], card=ws.card, root=ws.root,
                                 schema_version=ws.schema_version)
    vm = BrowserViewModel(browser_ws)
    group = next(r for r in vm.group_rows if len(r.members) == 2)
    assert [m.scan_number for m in group.members] == [1, 2]
    assert len(group.derived) == 1 and group.derived[0].scan_number == 9_000_000

    dock = BrowserDock(vm)
    qtbot.addWidget(dock)
    item = next(dock._tree.topLevelItem(i) for i in range(dock._tree.topLevelItemCount())
                if dock._tree.topLevelItem(i).childCount() == 3)
    derived = item.child(2)
    assert derived.text(0) == "Derived pooled result"
    assert derived.data(0, Qt.ItemDataRole.UserRole).pooled is True


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


def test_dock_explains_that_parsed_scans_need_analysis(qtbot, make_ws):
    """An empty Results tree must not look like a failed first ingest.

    Results deliberately remains a fit-only tree; raw ledger scans belong in Scans/Scan Table.
    """
    root = make_ws(rows=[])
    vm = BrowserViewModel(Workspace.open(root))
    dock = BrowserDock(vm)
    qtbot.addWidget(dock)

    assert dock._tree.topLevelItemCount() == 0
    assert not dock._tree.isVisible()
    assert not dock._no_results.isHidden()
    assert "No displayable Results yet" in dock._no_results.text()
    assert "Scans" in dock._no_results.text()
    assert "Run" in dock._no_results.text()

    # An empty display tree is not proof that the reducer made no records: keep that distinction
    # explicit when records exist but cannot be represented as browser group rows.
    vm._ws = SimpleNamespace(results=[object()], scans=[])
    assert "Analysis records exist" in dock._no_results_text()


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
    # The C2 inspector is the CenterHost's "scan" page, empty until a scan is selected.
    from isar_gui.views.inspector_widget import InspectorWidget
    assert isinstance(w._center_host.pane("scan"), InspectorWidget)
    assert w._center_host.pane("scan") is w.inspector
    assert w.inspector_vm.is_empty


def test_main_window_browser_selection_available(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    target = ws.results[0]
    with qtbot.waitSignal(w.browser_vm.selection_changed, timeout=1000):
        w.browser_vm.select(target)


def test_successful_ingest_refreshes_scans_and_keeps_results_fit_only(qtbot, make_ws):
    """A new workspace must immediately expose parsed scans even without WatchBridge.

    This exercises the real post-ingest slot after a facade cache was populated while the workspace
    was empty.  The Results tree remains empty until analysis writes fit results.
    """
    root = make_ws(rows=[])
    ws = Workspace.open(root)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    assert w.scans_vm.rows() == []  # populate the old empty facade cache before the write

    scan_id = "tavi:41:det"
    write_session(
        root,
        scans={scan_id: scan_entry(41, status="ingested")},
        files={"C:/incoming/scan41.dat": {
            "format": "measurement", "n_scans": 1, "scan_ids": [scan_id], "n_context": 0,
        }},
    )
    report = GuiIngestReport(
        files=({"path": "C:/incoming/scan41.dat", "scan_ids": [scan_id]},),
        added=(scan_id,),
    )

    w._on_ingest_report(report)

    assert [unit.key for unit in w.scans_vm.rows()] == [scan_id]
    assert w.browser_dock._tree.topLevelItemCount() == 0
    assert not w.browser_dock._no_results.isHidden()
    assert "No displayable Results yet" in w.browser_dock._no_results.text()
    assert w.scans_dock.isVisible()
    assert "1 analyzer unit(s) awaiting analysis" in w.statusBar().currentMessage()


def _legacy_unit(exp, scan, det):
    return SimpleNamespace(key=f"{exp}:{scan}:{det}", scan_number=scan, det=det)


def test_legacy_fit_browser_highlight_restores_after_rebuild_when_ledger_is_unique(
        qtbot, make_ws, monkeypatch):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    target = ws.results[0]
    assert target.unit_id is None  # frozen legacy fixture deliberately exercises the fallback
    monkeypatch.setattr(w.scans_vm, "rows",
                        lambda: [_legacy_unit("tavi", target.scan_number, target.detector_id)])

    w._inspect_fit(target)
    expected = f"tavi:{target.scan_number}:{target.detector_id}"
    assert w.active_units.key == expected
    assert w.browser_dock._tree.currentItem() is not None

    # Results-tree rendering must restore the same active row without emitting a recursive selection.
    w.browser_vm.refresh()
    current = w.browser_dock._tree.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole).scan_number == target.scan_number


def test_legacy_fit_never_maps_ambiguous_scan_detector_to_browser_or_active_unit(
        qtbot, make_ws, monkeypatch):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    target = ws.results[0]
    monkeypatch.setattr(
        w.scans_vm, "rows",
        lambda: [_legacy_unit("run_a", target.scan_number, target.detector_id),
                 _legacy_unit("run_b", target.scan_number, target.detector_id)])

    w._inspect_fit(target)
    assert w.active_units.key is None
    assert w.browser_dock._tree.currentItem() is None


# ---- load_workspace ----------------------------------------------------------
def test_load_workspace_swaps_tree(qtbot, make_ws, tmp_path, monkeypatch):
    import tempfile

    # note_workspace now refuses recents under the OS temp dir (defense against a leaked pytest
    # tmp_path polluting the real session). These synthetic workspaces live under tmp_path, so point
    # the perceived temp dir elsewhere to let root2 stand in for a real, recordable workspace.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "_not_temp"))

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
    """Repeated load_workspace must not grow the menu bar.

    The invariant is read off the menu bar's own action list, which ``removeAction()`` updates
    synchronously — no deferred-delete flush is needed. Two traps this test used to fall into, both
    established by bisection; do not "simplify" them back:

    * The previous version counted ``menuBar().findChildren(QMenu)`` behind a process-wide
      ``QApplication.sendPostedEvents(None, DeferredDelete)`` drain, because a Docks menu awaiting
      deletion is still a child of the menu bar. That drain hard-crashed the interpreter: forcing
      pending destruction while ordinary posted events were still queued destroyed a ScansDock whose
      zero-delay scrollbar-restore timer was still armed (fixed at its source in
      ``scans_dock._restore_master_view_state``). The object-level count was never the invariant
      anyway — a dead-but-pending QMenu is legitimate Qt state, not a leak.
    * Reading ``action.menu()`` off ``menuBar().actions()`` destroys the C++ QMenu in this PySide6
      build when the ``menuBar()`` wrapper is a temporary, leaving ``MainWindow._docks_menu`` dangling
      and breaking the very rebind under test. ``action.text()`` is the menu title for every menu-bar
      action and needs no wrapper for the menu itself.
    """
    ws1, _ = _open(make_ws, rows=_ROWS[:1])
    w = MainWindow(ws1, session=Session(), watch=False)
    qtbot.addWidget(w)

    def _menu_titles() -> list[str]:
        return [a.text() for a in w.menuBar().actions() if a.text()]

    baseline = _menu_titles()  # File + Help + Run + Backends + View + Docks
    assert "&Docks" in baseline
    # A leak adds one Docks menu per load; the titles must be identical instead. Four loads, so a
    # leak that only shows up after the first rebind still gets caught.
    for rows in (_ROWS, _ROWS[:2], _ROWS, _ROWS[:2]):
        assert w.load_workspace(make_ws(rows=rows)) is True
        assert _menu_titles() == baseline
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

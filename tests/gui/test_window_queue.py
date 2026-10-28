"""The window's wiring of the analysis queue (offscreen Qt, the real MainWindow and widgets).

The queue controller's own behaviour is in ``test_queue_controller.py``; the entry point's five routes and its
greyed reasons are in ``test_run_entry.py``. This file holds what only the window does: it builds the one controller
and hands it to the panel, the rail and the scan list, binds it to each experiment, feeds it arrivals, keeps the
automatic switch with the experiment, holds its starts while a campaign runs, and owns the cores setting.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QInputDialog

from isar.workspace_api import Workspace
from isar_gui import main_window as mw_mod
from isar_gui.commands import GuiIngestReport
from isar_gui.gui_store import GuiStore
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from synthetic_ws import scan_entry, write_session

WAIT_MS = 15000


def _win(qtbot, root=None, session=None):
    w = MainWindow(None, session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    if root is not None:
        with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
            assert w.load_workspace(str(root))
    return w


# ---- one controller, handed to every view -------------------------------------------------------
def test_the_window_builds_one_controller_and_hands_it_to_the_panel_the_rail_and_the_scan_list(qtbot):
    w = _win(qtbot)
    assert w.processing_panel._queue._controller is w._queue
    assert w.processing_rail._controller is w._queue
    assert w.scan_list_vm._queue is w._queue
    assert w.processing_panel._queue.isVisibleTo(w.processing_panel)       # the queue is shown, no spacer in its place
    assert not hasattr(w, "_launch_run") and not hasattr(w, "rerun")        # no batch path beside the queue
    assert not hasattr(w._operations, "request_run")


def test_the_controller_is_bound_to_each_experiment_and_to_none(qtbot, bl43_ws):
    w = _win(qtbot)
    assert w._queue._root is None
    w = _win(qtbot, bl43_ws)
    assert w._queue._root == bl43_ws and w._queue.view().bound
    assert w._queue.auto_fast()                                            # a new experiment starts with the switch on


def test_a_campaign_holds_the_queue_and_the_window_pokes_it_when_the_operations_change(qtbot, monkeypatch):
    w = _win(qtbot)
    assert w._queue._blocked() is None
    monkeypatch.setattr(w._operations, "has_campaign", lambda: True)
    assert w._queue._blocked() == "a campaign is running in this experiment"
    pokes = []
    monkeypatch.setattr(w._queue, "poke", lambda: pokes.append(1))
    w._on_operation_activity_changed({"kind": "drive", "queued": 0, "cancelling": False})
    w._on_operation_activity_changed({"kind": None, "queued": 0, "cancelling": False})
    assert pokes == [1, 1]


# ---- the automatic switch belongs to the experiment ---------------------------------------------
def test_the_automatic_switch_is_saved_with_the_experiment_and_read_back_on_open(qtbot, bl43_ws):
    w = _win(qtbot, bl43_ws)
    assert w._gui_store.auto_fast_pass() is True
    w._queue.set_auto_fast(False)
    assert w._gui_store.auto_fast_pass() is False and w._store_save_timer.isActive()
    w._flush_gui_store_if_pending()
    assert GuiStore.load(bl43_ws).auto_fast_pass() is False                # on disk, beside the watched folders
    other = _win(qtbot, bl43_ws)                                           # the next time the experiment is opened
    assert other._queue.auto_fast() is False
    other._queue.set_auto_fast(True)
    other._flush_gui_store_if_pending()
    assert GuiStore.load(bl43_ws).auto_fast_pass() is True


# ---- arrivals ----------------------------------------------------------------------------------
def test_new_and_updated_scans_are_handed_to_the_queue_after_the_ingest_refresh(qtbot, monkeypatch, make_ws):
    root = make_ws(rows=[])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    seen = []
    monkeypatch.setattr(w._queue, "on_arrivals", lambda ids: seen.append((list(ids), len(w.scan_list_vm.snapshot.sets))))
    scan_id = "tavi:41:det"
    write_session(root, scans={scan_id: scan_entry(41, status="ingested")},
                  files={"C:/incoming/scan41.dat": {"format": "measurement", "n_scans": 1, "scan_ids": [scan_id],
                                                    "n_context": 0}})
    w._on_ingest_report(GuiIngestReport(files=({"path": "C:/incoming/scan41.dat", "scan_ids": [scan_id]},),
                                        added=(scan_id,), updated=("tavi:42:det",)))
    assert seen == [([scan_id, "tavi:42:det"], 1)]                         # the scan list had already re-read


@pytest.mark.parametrize("auto, tail", [(True, "their fast passes are queued in Processing"),
                                        (False, "add them to the queue in Processing")])
def test_the_ingest_status_says_what_the_switch_did(qtbot, make_ws, auto, tail):
    root = make_ws(rows=[])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w._queue.set_auto_fast(auto)
    scan_id = "tavi:41:det"
    write_session(root, scans={scan_id: scan_entry(41, status="ingested")},
                  files={"C:/incoming/scan41.dat": {"format": "measurement", "n_scans": 1, "scan_ids": [scan_id],
                                                    "n_context": 0}})
    w._on_ingest_report(GuiIngestReport(files=({"path": "C:/incoming/scan41.dat", "scan_ids": [scan_id]},),
                                        added=(scan_id,)))
    assert f"1 analyzer unit(s) awaiting analysis; {tail}" in w.statusBar().currentMessage()
    assert "Run analysis" not in w.statusBar().currentMessage()


# ---- Config > Cores ------------------------------------------------------------------------------
def _cpu() -> int:
    return max(1, os.cpu_count() or 1)


def test_the_cores_default_is_all_the_machines_cores_but_two_and_the_menu_says_so(qtbot):
    w = _win(qtbot)
    n = max(1, _cpu() - 2)
    assert w._queue.view().cores == n
    assert w._cores_action.text() == f"Cores for analysis… ({n} of {_cpu()})"
    assert w._cores_action.toolTip() and "keeps its cores" in w._cores_action.toolTip()
    assert "&Config" in [m.title() for m in w.menuBar().findChildren(type(w._view_menu))]
    assert "cores" not in w._session.view_state                             # nothing is saved until the user chooses


def test_choosing_cores_saves_it_for_this_machine_and_reaches_the_controller(qtbot, monkeypatch):
    session = Session()
    w = _win(qtbot, session=session)
    asked = []

    def ask(parent, title, label, value, low, high):
        asked.append((title, label, value, low, high))
        return 1, True
    monkeypatch.setattr(QInputDialog, "getInt", ask)
    w._cores_action.trigger()
    title, label, value, low, high = asked[0]
    assert (low, high, value) == (1, _cpu(), max(1, _cpu() - 2))
    assert "lower number takes effect as running analyzers finish" in label and "until you stop it" in label
    assert session.view_state["cores"] == 1 and w._queue.view().cores == 1
    assert w._cores_action.text() == f"Cores for analysis… (1 of {_cpu()})"
    assert "Cores for analysis: 1" in w.log_dock._view.toPlainText()
    next_window = _win(qtbot, session=session)                              # the setting is remembered
    assert next_window._queue.view().cores == 1


def test_cancelling_or_keeping_the_same_number_changes_nothing(qtbot, monkeypatch):
    session = Session()
    w = _win(qtbot, session=session)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (1, False))
    w._cores_action.trigger()
    monkeypatch.setattr(QInputDialog, "getInt", lambda parent, title, label, value, *a, **k: (value, True))
    w._cores_action.trigger()
    assert "cores" not in session.view_state and w._queue.view().cores == max(1, _cpu() - 2)


def test_a_saved_number_above_this_machines_cores_is_clamped_and_a_bad_one_ignored(qtbot):
    assert _win(qtbot, session=Session(view_state={"cores": 10_000}))._queue.view().cores == _cpu()
    for bad in (0, -3, "4", True, 2.5):
        assert _win(qtbot, session=Session(view_state={"cores": bad}))._queue.view().cores == max(1, _cpu() - 2), bad


def test_the_run_menu_has_the_entry_point_and_cancel_and_nothing_else(qtbot):
    w = _win(qtbot)
    run_menu = next(m for m in w.menuBar().findChildren(type(w._view_menu)) if m.title() == "&Run")
    texts = [a.text() for a in run_menu.actions()]
    assert texts[0].startswith("Fast-pass all ready") and texts[1:] == ["Cancel active operation"]
    assert all(a.toolTip() for a in run_menu.actions())
    assert mw_mod._CORES_TIP


# ---- an unsaved Card draft is resolved before work is queued (hard rule 7) -----------------------
@pytest.mark.parametrize("answer, queued", [(False, 0), (True, 1)])
def test_start_analysis_asks_about_an_unsaved_card_draft_first_and_cancel_queues_nothing(
        qtbot, monkeypatch, bl43_ws, answer, queued):
    w = _win(qtbot, bl43_ws)
    asked, added = [], []
    monkeypatch.setattr(w.card_dock, "resolve_dirty", lambda action: asked.append(action) or answer)
    monkeypatch.setattr(w._queue, "fast_pass_all_ready", lambda: added.append(1))
    w.start_analysis()
    assert asked == ["start analysis"] and len(added) == queued

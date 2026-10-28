"""The window's wiring of the analysis queue (offscreen Qt, the real MainWindow and widgets).

The queue controller's own behaviour is in ``test_queue_controller.py``; the entry point's five routes and its
greyed reasons are in ``test_run_entry.py``. This file holds what only the window does: it builds the one controller
and hands it to the panel, the rail and the scan list, binds it to each experiment, feeds it arrivals, keeps the
automatic switch with the experiment, holds its starts while a campaign runs, and owns the cores setting. It also
holds the folder flows: leaving asks first and stops the analysis process in order, Save copy and Export take the
writer hold, Rename waits for the queue, a campaign is refused while the queue writes, and a saved result refreshes
the shown analyzer only. Work is queued against the scripted stand-in service (``fake_analysis_service.py``).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QInputDialog

from isar.workspace_api import Workspace
from isar_gui import main_window as mw_mod
from isar_gui import queue_controller as qc
from isar_gui.commands import DriveOptions, GuiIngestReport
from isar_gui.gui_store import GuiStore
from isar_gui.main_window import MainWindow
from isar_gui.queue_controller import LeaveSummary
from isar_gui.session import Session
from isar_gui.views.rename_dialog import RenameDialog
from synthetic_ws import scan_entry, write_session

WAIT_MS = 15000
FAKE = Path(__file__).with_name("fake_analysis_service.py")


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


# ---- the folder flows, against the stand-in service -------------------------------------------------------
def _fake_service(monkeypatch, tmp_path) -> Path:
    """Run the queue's service as the scripted stand-in; returns the file it records the commands in."""
    record = tmp_path / "commands.jsonl"
    record.write_text("", encoding="utf-8")
    monkeypatch.setattr(qc, "_service_argv", lambda py, root, log: [
        sys.executable, str(FAKE), "--workspace", str(root), "--log", str(log), "--record", str(record)])
    return record


def _commands(record: Path, op: str) -> list[dict]:
    lines = [json.loads(ln) for ln in record.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [c for c in lines if c["op"] == op]


def _emit(w, *events: dict) -> None:
    assert w._queue._send({"op": "_emit", "lines": list(events)})


def _forget_queue(w) -> None:
    """Leave no process and no queued work behind: closing the window at teardown then asks nothing."""
    w._queue._drop_service()
    w._queue.bind(None, auto_fast=True)


def _one_running(qtbot, w, record) -> dict:
    """Cores 1, Fast-pass all ready: one item runs (its ``start`` command is returned), the rest wait."""
    w._queue.set_cores(1)
    qtbot.waitUntil(lambda: bool(w._queue.fast_all()[0]), timeout=WAIT_MS)
    w.start_analysis()
    qtbot.waitUntil(lambda: bool(_commands(record, "start")) and w._queue.view().n_running["fast"] == 1
                    and any(i.started_at for g in w._queue.view().fast for i in g.items), timeout=WAIT_MS)
    assert sum(w._queue.view().n_waiting.values()) >= 1
    return _commands(record, "start")[0]


def _finish(qtbot, w, start: dict) -> None:
    """The running item's result is saved and its process is gone."""
    iid, key = start["item_id"], start["unit_key"]
    _emit(w, {"ev": "result", "item_id": iid, "unit_key": key, "pass": "fast", "status": "done", "reason": None,
              "saving": True},
          {"ev": "saved", "item_id": iid, "unit_key": key, "pass": "fast", "status": "done", "reason": None},
          {"ev": "finished", "item_id": iid, "end": "completed", "reason": None})
    qtbot.waitUntil(lambda: w._queue.writes_busy() is None, timeout=WAIT_MS)


def test_the_leave_question_names_what_is_lost():
    assert mw_mod._leave_text(LeaveSummary(5, 1, 2040.0, 14, 0), "before ISAR closes") == (
        "5 analyzers are running (one deep pass, 34 min in) and 14 are waiting. Leaving stops the running "
        "analyzers, discarding their work, and forgets the waiting ones.")
    assert mw_mod._leave_text(LeaveSummary(1, 0, None, 0, 2), "before ISAR closes") == (
        "1 analyzer is running and 2 finished results are being saved. Leaving stops the running analyzer, "
        "discarding its work. Every finished result is saved before ISAR closes.")
    assert mw_mod._leave_text(LeaveSummary(0, 0, None, 1, 0), "before ISAR closes") == (
        "1 analyzer is waiting. Leaving forgets the waiting ones.")


@pytest.mark.parametrize("verb", ["close", "open", "new"])
def test_leaving_with_queued_work_asks_keep_changes_nothing_and_stop_waits_for_the_process(
        qtbot, monkeypatch, bl43_ws, make_ws, tmp_path, verb):
    record = _fake_service(monkeypatch, tmp_path)
    other = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = _win(qtbot, bl43_ws)
    leave = {"close": w.close, "open": lambda: w.load_workspace(str(other)), "new": w.new_workspace}[verb]
    try:
        _one_running(qtbot, w, record)
        asked, answer = [], [False]
        monkeypatch.setattr(mw_mod, "_confirm_leave",
                            lambda parent, title, text, stay, go: asked.append((title, text, stay, go)) or answer[0])
        leave()                                                            # Keep: everything goes on
        title, text, stay, go = asked[0]
        assert (title, stay, go) == {"close": ("Close ISAR?", "Keep ISAR open", "Stop and close"),
                                     "open": ("Open another experiment?", "Stay in this experiment", "Stop and open"),
                                     "new": ("Start a new experiment?", "Stay in this experiment",
                                             "Stop and start a new one")}[verb]
        assert text.startswith("1 analyzer is running and ") and "forgets the waiting ones" in text
        assert w.isVisible() and w._wsm.root() == bl43_ws and w._queue.view().n_running["fast"] == 1
        assert not _commands(record, "shutdown")
        answer[0] = True
        leave()                                                            # Stop: the process is asked to end first
        assert w._wsm.root() == bl43_ws and _commands(record, "shutdown")
        if verb == "close":
            qtbot.waitUntil(lambda: not w.isVisible(), timeout=WAIT_MS)
        else:
            qtbot.waitUntil(lambda: w._wsm.root() != bl43_ws, timeout=WAIT_MS)
            assert (w._wsm.root() == other) == (verb == "open")
        assert len(asked) == 2 and w._queue.leave_summary() is None       # the re-entry did not ask again
    finally:
        _forget_queue(w)


def test_leaving_with_an_empty_queue_asks_nothing(qtbot, monkeypatch, bl43_ws):
    w = _win(qtbot, bl43_ws)

    def ask(*_a):
        raise AssertionError("the leave question was asked with nothing queued")
    monkeypatch.setattr(mw_mod, "_confirm_leave", ask)
    w.new_workspace()
    assert w._wsm.root() != bl43_ws


def test_a_copy_waits_for_the_writer_hold_and_releases_it_after(qtbot, monkeypatch, bl43_ws, tmp_path):
    record = _fake_service(monkeypatch, tmp_path)
    w = _win(qtbot, bl43_ws)
    dest = tmp_path / "copies" / "kept"
    monkeypatch.setattr(mw_mod.file_dialogs, "get_save_file_name", lambda *_a, **_k: (str(dest), ""))
    try:
        _one_running(qtbot, w, record)
        w.save_copy_as()
        qtbot.waitUntil(lambda: bool(_commands(record, "hold_writes")), timeout=WAIT_MS)
        assert not dest.exists() and "waiting for the result being saved" in w.statusBar().currentMessage()
        _emit(w, {"ev": "writes_held"})                                    # the writer has stopped between saves
        qtbot.waitUntil(lambda: (dest / "card.policy.toml").is_file(), timeout=WAIT_MS)
        qtbot.waitUntil(lambda: bool(_commands(record, "release_writes")), timeout=WAIT_MS)
        assert w._queue._holds == 0 and w._queue.view().n_running["fast"] == 1
    finally:
        _forget_queue(w)


def test_an_export_holds_the_writer_until_its_first_progress_line(qtbot, monkeypatch, bl43_ws):
    w = _win(qtbot, bl43_ws)
    started = []
    monkeypatch.setattr(w._export, "start", lambda root: started.append(root) or True)
    w.output_bar.export_requested.emit()
    assert started == [bl43_ws] and w._queue._holds == 1                   # no process: the hold is immediate
    w._export.progress.emit("Exporting: spectrum 1 of 3")
    assert w._queue._holds == 0
    w._export.progress.emit("Exporting: spectrum 2 of 3")                  # released once, never twice
    w._export.finished.emit(bl43_ws)
    assert w._queue._holds == 0 and not w._export_hold
    monkeypatch.setattr(w._export, "start", lambda root: False)            # an export that does not start
    w.output_bar.export_requested.emit()
    assert w._queue._holds == 0 and w.statusBar().currentMessage() == "Export did not start"


def test_rename_is_refused_while_an_item_runs_and_works_once_it_has_finished(
        qtbot, monkeypatch, bl43_ws, tmp_path):
    record = _fake_service(monkeypatch, tmp_path)
    w = _win(qtbot, bl43_ws)
    names = []
    monkeypatch.setattr(RenameDialog, "ask", lambda root, ws=None, parent=None: names.append(root) or "renamed")
    try:
        start = _one_running(qtbot, w, record)
        w.rename_experiment()
        assert names == [] and "Rename refused: analysis is running: press Pause" in w.statusBar().currentMessage()
        w._queue.pause()
        _finish(qtbot, w, start)
        w.rename_experiment()                                              # the idle process is stopped first
        new = bl43_ws.parent / "renamed"
        qtbot.waitUntil(lambda: w._wsm.root() == new, timeout=WAIT_MS)
        assert _commands(record, "shutdown") and not bl43_ws.exists()
    finally:
        _forget_queue(w)


def test_a_rename_keeps_the_waiting_items(qtbot, monkeypatch, bl43_ws):
    w = _win(qtbot, bl43_ws)
    monkeypatch.setattr(RenameDialog, "ask", lambda root, ws=None, parent=None: "renamed")
    w._queue.pause()                                                       # nothing starts: no process
    qtbot.waitUntil(lambda: bool(w._queue.fast_all()[0]), timeout=WAIT_MS)
    w.start_analysis()
    try:
        qtbot.waitUntil(lambda: sum(w._queue.view().n_waiting.values()) > 0, timeout=WAIT_MS)
        waiting = sum(w._queue.view().n_waiting.values())
        assert w._queue.view().service == "off"
        w.rename_experiment()
        assert w._wsm.root() == bl43_ws.parent / "renamed"
        assert sum(w._queue.view().n_waiting.values()) == waiting and w._queue.view().paused
    finally:
        _forget_queue(w)

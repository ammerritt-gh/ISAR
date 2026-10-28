"""Amendment 7 / 8: one window-level entry point for starting an analysis, and the Run button in the setup area.

Offscreen Qt, the real MainWindow. The entry point queues "Fast-pass all ready" (the analysis queue job): every
ready analyzer file without a current fast pass joins the fast queue. The geometry of the button at 1108x851 and
1920x1040 is in ``test_stage2_walkthrough.py`` (it needs the BL43 intake fixture); the BL43 readiness states are in
its scenario 3; the queue's own behaviour is in ``test_queue_controller.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from isar_gui import queue_controller as qc
from isar_gui.main_window import MainWindow
from isar_gui.session import Session

FAKE = Path(__file__).with_name("fake_analysis_service.py")
WAIT_MS = 15000


def _win(qtbot, root=None):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    if root is not None:
        with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
            assert w.load_workspace(str(root))
    return w


def _fake_service(monkeypatch, tmp_path):
    """Run the queue's service as the scripted stand-in; returns the file the stand-in records commands in."""
    record = tmp_path / "commands.jsonl"
    record.write_text("", encoding="utf-8")
    monkeypatch.setattr(qc, "_service_argv", lambda py, root, log: [
        sys.executable, str(FAKE), "--workspace", str(root), "--log", str(log), "--record", str(record)])
    return record


def _has_a_count(qtbot, w):
    qtbot.waitUntil(lambda: w._queue.fast_all()[0] and w.setup_section._run.isEnabled(), timeout=WAIT_MS)


def test_every_way_to_start_an_analysis_goes_through_start_analysis(qtbot, bl43_ws, monkeypatch):
    calls = []
    monkeypatch.setattr(MainWindow, "start_analysis", lambda self: calls.append("start"))
    w = _win(qtbot, bl43_ws)
    _has_a_count(qtbot, w)
    assert w.setup_section._run.isEnabled() and w.processing_panel._run.isEnabled() and w._run_action.isEnabled()
    w._run_action.trigger()                                  # Run > Fast-pass all ready
    w.processing_panel._run.click()                          # the Processing panel's button
    w.setup_section._run.click()                             # the visible button in the setup area
    w.readiness_line.run_requested.emit()                    # the readiness list's "Add to the fast queue"
    w.attention_list.run_requested.emit()                    # the attention group's "Fast-pass all ready"
    assert calls == ["start"] * 5


def test_start_analysis_queues_the_ready_analyzers_once(qtbot, bl43_ws, monkeypatch, tmp_path):
    record = _fake_service(monkeypatch, tmp_path)
    w = _win(qtbot, bl43_ws)
    _has_a_count(qtbot, w)
    count = w._queue.fast_all()[0]
    label = f"Fast-pass all ready ({count})"
    assert w.setup_section._run.full_text() == w.processing_panel._run.full_text() == label
    assert w._run_action.text() == f"Fast-pass all ready ({count})"
    try:
        w.start_analysis()
        view = w._queue.view()
        assert sum(view.n_running.values()) + sum(view.n_waiting.values()) == count
        assert all(i.pass_kind == "fast" and not i.automatic for g in view.fast for i in g.items)
        assert not view.deep
        qtbot.waitUntil(lambda: record.read_text(encoding="utf-8").count('"op": "start"') > 0, timeout=WAIT_MS)
        # the same press again adds nothing: every ready file is queued, so the count is 0 and the button says so
        w.start_analysis()
        view = w._queue.view()
        assert sum(view.n_running.values()) + sum(view.n_waiting.values()) == count
        assert w._queue.fast_all()[0] == 0
        assert w.setup_section._run.full_text() == "Fast-pass all ready (0)" and not w.setup_section._run.isEnabled()
        assert w.setup_section._run_note.full_text() == (
            "Every ready analyzer already has a current fast pass or is queued.")
        log = w.log_dock._view.toPlainText()
        assert "Queued for a fast pass" in log and "Not added:" in log and "a fast pass" in log.split("Not added:")[-1]
    finally:
        w._queue._drop_service()
        w._queue.bind(None, auto_fast=True)     # forgotten: closing the window then asks nothing


def test_the_run_button_is_off_with_its_reason_in_text_until_something_can_run(qtbot, bl43_ws, monkeypatch):
    w = _win(qtbot)                                          # no experiment
    run, note = w.setup_section._run, w.setup_section._run_note
    assert not run.isEnabled() and note.isVisible() and note.full_text() == "Open or create an experiment first."
    assert w._run_action.text() == "Fast-pass all ready — Open or create an experiment first"
    w = _win(qtbot, bl43_ws)                                 # data, and something to queue
    _has_a_count(qtbot, w)
    run, note = w.setup_section._run, w.setup_section._run_note
    assert run.isEnabled() and run.toolTip()
    w._operations.is_busy = lambda: True                     # an ingest or export holds the writer gate: adding needs none
    w._sync_run_actions()
    assert run.isEnabled() and w._run_action.isEnabled()
    monkeypatch.setattr(w._queue, "fast_all", lambda: (0, None))        # everything ready is done or queued
    w._sync_run_actions()
    done = "Every ready analyzer already has a current fast pass or is queued."
    assert not run.isEnabled() and note.full_text() == done
    assert run.toolTip() == note.full_text()                 # the same words on hover
    assert w._run_action.text() == f"Fast-pass all ready (0) — {done.rstrip('.')}"
    # visible beside the button, unless the amber curves line (which then says what is wrong) already shows
    assert note.isVisible() or w.setup_section._problem.isVisible()
    w.setup_section._problem.set_line(None)
    w.setup_section._fit_run_note()
    assert note.isVisible()
    monkeypatch.setattr(w._queue, "fast_all", lambda: (None, "checking which analyzers are ready"))
    w._sync_run_actions()
    assert not run.isEnabled() and note.full_text() == "Checking which analyzers are ready."
    assert w.processing_panel._run.full_text() == "Fast-pass all ready"     # no count to give

"""The queue panel's inter-spectra batch rows and counts (S1U8, P2): one row per batch with its own progress, the
done / failed / not-reached counts, the Stop and Remove actions, and the batch wording in the footer and the rail.

Offscreen Qt with the real widgets, the same stand-in controller as ``test_queue_panel.py``. The fixture clock is
``NOW``; no dialog is left unmocked (a batch's Stop asks nothing, so the stop-confirm modal must never open).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QLabel, QWidget

from gui.test_queue_panel import NOW, Stub, button, panel, qview, texts, walk_hovers
from isar_gui.queue_controller import BatchView
from isar_gui.theme import QUEUE_STATES
from isar_gui.views import queue_panel as qp
from isar_gui.views.queue_panel import batch_progress_text, rail_status

RING = {"stage": "ring", "index": 2, "of": 6, "done": 31, "failed": 1, "not_reached": 4, "fixed": 2, "total": 56}
SWEEP = {"stage": "sweep", "index": 3, "of": 20, "done": 40, "failed": 0, "not_reached": 0, "fixed": 0, "total": 56}
TITLE = "Inter-spectra batch, started 14:32 · anchored, spread in q · 56 spectra"


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(qp, "_now", lambda: NOW)
    monkeypatch.setattr(qp._currentness, "deep_calibration_note", lambda: None)


@pytest.fixture
def no_dialog(monkeypatch):
    """A batch's Stop and Remove ask nothing: a confirm dialog opening here is a failure."""
    def refuse(*args):
        raise AssertionError("a batch action opened a confirm dialog")
    monkeypatch.setattr(qp, "_confirm_stop", refuse)


def batch(n, *, status="running", kind="anchored", progress=None, reason=None, not_reached=(), started=NOW - 70,
          step=NOW - 5):
    live = status in ("running", "saving")
    return BatchView(
        item_id=f"q0-b{n}-inter", batch_id=f"b{n:012x}", label="Inter-spectra batch, started 14:32", kind=kind,
        direction="q" if kind == "anchored" else None, status=status, reason=reason, n_total=56,
        cores=2 if status == "running" else None, progress=progress, not_reached_reasons=tuple(not_reached),
        started_at=None if status == "waiting" else started, step_started_at=step if live else None,
        finished_at=None if status in ("waiting", "running", "saving") else NOW)


def bview(*batches, **kw):
    """A view whose inter counts agree with the batches, as the controller's do."""
    running = sum(b.status == "running" for b in batches)
    waiting = sum(b.status == "waiting" for b in batches)
    return qview(batches=batches, n_running={"fast": 0, "deep": 0, "inter": running},
                 n_waiting={"fast": 0, "deep": 0, "inter": waiting}, **kw)


def batch_rows(p, *batches):
    return [p._queue.findChild(QWidget, f"queue_batch_{b.item_id}") for b in batches]


# ---- the words: one line per batch, and its hover says each count -----------------------------------------
@pytest.mark.parametrize("b, expect", [
    (batch(1, progress=RING), "ring 2 of 6 · 31 of 56 done · 1 failed · 4 not reached · 0:05"),
    (batch(2, kind="simultaneous", progress=SWEEP), "sweep 3 (up to 20) · 40 of 56 done · 0:05"),
    (batch(3, progress={"stage": "prepare", "done": 0}, step=None), "preparing · 0 of 56 done"),
    (batch(4, progress=None, step=None), "starting"),
    (batch(5, status="done_warning", progress={"stage": "sweep", "index": 9, "of": 20, "done": 54, "failed": 2,
                                               "not_reached": 0}), "54 of 56 done · 2 failed"),
    (batch(6, status="stopped", progress=RING), "31 of 56 done · 1 failed · 4 not reached"),
])
def test_a_batch_line_says_its_stage_and_counts(b, expect):
    text, tip = batch_progress_text(b, NOW)
    assert text == expect and tip
    assert "%" not in text and "stalled" not in text.lower()


def test_each_count_and_its_reasons_are_said_in_the_hover():
    done = batch(7, status="done", progress=dict(SWEEP, done=40, failed=2, not_reached=4),
                 not_reached=(("no accepted hkl", 4),))
    text, tip = batch_progress_text(done, NOW)
    assert text == "40 of 56 done · 2 failed · 4 not reached"
    for word in ("done:", "failed:", "not reached:", "Anchors and locked spectra", "no accepted hkl"):
        assert word in tip
    assert "sweep" not in text and "ring" not in text          # a finished batch says its outcome, not its stage


# ---- the panel --------------------------------------------------------------------------------------------
def test_no_batch_section_and_no_empty_heading_without_batches(qtbot):
    p = panel(qtbot, Stub(qview()))
    assert p._queue.findChild(QWidget, "queue_inter_heading") is None
    assert texts(p, "queue_batch_title") == []


def test_every_batch_state_is_one_row_with_its_word_its_line_and_its_reason(qtbot):
    states = [batch(1, status="waiting", reason="waits for fast passes of its spectra"),
              batch(2, progress=RING), batch(3, status="saving", progress=SWEEP, kind="simultaneous"),
              batch(4, status="done_warning", progress=SWEEP, reason="a neighbour did not settle in time"),
              batch(5, status="failed", progress=RING, reason="the result could not be saved: disk full"),
              batch(6, status="stopped", progress=RING, reason="stopped by you; completed rings are kept")]
    p = panel(qtbot, Stub(bview(*states)))
    assert texts(p, "queue_inter_heading_counts") == ["1 running · 1 waiting"]
    rows = batch_rows(p, *states)
    assert all(row is not None for row in rows)
    assert texts(p, "queue_batch_title") == [TITLE, TITLE, TITLE.replace("anchored, spread in q", "simultaneous"),
                                             TITLE, TITLE, TITLE]        # named by its start time, never "Batch 2"
    assert texts(p, "queue_batch_word") == [QUEUE_STATES[s][0] for s in
                                            ("waiting", "running", "saving", "done_warning", "failed", "stopped")]
    assert texts(p, "queue_batch_progress") == [
        "waits for fast passes of its spectra",
        "ring 2 of 6 · 31 of 56 done · 1 failed · 4 not reached · 0:05",
        "sweep 3 (up to 20) · 40 of 56 done",
        "40 of 56 done",
        "31 of 56 done · 1 failed · 4 not reached",
        "31 of 56 done · 1 failed · 4 not reached"]
    assert texts(p, "queue_batch_reason") == ["a neighbour did not settle in time",
                                              "the result could not be saved: disk full",
                                              "stopped by you; completed rings are kept"]


def test_stop_asks_nothing_and_sends_the_batch_item_id(qtbot, no_dialog):
    running = batch(2, progress=RING)
    stub = Stub(bview(running))
    p = panel(qtbot, stub)
    stop = button(p, "queue_batch_stop")
    assert "rings or sweeps already saved are kept" in stop.toolTip()
    assert stop.accessibleName() == f"Stop {running.label}"
    stop.click()
    assert stub.calls == [("stop", running.item_id)]


def test_a_waiting_batch_is_removed_not_stopped(qtbot, no_dialog):
    waiting = batch(3, status="waiting", reason="waits for fast passes of its spectra")
    stub = Stub(bview(waiting))
    p = panel(qtbot, stub)
    assert not p._queue.findChildren(QWidget, "queue_batch_stop")
    button(p, "queue_batch_remove").click()
    assert stub.calls == [("remove", waiting.item_id)]


def test_the_counts_name_batches_in_the_footer_and_the_rail(qtbot):
    p = panel(qtbot, Stub(bview(batch(2, progress=RING))))
    assert texts(p, "queue_footer_text") == ["1 batch running on 5 cores"]
    assert rail_status(bview(batch(2, progress=RING))) == ("1 running", "")
    failed = bview(batch(5, status="failed", progress=RING, reason="x"), n_look=1)
    assert rail_status(failed) == ("", "1 needs a look")
    both = qview(n_running={"fast": 2, "deep": 0, "inter": 1}, n_waiting={"fast": 0, "deep": 0, "inter": 0},
                 batches=(batch(2, progress=RING),))
    assert texts(panel(qtbot, Stub(both)), "queue_footer_text") == ["2 analyzers and 1 batch running on 5 cores"]


def test_the_batch_line_and_its_hover_tick_while_it_runs(qtbot, monkeypatch):
    b = batch(2, progress=RING)
    p = panel(qtbot, Stub(bview(b)))
    assert "ring 2 of 6 · 31 of 56 done · 1 failed · 4 not reached · 0:05" in texts(p, "queue_batch_progress")
    monkeypatch.setattr(qp, "_now", lambda: NOW + 65)
    p._queue._tick_labels()
    assert "ring 2 of 6 · 31 of 56 done · 1 failed · 4 not reached · 1:10" in texts(p, "queue_batch_progress")
    assert "2:15" in {w.text() for w in p.findChildren(QLabel)}          # the batch's total time, from its start


def test_a_progress_only_change_keeps_the_batch_row_and_updates_its_line(qtbot):
    stub = Stub(bview(batch(2, progress=RING)))
    p = panel(qtbot, stub)
    row = batch_rows(p, batch(2))[0]
    stub.set(bview(batch(2, progress=dict(RING, index=3, done=44, failed=0, not_reached=0))))
    p._queue.refresh()
    assert batch_rows(p, batch(2))[0] is row
    assert texts(p, "queue_batch_progress") == ["ring 3 of 6 · 44 of 56 done · 0:05"]


def test_the_whole_batch_panel_has_a_hover_on_every_visible_control_and_word(qtbot, no_dialog):
    states = [batch(1, status="waiting", reason="waits for fast passes of its spectra"), batch(2, progress=RING),
              batch(3, status="failed", progress=RING, reason="x", not_reached=(("no accepted hkl", 4),))]
    p = panel(qtbot, Stub(bview(*states, n_look=1)))
    assert walk_hovers(p._queue) == []

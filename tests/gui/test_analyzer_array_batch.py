"""The batch marks on the Analyzer array dock (offscreen Qt, real widgets, made-up keys and data).

A fake controller (a QObject with ``members_changed`` and ``batch_members()``) drives the real view model and dock the
way the window's queue does; one test replays the member events through the real ``QueueController`` and its
stand-in service. Spectrum keys are the generated workspaces'; nothing here is a measured value (hard rule 10).
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtWidgets import QApplication

from bl43_fixture import KEY_A
from gui.array_helpers import hostile_state, inter_row, open_dock
from gui.inter_fakes import FakeInterState, make_overview
from gui.test_analyzer_array_window import (LAPTOP, LARGE, _check_cells_paint_inside, _children_inside, _open,
                                            _select, _unclipped, _window, analyzer, ws28)  # noqa: F401 (fixture)
from gui.window_frame_checks import assert_elided_have_tooltips
from isar_gui.queue_controller import BatchMembers
from isar_gui.theme import Tone, dot_icon
from isar_gui.viewmodels.analyzer_array import BATCH_STATES, BatchMark
from isar_gui.views.analyzer_cell import BATCH_TONES
from scans_view_fixture import SPECTRA_A, SPECTRA_B

A3, A4, A5, A6, A7, A8, A9 = SPECTRA_A
B0 = SPECTRA_B[0]
SHOTS = os.environ.get("ISAR_ARRAY_SHOTS")
LONG = "the runner said this and it goes on " * 5          # 180 characters


class FakeQueue(QObject):
    """The two members of the controller the dock reads (C-members-event); any other call is an AttributeError."""
    members_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.m: BatchMembers | None = None

    def batch_members(self):
        return self.m

    def put(self, m: BatchMembers | None) -> None:
        self.m = m
        self.members_changed.emit("" if m is None else m.item_id)


def members(states, *, reasons=None, kind="anchored", direction="q", stage="ring", index=2, of=3, ended=None,
            ended_reason=None) -> BatchMembers:
    counts = ({} if stage is None else {
        "done": sum(s == "done" for s in states.values()), "failed": sum(s == "failed" for s in states.values()),
        "not_reached": sum(s == "not_reached" for s in states.values()),
        "fixed": sum(s == "held" for s in states.values()), "total": len(states)})
    return BatchMembers("q1", "b0123456789a", kind, direction, stage, index, of, None, counts, dict(states),
                        dict(reasons or {}), ended, ended_reason)


STATES = {A3: "held", A4: "held", A5: "waiting", A6: "fitting", A7: "done", A8: "refused", A9: "failed",
          B0: "waiting", SPECTRA_B[1]: "not_reached"}
REASONS = {A8: "locked since the batch started", A9: "did not converge", SPECTRA_B[1]: "cut off by a failed neighbour"}


@pytest.fixture
def env(qtbot, scans_view_ws_readonly):
    inter = FakeInterState()
    inter.overview = make_overview(by_key={A3: inter_row(A3, locked=True, locked_fit_id="fast"),
                                           A4: inter_row(A4)}, anchors=(A4,))
    e = open_dock(qtbot, scans_view_ws_readonly, inter=inter)
    e.queue, e.inter = FakeQueue(), inter
    e.vm.follow_queue(e.queue)
    return e


def _settle(env) -> None:
    env.dock.flush_redraw()
    QApplication.processEvents()


def _show(env, *keys, **kw) -> None:
    env.selection.set(keys, **kw)
    _settle(env)


def _put(env, m) -> None:
    env.queue.put(m)
    _settle(env)


def _dot(cell):
    return cell.state_dot.pixmap().toImage()


def test_a_cell_goes_waiting_then_fitting_now_then_done_and_others_end_apart(env):
    d = env.dock
    _show(env, A6)
    assert all(c.batch_state is None and c.state_caption.isHidden() and not c.ringing for c in d.cells.values())
    assert not d.ring_timer.isActive() and d.batch_label.isHidden()          # nothing animates, nothing is said, no batch
    _put(env, members({**STATES, A6: "waiting"}, reasons=REASONS))
    cell = d.cell_at("a06")
    assert (cell.state_caption.full_text(), cell.dimmed, cell.ringing) == ("waiting", True, False)
    assert cell.veil.isVisible() and cell.veil.geometry() == cell.plot_box.rect()    # the plot is veiled, not the word
    assert "worker does not have it yet" in cell.state_caption.toolTip()
    spot = cell.plot_box.mapTo(cell, cell.plot_box.rect().center())
    veiled = cell.grab().toImage().pixelColor(spot)
    assert not d.ring_timer.isActive()
    _put(env, members(STATES, reasons=REASONS))
    assert (cell.state_caption.full_text(), cell.dimmed, cell.ringing) == ("fitting now", False, True)
    assert cell.veil.isHidden() and d.ring_timer.isActive()
    assert cell.grab().toImage().pixelColor(spot) != veiled                    # the veil really painted over the plot
    assert "worker has this spectrum now" in cell.state_caption.toolTip()
    _put(env, members({**STATES, A6: "done"}, reasons=REASONS))
    assert (cell.state_caption.full_text(), cell.dimmed, cell.ringing) == ("done", False, False)
    assert not d.ring_timer.isActive()                                        # the ring stops with the work
    assert "the new fit replaces the drawn one once ISAR reloads it" in cell.state_caption.toolTip()
    # every word comes with its tone as a dot, and the three that stand apart name the runner's reason
    for name, state in (("a05", "waiting"), ("a06", "done"), ("a07", "done"), ("a08", "refused"), ("a09", "failed")):
        c = d.cell_at(name)
        assert c.state_dot.isVisible() and _dot(c) == dot_icon(BATCH_TONES[state]).pixmap(10, 10).toImage()
    assert d.cell_at("a08").state_caption.full_text() == "refused"
    assert "locked since the batch started" in d.cell_at("a08").state_caption.toolTip()
    assert d.cell_at("a09").state_caption.full_text() == "failed"
    assert "did not converge" in d.cell_at("a09").state_caption.toolTip()
    assert "did not converge" in d.cell_at("a09").toolTip()
    assert BATCH_TONES["refused"] is Tone.WARN and BATCH_TONES["failed"] is Tone.ERR
    assert BATCH_TONES["fitting"] is Tone.ACTIVE and BATCH_TONES["not_reached"] is Tone.IDLE
    assert set(BATCH_TONES) == set(BATCH_STATES)
    assert "batch: failed" in d.cell_at("a09").accessibleDescription()
    assert env.inter.calls == []                                              # read-only: no writer was called


def test_a_not_reached_member_says_so_with_the_reason_on_hover(env):
    _show(env, SPECTRA_B[0])
    _put(env, members(STATES, reasons=REASONS))
    d = env.dock
    names = [n for n, c in d.cells.items() if c.key == SPECTRA_B[1]]
    assert names and d.cell_at(names[0]).state_caption.full_text() == "not reached"
    assert "cut off by a failed neighbour" in d.cell_at(names[0]).state_caption.toolTip()


def test_held_members_keep_their_outline_and_carry_no_batch_word(env):
    _show(env, A6)
    _put(env, members(STATES, reasons=REASONS))
    d = env.dock
    assert (d.cell_at("a03").outline_kind, d.cell_at("a04").outline_kind) == ("lock", "anchor")
    for name in ("a03", "a04"):
        c = d.cell_at(name)
        assert c.state_caption.isHidden() and c.state_dot.isHidden() and c.batch_state is None
        assert not c.dimmed and not c.ringing and "batch" not in c.accessibleDescription()


def test_the_header_line_counts_members_elsewhere_and_says_how_the_batch_ended(env):
    d = env.dock
    _show(env, A6)
    _put(env, members(STATES, reasons=REASONS))
    # 1 done of 9 members less 2 held; B0 and B1 are in the other scan
    assert d.batch_label.isVisible()
    assert d.batch_label.full_text() == "Anchored batch, spread in q · ring 2 · 1 of 7 done · 2 in other scans"
    assert "waiting 2 · fitting now 1" in d.batch_label.toolTip() and "2 anchors and locks" in d.batch_label.toolTip()
    _put(env, members(STATES, reasons=REASONS, ended="stopped", ended_reason="you stopped it"))
    assert d.batch_label.full_text().endswith("· 2 in other scans · stopped")
    assert "you stopped it" in d.batch_label.toolTip()
    _put(env, members({A5: "done"}, kind="simultaneous", direction=None, stage="sweep", index=3, of=20, ended="failed"))
    assert d.batch_label.full_text() == "Simultaneous batch · sweep 3 · 1 of 1 done · failed"
    assert d.cell_at("a05").state_caption.full_text() == "done"             # the marks stay after the end


def test_the_first_sweep_is_never_called_sweep_0(env):
    _show(env, A6)
    _put(env, members({A5: "waiting", A6: "waiting"}, kind="simultaneous", direction=None, stage="sweep", index=0, of=20))
    text = env.dock.batch_label.full_text()
    assert "sweep 1 starting" in text and "sweep 0" not in text and "0 of 2 done" in text


def test_switching_scans_away_and_back_restores_the_marks(env):
    d = env.dock
    _show(env, A6)
    _put(env, members(STATES, reasons=REASONS))
    first = {n: c.batch_state for n, c in d.cells.items()}
    assert first["a06"] == "fitting" and d.ring_timer.isActive()
    _show(env, B0)                                                           # the other scan: its own members
    shown = {n: c.batch_state for n, c in d.cells.items() if c.batch_state}
    assert set(shown.values()) == {"waiting", "not_reached"} and d.cell_at("a06").batch_state is None
    assert not d.ring_timer.isActive()                                       # nothing rings in the scan on show
    assert d.batch_label.full_text().endswith("· 5 in other scans")          # A5..A9 are elsewhere now
    _show(env, A6)
    assert {n: c.batch_state for n, c in d.cells.items()} == first and d.ring_timer.isActive()


def test_the_next_batch_clears_the_marks_and_says_starting_until_its_first_event(env):
    d = env.dock
    _show(env, A6)
    _put(env, members(STATES, reasons=REASONS))
    _put(env, members({}, kind="simultaneous", direction=None, stage=None, index=None, of=None))   # started, no event yet
    assert all(c.batch_state is None and c.state_caption.isHidden() for c in d.cells.values())
    assert d.batch_label.full_text() == "Simultaneous batch · starting" and not d.ring_timer.isActive()
    _put(env, members({A5: "fitting"}, kind="simultaneous", direction=None, stage="sweep", index=1, of=20))
    assert d.cell_at("a05").batch_state == "fitting" and d.ring_timer.isActive()
    _put(env, None)                                                          # a rebind: no batch in this bind
    assert d.batch_label.isHidden() and all(c.batch_state is None for c in d.cells.values())
    assert not d.ring_timer.isActive()


def test_the_ring_runs_only_while_a_visible_cell_fits_and_a_stopped_ring_is_still_painted(env):
    d = env.dock
    _show(env, A6)
    _put(env, members(STATES, reasons=REASONS))
    cell = d.cell_at("a06")
    assert d.ring_timer.isActive() and d.ring_timer.interval() >= 100          # slow
    phases = set()
    for _ in range(12):
        d._ring_tick()
        phases.add(round(cell.ring_phase, 3))
    assert len(phases) > 3 and all(0.0 <= p <= 1.0 for p in phases)
    d.ring_timer.stop()                                                      # the timer off: the ring is still there
    y = cell.height() // 2
    ring_px = cell.grab().toImage().pixelColor(2, y)                         # the same cell, ring on ...
    d.hide()
    assert not d.ring_timer.isActive()
    d.show()
    assert d.ring_timer.isActive()
    d.apply(replace(d.vm.state, cells=tuple(replace(c, batch=None) for c in d.vm.state.cells)))
    assert not d.ring_timer.isActive() and not any(c.ringing for c in d.cells.values())
    assert ring_px != cell.grab().toImage().pixelColor(2, y)                 # ... and off, same selection


def test_a_members_event_never_redraws_a_curve(env):
    d = env.dock
    _show(env, A6)
    before = {n: c.redraws for n, c in d.cells.items()}
    for state in ("waiting", "fitting", "done"):
        _put(env, members({**STATES, A6: state}, reasons=REASONS))
    assert {n: c.redraws for n, c in d.cells.items()} == before


def test_the_dock_reads_the_member_map_again_when_it_opens_mid_batch(qtbot, scans_view_ws_readonly):
    e = open_dock(qtbot, scans_view_ws_readonly)
    q = FakeQueue()
    q.m = members(STATES, reasons=REASONS)                                    # a batch already running
    e.vm.follow_queue(q)
    _show(e, A6)
    assert e.dock.cell_at("a06").state_caption.full_text() == "fitting now" and e.dock.ring_timer.isActive()


def test_the_real_controller_drives_a_cell_through_waiting_fitting_and_done(qtbot, monkeypatch, tmp_path,
                                                                           scans_view_ws_readonly):
    from gui.test_queue_controller import Rig
    from gui.test_queue_members import finished_event, members_event, start
    e = open_dock(qtbot, scans_view_ws_readonly)      # first: the rig patches the module-level scan_list read
    r = Rig(qtbot, monkeypatch, tmp_path, cores=2)
    try:
        e.vm.follow_queue(r.ctrl)
        _show(e, A6)
        iid = start(r, monkeypatch, (A5, A6, A7))
        _settle(e)
        assert e.dock.batch_label.full_text() == "Anchored batch, spread in q · starting"
        assert all(c.batch_state is None for c in e.dock.cells.values())
        for states, word in (({A5: "waiting", A6: "waiting", A7: "waiting"}, "waiting"),
                             ({A5: "fitting", A6: "waiting", A7: "waiting"}, "waiting"),
                             ({A5: "done", A6: "fitting", A7: "waiting"}, "fitting now"),
                             ({A5: "done", A6: "done", A7: "fitting"}, "done")):
            r.emit(members_event(iid, states))
            r.wait(lambda s=states: dict(r.ctrl.batch_members().states) == s)
            _settle(e)
            assert e.dock.cell_at("a06").state_caption.full_text() == word
        assert e.dock.ring_timer.isActive()                                   # A7 is fitting
        r.emit(finished_event(iid, "completed"))
        r.wait(lambda: r.ctrl.batch_members().ended)
        _settle(e)
        assert e.dock.batch_label.full_text().endswith("· finished") and e.dock.cell_at("a06").batch_state == "done"
        r.ctrl.clear_finished()                                               # Clear finished keeps the marks
        _settle(e)
        assert e.dock.cell_at("a06").batch_state == "done" and e.dock.batch_label.isVisible()
        iid2 = start(r, monkeypatch, (A5, A6))                                # the next batch's start clears them
        _settle(e)
        assert e.dock.cell_at("a06").batch_state is None and e.dock.batch_label.full_text().endswith("starting")
    finally:
        r.ctrl._drop_service()


def _hostile_with_marks():
    state = hostile_state()
    cells = tuple(replace(c, batch=None if c.anchor or c.lock else BatchMark(
        BATCH_STATES[i % len(BATCH_STATES)], LONG if i % 2 else None)) for i, c in enumerate(state.cells))
    return replace(state, cells=cells)


@pytest.mark.parametrize("size", [(480, 420), (1060, 800), LARGE], ids=["small", "default", "large"])
def test_a_caption_and_a_long_reason_in_every_cell_clip_nothing(qtbot, scans_view_ws_readonly, size):
    dock = open_dock(qtbot, scans_view_ws_readonly, size=size).dock
    dock.flush_redraw()
    dock.apply(_hostile_with_marks())
    QApplication.processEvents()
    assert not _children_inside(dock)
    assert_elided_have_tooltips(dock)
    assert dock.ring_timer.isActive()
    for cell in dock.cells.values():
        assert cell.rect().contains(cell.name_row.geometry())
        if cell.state_caption.isVisible():
            assert cell.name_row.rect().contains(cell.state_caption.geometry())
            assert cell.name_row.rect().contains(cell.state_dot.geometry())
            assert cell.state_caption.full_text() in cell.state_caption.toolTip()
        assert cell.absence_label.text() == "not zeroed"


@pytest.mark.parametrize("size", [LAPTOP, LARGE], ids=["laptop", "large"])
def test_composed_in_the_real_window_with_a_mark_on_every_cell(qtbot, monkeypatch, ws28, size):
    w = _window(qtbot, monkeypatch, size, ws28)
    assert w.array_vm._queue is w._queue                                      # the one wiring line
    inter = FakeInterState()
    a3, a4, a6 = (analyzer(KEY_A, n) for n in (3, 4, 6))
    inter.overview = make_overview(by_key={a3: inter_row(a3, locked=True, locked_fit_id="fast")}, anchors=(a4,))
    w.array_vm.set_inter(inter)
    w.scan_list_vm.set_inter(inter)
    dock = _open(w, qtbot)
    _select(w, a6)
    keys = [c.key for c in dock.cells.values()]
    assert len(keys) == 28
    states = {k: BATCH_STATES[i % len(BATCH_STATES)] for i, k in enumerate(keys)}
    states[a3] = states[a4] = "held"
    states[a6] = "fitting"
    reasons = {k: LONG for k, s in states.items() if s in ("refused", "failed", "not_reached")}
    m = members(states, reasons=reasons)
    monkeypatch.setattr(w._queue, "batch_members", lambda: m)
    w._queue.members_changed.emit("q1")
    _select(w, a6)
    assert dock.cell_at("a06").ringing and dock.ring_timer.isActive()
    assert dock.cell_at("a03").state_caption.isHidden() and dock.cell_at("a04").outline_kind == "anchor"
    _check_cells_paint_inside(dock)
    for cell in dock.cells.values():
        if cell.state_caption.isVisible():
            assert cell.name_row.rect().contains(cell.state_caption.geometry())
        if cell.ringing:                      # the ring (inset 2 px) lies inside the cell
            assert cell.rect().adjusted(2, 2, -2, -2).isValid()
    _unclipped(dock)
    assert_elided_have_tooltips(dock)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        dock.grab().save(str(Path(SHOTS) / f"batch_{size[0]}x{size[1]}.png"))

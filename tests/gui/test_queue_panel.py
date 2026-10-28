"""The Processing panel's queue view and the rail (S2U2), offscreen Qt with the real widgets.

A small stand-in for the queue controller emits the ``QueueView`` / ``Selection`` the panel reads; what the
panel sends back is recorded in ``calls``. The 3 AM threshold is checked by a walk over the widget tree: every
visible button, switch and label has a hover. No dialog is left unmocked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QRegularExpression, Qt, Signal
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QScrollArea, QWidget

import isar_gui.status_words as sw
from isar.workspace_api import scan_list as facade
from isar_gui.main_window import MainWindow
from isar_gui.queue_controller import FinishedGroup, ItemView, QueueGroup, QueueView, Selection
from isar_gui.session import Session
from isar_gui.theme import DEEP, QUEUE_STATES
from isar_gui.viewmodels.scan_list import _status_cell
from isar_gui.views import queue_panel as qp
from isar_gui.views import readiness_line as rl
from isar_gui.views.processing_panel import ProcessingPanel, ProcessingRail
from isar_gui.views.queue_panel import QueuePanel, fmt_elapsed, progress_text, rail_status

NOW = 10_000.0
NOTE = "Deep-pass evidence calibration is provisional: all deep results carry this"


@pytest.fixture(autouse=True)
def _clock_and_note(monkeypatch):
    """A fixed clock for elapsed time, and a known standing note (the real one needs the fitting stack)."""
    monkeypatch.setattr(qp, "_now", lambda: NOW)
    monkeypatch.setattr(qp._currentness, "deep_calibration_note", lambda: NOTE)


# ---- builders ---------------------------------------------------------------------------------------------
def item(n, *, kind="fast", status="waiting", set_key="E:12", label="97 K", analyzer=None, reason=None, auto=False,
         runnable=True, progress=None, started=None, step=None, cores=None):
    analyzer = analyzer or f"a{n:02d}"
    return ItemView(
        item_id=f"q0-{n}-{kind}", unit_key=f"{set_key}:{analyzer}", set_key=set_key, set_label=label, analyzer=analyzer,
        pass_kind=kind, status=status, reason=reason, automatic=auto, runnable=runnable, cores=cores,
        started_at=started, step_started_at=step, progress=progress, finished_at=NOW if status not in
        ("waiting", "running", "saving") else None)


def group(items, *, done=0, total=None, auto=False):
    first = items[0]
    return QueueGroup(first.set_key, first.set_label, auto, done, total or len(items), tuple(items))


def qview(fast=(), deep=(), finished=(), **kw):
    def count(groups, state):
        return sum(1 for g in groups for i in g.items if i.status == state)

    base = dict(bound=True, fast=tuple(fast), deep=tuple(deep), finished=tuple(finished),
                n_running={"fast": count(fast, "running"), "deep": count(deep, "running")},
                n_waiting={"fast": count(fast, "waiting"), "deep": count(deep, "waiting")},
                n_saving=count(fast, "saving") + count(deep, "saving"), n_look=0, paused=False, blocked=None,
                cores=14, cores_in_use=5, service="ready", service_error=None)
    base.update(kw)
    return QueueView(**base)


SEL = Selection(("E:12:a06", "E:12:a07"), "2 analyzers selected (97 K: a06, a07)", 0)
NO_SEL = Selection((), "", 0)


class Stub(QObject):
    """What the panel reads from the controller, and a record of what it asked of it."""
    changed = Signal()

    def __init__(self, view=None, selection=SEL, auto=True):
        super().__init__()
        self._view = view or qview()
        self._sel = selection
        self._auto = auto
        self.calls: list[tuple] = []

    def set(self, view=None, selection=None):
        self._view = view or self._view
        self._sel = self._sel if selection is None else selection
        self.changed.emit()

    def view(self):
        return self._view

    def selection(self):
        return self._sel

    def auto_fast(self):
        return self._auto

    def __getattr__(self, name):                 # an action: record it
        if name in ("add_selection", "remove", "stop", "pause", "resume", "clear_finished", "show", "set_auto_fast"):
            return lambda *a: self.calls.append((name, *a))
        raise AttributeError(name)


def panel(qtbot, stub, width=300, height=700):
    p = ProcessingPanel()
    qtbot.addWidget(p)
    p.set_controller(stub)
    p.resize(width, height)
    p.show()
    qtbot.waitExposed(p)
    p._queue.refresh()
    QApplication.processEvents()
    return p


def texts(p, name):
    QApplication.processEvents()                  # a rebuild shows its new widgets on the next layout pass
    return [w.text() for w in p.findChildren(QLabel) if w.objectName() == name and w.isVisible()]


def button(p, name):
    QApplication.processEvents()
    found = [b for b in p.findChildren(QAbstractButton) if b.objectName() == name and b.isVisible()]
    assert found, name
    return found[0]


def rows(p):
    QApplication.processEvents()
    return p.findChildren(QWidget, QRegularExpression(r"^queue_row_"))


def rich_view():
    run_fast = [item(n, status="running", started=NOW - 40, step=NOW - 40, cores=1,
                     progress={"pass": "fast", "phase": "ladder", "step": 3, "of": 5, "stage": None, "stages": None})
                for n in range(3, 7)]
    wait_fast = [item(n, status="waiting", auto=True) for n in range(7, 20)]
    deep_run = item(6, kind="deep", status="running", started=NOW - 2050, step=NOW - 180, cores=8,
                    progress={"pass": "deep", "phase": "sampling", "stage": 2, "stages": 3, "step": 4, "of": 9})
    deep_wait = item(8, kind="deep", status="waiting", runnable=False)
    saving = item(2, status="saving")
    fin = [FinishedGroup("E:12", "97 K", "fast", {"done": 27, "held": 1}, (
        item(9, status="held", reason="no resolution curve at the slit"),
        item(10, status="done_warning", reason="Fast pass done: search limited, peak-count ceiling reached"))),
           FinishedGroup("E:12", "97 K", "deep", {"done": 1}, (item(5, kind="deep", status="done"),))]
    return qview(fast=[group(run_fast + [saving] + wait_fast, done=11, total=28, auto=False)],
                 deep=[group([deep_run, deep_wait], done=0, total=2)], finished=fin, n_look=2)


# ---- the words --------------------------------------------------------------------------------------------
def _p(pass_, phase, step=None, of=None, stage=None, stages=None):
    return {"pass": pass_, "phase": phase, "step": step, "of": of, "stage": stage, "stages": stages}


@pytest.mark.parametrize("kind, progress, since, expect", [
    ("deep", _p("deep", "sampling", 4, 9, 2, 3), 180, "stage 2 of 3 · model 4 of 9 · this model 3:00"),
    ("deep", _p("deep", "sampling", 1, 2, 3, 3), 5, "stage 3 · model 1 of 2 · this model 0:05"),
    ("deep", _p("deep", "errors"), 130, "error bars · 2:10"),
    ("fast", _p("fast", "ladder", 3, 5), 40, "trying 3 peaks (up to 5) · 0:40"),
    ("fast", _p("fast", "ladder", 1, 5), 4, "trying 1 peak (up to 5) · 0:04"),
    ("fast", _p("fast", "ladder", 0, 5), 4, "trying no peaks (up to 5) · 0:04"),
    ("fast", _p("fast", "seed"), 2, "starting values · 0:02"),
    ("fast", _p("fast", "evidence"), 9, "comparing models · 0:09"),
    ("fast", _p("fast", "audits"), 9, "checking the fit · 0:09"),
    ("fast", _p("fast", "errors"), 9, "error bars · 0:09"),
    ("deep", _p("fast", "ladder", 3, 5), 40, "fast pass first: trying 3 peaks (up to 5) · 0:40"),
])
def test_step_12a_progress_strings(kind, progress, since, expect):
    text, tip = progress_text(item(1, kind=kind, status="running", started=NOW - 3000, step=NOW - since,
                                   progress=progress), NOW)
    assert text == expect and tip
    assert "%" not in text and "stalled" not in text.lower()          # no percentage, no verdict


def test_a_running_item_with_no_step_yet_and_the_clock_format():
    assert progress_text(item(1, status="running", started=NOW), NOW)[0] == "starting"
    assert (fmt_elapsed(34 * 60 + 10), fmt_elapsed(3600 + 125), fmt_elapsed(-5)) == ("34:10", "1:02:05", "0:00")


# ---- the queue --------------------------------------------------------------------------------------------
def test_headings_counts_groups_and_each_running_items_progress_and_clock(qtbot):
    p = panel(qtbot, Stub(rich_view()))
    assert texts(p, "queue_fast_heading_counts") == ["4 running · 1 saving · 13 waiting"]
    assert texts(p, "queue_deep_heading_counts") == ["1 running · 1 waiting"]
    assert "97 K · 11 of 28 done" in texts(p, "queue_group")
    assert "stage 2 of 3 · model 4 of 9 · this model 3:00" in texts(p, "queue_item_progress")
    assert "trying 3 peaks (up to 5) · 0:40" in texts(p, "queue_item_progress")
    assert "waits for this analyzer's fast pass" in texts(p, "queue_item_progress")
    assert "writing the result into the experiment folder" in texts(p, "queue_item_progress")
    clocks = {w.text() for w in p.findChildren(QLabel) if w.isVisible()}
    assert "34:10" in clocks                                           # the deep item's total time
    assert texts(p, "queue_footer_text") == ["5 analyzers running on 5 cores · 1 saving"]


def test_an_automatic_group_says_auto(qtbot):
    v = qview(fast=[group([item(3, auto=True), item(4, auto=True)], auto=True)])
    assert "97 K · auto · 0 of 2 done" in texts(panel(qtbot, Stub(v)), "queue_group")


def test_long_groups_stay_short_and_expand_in_place(qtbot):
    items = [item(n, status="running", started=NOW - 90, step=NOW - 30, cores=1,
                  progress=_p("fast", "ladder", 2, 5)) for n in range(1, 9)] + [item(n) for n in range(9, 29)]
    p = panel(qtbot, Stub(qview(fast=[group(items)])))
    assert len(rows(p)) == 6
    more = button(p, "queue_more")
    assert more.text() == "… 22 more: 2 running, 20 waiting" and more.toolTip() == "Show every analyzer of this scan set"
    more.click()
    assert len(rows(p)) == 28
    stops = [b for b in p.findChildren(QAbstractButton) if b.objectName() == "queue_item_stop" and b.isVisible()]
    assert len(stops) == 8
    assert len(texts(p, "queue_item_progress")) == 8                  # each running item keeps its progress
    button(p, "queue_more").click()                                   # "Show fewer"
    assert len(rows(p)) == 6
    only_waiting = [item(n, status="running") for n in range(1, 7)] + [item(n) for n in range(7, 13)]
    p2 = panel(qtbot, Stub(qview(fast=[group(only_waiting)])))
    assert button(p2, "queue_more").text() == "… 6 more waiting"


# ---- the actions ------------------------------------------------------------------------------------------
@pytest.fixture
def asked(monkeypatch):
    log = []

    def make(answer):
        monkeypatch.setattr(qp, "_confirm_stop", lambda parent, title, text, label: log.append((text, label)) or answer)
        return log
    return make


def test_stopping_a_deep_pass_asks_first_and_naming_the_work_lost(qtbot, asked):
    log = asked(False)
    deep = item(6, kind="deep", status="running", started=NOW - 34 * 60, step=NOW - 5, cores=8,
                progress=_p("deep", "sampling", 1, 9, 1, 3))
    stub = Stub(qview(deep=[group([deep])]))
    p = panel(qtbot, stub)
    assert "fast-pass result stays once it has been saved." in button(p, "queue_item_stop").toolTip()
    button(p, "queue_item_stop").click()
    assert log == [("a06 has been in its deep pass for 34 min. Stopping now discards that work; its fast-pass "
                    "result stays once it has been saved.", "Stop a06")]
    assert stub.calls == []                                            # "Keep running": nothing sent
    asked(True)
    button(p, "queue_item_stop").click()
    assert stub.calls == [("stop", deep.item_id)]


def test_stopping_a_fast_item_and_removing_a_waiting_one_ask_nothing(qtbot, asked):
    log = asked(False)
    running, waiting = item(3, status="running", started=NOW - 5, step=NOW - 5), item(4)
    stub = Stub(qview(fast=[group([running, waiting])]))
    p = panel(qtbot, stub)
    button(p, "queue_item_stop").click()
    button(p, "queue_item_remove").click()
    assert log == [] and stub.calls == [("stop", running.item_id), ("remove", waiting.item_id)]
    assert button(p, "queue_item_stop").accessibleName() == "Stop a03's fast pass"
    assert button(p, "queue_item_remove").accessibleName() == "Remove a04 from the fast queue"


def test_a_deep_item_still_in_its_fast_stage_does_not_promise_a_fast_result(qtbot, asked):
    log = asked(False)
    deep = item(6, kind="deep", status="running", started=NOW - 60, step=NOW - 5, progress=_p("fast", "seed"))
    p = panel(qtbot, Stub(qview(deep=[group([deep])])))
    button(p, "queue_item_stop").click()
    assert len(log) == 1 and "its fast-pass result stays" not in log[0][0] and "fast pass its deep pass" in log[0][0]


def test_the_buttons_and_the_switch_send_the_users_actions_down(qtbot):
    stub = Stub(qview(fast=[group([item(3, status="running", started=NOW)])]), auto=False)
    p = panel(qtbot, stub)
    assert not p._queue._auto.isChecked()                              # shows the experiment's setting
    button(p, "queue_add_fast").click()
    button(p, "queue_add_deep").click()
    p._queue._auto.click()
    button(p, "queue_pause").click()
    assert stub.calls == [("add_selection", "fast"), ("add_selection", "deep"), ("set_auto_fast", True), ("pause",)]
    stub.set(qview(paused=True))
    p._queue.refresh()
    assert button(p, "queue_pause").text() == "Resume"
    button(p, "queue_pause").click()
    button(p, "queue_banner_resume").click()
    assert [c[0] for c in stub.calls[-2:]] == ["resume", "resume"]


def test_the_finished_list_shows_counts_reasons_show_and_clears_only_itself(qtbot):
    stub = Stub(rich_view())
    p = panel(qtbot, stub)
    assert texts(p, "queue_finished_line") == ["97 K · fast pass · 27 done · 1 held",
                                               "97 K · deep pass · 1 done"]
    assert "no resolution curve at the slit" in texts(p, "queue_item_reason")
    show = [b for b in p.findChildren(QAbstractButton) if b.objectName() == "queue_item_show"]
    assert len(show) == 1                                              # only the finished deep item
    show[0].click()
    button(p, "queue_clear").click()
    assert stub.calls == [("show", "E:12:a05"), ("clear_finished",)]
    empty = panel(qtbot, Stub(qview()))
    assert not button(empty, "queue_clear").isEnabled() and button(empty, "queue_clear").toolTip()


def test_every_finished_deep_item_has_show_whatever_its_end(qtbot):
    ends = ("done", "done_warning", "held", "failed", "stopped")
    look = tuple(item(n, kind="deep", status=s, reason=None if s in ("done", "stopped") else "why")
                 for n, s in enumerate(ends, 1))
    stub = Stub(qview(finished=[FinishedGroup("E:12", "97 K", "deep", {"done": 1}, look)]))
    p = panel(qtbot, stub)
    show = [b for b in p.findChildren(QAbstractButton) if b.objectName() == "queue_item_show"]
    assert len(show) == 5 and all(b.toolTip() for b in show)
    assert "reason" in show[2].toolTip()                               # the held item's: where to find why
    show[2].click()
    assert stub.calls == [("show", "E:12:a03")]


# ---- the states that say why ------------------------------------------------------------------------------
@pytest.mark.parametrize("selection, bound, reason", [
    (NO_SEL, True, "Select analyzer files in the scan list, then press Fast pass or Deep pass."),
    (Selection((), "", 2), True, "Pooled masters cannot be queued: select analyzer files in the scan list."),
    (SEL, False, "Open or create an experiment to queue analysis."),
])
def test_each_disabled_add_button_shows_its_reason_in_place(qtbot, selection, bound, reason):
    p = panel(qtbot, Stub(qview(bound=bound), selection=selection))
    for name in ("queue_add_fast", "queue_add_deep"):
        assert not button(p, name).isEnabled() and button(p, name).toolTip() == reason
    assert texts(p, "queue_add_reason") == [reason]
    enabled = panel(qtbot, Stub(qview()))
    assert button(enabled, "queue_add_fast").isEnabled() and texts(enabled, "queue_add_reason") == []


def test_the_paused_blocked_and_failed_banners(qtbot):
    p = panel(qtbot, Stub(qview(paused=True, n_running={"fast": 2, "deep": 0}, n_waiting={"fast": 16, "deep": 0})))
    assert texts(p, "queue_banner_text") == ["Paused. 2 analyzers finishing; nothing new starts; 16 waiting."]
    blocked = panel(qtbot, Stub(qview(blocked="a campaign is running in this experiment")))
    assert texts(blocked, "queue_banner_text") == [
        "Waiting: a campaign is running in this experiment. Nothing starts until it ends."]
    failed = panel(qtbot, Stub(qview(service="failed", service_error="the analysis process ended unexpectedly; see x.log")))
    assert texts(failed, "queue_banner_text") == ["the analysis process ended unexpectedly; see x.log"]
    assert not panel(qtbot, Stub(qview()))._queue._banner.isVisible()


def test_the_provisional_note_stands_once_under_the_deep_heading_and_only_while_there_is_one(qtbot, monkeypatch):
    p = panel(qtbot, Stub(rich_view()))
    assert texts(p, "queue_deep_note") == [NOTE]
    note = next(w for w in p.findChildren(QLabel) if w.objectName() == "queue_deep_note")
    assert "every deep result" in note.toolTip() and "(provisional)" in note.toolTip()
    monkeypatch.setattr(qp._currentness, "deep_calibration_note", lambda: None)
    p._queue.refresh()
    assert texts(p, "queue_deep_note") == []


def test_a_provisional_deep_cell_hovers_towards_the_note():
    """The scan list's per-result word keeps "(provisional)" and its hover names the panel's note."""
    fv = SimpleNamespace(deep_pass={"state": "resolved", "trusted": False, "reason": "resolved: count 2 (P 0.85)"})
    cell = _status_cell(facade._deep(fv), DEEP)
    assert cell.text == "clear (provisional)" and "note under Deep queue in the Processing panel" in cell.tooltip
    assert "settles" not in cell.tooltip


# ---- the threshold ----------------------------------------------------------------------------------------
def walk_hovers(widget):
    """Every visible button, switch and label under ``widget`` that has no hover."""
    return [f"{type(w).__name__}:{w.objectName()}:{getattr(w, 'text', lambda: '')()}"
            for w in widget.findChildren(QWidget)
            if w.isVisible() and isinstance(w, (QAbstractButton, QLabel)) and not w.toolTip()]


@pytest.mark.parametrize("view, selection", [
    (rich_view(), SEL), (qview(), NO_SEL), (qview(bound=False), NO_SEL), (qview(paused=True, n_look=1), Selection((), "", 1)),
    (qview(service="failed", service_error="x", blocked="a campaign is running in this experiment"), SEL)])
def test_every_visible_control_word_and_count_has_a_hover(qtbot, view, selection):
    p = panel(qtbot, Stub(view, selection=selection), width=300)
    assert walk_hovers(p._queue) == []
    for state in QUEUE_STATES.values():                                # every word the panel can show
        assert state[2]


def test_wrapped_lines_get_their_full_height_at_the_narrowest_panel(qtbot):
    """A wrapping label that is given less height than its text needs paints over the line below it."""
    long_reason = "no resolution curve at the slit; choose the folder that holds the measured curves and add it again"
    v = rich_view()
    v = qview(fast=v.fast, deep=v.deep, n_look=1, finished=[FinishedGroup("E:12", "97 K", "fast", {"held": 1}, (
        item(9, status="held", reason=long_reason),))])
    p = panel(qtbot, Stub(v), width=240)
    wrapped = [w for w in p._queue.findChildren(QLabel) if w.isVisible() and w.wordWrap()]
    assert len(wrapped) > 8 and long_reason in {w.text() for w in wrapped}
    for w in wrapped:
        assert w.height() >= w.heightForWidth(w.width()) > 0, w.text()


def test_clicking_the_switch_words_toggles_the_switch(qtbot):
    stub = Stub(qview())
    p = panel(qtbot, stub)
    qtbot.mouseClick(p._queue._auto_text, Qt.MouseButton.LeftButton)
    assert stub.calls == [("set_auto_fast", False)] and not p._queue._auto.isChecked()
    assert p._queue._auto.accessibleName() == "Fast-pass new scans as they arrive"


def test_the_rail_says_what_is_happening_and_what_needs_a_look(qtbot):
    stub = Stub(qview(fast=[group([item(3, status="running")])]))
    rail = ProcessingRail()
    qtbot.addWidget(rail)
    rail.set_controller(stub)
    assert rail.text() == "Processing ‹ · 1 running"
    stub.set(qview(n_running={"fast": 5, "deep": 0}, n_waiting={"fast": 14, "deep": 0}, n_look=2))
    rail.refresh()
    assert rail.text() == "Processing ‹ · 5 running · 14 waiting · 2 need a look"
    assert "2 need a look" in rail.accessibleName() and "2 need a look" in rail.toolTip()
    stub.set(qview(paused=True, n_waiting={"fast": 14, "deep": 0}, n_look=1))
    rail.refresh()
    assert rail.text() == "Processing ‹ · Paused · 14 waiting · 1 needs a look"
    stub.set(qview())
    rail.refresh()
    assert rail.text() == "Processing ‹" and rail.toolTip() == "Expand the processing panel"
    stub.set(qview(service="failed", service_error="x"))
    rail.refresh()
    assert rail.text().endswith("analysis process failed")
    rail.set_collapsed(False)
    assert rail.text() == "›"                                     # expanded: unchanged
    rail.set_collapsed(True)
    rail.resize(24, 400)
    rail.show()
    assert not rail.grab().isNull()                                    # paints, warning part included
    assert rail_status(qview()) == ("", "")
    rail.set_controller(None)
    assert rail.text() == "Processing ‹"


def test_a_burst_of_changes_redraws_once_and_keeps_scroll_and_selection(qtbot):
    groups = [group([item(n, set_key=f"E:{s}", label=f"{s} K", status="running", started=NOW, step=NOW,
                          progress=_p("fast", "seed")) for n in range(1, 4)]) for s in range(10, 30)]
    stub = Stub(qview(fast=groups))
    p = panel(qtbot, stub, height=400)
    scroll = p._queue.findChild(QScrollArea, "queue_scroll")
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 200
    bar.setValue(200)
    for _ in range(5):
        stub.changed.emit()
    qtbot.wait(40)
    assert bar.value() == 200 and stub.calls == []                     # the user's place and selection stand
    assert not p._queue._soon.isActive()


def test_the_clock_ticks_only_while_something_runs(qtbot, monkeypatch):
    stub = Stub(qview(fast=[group([item(3, status="running", started=NOW - 5, step=NOW - 5,
                                        progress=_p("fast", "seed"))])]))
    p = panel(qtbot, stub)
    assert p._queue._tick.isActive()
    assert "starting values · 0:05" in texts(p, "queue_item_progress")
    monkeypatch.setattr(qp, "_now", lambda: NOW + 65)
    p._queue._tick_labels()
    assert "starting values · 1:10" in texts(p, "queue_item_progress")
    stub.set(qview(fast=[group([item(4)])]))
    p._queue.refresh()
    assert not p._queue._tick.isActive()


def test_without_a_controller_the_panel_is_as_it_was(qtbot):
    p = ProcessingPanel()
    qtbot.addWidget(p)
    p.show()
    assert not p._queue.isVisible() and p._run.isVisible()
    p.set_activity("Importing")
    assert p._activity.text() == "Importing" and p._activity.isVisible()
    p.set_activity("")
    assert not p._activity.isVisible()
    p.set_controller(Stub(qview()))
    assert p._queue.isVisible()
    p.set_controller(None)
    assert not p._queue.isVisible()


def test_the_readiness_list_button_adds_to_the_fast_queue(qtbot):
    assert rl._BUTTONS["start_analysis"] == "Add to the fast queue"
    owner = QWidget()
    qtbot.addWidget(owner)
    popup = rl._Popup(owner)
    popup.fill("1 spectrum is ready", [("97 K / a06", "ready")], [("readiness_start_analysis", "Add to the fast queue", lambda: None)])
    start = popup.findChild(QAbstractButton, "readiness_start_analysis")
    assert start.toolTip() == "Adds a fast pass for every analyzer file listed here that has no current fast pass"


# ---- in the real window -----------------------------------------------------------------------------------
def _window(qtbot, monkeypatch, size):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(30)
    return w


def test_the_laptop_window_keeps_its_frame_with_the_queue_on_the_rail_and_in_the_panel(qtbot, monkeypatch):
    from gui.window_frame_checks import assert_frame_geometry
    size = (1108, 851)
    w = _window(qtbot, monkeypatch, size)
    stub = Stub(rich_view())
    w.processing_panel.set_controller(stub)
    w.processing_rail.set_controller(stub)
    w.processing_rail.refresh()
    QApplication.processEvents()
    assert w._right_panel_collapsed()
    assert w.processing_rail.text() == "Processing ‹ · 5 running · 1 saving · 14 waiting · 2 need a look"
    assert_frame_geometry(w, size)
    w.processing_rail.toggled.emit()                                   # open the panel at the laptop width
    qtbot.wait(50)
    q = w.processing_panel._queue
    assert not w._right_panel_collapsed() and q.isVisible() and w.processing_rail.text() == "›"
    assert_frame_geometry(w, size)
    top = q.mapTo(w.processing_panel, q._pause.rect().topLeft())
    assert q._fast.isVisible() and q._deep.isVisible() and q._pause.isVisible()
    assert q.rect().contains(q._fast.geometry()) and 0 <= top.y() < w.processing_panel.height()
    assert walk_hovers(q) == []

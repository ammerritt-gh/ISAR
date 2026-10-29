"""The queue controller (C-controller), offscreen Qt, against a scripted stand-in for the analysis service.

The stand-in (``fake_analysis_service.py``) is a real subprocess behind the real ``ProcessRunner``, so the
pipe, the framing and the process lifecycle are real; the test sends the service events it wants through the
stand-in's ``_emit`` command and reads what the controller sent from its record. One test runs the real
``isar.session.analysis_service`` for the start and the shutdown only. The intake is a small stand-in except
where the test names the real ``IntakeState`` (the add waits for the worker's map).

Every wait is bounded; nothing here touches the operator's profile (the conftest redirects it).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

import bl43_fixture
import bl43_intake_fixture
from isar.workspace_api import Workspace
from isar.workspace_api.inter_spectra import BatchPreview, BatchRequest
from isar.workspace_api.scan_list import Fact, PooledRow, ScanListSnapshot, ScanSetRow, SpectrumRow, Status
from isar_gui import queue_controller as qc
from isar_gui.queue_controller import QueueController
from isar_gui.theme import QUEUE_STATES, Tone, summary_tone
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.readiness_line import ReadinessLine

REPO = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).with_name("fake_analysis_service.py")
WAIT_MS = 15000
SET = "E:12"
SET2 = "E:13"


def unit(set_key: str, analyzer: str) -> str:
    return f"{set_key}:{analyzer}"


def _snapshot(**sets: tuple[str, ...]) -> ScanListSnapshot:
    """A scan list read of sets ``{"E:12": ("a06", ...)}``; the label of a set is its temperature ("97 K" for E:12)."""
    labels = {SET: "97 K", SET2: "120 K"}
    rows = []
    for set_key, analyzers in sets.items():
        exp, scan = set_key.rsplit(":", 1)
        spectra = tuple(SpectrumRow(
            key=unit(set_key, a), set_key=set_key, analyzer=a, q=Fact((0.5, 0.5, 3.0), "rlu", "file", "file"),
            parsed=Status("ok", True), fast=Status("not run", False), deep=Status("not run", False),
            file_name=None, file_path=None, n_points=None) for a in analyzers)
        rows.append(ScanSetRow(
            key=set_key, label=labels.get(set_key, set_key), experiment_id=exp, scan_numbers=(int(scan),),
            summed=False, n_merged=None, temperature=Fact(97.0, "K", "file", "file"),
            slit=Fact(None, None, "missing", "missing", "not in the file"), spectra=spectra,
            files_received=len(spectra)))
    return ScanListSnapshot(sets=tuple(rows))


class FakeIntake(QObject):
    """What the controller reads of IntakeState: ``current`` and its freshness, readiness, the worker signal."""

    worker_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.current: dict | None = {}
        self.current_pending = False
        self.attention = None
        self.summary = None
        self.error = None


class Rig:
    """A controller over a stand-in intake and scan list, bound to a workspace folder, speaking to the fake service."""

    def __init__(self, qtbot, monkeypatch, tmp_path, *, cores=2, blocked=None, sets=None) -> None:
        self.qtbot = qtbot
        self.root = tmp_path / "ws"
        self.root.mkdir()
        self.record = tmp_path / "commands.jsonl"
        self.record.write_text("", encoding="utf-8")
        self.snapshot = _snapshot(**(sets or {SET: ("a06", "a07", "a08", "a09"), SET2: ("a06",)}))
        monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws, **_reads: self.snapshot)
        self.vm = ScanListViewModel(SimpleNamespace())
        self.intake = FakeIntake()
        monkeypatch.setattr(qc, "_service_argv", lambda py, root, log: [
            sys.executable, str(FAKE), "--workspace", str(root), "--log", str(log), "--record", str(self.record)])
        self.ctrl = QueueController(sys.executable, self.intake, self.vm, cores=cores,
                                    blocked=blocked or (lambda: None))
        self.lines: list[str] = []
        self.ctrl.log_line.connect(self.lines.append)
        self.saved: list[tuple] = []
        self.ctrl.results_saved.connect(self.saved.append)
        self.ctrl.bind(self.root, auto_fast=True)

    # ---- driving ----
    def select(self, *keys: str) -> None:
        """Use the real shared selection, as the scan-list and saved groups do."""
        self.vm.select_keys(keys)

    def emit(self, *events: dict) -> None:
        assert self.ctrl._send({"op": "_emit", "lines": list(events)})

    def wait(self, predicate, what="the condition") -> None:
        self.qtbot.waitUntil(lambda: bool(predicate()), timeout=WAIT_MS)

    def commands(self, op: str | None = None) -> list[dict]:
        cmds = [json.loads(ln) for ln in self.record.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [c for c in cmds if op is None or c["op"] == op]

    def wait_commands(self, op: str, n: int) -> list[dict]:
        self.wait(lambda: len(self.commands(op)) >= n)
        return self.commands(op)

    def id_of(self, key: str, pass_kind: str | None = None) -> str:
        found = [c["item_id"] for c in self.commands("start")
                 if c["unit_key"] == key and pass_kind in (None, c["pass"])]
        assert found, f"no start was sent for {key}"
        return found[-1]

    def item(self, key: str, pass_kind: str = "fast"):
        view = self.ctrl.view()
        groups = view.fast if pass_kind == "fast" else view.deep
        found = [i for g in groups for i in g.items if i.unit_key == key]
        return found[0] if found else None

    def started(self, item_id: str, key: str) -> None:
        self.wait(lambda: any(i.started_at is not None for g in (*self.ctrl.view().fast, *self.ctrl.view().deep)
                              for i in g.items if i.item_id == item_id))


@pytest.fixture
def rig(qtbot, monkeypatch, tmp_path):
    made = []

    def make(**kw) -> Rig:
        r = Rig(qtbot, monkeypatch, tmp_path, **kw)
        made.append(r)
        return r

    yield make
    for r in made:               # leave no service process behind, whatever the test did
        r.ctrl._drop_service()


A06, A07, A08, A09 = (unit(SET, f"a{n:02d}") for n in (6, 7, 8, 9))


def _result(iid, key, pass_kind="fast", status="done", reason=None, saving=True, event=None):
    return {"ev": "result", "item_id": iid, "unit_key": key, "pass": pass_kind, "status": status,
            "reason": reason, "saving": saving, "event": event}


def _saved(iid, key, pass_kind="fast", status="done", reason=None):
    return {"ev": "saved", "item_id": iid, "unit_key": key, "pass": pass_kind, "status": status, "reason": reason}


def _finished(iid, end="completed", reason=None):
    return {"ev": "finished", "item_id": iid, "end": end, "reason": reason}


# ---- worked example 1: two fast items ---------------------------------------------------------------------
def test_two_fast_items_run_save_and_count_once_saved(rig):
    r = rig(cores=2)
    r.select(A06, A07)
    assert r.ctrl.selection().text == "2 analyzers selected (97 K: a06, a07)"
    r.ctrl.add_selection("fast")
    assert r.lines == ["Queued for a fast pass: 97 K a06, a07"]
    starts = r.wait_commands("start", 2)
    assert [(c["unit_key"], c["pass"], c["cores"]) for c in starts] == [(A06, "fast", 1), (A07, "fast", 1)]
    assert r.ctrl.view().n_running == {"fast": 2, "deep": 0, "inter": 0} and r.ctrl.view().service == "ready"
    assert r.ctrl.marks()[A06] == {"fast": "running"}
    iid = r.id_of(A06)

    r.emit(_result(iid, A06))                                  # the result is handed to the writer
    r.wait(lambda: r.item(A06).status == "saving")
    assert r.ctrl.view().fast[0].n_done == 0 and r.ctrl.view().n_saving == 1
    assert r.ctrl.marks()[A06] == {"fast": "saving"}
    r.emit(_finished(iid))                                     # its process is gone: the core is free, still saving
    r.wait(lambda: r.ctrl.view().cores_in_use == 1)
    assert r.item(A06).status == "saving"
    r.emit(_saved(iid, A06))                                   # only now is it done
    r.wait(lambda: r.ctrl.view().fast[0].n_done == 1)
    view = r.ctrl.view()
    assert view.finished[0].counts == {"done": 1} and view.finished[0].pass_kind == "fast"
    assert A06 not in r.ctrl.marks() and r.ctrl.marks()[A07] == {"fast": "running"}
    assert (view.fast[0].n_total, view.n_look) == (2, 0)
    r.wait(lambda: r.saved)
    assert r.saved == [(A06,)]                                 # one batch, one key
    assert any(f"queue-" in p.name for p in (r.root / "report" / "gui-runs").iterdir())


def test_a_batch_of_saves_is_reported_once(rig):
    r = rig(cores=2)
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 2)
    ids = [r.id_of(A06), r.id_of(A07)]
    r.emit(*[_result(i, k) for i, k in zip(ids, (A06, A07))], *[_finished(i) for i in ids],
           *[_saved(i, k) for i, k in zip(ids, (A06, A07))])
    r.wait(lambda: r.saved)
    assert r.saved == [(A06, A07)]


# ---- worked example 2: duplicates -------------------------------------------------------------------------
def test_work_already_current_or_queued_is_not_added_and_says_why(rig):
    r = rig(cores=2)
    r.intake.current = {A06: frozenset({"fast"})}
    r.ctrl.pause()                                             # nothing starts, so a07 stays waiting
    r.select(A07)
    r.ctrl.add_selection("fast")
    del r.lines[:]
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    assert r.lines == ["Not added: 97 K a06 already has a current fast pass",
                       "Not added: 97 K a07 is already queued for a fast pass"]
    assert [i.unit_key for g in r.ctrl.view().fast for i in g.items] == [A07]
    del r.lines[:]
    r.ctrl.add_selection("deep")                               # a fast pass does not block a deep one
    assert r.lines == ["Queued for a deep pass: 97 K a06, a07"]
    assert [i.unit_key for g in r.ctrl.view().deep for i in g.items] == [A06, A07]


def test_a_running_file_is_reported_as_running(rig):
    r = rig(cores=2)
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    del r.lines[:]
    r.ctrl.add_selection("fast")
    assert r.lines == ["Not added: 97 K a06 is already running a fast pass"]


# ---- worked example 3: a deep item with its fast stage ----------------------------------------------------
def test_a_deep_items_fast_stage_shows_on_the_fast_mark_and_lands_as_a_finished_fast_result(rig):
    r = rig(cores=4)
    r.select(A08)
    r.ctrl.add_selection("deep")
    start = r.wait_commands("start", 1)[0]
    assert (start["unit_key"], start["pass"], start["cores"]) == (A08, "deep", 4)
    iid = start["item_id"]
    assert r.ctrl.view().n_running == {"fast": 0, "deep": 1, "inter": 0}
    fast_step = {"ev": "progress", "item_id": iid, "t": 1.0, "pass": "fast", "phase": "ladder", "step": 2, "of": 5,
                 "stage": None, "stages": None}
    r.emit(fast_step)
    r.wait(lambda: r.ctrl.marks().get(A08, {}).get("fast") == "running")
    assert r.ctrl.marks()[A08] == {"fast": "running", "deep": "queued"}
    assert r.item(A08, "deep").progress == {k: fast_step[k] for k in ("pass", "phase", "step", "of", "stage", "stages")}
    r.emit(_result(iid, A08))                                  # the fast stage's result, handed to the writer
    r.wait(lambda: r.ctrl.marks().get(A08, {}).get("fast") == "saving")
    assert r.ctrl.marks()[A08] == {"fast": "saving", "deep": "queued"}
    r.emit(_saved(iid, A08))
    r.wait(lambda: r.ctrl.marks()[A08].get("fast") is None)
    fin = r.ctrl.view().finished
    assert [(g.pass_kind, g.counts, g.set_label) for g in fin] == [("fast", {"done": 1}, "97 K")]
    r.emit({"ev": "progress", "item_id": iid, "t": 9.0, "pass": "deep", "phase": "sampling", "step": 1, "of": 9,
            "stage": 1, "stages": 3})
    r.wait(lambda: r.ctrl.marks()[A08].get("deep") == "running")
    assert r.ctrl.marks()[A08] == {"deep": "running"}
    assert r.item(A08, "deep").step_started_at is not None
    r.ctrl.clear_finished()                                    # tidies the list only
    assert r.ctrl.view().finished == () and r.item(A08, "deep").status == "running"


def test_a_search_limited_deep_result_is_done_with_a_warning_and_needs_a_look(rig):
    r = rig(cores=4)
    r.intake.current = {A06: frozenset({"fast"})}
    r.select(A06)
    r.ctrl.add_selection("deep")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    why = "comparison incomplete, 1 model unresolved after restarts"
    r.emit(_result(iid, A06, "deep", "done_warning", why), _finished(iid), _saved(iid, A06, "deep", "done_warning", why))
    r.wait(lambda: r.ctrl.view().n_look == 1)
    view = r.ctrl.view()
    (group,) = view.finished
    assert group.pass_kind == "deep" and group.counts == {"done_warning": 1}
    assert [(i.status, i.reason) for i in group.look] == [("done_warning", why)]
    assert f"Finished with a warning: 97 K a06 (deep pass): {why}" in r.lines
    r.ctrl.clear_finished()
    assert r.ctrl.view().finished == () and r.ctrl.view().n_look == 0


def test_held_failed_and_unsaved_items_say_why_in_their_own_words(rig):
    r = rig(cores=3)
    r.select(A06, A07, A08)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 3)
    ids = {k: r.id_of(k) for k in (A06, A07, A08)}
    held = "a06: no resolution curve for a06 at slit 3"
    r.emit(_result(ids[A06], A06, status="held", reason=held, saving=False), _finished(ids[A06]),
           _result(ids[A07], A07), _finished(ids[A07]),
           {"ev": "save_failed", "item_id": ids[A07], "unit_key": A07, "pass": "fast", "error": "disk full"},
           _finished(ids[A08], "failed", "ValueError: boom"))
    r.wait(lambda: r.ctrl.view().n_look == 3)
    reasons = {i.unit_key: (i.status, i.reason) for g in r.ctrl.view().finished for i in g.look}
    assert reasons == {A06: ("held", held), A07: ("failed", "the result could not be saved: disk full"),
                       A08: ("failed", "ValueError: boom")}
    assert f"Held: 97 K a06 (fast pass): {held}" in r.lines
    assert "Failed: 97 K a07 (fast pass): the result could not be saved: disk full" in r.lines


@pytest.mark.parametrize("answer", ["saved", "save_failed"])
def test_a_save_answer_starts_the_deep_item_waiting_on_the_same_file(rig, answer):
    r = rig(cores=2)
    r.select(A06)
    r.ctrl.add_selection("fast")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    r.ctrl.add_selection("deep")                               # waits: its file's fast pass is still running
    r.emit(_result(iid, A06), _finished(iid))                  # the process is gone; the save is not answered yet
    r.wait(lambda: r.item(A06).status == "saving")
    assert [c["pass"] for c in r.commands("start")] == ["fast"]
    if answer == "saved":
        r.emit(_saved(iid, A06))
    else:
        r.emit({"ev": "save_failed", "item_id": iid, "unit_key": A06, "pass": "fast", "error": "disk full"})
    (_, deep) = r.wait_commands("start", 2)                    # the save's answer frees the file: the deep item starts
    assert (deep["unit_key"], deep["pass"]) == (A06, "deep")


# ---- worked example 4: the writer hold -------------------------------------------------------------------
def test_a_hold_waits_for_the_writer_and_a_release_is_sent_at_zero(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    called = []
    r.ctrl.hold_writes(lambda: called.append("first"))
    r.wait_commands("hold_writes", 1)
    r.qtbot.wait(100)
    assert called == [] and r.ctrl.writes_busy().startswith("analysis is running")
    r.emit({"ev": "writes_held"})
    r.wait(lambda: called == ["first"])
    r.ctrl.hold_writes(lambda: called.append("second"))        # holds nest: already held, answered at once
    assert called == ["first", "second"]
    r.ctrl.release_writes()
    r.qtbot.wait(100)
    assert r.commands("release_writes") == []                  # one hold still stands
    r.ctrl.release_writes()
    r.wait_commands("release_writes", 1)
    assert len(r.commands("hold_writes")) == 1


def test_a_hold_before_the_service_is_ready_is_sent_first_after_ready(rig, qtbot):
    r = rig(cores=1)
    called = []
    r.ctrl.hold_writes(lambda: called.append("now"))           # no service yet: nothing can save
    assert called == ["now"] and r.ctrl.writes_busy() == "a copy or an export is reading the experiment"
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    ops = [c["op"] for c in r.commands()]
    assert ops[0] == "hold_writes" and ops.index("hold_writes") < ops.index("start")


# ---- worked example 5: leaving -----------------------------------------------------------------------------
def test_leaving_asks_with_truthful_counts_and_shuts_down_in_order(rig):
    sets = {SET: tuple(f"a{n:02d}" for n in range(1, 29))}
    r = rig(cores=1, sets=sets)
    clock = [1000.0]
    r.ctrl._clock = lambda: clock[0]
    keys = tuple(unit(SET, f"a{n:02d}") for n in range(6, 21))
    r.select(*keys)
    assert r.ctrl.leave_summary() is None
    r.ctrl.add_selection("deep")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    r.started(iid, keys[0])
    clock[0] += 34 * 60
    assert r.ctrl.leave_summary() == qc.LeaveSummary(1, 1, 2040.0, 14, 0)
    r.ctrl._send({"op": "_on_shutdown", "lines": [_finished(iid, "stopped")]})
    done = []
    r.ctrl.shutdown(lambda: done.append(True))
    r.wait(lambda: done)
    assert done == [True] and r.commands("shutdown")
    assert r.ctrl.view().fast == () and r.ctrl.view().deep == () and r.ctrl.view().service == "off"
    assert r.ctrl.leave_summary() is None                      # the queue is forgotten with the process
    r.ctrl.bind(r.root.parent / "other", auto_fast=True)       # the next experiment: no defect warning, empty


def test_a_silent_service_is_killed_after_the_quiet_time_and_the_unsaved_are_named(rig, monkeypatch):
    monkeypatch.setattr(qc, "_SHUTDOWN_SILENCE_S", 0.4)
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    r.emit(_result(iid, A06))                                  # handed over, never confirmed
    r.wait(lambda: r.item(A06).status == "saving")
    r.ctrl._send({"op": "_hang_on_shutdown"})
    done = []
    r.ctrl.shutdown(lambda: done.append(True))
    r.wait(lambda: done)
    assert done == [True]
    assert any("Not confirmed saved" in ln and "97 K a06" in ln for ln in r.lines)


def test_a_hold_asked_while_stopping_waits_for_the_exit_and_runs_before_the_leave_goes_on(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    order = []
    r.ctrl.shutdown(lambda: order.append("leave"))
    assert r.ctrl.view().service == "stopping"
    r.ctrl.hold_writes(lambda: order.append("copy"))           # a Save copy pressed during the leave
    assert order == []                                         # the service may still be saving: it waits
    r.wait(lambda: order)
    assert order == ["copy", "leave"]                          # the copy reads the folder before the reopen
    r.ctrl.release_writes()


def test_a_hold_confirmed_while_stopping_waits_for_the_exit_too(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    order = []
    r.ctrl.hold_writes(lambda: order.append(("copy", r.ctrl.view().service)))   # asked; writes_held not back yet
    r.wait_commands("hold_writes", 1)
    r.ctrl._send({"op": "_on_shutdown", "lines": [{"ev": "writes_held"}]})     # confirmed while the service leaves
    r.ctrl.shutdown(lambda: order.append(("leave", r.ctrl.view().service)))
    r.wait(lambda: len(order) == 2)
    assert order == [("copy", "off"), ("leave", "off")]        # the copy runs at the exit, before the leave goes on
    r.ctrl.release_writes()


def test_a_service_that_crashes_while_stopping_names_the_saves_it_never_confirmed(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    r.emit(_result(iid, A06))                                  # handed over, never confirmed
    r.wait(lambda: r.item(A06).status == "saving")
    r.ctrl._send({"op": "_hang_on_shutdown"})
    done = []
    r.ctrl.shutdown(lambda: done.append(True))
    r.ctrl._send({"op": "_exit", "code": 3})
    r.wait(lambda: done)
    assert any("Not confirmed saved" in ln and "97 K a06" in ln for ln in r.lines)


def test_a_ready_after_a_shutdown_request_does_not_start_work_or_stall_the_leave(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    assert r.ctrl.view().service == "starting"
    done = []
    r.ctrl.shutdown(lambda: done.append(True))                 # asked before the service said ready
    r.wait(lambda: done)
    assert done == [True] and r.commands("start") == [] and r.ctrl.view().service == "off"


def test_a_pause_does_not_outlive_the_queue_it_was_made_on(rig):
    r = rig(cores=1)
    r.ctrl.pause()
    done = []
    r.ctrl.shutdown(lambda: done.append(True))                 # the queue is forgotten; the open then fails
    r.wait(lambda: done)
    assert not r.ctrl.view().paused
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)


# ---- worked example 6: cores a deep item cannot use --------------------------------------------------------
def test_cores_a_deep_item_cannot_use_go_to_the_next_item(rig):
    r = rig(cores=4)
    r.intake.current = {A08: frozenset({"fast"}), A09: frozenset({"fast"})}
    r.select(A08, A09)
    r.ctrl.add_selection("deep")
    first = r.wait_commands("start", 1)[0]
    assert (first["unit_key"], first["cores"]) == (A08, 4)
    r.emit({"ev": "cores", "item_id": first["item_id"], "cores": 3})
    second = r.wait_commands("start", 2)[1]
    assert (second["unit_key"], second["cores"]) == (A09, 1)
    assert r.ctrl.view().cores_in_use == 4 and r.item(A08, "deep").cores == 3


# ---- pause, cores, the folder and the campaign guard -------------------------------------------------------
def test_pause_starts_nothing_and_resume_starts_what_waits(rig):
    r = rig(cores=2)
    r.ctrl.pause()
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.qtbot.wait(150)
    view = r.ctrl.view()
    assert view.paused and view.service == "off" and view.n_waiting["fast"] == 1 and r.commands("start") == []
    r.ctrl.resume()
    r.wait_commands("start", 1)
    assert not r.ctrl.view().paused


def test_set_cores_applies_to_the_next_start_and_rejects_zero(rig):
    r = rig(cores=1)
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    assert r.ctrl.view().n_waiting["fast"] == 1
    r.ctrl.set_cores(2)
    r.wait_commands("start", 2)
    assert r.ctrl.view().cores == 2
    with pytest.raises(ValueError):
        r.ctrl.set_cores(0)


def test_a_campaign_blocks_starts_until_poked(rig):
    box = {"why": "a campaign is running in this experiment"}
    r = rig(cores=2, blocked=lambda: box["why"])
    r.select(A06)
    r.ctrl.add_selection("fast")
    r.qtbot.wait(150)
    assert r.ctrl.view().blocked == box["why"] and r.ctrl.view().service == "off" and r.commands() == []
    box["why"] = None
    r.ctrl.poke()
    r.wait_commands("start", 1)
    assert r.ctrl.view().blocked is None


def test_remove_takes_out_a_waiting_item_only(rig):
    r = rig(cores=1)
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    waiting = r.item(A07)
    assert waiting.status == "waiting"
    r.ctrl.remove(waiting.item_id)
    r.ctrl.remove(r.id_of(A06))                                # running: left alone
    assert [i.unit_key for g in r.ctrl.view().fast for i in g.items] == [A06]
    assert r.ctrl.view().fast[0].n_total == 1


def test_stop_is_sent_only_for_a_running_item(rig):
    r = rig(cores=1)
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    r.ctrl.stop(r.item(A07).item_id)                           # waiting: nothing to stop
    r.ctrl.stop(r.id_of(A06))
    r.wait_commands("stop", 1)
    assert [c["item_id"] for c in r.commands("stop")] == [r.id_of(A06)]


# ---- arrivals and the switch -------------------------------------------------------------------------------
def test_arrivals_join_the_fast_queue_as_automatic_items_while_the_switch_is_on(rig):
    r = rig(cores=1)
    r.ctrl.pause()
    seen = []
    r.ctrl.auto_fast_changed.connect(seen.append)
    r.ctrl.on_arrivals([SET2, "E:99"])                         # an id the list lacks adds nothing
    (group,) = r.ctrl.view().fast
    assert group.automatic and group.set_label == "120 K" and [i.analyzer for i in group.items] == ["a06"]
    assert r.lines == ["Queued for a fast pass: 120 K a06 (added automatically)"]
    r.ctrl.set_auto_fast(False)
    r.ctrl.set_auto_fast(False)
    assert seen == [False] and not r.ctrl.auto_fast()
    r.ctrl.on_arrivals([SET])                                  # off: nothing; the user's own work is left alone
    assert len(r.ctrl.view().fast[0].items) == 1
    r.ctrl.set_auto_fast(True)
    r.ctrl.on_arrivals([SET])                                  # on again: only what arrives from now on
    assert sum(len(g.items) for g in r.ctrl.view().fast) == 1 + 4


def test_an_arrival_waits_for_a_fresh_current_map(rig):
    r = rig(cores=1)
    r.ctrl.pause()
    r.intake.current_pending = True
    r.intake.current = {unit(SET2, "a06"): frozenset({"fast"})}
    r.ctrl.on_arrivals([SET2])
    assert r.ctrl.view().fast == ()                            # held: the map may predate the last change
    r.intake.current_pending = False
    r.intake.worker_changed.emit()
    assert r.ctrl.view().fast == () and r.lines == []          # the fresh map says it is current: nothing to add


# ---- the process dying ------------------------------------------------------------------------------------
def test_an_unexpected_exit_fails_running_items_keeps_waiting_ones_and_restarts_on_the_next_add(rig):
    r = rig(cores=2)
    r.select(A06, A07, A08)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 2)
    i06, i07 = r.id_of(A06), r.id_of(A07)
    r.emit(_result(i06, A06))                                  # a06 is handed over, not yet confirmed
    r.wait(lambda: r.item(A06).status == "saving")
    r.ctrl._send({"op": "_exit", "code": 3})
    r.wait(lambda: r.ctrl.view().service == "failed")
    view = r.ctrl.view()
    reasons = {i.unit_key: (i.status, i.reason) for g in view.finished for i in g.look}
    log_path = r.ctrl._service_log
    assert reasons == {A06: ("failed", "not confirmed saved; re-add it"),
                       A07: ("failed", f"the analysis process ended unexpectedly; see {log_path}")}
    assert view.n_waiting["fast"] == 1 and "exit code 3" in view.service_error and str(log_path) in view.service_error
    assert view.cores_in_use == 0
    r.select(A09)
    r.ctrl.add_selection("fast")                               # the next start brings the service back
    r.wait(lambda: r.ctrl.view().service == "ready")
    r.wait_commands("start", 4)                                # a08 waiting from before, and the new a09


def test_events_of_a_service_already_left_are_ignored(rig):
    r = rig(cores=1)
    r.select(A06)
    r.ctrl.add_selection("fast")
    iid = r.wait_commands("start", 1)[0]["item_id"]
    old = r.ctrl._runner
    r.ctrl.bind(r.root.parent / "other", auto_fast=True)       # a defect path: warns, kills the service, forgets
    assert r.ctrl.view().fast == () and r.ctrl.view().service == "off" and r.ctrl.marks() == {}
    r.ctrl._on_line(old, json.dumps(_result(iid, A06)))        # a late line from the old process
    r.ctrl._on_exit(old, 0)
    assert r.ctrl.view().finished == () and r.ctrl.view().service == "off"


def test_binding_the_same_experiment_again_keeps_its_queue_and_service(rig):
    r = rig(cores=1)
    r.select(A06, A07)
    r.ctrl.add_selection("fast")
    r.wait_commands("start", 1)
    runner = r.ctrl._runner
    r.ctrl.bind(r.root, auto_fast=False)
    assert r.ctrl._runner is runner and not r.ctrl.auto_fast()
    assert (r.ctrl.view().n_running["fast"], r.ctrl.view().n_waiting["fast"]) == (1, 1)


def test_keep_waiting_keeps_the_waiting_items_at_the_new_root(rig):
    r = rig(cores=1)
    r.ctrl.pause()
    r.select(A06)
    r.ctrl.add_selection("fast")
    new_root = r.root.parent / "renamed"
    new_root.mkdir()
    r.ctrl.bind(new_root, auto_fast=False, keep_waiting=True)
    assert r.ctrl.view().n_waiting["fast"] == 1 and not r.ctrl.auto_fast() and r.ctrl.view().paused
    r.ctrl.resume()
    r.wait_commands("start", 1)
    assert (new_root / "report" / "gui-runs").is_dir()         # the service log lives under the new root


# ---- the selection and the readiness ----------------------------------------------------------------------
def test_the_selection_names_analyzers_by_scan_set_and_leaves_pooled_masters_out(rig, monkeypatch):
    r = rig()
    assert r.ctrl.selection() == qc.Selection((), "", 0)
    r.vm.select(A06)
    assert r.ctrl.selection().text == "1 analyzer selected (97 K: a06)"
    r.select(A06, A07, unit(SET2, "a06"))
    assert r.ctrl.selection().text == "3 analyzers selected (97 K: a06, a07; 120 K: a06)"
    pooled = PooledRow(key="E:900:det", label="pooled master (2 members)", temperature=Fact(1.0, "K", "fit", "fit"),
                       q=Fact(None, None, "missing", "missing"), n_members=2,
                       fast=Status("not run", False), deep=Status("not run", False))
    r.snapshot = ScanListSnapshot(sets=r.snapshot.sets, pooled=(pooled,))
    r.vm.on_workspace_changed()
    r.select("E:900:det")
    assert r.ctrl.selection() == qc.Selection((), "", 1)
    r.select("E:900:det", A07)
    assert r.ctrl.selection().keys == (A07,) and r.ctrl.selection().n_pooled == 1
    assert r.ctrl.view().fast == ()                            # selecting never queues anything


def test_a_set_row_click_gives_the_controller_all_analyzers(rig):
    r = rig()
    r.vm.select(A06)
    r.vm.click(f"set:{SET}")
    assert r.ctrl.selection().keys == tuple(sp.key for sp in r.snapshot.scan_set(SET).spectra)
    assert r.ctrl.view().fast == ()


def test_show_is_the_one_action_that_moves_the_selection(rig):
    r = rig()
    shown = []
    r.vm.selection.changed.connect(shown.append)
    r.ctrl.show(A07)
    assert r.vm.selection.active == A07 and len(shown) == 1


def test_the_controller_tells_its_views_when_anything_changes(rig):
    r = rig(cores=1)
    changes = []
    r.ctrl.changed.connect(lambda: changes.append(1))
    r.vm.select(A06)
    assert changes
    del changes[:]
    r.vm.on_workspace_changed()                                # a refreshed list changes labels and the selection text
    assert changes


# ---- the real IntakeState: the add waits for the worker's map ----------------------------------------------
@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("qc"))


def test_an_add_waits_for_the_workers_map_and_the_run_count_follows_the_readiness_line(
        qtbot, monkeypatch, tmp_path, built):
    from isar_gui.viewmodels import intake as intake_mod

    real = intake_mod.current_passes
    a06 = f"{bl43_fixture.KEY_A}:a06"
    gate = {"open": False}

    def delayed(root, keys=None):
        end = time.monotonic() + 15                            # the worker thread must not touch Qt: poll a flag
        while not gate["open"] and time.monotonic() < end:
            time.sleep(0.01)
        out = dict(real(root, keys))
        out[a06] = frozenset({"fast"})                         # what a saved fast pass would say
        return out

    monkeypatch.setattr(intake_mod, "current_passes", delayed)
    root = tmp_path / "copy" / built.name
    shutil.copytree(built.parent, root.parent)
    monkeypatch.setattr(qc, "_service_argv", lambda py, rt, lg: [
        sys.executable, str(FAKE), "--workspace", str(rt), "--log", str(lg)])
    vm = ScanListViewModel(Workspace.open(root))
    intake = IntakeState()
    vm.set_intake(intake)
    intake.changed.connect(lambda: vm.on_workspace_changed(None))
    intake.worker_changed.connect(lambda: vm.on_workspace_changed(None))
    ctrl = QueueController(sys.executable, intake, vm, cores=2)
    lines: list[str] = []
    ctrl.log_line.connect(lines.append)
    intake.bind(root)
    ctrl.bind(root, auto_fast=True)
    assert intake.current is None and intake.current_pending         # the worker has not answered
    vm.select(a06)
    ctrl.add_selection("fast")
    assert lines == [] and ctrl.view().fast == ()                    # held: the map may predate the last change
    gate["open"] = True
    qtbot.waitUntil(lambda: bool(lines), timeout=WAIT_MS)
    assert not intake.current_pending and set(intake.current) >= {a06}
    assert lines == [f"Not added: {ctrl._label(bl43_fixture.KEY_A)} a06 already has a current fast pass"]
    assert ctrl.view().fast == ()                                    # the held add used the fresh map
    # the Run button's count is the readiness line's ready set, less what already has a current fast pass
    line = ReadinessLine(intake)
    count, why = ctrl.fast_all()
    assert why is None and line.ready_count > 1 and count == line.ready_count - 1
    ctrl.pause()
    ctrl.fast_pass_all_ready()
    assert sum(len(g.items) for g in ctrl.view().fast) == count
    assert ctrl.fast_all() == (0, None)                              # all of them are queued now
    intake.bind(None)
    assert intake.current is None and not intake.current_pending
    ctrl._drop_service()


# ---- C-current-read: the intake worker's map and its freshness -------------------------------------------
def test_intake_reads_the_current_map_in_its_worker_and_says_when_it_may_be_old(qtbot, built):
    intake = IntakeState()
    assert intake.current is None and not intake.current_pending         # unbound
    intake.bind(built)
    assert intake.current is None and intake.current_pending             # asked, not answered
    qtbot.waitUntil(lambda: not intake.current_pending, timeout=WAIT_MS)
    current = intake.current
    assert f"{bl43_fixture.KEY_A}:a06" in current and all(isinstance(v, frozenset) for v in current.values())
    intake.reload()                                                      # any re-read: the map may predate it
    assert intake.current_pending and intake.current is current          # the old map stays readable meanwhile
    qtbot.waitUntil(lambda: not intake.current_pending, timeout=WAIT_MS)
    other = IntakeState()
    other.bind(built)
    other.bind(None)
    assert other.current is None and not other.current_pending           # another experiment's map is never shown


# ---- the real service: start and shut down in order --------------------------------------------------------
def test_the_real_service_starts_for_an_item_and_shuts_down_cleanly(qtbot, monkeypatch, tmp_path, built):
    root = tmp_path / "real" / built.name
    shutil.copytree(built.parent, root.parent)
    monkeypatch.chdir(REPO)                                     # `python -m isar...` imports the checkout under test
    snapshot = _snapshot(**{bl43_fixture.KEY_A: ("a06",)})
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws, **_reads: snapshot)
    vm = ScanListViewModel(SimpleNamespace())
    ctrl = QueueController(sys.executable, FakeIntake(), vm, cores=1)
    ctrl.bind(root, auto_fast=False)
    vm.select(f"{bl43_fixture.KEY_A}:a06")
    ctrl.add_selection("fast")
    qtbot.waitUntil(lambda: ctrl.view().service == "ready", timeout=60000)
    done = []
    ctrl.shutdown(lambda: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=120000)
    view = ctrl.view()
    assert view.service == "off" and view.fast == () and view.deep == ()
    runs = root / "report" / "gui-runs"
    (stdout_log,) = [p for p in runs.glob("queue-*.log") if not p.name.endswith(".service.log")]
    events = [json.loads(ln)["ev"] for ln in stdout_log.read_text(encoding="utf-8").splitlines() if ln.startswith("{")]
    assert events[0] == "ready" and events[-1] == "bye"            # ordered: the service said goodbye itself
    assert len(list(runs.glob("queue-*.service.log"))) == 1


# ---- a batch: one inter item with the batch object, progress, saves, stop and a refusal (S1U14, packet 3) -----
ANCHORED = BatchRequest(kind="anchored", direction="q", anchors=(A06,), selection=())
LINE = "1 anchor \u00b7 spread in q \u00b7 3 spectra \u00b7 2 rings"
# a batch is named by its kind and its start time (local HH:MM), never by a per-window number
BATCH_NAME = r"Inter-spectra batch, started \d\d:\d\d"
QUEUED_RE = rf"Queued {BATCH_NAME} \(anchored\): {re.escape(LINE)}"


def _start_batch(r, monkeypatch, members=(A06, A07, A08), *, can_start=True, reason=None):
    monkeypatch.setattr(qc, "batch_preview", lambda root, req: BatchPreview(
        can_start=can_start, reason=reason, members=tuple(members), line=LINE, notes=()))
    return r.ctrl.start_inter_batch(ANCHORED)


def _batch_saved(iid, key, keys, status="done"):
    return {"ev": "saved", "item_id": iid, "unit_key": key, "pass": "inter", "status": status, "reason": None,
            "keys": list(keys), "refused": 0}


def test_a_batch_is_one_inter_item_that_starts_with_its_spec_and_is_listed_as_a_batch(rig, monkeypatch):
    r = rig(cores=2)
    ok, message = _start_batch(r, monkeypatch)
    assert ok and re.fullmatch(QUEUED_RE, message), message
    (start,) = r.wait_commands("start", 1)
    batch = start["batch"]
    assert re.fullmatch(r"b[0-9a-f]{12}", batch["batch_id"])
    assert start["unit_key"] == f"inter:{batch['batch_id']}:batch" and (start["pass"], start["cores"]) == ("inter", 2)
    assert batch == {"batch_id": batch["batch_id"], "kind": "anchored", "direction": "q", "anchors": [A06],
                     "members": [A06, A07, A08]}
    view = r.ctrl.view()
    assert view.n_running == {"fast": 0, "deep": 0, "inter": 1} and [i.item_id for i in view.inter] == [start["item_id"]]
    (b,) = view.batches
    assert re.fullmatch(BATCH_NAME, b.label) and (b.kind, b.direction, b.status, b.n_total, b.cores) == (
        "anchored", "q", "running", 3, 2)
    assert r.ctrl.marks() == {}                                 # a batch is no mark on its files
    assert r.ctrl.writes_busy() and r.ctrl.leave_summary().n_running == 1
    assert any(re.fullmatch(QUEUED_RE, line) for line in r.lines)


def test_a_second_batch_on_the_same_files_waits_for_the_first_and_says_so(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    r.wait_commands("start", 1)
    _start_batch(r, monkeypatch)                                # the same three spectra
    newer, older = r.ctrl.view().batches                        # newest first
    assert (newer.status, newer.reason) == ("waiting", f"waiting for {older.label} to finish")
    assert older.status == "running" and re.fullmatch(BATCH_NAME, older.label)
    assert len(r.commands("start")) == 1


def test_batch_progress_and_each_saved_ring_reach_the_view_and_the_results_saved_signal(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    (start,) = r.wait_commands("start", 1)
    iid, key = start["item_id"], start["unit_key"]
    progress = {"ev": "progress", "item_id": iid, "t": 1.0, "pass": "inter", "kind": "anchored", "stage": "ring",
                "index": 1, "of": 2, "round": None, "phase": "assign", "done": 2, "failed": 0, "not_reached": 1,
                "fixed": 1, "total": 3}
    r.emit(progress)
    r.wait(lambda: r.ctrl.view().batches[0].progress is not None)
    assert r.ctrl.view().batches[0].progress == {k: v for k, v in progress.items() if k not in ("ev", "item_id", "t")}
    r.emit(_result(iid, key, "inter"))                          # ring 1 handed to the writer
    r.wait(lambda: r.ctrl.view().batches[0].status == "saving")
    r.emit(_batch_saved(iid, key, (A06, A07)))
    r.wait(lambda: r.saved)
    assert r.saved == [(A06, A07)] and r.ctrl.view().batches[0].status == "running"
    r.emit(_result(iid, key, "inter", event="end"), _batch_saved(iid, key, ()))   # the end record, saved
    r.emit(_finished(iid))
    r.wait(lambda: r.ctrl.view().batches[0].status == "done")
    assert r.lines[-1] == (f"Batch finished: {r.ctrl.view().batches[0].label} (anchored): "
                           "2 refitted, 0 failed, 1 not reached")


def test_a_batch_whose_process_ends_without_an_end_record_settles_failed_though_its_rings_were_saved(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    (start,) = r.wait_commands("start", 1)
    iid, key = start["item_id"], start["unit_key"]
    r.emit(_result(iid, key, "inter", event="stage"), _batch_saved(iid, key, (A06, A07)))
    r.wait(lambda: r.saved)
    r.emit(_finished(iid))
    r.wait(lambda: r.ctrl.view().batches[0].status == "failed")
    assert r.ctrl.view().batches[0].reason == "the batch ended without an end record"


def test_a_stopped_batch_counts_its_saved_rings_and_not_its_start_payload(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    (start,) = r.wait_commands("start", 1)
    iid, key = start["item_id"], start["unit_key"]
    r.emit(_result(iid, key, "inter", event="start"), _batch_saved(iid, key, ()))
    for spectrum in (A06, A07, A08):
        r.emit(_result(iid, key, "inter", event="stage"), _batch_saved(iid, key, (spectrum,)))
    r.wait(lambda: {A06, A07, A08} <= {k for keys in r.saved for k in keys})
    r.ctrl.stop(iid)
    r.wait_commands("stop", 1)
    r.emit(_finished(iid, end="stopped"))
    r.wait(lambda: r.ctrl.view().batches[0].status == "stopped")
    assert r.lines[-1] == f"Stopped: {r.ctrl.view().batches[0].label} (anchored): kept 3 rings"


def test_a_stopped_batch_keeps_its_saved_rings_says_what_it_kept_and_never_settles_done(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    (start,) = r.wait_commands("start", 1)
    iid, key = start["item_id"], start["unit_key"]
    r.emit(_result(iid, key, "inter", event="stage"), _batch_saved(iid, key, (A06, A07)))
    r.wait(lambda: r.saved)
    r.ctrl.stop(iid)
    r.wait_commands("stop", 1)
    r.emit(_finished(iid, end="stopped"))
    r.wait(lambda: r.ctrl.view().batches[0].status == "stopped")
    assert r.lines[-1] == f"Stopped: {r.ctrl.view().batches[0].label} (anchored): kept 1 ring"
    assert r.saved == [(A06, A07)]


def test_a_batch_whose_save_failed_settles_failed_with_the_writers_reason(rig, monkeypatch):
    r = rig(cores=2)
    _start_batch(r, monkeypatch)
    (start,) = r.wait_commands("start", 1)
    iid, key = start["item_id"], start["unit_key"]
    r.emit(_result(iid, key, "inter"), {"ev": "save_failed", "item_id": iid, "unit_key": key, "pass": "inter",
                                        "error": "WorkspaceBusy: timed out"}, _finished(iid))
    r.wait(lambda: r.ctrl.view().batches[0].status == "failed")
    assert r.ctrl.view().batches[0].reason == "not saved: WorkspaceBusy: timed out"
    assert r.lines[-1] == f"Failed: {r.ctrl.view().batches[0].label} (anchored): not saved: WorkspaceBusy: timed out"


def test_a_batch_the_preview_refuses_is_not_queued_and_the_preview_says_why(rig, monkeypatch):
    r = rig(cores=2)
    ok, message = _start_batch(r, monkeypatch, can_start=False, reason="no accepted hkl for the anchor")
    assert (ok, message) == (False, "no accepted hkl for the anchor")
    view = r.ctrl.view()
    assert view.batches == () and view.n_waiting == {"fast": 0, "deep": 0, "inter": 0}
    assert r.commands("start") == []


# ---- the words ---------------------------------------------------------------------------------------------
def test_the_queue_words_cover_every_status_with_a_tooltip_and_active_is_never_settled():
    wanted = {"waiting", "queued", "running", "saving", "done", "done_warning", "held", "failed", "stopped"}
    assert set(QUEUE_STATES) == wanted
    assert all(word and tip for word, _tone, tip in QUEUE_STATES.values())
    assert {s for s, (_w, tone, _t) in QUEUE_STATES.items() if tone is Tone.ACTIVE} == {"running", "saving"}
    assert summary_tone([Tone.OK, Tone.ACTIVE, Tone.IDLE]) is Tone.ACTIVE
    assert summary_tone([Tone.ACTIVE, Tone.WARN]) is Tone.WARN

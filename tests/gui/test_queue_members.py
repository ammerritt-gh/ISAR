"""The queue controller's batch member map (C-members-event), offscreen Qt against the scripted stand-in service.

The service relays the runner's ``members`` events; these tests send them through the stand-in and check what the
controller keeps: the newest started batch's map, how it settles at an abnormal end, and when it signals. The rig
and the stand-in are the ones ``test_queue_controller`` uses; a batch starts through the real ``start_inter_batch``.
Keys are made up (``e:11:a03``): the controller does not read them as analyzers.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("PySide6")

from gui.test_queue_controller import Rig
from isar.workspace_api.inter_spectra import BatchPreview, BatchRequest
from isar_gui import queue_controller as qc

ANCHORED = BatchRequest(kind="anchored", direction="q", anchors=("e:11:a03",), selection=())
A = ("e:11:a03", "e:12:a03", "e:13:a03", "e:14:a03", "e:15:a03")
B = ("e:21:a03", "e:22:a03")
LOG = "isar_gui.queue_controller"


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


def start(r: Rig, monkeypatch, members) -> str:
    """Queue one batch over ``members`` through the real controller; return its item id once the service started it."""
    monkeypatch.setattr(qc, "batch_preview", lambda root, req: BatchPreview(
        can_start=True, reason=None, members=tuple(members), line="line", notes=()))
    n = len(r.commands("start")) + 1            # counted before the start may go out
    assert r.ctrl.start_inter_batch(ANCHORED)[0]
    iid = r.wait_commands("start", n)[-1]["item_id"]
    r.wait(lambda: r.ctrl.batch_members() is not None and r.ctrl.batch_members().item_id == iid)
    return iid


def members_event(iid, states, *, reasons=None, stage="ring", index=1, of=2):
    counts = {"done": 0, "failed": 0, "not_reached": 0, "fixed": 0, "total": len(states)}
    return {"ev": "members", "item_id": iid, "t": 1.0, "batch": "b0123456789a", "kind": "anchored", "stage": stage,
            "index": index, "of": of, "round": None, "counts": counts, "states": states, "reasons": reasons or {}}


def finished_event(iid, end, reason=None):
    return {"ev": "finished", "item_id": iid, "end": end, "reason": reason}


def test_a_started_batch_shows_an_empty_map_until_its_first_members_event(rig, monkeypatch):
    r = rig(cores=2)
    seen = []
    r.ctrl.members_changed.connect(seen.append)
    iid = start(r, monkeypatch, A[:3])
    m = r.ctrl.batch_members()
    assert (m.item_id, m.batch_id, m.kind, m.direction) == (
        iid, r.commands("start")[-1]["batch"]["batch_id"], "anchored", "q")
    assert (m.stage, m.index, m.of, m.round, dict(m.counts), dict(m.states), dict(m.reasons), m.ended) == (
        None, None, None, None, {}, {}, {}, None)
    assert seen == [iid]


def test_worked_example_3_a_stopped_batch_settles_its_waiting_and_fitting_members(rig, monkeypatch):
    r = rig(cores=2)
    seen = []
    r.ctrl.members_changed.connect(seen.append)
    iid = start(r, monkeypatch, A)
    states = {"e:11:a03": "waiting", "e:12:a03": "done", "e:13:a03": "held", "e:14:a03": "fitting",
              "e:15:a03": "waiting"}
    r.emit(members_event(iid, states), finished_event(iid, "stopped"))
    r.wait(lambda: r.ctrl.batch_members().ended is not None)
    m = r.ctrl.batch_members()
    assert dict(m.states) == {"e:11:a03": "not_reached", "e:12:a03": "done", "e:13:a03": "held",
                              "e:14:a03": "not_reached", "e:15:a03": "not_reached"}
    assert dict(m.reasons) == {k: "the batch was stopped" for k in ("e:11:a03", "e:14:a03", "e:15:a03")}
    assert (m.ended, m.ended_reason) == ("stopped", None)
    assert seen == [iid, iid, iid]          # started, its members, and the settling at its end


def test_worked_example_4_a_second_batch_takes_the_map_and_the_first_keeps_its_own_silently(rig, monkeypatch):
    r = rig(cores=4)
    seen = []
    r.ctrl.members_changed.connect(seen.append)
    first = start(r, monkeypatch, A[:2])
    # a batch claims every idle core; giving back the ones it does not use lets a second batch run beside it
    r.emit(members_event(first, {"e:11:a03": "waiting", "e:12:a03": "fitting"}),
           {"ev": "cores", "item_id": first, "cores": 1})
    r.wait(lambda: r.ctrl.batch_members().states)
    second = start(r, monkeypatch, B)
    assert r.ctrl.batch_members().item_id == second and not r.ctrl.batch_members().states
    # the first batch's map arrives after the second started: kept, not shown, no signal; the second's map is shown
    r.emit(members_event(first, {"e:11:a03": "done", "e:12:a03": "done"}),
           members_event(second, {"e:21:a03": "fitting", "e:22:a03": "waiting"}))
    r.wait(lambda: dict(r.ctrl.batch_members().states) == {"e:21:a03": "fitting", "e:22:a03": "waiting"})
    assert r.ctrl.batch_members().item_id == second
    assert seen == [first, first, second, second]


BAD_EVENTS = {
    "states is not a map": {"states": ["waiting"]},
    "unknown state word": {"states": {"e:11:a03": "running"}},
    "reason is not text": {"reasons": {"e:11:a03": 3}},
    "counts is not a map": {"counts": 5},
}


@pytest.mark.parametrize("bad", BAD_EVENTS.values(), ids=BAD_EVENTS.keys())
def test_a_members_event_that_cannot_be_applied_is_logged_and_dropped(rig, monkeypatch, caplog, bad):
    r = rig(cores=2)
    seen = []
    r.ctrl.members_changed.connect(seen.append)
    iid = start(r, monkeypatch, A[:2])
    r.emit(members_event(iid, {"e:11:a03": "waiting", "e:12:a03": "waiting"}))
    r.wait(lambda: r.ctrl.batch_members().states)
    broken = {**members_event(iid, {"e:11:a03": "fitting"}), **bad}
    with caplog.at_level(logging.WARNING, logger=LOG):
        r.emit(broken, members_event(iid, {"e:11:a03": "done", "e:12:a03": "waiting"}))
        r.wait(lambda: dict(r.ctrl.batch_members().states).get("e:11:a03") == "done")
    assert "not applied" in caplog.text
    assert seen == [iid, iid, iid]          # the broken event moved nothing and stopped nothing after it


def test_a_members_event_for_an_unknown_or_ended_batch_is_logged_and_dropped(rig, monkeypatch, caplog):
    r = rig(cores=2)
    iid = start(r, monkeypatch, A[:2])
    with caplog.at_level(logging.WARNING, logger=LOG):
        r.emit(members_event("q9-99", {"e:11:a03": "done"}))
        r.emit(finished_event(iid, "completed"), members_event(iid, {"e:11:a03": "done", "e:12:a03": "done"}))
        r.wait(lambda: "after the batch ended" in caplog.text)
    assert "has no started batch" in caplog.text
    assert r.ctrl.batch_members().ended == "completed"
    assert dict(r.ctrl.batch_members().states) == {}


def test_a_members_event_does_not_fire_changed(rig, monkeypatch):
    r = rig(cores=2)
    iid = start(r, monkeypatch, A[:2])
    changes = []
    r.ctrl.changed.connect(lambda: changes.append(1))
    r.emit(members_event(iid, {"e:11:a03": "waiting", "e:12:a03": "fitting"}),
           members_event(iid, {"e:11:a03": "done", "e:12:a03": "fitting"}))
    r.wait(lambda: r.ctrl.batch_members().states.get("e:11:a03") == "done")
    assert changes == []


def test_completed_only_ends_the_map_clear_finished_keeps_it_and_leaving_the_experiment_clears_it(rig, monkeypatch):
    r = rig(cores=2)
    seen = []
    r.ctrl.members_changed.connect(seen.append)
    iid = start(r, monkeypatch, A[:2])
    states = {"e:11:a03": "done", "e:12:a03": "not_reached"}
    reasons = {"e:12:a03": "cut off by a failed neighbour"}
    r.emit(members_event(iid, states, reasons=reasons), finished_event(iid, "completed"))
    r.wait(lambda: r.ctrl.batch_members().ended is not None)
    m = r.ctrl.batch_members()
    assert (m.ended, m.ended_reason, dict(m.states), dict(m.reasons)) == ("completed", None, states, reasons)
    r.ctrl.clear_finished()
    assert r.ctrl.batch_members() == m
    other = r.root.parent / "other"
    other.mkdir()
    r.ctrl.bind(other, auto_fast=True)
    assert r.ctrl.batch_members() is None and seen[-1] == ""


def test_a_batch_whose_process_dies_settles_its_open_members_as_failed(rig, monkeypatch):
    r = rig(cores=2)
    iid = start(r, monkeypatch, A[:3])
    r.emit(members_event(iid, {"e:11:a03": "waiting", "e:12:a03": "fitting", "e:13:a03": "done"}))
    r.wait(lambda: r.ctrl.batch_members().states.get("e:12:a03") == "fitting")
    r.ctrl._send({"op": "_exit", "code": 3})
    r.wait(lambda: r.ctrl.batch_members().ended is not None)
    m = r.ctrl.batch_members()
    assert (m.ended, m.ended_reason.startswith("the analysis process ended unexpectedly")) == ("failed", True)
    assert dict(m.states) == {"e:11:a03": "not_reached", "e:12:a03": "not_reached", "e:13:a03": "done"}
    assert dict(m.reasons)["e:12:a03"].startswith("the batch failed: the analysis process ended unexpectedly")

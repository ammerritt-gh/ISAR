"""C-schedule worked examples E1-E12 and the invariants of the analysis queue scheduler."""

from __future__ import annotations

import random

import pytest

from isar.session.analysis_queue import AnalysisQueue

NONE: dict = {}


def ids(starts):
    return [(s.item_id, s.pass_kind, s.cores) for s in starts]


def fin(q, item_id, pass_kind, status="done", reason=None):
    """Result handed to the writer, then the process ends."""
    q.on_result(item_id, pass_kind, status, reason, saving=True)
    q.on_finished(item_id, "completed", None)


def state(q, item_id):
    snap = q.snapshot()
    for v in snap.fast + snap.deep + snap.inter + snap.finished:
        if v.item_id == item_id:
            return v
    raise AssertionError(f"{item_id} not in snapshot")


def test_e1_fast_first_beats_age():
    q = AnalysisQueue(2)
    assert q.add(["A"], "deep", automatic=False, current=NONE).added[0].item_id == "q1"
    rep = q.add(["B", "C"], "fast", automatic=True, current=NONE)
    assert [v.item_id for v in rep.added] == ["q2", "q3"]
    assert ids(q.next_starts()) == [("q2", "fast", 1), ("q3", "fast", 1)]
    fin(q, "q2", "fast")
    assert q.next_starts() == []                      # q3 still runs: the deep item waits (option b)
    fin(q, "q3", "fast")
    assert ids(q.next_starts()) == [("q1", "deep", 2)]


def test_e2_oldest_runnable_skips_a_blocked_item():
    q = AnalysisQueue(4)
    q.add(["A"], "deep", automatic=False, current=NONE)
    assert ids(q.next_starts()) == [("q1", "deep", 4)]
    q.add(["A", "E"], "fast", automatic=True, current=NONE)       # q2 (A), q3 (E)
    assert q.next_starts() == []
    fin(q, "q1", "deep")
    assert ids(q.next_starts()) == [("q3", "fast", 1)]
    v = state(q, "q2")
    assert (v.state, v.waiting_on, v.runnable) == ("waiting", "q1", False)
    assert state(q, "q1").state == "saving"


def test_e3_deep_waits_for_its_files_fast_item_through_saving():
    q = AnalysisQueue(2)
    q.add(["A"], "fast", automatic=True, current=NONE)             # q1
    assert ids(q.next_starts()) == [("q1", "fast", 1)]
    q.add(["A", "B"], "deep", automatic=False, current=NONE)      # q2 (A), q3 (B)
    assert q.next_starts() == []
    assert state(q, "q2").waiting_on == "q1"
    fin(q, "q1", "fast")                                           # saving
    assert ids(q.next_starts()) == [("q3", "deep", 2)]            # another file's deep item starts
    assert state(q, "q2").waiting_on == "q1"
    q.on_saved("q1", "fast")
    assert state(q, "q1").state == "done"
    assert q.next_starts() == []                                   # no free core
    fin(q, "q3", "deep")
    assert ids(q.next_starts()) == [("q2", "deep", 2)]


def test_e4_core_sharing_option_b():
    q = AnalysisQueue(8)
    q.add(["F1", "F2", "F3"], "fast", automatic=True, current=NONE)
    assert len(q.next_starts()) == 3
    q.add(["A", "B"], "deep", automatic=False, current=NONE)      # q4, q5
    assert q.next_starts() == []
    q.add(["F4"], "fast", automatic=True, current=NONE)           # q6
    assert ids(q.next_starts()) == [("q6", "fast", 1)]
    for i in ("q1", "q2", "q3"):
        fin(q, i, "fast")
        assert q.next_starts() == []
    fin(q, "q6", "fast")
    assert ids(q.next_starts()) == [("q4", "deep", 8)]
    q.add(["F5"], "fast", automatic=True, current=NONE)           # q7
    assert q.next_starts() == []
    fin(q, "q4", "deep")
    assert ids(q.next_starts()) == [("q7", "fast", 1)]
    fin(q, "q7", "fast")
    assert ids(q.next_starts()) == [("q5", "deep", 8)]


def test_e5_lowered_cores_apply_as_items_end():
    q = AnalysisQueue(8)
    q.add(["A"], "deep", automatic=False, current=NONE)
    assert ids(q.next_starts()) == [("q1", "deep", 8)]
    q.set_cores(4)
    snap = q.snapshot()
    assert (snap.cores_in_use, snap.cores_setting, snap.deep[0].cores) == (8, 4, 8)
    q.add(["G"], "fast", automatic=True, current=NONE)
    assert q.next_starts() == []
    fin(q, "q1", "deep")
    assert ids(q.next_starts()) == [("q2", "fast", 1)]
    q.add(["H"], "deep", automatic=False, current=NONE)           # q3
    fin(q, "q2", "fast")
    assert ids(q.next_starts()) == [("q3", "deep", 4)]


def test_e6_pause():
    q = AnalysisQueue(2)
    q.add(["R"], "fast", automatic=True, current=NONE)
    assert ids(q.next_starts()) == [("q1", "fast", 1)]
    q.pause()
    rep = q.add(["G"], "fast", automatic=True, current=NONE)
    assert rep.added[0].state == "waiting"
    assert q.next_starts() == []
    assert q.snapshot().paused
    fin(q, "q1", "fast")                                           # running items finish normally
    assert q.next_starts() == []
    q.resume()
    assert ids(q.next_starts()) == [("q2", "fast", 1)]


def test_e7_duplicates():
    q = AnalysisQueue(4)
    cur = {"A": frozenset({"fast"})}
    rep = q.add(["A", "B", "B"], "fast", automatic=False, current=cur)
    assert [v.unit_key for v in rep.added] == ["B"]
    assert rep.skipped == (("A", "current"), ("B", "waiting"))
    assert [v.unit_key for v in q.add(["A"], "deep", automatic=False, current=cur).added] == ["A"]
    assert q.addable(["A", "B", "C", "C"], "fast", cur) == ("C",)   # no side effects
    assert len(q.snapshot().fast) == 1
    q.next_starts()                                                # only B runs (a fast item runs, so the deep item waits)
    assert q.add(["B"], "fast", automatic=True, current=NONE).skipped == (("B", "running"),)
    fin(q, "q1", "fast")                                           # B saving: still "running"
    assert q.add(["B"], "fast", automatic=True, current=NONE).skipped == (("B", "running"),)
    q.on_saved("q1", "fast")
    cur2 = {"B": frozenset({"fast"})}
    assert q.add(["B"], "fast", automatic=True, current=cur2).skipped == (("B", "current"),)
    q.clear_finished()
    assert q.add(["B"], "fast", automatic=True, current=cur2).skipped == (("B", "current"),)


def test_e8_stopped_deep_item_keeps_its_fast_result():
    q = AnalysisQueue(2)
    q.add(["A"], "deep", automatic=False, current=NONE)
    q.next_starts()
    q.on_result("q1", "fast", "done", None, saving=True)
    q.on_saved("q1", "fast")
    assert q.on_finished("q1", "stopped", None)
    v = state(q, "q1")
    assert (v.state, v.reason, v.saved_passes) == ("stopped", "stopped by the user", frozenset({"fast"}))


def test_e9_deep_item_whose_fast_stage_is_held():
    q = AnalysisQueue(2)
    q.add(["A"], "deep", automatic=False, current=NONE)
    q.next_starts()
    why = "a06: no resolution curve for a06 at slit 3"
    q.on_result("q1", "fast", "held", why, saving=False)
    q.on_finished("q1", "completed", None)
    v = state(q, "q1")
    assert (v.state, v.reason) == ("held", why)


def test_e10_search_limited_deep_result_is_done_with_a_warning():
    q = AnalysisQueue(2)
    q.add(["A"], "deep", automatic=False, current=NONE)
    q.next_starts()
    why = "comparison incomplete, 1 model unresolved after restarts"
    q.on_result("q1", "deep", "done_warning", why, saving=True)
    q.on_finished("q1", "completed", None)
    assert state(q, "q1").state == "saving"
    q.on_saved("q1", "deep")
    v = q.snapshot().finished[0]
    assert (v.state, v.reason, v.saved_passes) == ("done_warning", why, frozenset({"deep"}))
    q.clear_finished()
    assert q.snapshot().finished == ()


def test_e11_cores_a_deep_item_cannot_use_go_to_the_next_runnable_item():
    q = AnalysisQueue(8)
    q.add(["A", "B", "C"], "deep", automatic=False, current=NONE)
    assert ids(q.next_starts()) == [("q1", "deep", 8)]
    assert q.on_cores("q1", 3) is True
    assert q.snapshot().cores_in_use == 3
    assert ids(q.next_starts()) == [("q2", "deep", 5)]
    q.add(["F"], "fast", automatic=True, current=NONE)             # q4
    assert q.next_starts() == []
    assert q.on_cores("q2", 2)
    assert ids(q.next_starts()) == [("q4", "fast", 1)]             # fast first; q3 waits (a fast item runs)
    assert q.snapshot().cores_in_use == 6
    fin(q, "q4", "fast")
    assert ids(q.next_starts()) == [("q3", "deep", 3)]


def test_e12_the_reports_edges():
    q = AnalysisQueue(4)
    q.add(["A"], "deep", automatic=False, current=NONE)
    assert ids(q.next_starts()) == [("q1", "deep", 4)]
    with pytest.raises(ValueError):
        q.on_cores("q1", 5)
    with pytest.raises(ValueError):
        q.on_cores("q1", 0)
    assert q.on_cores("q1", 4) is False
    q.on_result("q1", "fast", "done", None, saving=True)
    assert q.on_cores("q1", 2) is True
    snap = q.snapshot()
    assert (snap.cores_in_use, snap.deep[0].cores) == (2, 2)
    assert q.abandon_active("the analysis process ended") == ("q1",)
    assert q.on_cores("q1", 1) is False
    q.add(["B"], "fast", automatic=True, current=NONE)
    assert q.on_cores("q2", 1) is False                            # waiting item


def test_settled_state_table():
    q = AnalysisQueue(8)
    q.add(["A", "B", "C", "D", "E"], "fast", automatic=False, current=NONE)
    q.next_starts()
    # save failed
    q.on_result("q1", "fast", "done", None, saving=True)
    q.on_finished("q1", "completed", None)
    assert q.on_save_failed("q1", "fast", "disk full")
    assert (state(q, "q1").state, state(q, "q1").reason) == ("failed", "not saved: disk full")
    # own present, not saving: already current
    q.on_result("q2", "fast", "already_current", "already current", saving=False)
    q.on_finished("q2", "completed", None)
    assert state(q, "q2").state == "already_current"
    # no own: failed with the finished reason, or the fallback; completed without result
    q.on_finished("q3", "failed", "boom")
    assert (state(q, "q3").state, state(q, "q3").reason) == ("failed", "boom")
    q.on_finished("q4", "failed", None)
    assert state(q, "q4").reason == "the fitting process failed"
    q.on_finished("q5", "completed", None)
    assert state(q, "q5").reason == "the fitting process ended without a result"
    assert [v.item_id for v in q.snapshot().finished] == ["q1", "q2", "q3", "q4", "q5"]


def test_save_may_arrive_before_the_process_ends():
    q = AnalysisQueue(2)
    q.add(["A"], "fast", automatic=False, current=NONE)
    q.next_starts()
    q.on_result("q1", "fast", "done", None, saving=True)
    assert q.on_saved("q1", "fast")
    assert state(q, "q1").state == "running"
    q.on_finished("q1", "completed", None)
    assert state(q, "q1").state == "done"
    assert q.on_saved("q1", "fast") is False                       # nothing pending any more


def test_progress_and_started_are_recorded_verbatim():
    q = AnalysisQueue(2)
    q.add(["A"], "fast", automatic=False, current=NONE)
    q.next_starts()
    assert q.on_started("q1", at=10.0)
    ev = {"pass": "fast", "phase": "ladder", "step": 1, "of": 5, "stage": None, "stages": None}
    assert q.on_progress("q1", ev, at=12.5)
    v = state(q, "q1")
    assert (v.started_at, v.progress_at, dict(v.progress)) == (10.0, 12.5, ev)
    ev["step"] = 99
    assert state(q, "q1").progress["step"] == 1                    # the view holds a copy


def test_remove_and_argument_errors():
    q = AnalysisQueue(1)
    q.add(["A", "B"], "fast", automatic=False, current=NONE)
    q.next_starts()                                                # q1 runs
    assert q.remove("q1") is False and state(q, "q1").state == "running"
    assert q.remove("q2") is True and q.remove("q2") is False
    assert q.snapshot().fast[0].item_id == "q1"
    assert q.remove("nope") is False
    with pytest.raises(ValueError):
        AnalysisQueue(0)
    with pytest.raises(ValueError):
        q.set_cores(0)
    with pytest.raises(ValueError):
        q.add(["X"], "medium", automatic=False, current=NONE)
    for call in (lambda: q.on_started("zz", at=0.0), lambda: q.on_cores("zz", 1),
                 lambda: q.on_saved("zz", "fast"), lambda: q.on_finished("zz", "completed", None)):
        with pytest.raises(KeyError):
            call()


def test_events_after_abandon_return_false_and_waiting_items_stay():
    q = AnalysisQueue(2)
    q.add(["A", "B", "C"], "fast", automatic=False, current=NONE)
    q.next_starts()                                                # q1, q2 run
    q.on_result("q1", "fast", "done", None, saving=True)
    q.on_finished("q1", "completed", None)                         # q1 saving
    assert q.abandon_active("the analysis process ended") == ("q1", "q2")
    for i in ("q1", "q2"):
        assert state(q, i).state == "failed" and state(q, i).reason == "the analysis process ended"
        assert q.on_started(i, at=1.0) is False
        assert q.on_progress(i, {}, at=1.0) is False
        assert q.on_result(i, "fast", "done", None, saving=False) is False
        assert q.on_saved(i, "fast") is False
        assert q.on_save_failed(i, "fast", "x") is False
        assert q.on_finished(i, "completed", None) is False
    assert state(q, "q3").state == "waiting"
    assert q.snapshot().cores_in_use == 0
    assert ids(q.next_starts()) == [("q3", "fast", 1)]


def test_inter_item_shape_and_fast_members_first():
    q = AnalysisQueue(8)
    q.add(["A"], "fast", automatic=True, current=NONE)             # q1 runs
    assert ids(q.next_starts()) == [("q1", "fast", 1)]
    v = q.add_inter("b1", ["A", "B"])                              # q2 waits for q1
    assert (v.unit_key, v.pass_kind, v.members) == ("inter:b1:batch", "inter", ("A", "B"))
    assert state(q, "q1").members == ("A",)
    assert q.next_starts() == []
    assert state(q, "q2").waiting_on == "q1"
    assert [w.item_id for w in q.snapshot().inter] == ["q2"]
    fin(q, "q1", "fast")                                           # saving still blocks the batch
    assert q.next_starts() == []
    q.on_saved("q1", "fast")
    assert ids(q.next_starts()) == [("q2", "inter", 8)]           # the batch claims every idle core


def test_running_batch_blocks_fast_and_deep_on_members_only_and_gives_cores_back():
    q = AnalysisQueue(4)
    q.add_inter("b1", ["A", "B"])
    assert ids(q.next_starts()) == [("q1", "inter", 4)]
    q.add(["A"], "fast", automatic=True, current=NONE)             # q2 (member)
    q.add(["B"], "deep", automatic=False, current=NONE)            # q3 (member)
    q.add(["C"], "fast", automatic=True, current=NONE)             # q4 (not a member)
    assert q.next_starts() == []
    assert q.on_cores("q1", 2)                                     # the batch gives back two cores
    assert ids(q.next_starts()) == [("q4", "fast", 1)]
    assert (state(q, "q2").waiting_on, state(q, "q3").waiting_on) == ("q1", "q1")


def test_batch_waits_only_for_running_items_on_its_members():
    q = AnalysisQueue(4)
    q.add(["A", "B"], "deep", automatic=False, current=NONE)       # q1 (A), q2 (B)
    assert ids(q.next_starts()) == [("q1", "deep", 4)]
    assert q.on_cores("q1", 2)
    assert ids(q.next_starts()) == [("q2", "deep", 2)]
    q.add_inter("b1", ["A"])                                       # q3 waits for q1 only
    assert state(q, "q3").waiting_on == "q1"
    fin(q, "q1", "deep")
    q.on_saved("q1", "deep")
    assert ids(q.next_starts()) == [("q3", "inter", 2)]           # q2 on B keeps running


def test_overlapping_batches_serialise_and_no_fast_item_runs_before_a_batch():
    q = AnalysisQueue(4)
    q.add(["F"], "fast", automatic=True, current=NONE)             # q1 runs
    assert ids(q.next_starts()) == [("q1", "fast", 1)]
    q.add_inter("b1", ["A", "B"])                                  # q2: waits while fast runs
    assert q.next_starts() == []
    fin(q, "q1", "fast")
    q.on_saved("q1", "fast")
    assert ids(q.next_starts()) == [("q2", "inter", 4)]
    q.add_inter("b2", ["B", "C"])                                  # q3 overlaps q2 on B
    assert q.next_starts() == []
    assert state(q, "q3").waiting_on == "q2"
    fin(q, "q2", "inter")
    q.on_saved("q2", "inter")
    assert ids(q.next_starts()) == [("q3", "inter", 4)]


def test_batch_results_accumulate_and_settle_after_every_save():
    q = AnalysisQueue(4)
    q.add_inter("b1", ["A"])
    q.next_starts()                                                # q1
    q.on_result("q1", "inter", "done", None, saving=True)          # ring 1
    q.on_result("q1", "inter", "done", None, saving=True)          # ring 2
    assert q.on_saved("q1", "inter")
    assert state(q, "q1").state == "running"                       # the process still runs between rings
    assert q.on_saved("q1", "inter")
    q.on_result("q1", "inter", "done_warning", "1 spectrum not reached", saving=True, end_record=True)   # ring 3, the end
    q.on_finished("q1", "completed", None)
    assert state(q, "q1").state == "saving"
    assert q.on_saved("q1", "inter")
    v = q.snapshot().finished[0]
    assert (v.state, v.reason, v.saved_passes) == ("done_warning", "1 spectrum not reached", frozenset({"inter"}))


def test_a_batch_that_completes_without_an_end_record_settles_failed_not_on_its_last_ring():
    q = AnalysisQueue(4)
    q.add_inter("b1", ["A"])
    q.next_starts()                                                # q1
    q.on_result("q1", "inter", "done", None, saving=True)          # ring 1 saved, then the process ends
    q.on_saved("q1", "inter")
    q.on_finished("q1", "completed", None)
    assert (state(q, "q1").state, state(q, "q1").reason) == ("failed", "the batch ended without an end record")


def test_stopped_or_failed_batch_with_saved_rings_does_not_settle_done():
    q = AnalysisQueue(4)
    q.add_inter("b1", ["A"])
    q.next_starts()                                                # q1
    q.on_result("q1", "inter", "done", None, saving=True)
    q.on_saved("q1", "inter")
    q.on_finished("q1", "stopped", None)
    v = state(q, "q1")
    assert (v.state, v.reason, v.saved_passes) == (
        "stopped", "stopped by the user; completed rings/sweeps are kept", frozenset({"inter"}))
    q.add_inter("b2", ["A"])                                       # q2
    q.next_starts()
    q.on_result("q2", "inter", "done", None, saving=True)
    q.on_saved("q2", "inter")
    q.on_finished("q2", "failed", "the fitting process crashed")
    assert (state(q, "q2").state, state(q, "q2").reason) == ("failed", "the fitting process crashed")


def test_failed_save_settles_the_batch_failed():
    q = AnalysisQueue(4)
    q.add_inter("b1", ["A"])
    q.next_starts()                                                # q1
    q.on_result("q1", "inter", "done", None, saving=True)          # ring 1 saved
    assert q.on_saved("q1", "inter")
    q.on_result("q1", "inter", "done", None, saving=True)          # ring 2 cannot be written
    q.on_finished("q1", "completed", None)
    assert q.on_save_failed("q1", "inter", "WorkspaceBusy")
    assert (state(q, "q1").state, state(q, "q1").reason) == ("failed", "not saved: WorkspaceBusy")


def test_random_sequence_keeps_the_invariants():
    rng = random.Random(20261027)
    q = AnalysisQueue(8)
    keys = [f"K{i}" for i in range(6)]
    pending = []                       # (item_id, pass_kind) the writer has not answered
    handed_out: set[str] = set()

    def check_after_starts(before_in_use):
        snap = q.snapshot()
        items = snap.fast + snap.deep + snap.inter
        occupied = [f for v in items if v.state in ("running", "saving") for f in v.members]
        assert len(occupied) == len(set(occupied))
        assert snap.cores_in_use == sum(v.cores for v in items if v.state == "running")
        assert all(v.cores == 0 for v in items if v.state != "running")
        return snap, items

    for _ in range(600):
        op = rng.choice(["add", "add", "batch", "start", "start", "cores", "end", "end", "save", "setc", "pause"])
        snap = q.snapshot()
        running = [v for v in snap.fast + snap.deep + snap.inter if v.state == "running"]
        if op == "add":
            q.add(rng.sample(keys, rng.randint(1, 3)), rng.choice(["fast", "deep"]),
                  automatic=rng.random() < 0.5, current=NONE)
        elif op == "batch":
            q.add_inter(f"b{rng.randrange(10**6)}", rng.sample(keys, rng.randint(1, 3)))
        elif op == "start":
            before = snap.cores_in_use
            started = q.next_starts()
            for s in started:
                assert s.item_id not in handed_out                 # nothing is returned twice
                assert s.cores >= 1 and (s.pass_kind != "fast" or s.cores == 1)
                handed_out.add(s.item_id)
            snap2, items = check_after_starts(before)
            assert snap2.cores_in_use <= max(snap.cores_setting, before)
            free = snap2.cores_setting - snap2.cores_in_use
            if not snap2.paused and free >= 1:
                waiting_runnable = [v for v in items if v.state == "waiting" and v.runnable]
                assert not [v for v in waiting_runnable if v.pass_kind == "fast"]
                fast_running = any(v.state == "running" and v.pass_kind == "fast" for v in items)
                if not fast_running:
                    assert not waiting_runnable
        elif op == "cores" and running:
            v = rng.choice(running)
            n = rng.randint(1, v.cores)
            changed = q.on_cores(v.item_id, n)
            assert changed == (n < v.cores)
            assert q.snapshot().cores_in_use == snap.cores_in_use - (v.cores - n)
        elif op == "end" and running:
            v = rng.choice(running)
            if rng.random() < 0.8:
                q.on_result(v.item_id, v.pass_kind, "done", None, saving=True)
                pending.append((v.item_id, v.pass_kind))
                q.on_finished(v.item_id, "completed", None)
            else:
                q.on_finished(v.item_id, "stopped", None)
            assert q.snapshot().cores_in_use == snap.cores_in_use - v.cores
        elif op == "save" and pending:
            iid, pk = pending.pop(rng.randrange(len(pending)))
            assert q.on_saved(iid, pk)
        elif op == "setc":
            q.set_cores(rng.randint(1, 10))
        elif op == "pause":
            q.resume() if snap.paused else q.pause()
        check_after_starts(snap.cores_in_use)

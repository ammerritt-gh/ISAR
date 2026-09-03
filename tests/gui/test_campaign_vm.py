"""CampaignViewModel tests — segment-scoped reduction, cursor-exact feed delivery, honest status.

Events are written with the engine's own ``log_event`` (real JSONL, real fsync discipline) into a
synthetic workspace, and read back through the facade exactly as the GUI does. The multi-segment
shapes mirror the loop's real call sites (campaign_start/model_update decides, run spend ledger,
operator_stop ack, terminal stop) — see isar/drive/loop.py.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from isar.drive.session_log import log_event
from isar.workspace_api import Workspace
from isar_gui.viewmodels.campaign import (
    CampaignState, CampaignViewModel, budget_readout, current_activity_text, terminal_status_text,
    reduce_campaign, render_feed_item)


def _log_path(root):
    return root / "report" / "session_log.jsonl"


def _start_event(path, sha="a" * 64, kind="goal", n_obs=0, spent=0.0):
    log_event(path, "decide", {
        "event": "campaign_start", "action": "campaign_start",
        "objective": {"tier": "deterministic", "budget": {"kind": "neutrons", "amount": 1e9}},
        "source": {"kind": kind, "sha256": sha},
        "resumed": {"scan_counter": 0, "n_observations": n_obs, "spent": spent}})


def _run_event(path, it, cost=1000.0, job="j-1"):
    log_event(path, "run", {"job_id": job, "state": "queued", "eta": {"estimated_seconds": 30.0},
                            "attempt": 0, "kind": "production", "ncount": 100, "n_points": 10,
                            "hkl": [2.0, 0.0, 0.0], "cost": cost, "iter": it})


def _model_update(path, it, converged, n_grid, n_obs):
    log_event(path, "decide", {"event": "model_update", "action": "model_update",
                               "n_observations": n_obs, "converged": converged,
                               "n_grid": n_grid, "iter": it})


# ---- reducer (pure) ----------------------------------------------------------
def test_reducer_scopes_to_latest_segment_but_sums_spend_whole_log(make_ws, tmp_path):
    root = make_ws(rows=[])
    path = _log_path(root)
    # Segment 1: runs, a model update, a terminal stop.
    _start_event(path, sha="1" * 64)
    _run_event(path, 0, cost=500.0)
    _model_update(path, 0, converged=1, n_grid=4, n_obs=3)
    log_event(path, "stop", {"state": "unmeetable", "reasons": ["budget exhausted"]})
    # Segment 2: a resume with fresh iteration numbering, no stop yet.
    _start_event(path, sha="2" * 64, kind="resume", n_obs=3, spent=500.0)
    _run_event(path, 0, cost=250.0, job="j-2")
    _run_event(path, 1, cost=250.0, job="j-3")
    _model_update(path, 1, converged=2, n_grid=4, n_obs=5)
    log_event(path, "timing", {"stage": "propose", "iteration": 1, "seconds": 0.5, "ok": True})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 1})

    ws = Workspace.open(root)
    state = reduce_campaign(list(ws.session_events()))
    assert state.has_campaign
    assert state.source == {"kind": "resume", "sha256": "2" * 64}
    assert state.stop_state is None                  # segment 1's stop must not bleed through
    assert state.iteration == 1                      # segment-2 numbering, not cumulative
    assert state.converged == 2 and state.n_grid == 4 and state.n_observations == 5
    assert state.exception_count == 1
    assert state.spent_neutrons == 1000.0            # 500 + 250 + 250 across ALL segments
    assert state.scan_count == 3
    assert state.budget_kind == "neutrons" and state.budget_amount == 1e9
    assert state.stage_seconds.get("propose") == 0.5


def test_reducer_empty_log_and_operator_stop(make_ws):
    root = make_ws(rows=[])
    assert not reduce_campaign([]).has_campaign

    path = _log_path(root)
    _start_event(path)
    log_event(path, "decide", {"event": "operator_stop", "action": "operator_stop",
                               "iter": 2, "token": "t" * 32})
    log_event(path, "stop", {"state": "operator_stopped",
                             "reasons": ["operator stop request honored at iteration 2"],
                             "grid_state": {"state": "continue", "reasons": [], "per_q": []}})
    ws = Workspace.open(root)
    state = reduce_campaign(list(ws.session_events()))
    assert state.stop_state == "operator_stopped"
    assert state.grid_state == {"state": "continue", "reasons": [], "per_q": []}
    assert any("iteration 2" in r for r in state.stop_reasons)


def test_reducer_tolerates_unknown_kinds_and_missing_fields(make_ws):
    root = make_ws(rows=[])
    path = _log_path(root)
    _start_event(path)
    log_event(path, "someday_new_kind", {"whatever": 1})
    log_event(path, "run", {})                      # every field missing
    ws = Workspace.open(root)
    state = reduce_campaign(list(ws.session_events()))   # must not raise
    assert state.scan_count == 1


def test_reducer_captures_last_scan_identical_to_the_feed_line(make_ws):
    # last_scan is state-derived so a consumer built mid-campaign (the lazily-created compact
    # monitor) shows the current scan immediately — and it must be the SAME text the feed shows.
    root = make_ws(rows=[])
    path = _log_path(root)
    _start_event(path)
    propose = {"hkl": [2.0, 0.0, 0.0], "e_lo": 1.0, "e_hi": 9.0, "ncount": 200,
               "kind": "production", "iter": 3}
    log_event(path, "propose", propose)
    ws = Workspace.open(root)
    state = reduce_campaign(list(ws.session_events()))
    assert state.last_scan == render_feed_item({"kind": "propose", "ts": 1.0,
                                                "payload": propose}).text

    _run_event(path, 3, job="j-9")                    # the submitted scan supersedes the proposal
    ws.refresh()
    state = reduce_campaign(list(ws.session_events()))
    assert "j-9" in state.last_scan

    _start_event(path, sha="b" * 64)                  # a new segment starts with no scan yet
    ws.refresh()
    assert reduce_campaign(list(ws.session_events())).last_scan is None


# ---- budget readout (pure) ---------------------------------------------------
def test_budget_readout_time_budgets_use_the_engine_dichotomy():
    # The engine's canonical time kind is counting_seconds (isar/cli/main.py::_parse_budget,
    # goal_to_spec::_budget) and its ledger split is neutrons-vs-everything-else (loop.py
    # budget_state) — the readout must not keep a second kind list that misses it.
    st = CampaignState(has_campaign=True, budget_kind="counting_seconds",
                       budget_amount=8 * 3600.0, spent_seconds=2 * 3600.0)
    permille, text = budget_readout(st)
    assert permille == 250
    assert "2.00 / 8.00 h" in text
    st = CampaignState(has_campaign=True, budget_kind="counting_seconds",
                       budget_amount=120.0, spent_seconds=30.0)
    permille, text = budget_readout(st)
    assert permille == 250 and "30s / 120s" in text
    assert budget_readout(CampaignState())[1] == "budget: —"     # no budget -> honest dash


def test_terminal_outcome_text_names_the_exhausted_clock_exactly():
    assert terminal_status_text(CampaignState(
        stop_state="incomplete", stop_reasons=("analysis limit exhausted (3600/3600s)",))) == (
            "Incomplete — analysis limit exhausted")
    assert terminal_status_text(CampaignState(
        stop_state="incomplete", stop_reasons=("budget exhausted (8/8 h)",))) == (
            "Incomplete — measurement budget exhausted")


# ---- feed rendering ----------------------------------------------------------
def test_feed_renders_kinds_and_skips_timing():
    assert render_feed_item({"kind": "timing", "payload": {"stage": "x"}, "ts": 1}) is None
    prop = render_feed_item({"kind": "propose", "ts": 1, "payload": {
        "hkl": [2, 0, 0], "e_lo": 1.0, "e_hi": 9.0, "ncount": 200, "kind": "production"}})
    assert "H=2" in prop.text and prop.severity == "info"
    stop = render_feed_item({"kind": "stop", "ts": 1, "payload": {
        "state": "operator_stopped", "reasons": ["op"]}})
    assert stop.severity == "terminal" and "operator stopped" in stop.text
    unknown = render_feed_item({"kind": "mystery", "ts": 1, "payload": {}})
    assert unknown is not None                       # unknown kinds surface, never vanish


def test_now_panel_exact_activity_strings_and_per_q_join(make_ws):
    root = make_ws(rows=[])
    path = _log_path(root)
    _start_event(path)
    log_event(path, "decide", {"event": "model_update", "action": "model_update",
                                "per_q": [{"hkl": [2.5, 0, 0], "state": "continue"}]})
    log_event(path, "activity_start", {
        "activity_id": "fit-1", "stage": "full_fit", "q": [2.5, 0, 0],
        "fit_tier": "full", "started_ts": 100.0, "completed": 0,
        "total": 8, "progress_unit": "fits"}, ts=100.0)
    log_event(path, "activity_progress", {
        "activity_id": "fit-1", "completed": 3, "total": 8,
        "progress_unit": "fits"}, ts=150.0)
    state = reduce_campaign(list(Workspace.open(root).session_events()))
    assert state.per_q == ({"hkl": [2.5, 0, 0], "state": "continue"},)
    assert current_activity_text(state, now=202.0) == (
        "Now: Full fit at (2.50, 0, 0) — 3/8 fits — 01:42 elapsed")


def test_final_certification_exact_live_publishing_and_interrupted_strings():
    live = CampaignState(active_activity={
        "activity_id": "cert-1", "stage": "final_certification", "started_ts": 100.0,
        "completed": 7, "total": 21, "progress_unit": "points", "active": 14,
        "remaining_analysis_seconds": 18 * 60 + 42, "phase": "fitting",
    })
    assert current_activity_text(live, now=352.0) == (
        "Now: Final certification — 7/21 points checkpointed — 14 active — "
        "18:42 analysis left — 04:12 elapsed")
    publishing = CampaignState(active_activity={
        **live.active_activity, "completed": 21, "active": 0, "phase": "publishing",
    })
    assert current_activity_text(publishing, now=352.0) == (
        "Now: Final certification — 21/21 points checkpointed — publishing — 04:12 elapsed")
    unlimited = CampaignState(active_activity={
        **live.active_activity, "remaining_analysis_seconds": None,
    })
    assert "analysis left" not in current_activity_text(unlimited, now=352.0)
    malformed = CampaignState(active_activity={
        **live.active_activity, "remaining_analysis_seconds": "not-a-clock",
    })
    assert "analysis left" not in current_activity_text(malformed, now=352.0)
    interrupted = CampaignState(certification_status={
        "completed": 7, "total": 21, "phase": "interrupted", "published": False,
    })
    assert current_activity_text(interrupted) == (
        "Final certification interrupted — 7/21 checkpoints retained")


def test_unmatched_activity_before_resume_is_not_current(make_ws):
    root = make_ws(rows=[])
    path = _log_path(root)
    _start_event(path)
    log_event(path, "activity_start", {
        "activity_id": "old", "stage": "scan", "q": [2.2, 0, 0]}, ts=10.0)
    _start_event(path, sha="b" * 64, kind="resume")
    state = reduce_campaign(list(Workspace.open(root).session_events()))
    assert state.active_activity is None


def test_interrupted_certification_end_is_retained_for_durable_status_join(make_ws):
    root = make_ws(rows=[])
    path = _log_path(root)
    _start_event(path)
    log_event(path, "activity_start", {
        "activity_id": "cert-old", "stage": "final_certification", "total": 21}, ts=10.0)
    log_event(path, "activity_end", {
        "activity_id": "cert-old", "stage": "final_certification",
        "status": "interrupted"}, ts=11.0)

    state = reduce_campaign(list(Workspace.open(root).session_events()))

    assert state.active_activity is None
    assert state.certification_end_status == "interrupted"


# ---- viewmodel: cursor + liveness + terminal summary --------------------------
@pytest.fixture
def vm_ws(make_ws):
    root = make_ws(rows=[])
    ws = Workspace.open(root)
    vm = CampaignViewModel(ws)
    return vm, ws, root


def _tick(vm, ws):
    ws.refresh()
    vm.on_workspace_changed({"events"})


def test_cursor_delivers_each_record_exactly_once(qtbot, vm_ws):
    vm, ws, root = vm_ws
    path = _log_path(root)
    delivered = []
    vm.feed_appended.connect(delivered.extend)

    _start_event(path)
    _run_event(path, 0)
    _tick(vm, ws)
    assert len(delivered) == 2

    _model_update(path, 0, 1, 4, 2)
    log_event(path, "timing", {"stage": "propose", "seconds": 0.1})   # cursor counts it, feed skips
    _tick(vm, ws)
    assert len(delivered) == 3
    _tick(vm, ws)                                     # no new records -> no re-delivery
    assert len(delivered) == 3


def test_truncated_final_line_delivered_once_when_completed(qtbot, vm_ws):
    vm, ws, root = vm_ws
    path = _log_path(root)
    delivered = []
    vm.feed_appended.connect(delivered.extend)

    _start_event(path)
    _tick(vm, ws)
    assert len(delivered) == 1

    partial = json.dumps({"ts": 1.0, "kind": "propose", "payload": {"hkl": [1, 0, 0]}})
    with open(path, "a", encoding="utf-8") as f:
        f.write(partial[: len(partial) // 2])         # a crash mid-write: no newline, invalid JSON
    _tick(vm, ws)
    assert len(delivered) == 1                        # the facade drops the partial line

    with open(path, "a", encoding="utf-8") as f:      # the "crashed" writer completes the line
        f.write(partial[len(partial) // 2:] + "\n")
    _tick(vm, ws)
    assert len(delivered) == 2                        # delivered exactly once, never twice


def test_shrunken_log_resets_the_feed(qtbot, vm_ws):
    vm, ws, root = vm_ws
    path = _log_path(root)
    resets = []
    vm.feed_reset.connect(lambda: resets.append(True))
    delivered = []
    vm.feed_appended.connect(delivered.extend)

    _start_event(path)
    _run_event(path, 0)
    _tick(vm, ws)
    assert len(delivered) == 2

    path.write_text("", encoding="utf-8")             # a fresh log file (new workspace epoch)
    _start_event(path)
    _tick(vm, ws)
    assert resets == [True]
    assert len(delivered) == 3                        # the fresh segment re-delivered post-reset


def test_live_progress_needs_no_campaign_summary(vm_ws):
    vm, ws, root = vm_ws
    path = _log_path(root)
    _start_event(path)
    _run_event(path, 0)
    _model_update(path, 0, 1, 4, 2)
    _tick(vm, ws)
    assert not (root / "report" / "campaign_summary.json").exists()
    assert vm.state.iteration == 0 and vm.state.converged == 1 and vm.state.spent_neutrons > 0


def test_terminal_summary_gated_on_liveness_and_stop(vm_ws):
    vm, ws, root = vm_ws
    path = _log_path(root)
    _start_event(path)
    log_event(path, "stop", {"state": "met", "reasons": ["all q converged"]})
    (root / "report" / "campaign_summary.json").write_text(
        json.dumps({"stop": {"state": "met", "reasons": ["all q converged"]}, "n_observations": 5}),
        encoding="utf-8")
    vm.set_process_alive(True)
    _tick(vm, ws)
    assert vm.terminal_summary is None                # alive -> the summary may be stale, hide it
    vm.set_process_alive(False)
    assert vm.terminal_summary is not None


def test_scan_verdict_counts_by_definition(make_ws):
    root = make_ws(rows=[
        {"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0},
        {"scan": 2, "detector": "det", "status": "fitted", "omega0": 5.1,
         "disposition": "needs_review"},
        {"scan": 3, "detector": "det", "status": "held_no_calibration"},
    ])
    vm = CampaignViewModel(Workspace.open(root))
    counts = vm.scan_verdicts
    assert counts.get("held", 0) == 1
    assert counts.get("flagged", 0) == 1
    assert counts.get("ok", 0) >= 1

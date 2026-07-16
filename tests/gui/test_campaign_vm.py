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
from isar_gui.viewmodels.campaign import CampaignViewModel, reduce_campaign, render_feed_item


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


# ---- feed rendering ----------------------------------------------------------
def test_feed_renders_kinds_and_skips_timing():
    assert render_feed_item({"kind": "timing", "payload": {"stage": "x"}, "ts": 1}) is None
    prop = render_feed_item({"kind": "propose", "ts": 1, "payload": {
        "hkl": [2, 0, 0], "e_lo": 1.0, "e_hi": 9.0, "ncount": 200, "kind": "production"}})
    assert "q=(2, 0, 0)" in prop.text and prop.severity == "info"
    stop = render_feed_item({"kind": "stop", "ts": 1, "payload": {
        "state": "operator_stopped", "reasons": ["op"]}})
    assert stop.severity == "terminal" and "operator_stopped" in stop.text
    unknown = render_feed_item({"kind": "mystery", "ts": 1, "payload": {}})
    assert unknown is not None                       # unknown kinds surface, never vanish


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

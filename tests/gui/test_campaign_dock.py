"""CampaignDock tests — form mapping, Start/Resume honesty, stop routing, enablement, feed.

The dock talks only to a CampaignViewModel (state) and the coordinator (control); both are faked
or driven through their real public surface here. No modal paths execute (the spec Browse dialog
is never triggered).
"""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("PySide6")

from isar.drive.session_log import log_event
from isar.workspace_api import Workspace
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.campaign import CampaignViewModel
from isar_gui.views.campaign_dock import CampaignDock


class _Ops:
    """Coordinator stand-in exposing exactly the surface the dock uses."""

    def __init__(self):
        self.drive_requests = []
        self.stop_requests = 0
        self.force_requests = 0
        self.running = False
        self.stop_pending = False
        # borrowed real signals: the dock connects to these names
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            drive_started = Signal()
            drive_finished = Signal(int, bool)
            activity_changed = Signal(object)

        self._sig = _Sig()
        self.drive_started = self._sig.drive_started
        self.drive_finished = self._sig.drive_finished
        self.activity_changed = self._sig.activity_changed

    def request_drive(self, opts, *, env=None):
        self.drive_requests.append((opts, dict(env or {})))
        self.running = True
        return True

    def request_drive_stop(self):
        self.stop_requests += 1
        self.stop_pending = True
        return True

    def cancel_drive(self):
        self.force_requests += 1
        return True

    def is_running_campaign(self):
        return self.running

    def is_busy(self):
        return self.running

    def is_stop_pending(self):
        return self.stop_pending


@pytest.fixture
def dock_env(qtbot, make_ws):
    root = make_ws(rows=[])
    ws = Workspace.open(root)
    vm = CampaignViewModel(ws)
    ops = _Ops()
    dock = CampaignDock(vm, ops, store=None, llm_env=lambda: {"ISAR_LLM": "x"})
    qtbot.addWidget(dock)
    dock.show()          # isVisible() on children is meaningless under an unshown parent
    return dock, vm, ops, ws, root


def _goal_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_start_disabled_until_tavi_ok_and_source_present(dock_env):
    dock, vm, ops, ws, root = dock_env
    assert not dock._start_btn.isEnabled()          # TAVI down + empty goal
    dock.set_backend_status("tavi", "ok", "up")
    assert not dock._start_btn.isEnabled()          # still no goal text
    dock._goal.setPlainText("map the branch")
    assert dock._start_btn.isEnabled()
    dock.set_backend_status("tavi", "down", "gone")
    assert not dock._start_btn.isEnabled()
    assert "TAVI" in dock._start_btn.toolTip()


def test_start_maps_form_to_drive_options_with_llm_env(dock_env):
    dock, vm, ops, ws, root = dock_env
    dock.set_backend_status("tavi", "ok", "up")
    dock._goal.setPlainText("map the branch")
    dock._budget.setText("1e9n")
    dock._seed.setValue(7)
    dock._max_iters.setValue(12)
    dock._start_btn.click()
    assert len(ops.drive_requests) == 1
    opts, env = ops.drive_requests[0]
    assert opts.goal == "map the branch" and opts.spec_path is None and not opts.resume_latest
    assert opts.budget == "1e9n" and opts.seed == 7 and opts.max_iters == 12
    assert env == {"ISAR_LLM": "x"}


def test_resume_labeling_from_fingerprint_match(dock_env):
    dock, vm, ops, ws, root = dock_env
    goal = "map the acoustic branch"
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start",
        "objective": {"budget": {"kind": "neutrons", "amount": 1e9}},
        "source": {"kind": "goal", "sha256": _goal_sha(goal)}})
    ws.refresh()
    vm.on_workspace_changed({"events"})

    dock._goal.setPlainText(goal)                    # same objective -> Resume
    assert dock._start_btn.text() == "Resume"
    assert not dock._mismatch.isVisible()

    dock._goal.setPlainText("a different goal")      # different objective -> explicit warning
    assert dock._start_btn.text() == "Start anyway"
    assert dock._mismatch.isVisible()
    assert "spend ledger" in dock._mismatch.text()


def test_resume_launches_resume_latest_without_goal(dock_env):
    dock, vm, ops, ws, root = dock_env
    goal = "map the acoustic branch"
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start", "objective": {},
        "source": {"kind": "goal", "sha256": _goal_sha(goal)}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock.set_backend_status("tavi", "ok", "up")
    dock._goal.setPlainText(goal)
    dock._start_btn.click()
    opts, _env = ops.drive_requests[0]
    assert opts.resume_latest is True
    assert opts.goal is None and opts.spec_path is None   # the frozen objective, no LLM re-compile


def test_stop_and_force_route_to_coordinator(dock_env):
    dock, vm, ops, ws, root = dock_env
    ops.running = True
    dock._sync_status()
    assert dock._stop_btn.isEnabled()
    assert not dock._force_btn.isVisible()           # force appears only once a stop is pending
    dock._stop_btn.click()
    assert ops.stop_requests == 1
    dock._sync_status()
    assert dock._force_btn.isVisible()
    dock._force_btn.click()
    assert ops.force_requests == 1


def test_status_lines_for_terminal_states(dock_env, make_ws):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "stop", {"state": "operator_stopped", "reasons": ["operator stop"],
                             "grid_state": "continue"})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    assert "press Resume" in dock._status_line.text()


def test_feed_renders_and_filters_exceptions(dock_env):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 0})
    log_event(path, "timing", {"stage": "propose", "seconds": 0.1})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    assert dock._feed.count() == 2                   # timing never reaches the feed
    dock._exceptions_only.setChecked(True)
    assert dock._feed.count() == 1
    assert "proposal_clamped" in dock._feed.item(0).text()


def test_form_round_trips_through_gui_store(qtbot, dock_env, tmp_path):
    dock, vm, ops, ws, root = dock_env
    store = GuiStore.load(root)
    dock.set_store(store)
    dock._goal.setPlainText("persist me")
    dock._max_iters.setValue(42)
    dock._save_timer.stop()
    dock._save_form()          # flush the debounce deterministically

    store2 = GuiStore.load(root)
    dock2 = CampaignDock(vm, ops, store=store2)
    qtbot.addWidget(dock2)
    assert dock2._goal.toPlainText() == "persist me"
    assert dock2._max_iters.value() == 42

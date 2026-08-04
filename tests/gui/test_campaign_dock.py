"""CampaignDock tests — form mapping, Start/Resume honesty, stop routing, enablement, feed.

The dock talks only to a CampaignViewModel (state) and the coordinator (control); both are faked
or driven through their real public surface here. No modal paths execute (the spec Browse dialog
is never triggered).
"""
from __future__ import annotations

from types import SimpleNamespace

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
        self.compile_requests = []
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
            drive_compiled = Signal(object, str)
            drive_compile_failed = Signal(str)

        self._sig = _Sig()
        self.drive_started = self._sig.drive_started
        self.drive_finished = self._sig.drive_finished
        self.activity_changed = self._sig.activity_changed
        self.drive_compiled = self._sig.drive_compiled
        self.drive_compile_failed = self._sig.drive_compile_failed

    def request_drive_compile(self, opts, *, env=None):
        self.compile_requests.append((opts, dict(env or {})))
        objective = {"schema": "isar.objective/2",
                     "path": [[2, 0, 0], [2, 0.5, 0]], "target_branch": "optic",
                     "target_label": "transverse optic", "energy_search_meV": [-2, 12],
                     "tolerance_meV": 0.5,
                     "budget": {"kind": "neutrons", "amount": 1e9}}
        self.drive_compiled.emit({"schema": "isar.objective-envelope/2",
                                  "objective": objective, "source": {"kind": "goal"}},
                                 "frozen-objective.json")
        return True

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

    def drive_options(self):
        # Launch-captured options of the active campaign, mirroring the real coordinator.
        if self.running and self.drive_requests:
            return self.drive_requests[-1][0]
        return None


@pytest.fixture
def dock_env(qtbot, make_ws):
    root = make_ws(rows=[])
    ws = Workspace.open(root)
    vm = CampaignViewModel(ws)
    ops = _Ops()
    dock = CampaignDock(vm, ops, store=None, llm_env=lambda: {"ISAR_LLM": "x"})
    dock._confirm_objective = lambda _envelope: True
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
    dock._analysis_limit.setText("10m")
    dock._seed.setValue(7)
    dock._start_btn.click()
    assert len(ops.compile_requests) == 1
    assert len(ops.drive_requests) == 1
    opts, env = ops.drive_requests[0]
    compile_opts, compile_env = ops.compile_requests[0]
    assert (compile_opts.goal == "map the branch" and compile_opts.spec_path is None
            and compile_opts.budget == "1e9n" and compile_opts.analysis_limit == "10m")
    assert compile_env == {"ISAR_LLM": "x"}
    assert opts.envelope_path == "frozen-objective.json" and not opts.resume_latest
    assert opts.budget is None and opts.seed == 7
    assert not hasattr(opts, "max_iters")            # budgets are the only stop conditions now
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


def test_resume_button_works_without_any_recomputable_fingerprint(dock_env):
    # A pre-fingerprint campaign log (campaign_start without `source`): the primary button cannot
    # compare anything and the form is empty, but the frozen objective in the log is sufficient —
    # the explicit resume route must be reachable (fingerprints decide confidence, not possibility).
    dock, vm, ops, ws, root = dock_env
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start", "objective": {}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock.set_backend_status("tavi", "ok", "up")
    assert not dock._start_btn.isEnabled()           # empty form: no fresh start
    assert dock._resume_btn.isVisible() and dock._resume_btn.isEnabled()
    dock._resume_btn.click()
    opts, _env = ops.drive_requests[0]
    assert opts.resume_latest is True and opts.goal is None and opts.spec_path is None


def test_resume_button_covers_a_moved_spec_file(dock_env, tmp_path):
    # The original spec was moved/deleted: the form fingerprint is unreadable, so the primary
    # button is disabled — but --resume-latest needs no source file at all.
    dock, vm, ops, ws, root = dock_env
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start", "objective": {},
        "source": {"kind": "spec", "sha256": "f" * 64}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock.set_backend_status("tavi", "ok", "up")
    dock._src_spec.setChecked(True)
    dock._spec_path.setText(str(tmp_path / "moved-away.json"))   # does not exist
    assert not dock._start_btn.isEnabled()
    assert dock._resume_btn.isVisible() and dock._resume_btn.isEnabled()
    dock._resume_btn.click()
    assert ops.drive_requests and ops.drive_requests[0][0].resume_latest is True


def test_resume_button_hidden_when_primary_offers_verified_resume(dock_env):
    dock, vm, ops, ws, root = dock_env
    goal = "map the acoustic branch"
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start", "objective": {},
        "source": {"kind": "goal", "sha256": _goal_sha(goal)}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock._goal.setPlainText(goal)                    # verified match -> one Resume, not two
    assert dock._start_btn.text() == "Resume"
    assert not dock._resume_btn.isVisible()
    dock._goal.setPlainText("something else")        # mismatch -> the explicit route reappears
    assert dock._resume_btn.isVisible()


def test_tier_combo_frozen_when_primary_offers_resume(dock_env):
    # Tier is frozen with a resumed objective (the CLI ignores it) — a live combo would lie.
    dock, vm, ops, ws, root = dock_env
    goal = "map the acoustic branch"
    log_event(root / "report" / "session_log.jsonl", "decide", {
        "event": "campaign_start", "action": "campaign_start", "objective": {},
        "source": {"kind": "goal", "sha256": _goal_sha(goal)}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock._goal.setPlainText(goal)
    assert not dock._tier.isEnabled()
    assert "frozen" in dock._tier.toolTip()
    dock._goal.setPlainText("a different goal")      # fresh start -> tier applies again
    assert dock._tier.isEnabled()


def test_running_status_is_one_based_and_names_no_iteration_cap(dock_env):
    dock, vm, ops, ws, root = dock_env
    dock.set_backend_status("tavi", "ok", "up")
    dock._goal.setPlainText("map the branch")
    dock._start_btn.click()                          # ops fake captures the options, running=True
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    _run_event_iter0 = {"job_id": "j-1", "cost": 1.0, "iter": 0}
    log_event(path, "run", _run_event_iter0)
    ws.refresh()
    vm.on_workspace_changed({"events"})
    assert "iteration 1 (this run)" in dock._status_line.text()
    assert "of" not in dock._status_line.text()      # no iteration cap exists to report
    assert "iteration: 1 " in dock._iter_label.text()


def test_none_state_start_hands_off_instead_of_requesting(qtbot, dock_env, make_ws):
    # With NO workspace at all the reserve rebind would tear this dock down mid-click, so Start
    # must emit virtual_start_requested (window-owned sequence) and never call request_drive.
    _dock, vm, _bound_ops, ws, root = dock_env
    ops = _Ops()
    dock = CampaignDock(vm, ops, store=None, llm_env=lambda: {"ISAR_LLM": "x"},
                        workspace_state=lambda: "none")
    dock._confirm_objective = lambda _envelope: True
    qtbot.addWidget(dock)
    dock.show()
    handed = []
    dock.virtual_start_requested.connect(lambda opts, env: handed.append((opts, env)))
    dock.set_backend_status("tavi", "ok", "up")
    dock._goal.setPlainText("map the branch")
    dock._start_btn.click()
    assert ops.drive_requests == []
    assert len(handed) == 1
    opts, env = handed[0]
    assert opts.envelope_path == "frozen-objective.json" and env == {"ISAR_LLM": "x"}


def test_pending_running_status_names_the_coming_workspace(dock_env):
    dock, vm, ops, ws, root = dock_env
    dock._workspace_state = lambda: "pending"
    ops.running = True
    dock._sync_status()
    assert "workspace appears after the first scan" in dock._status_line.text()


def test_form_dict_round_trips_through_apply_form(qtbot, dock_env):
    dock, vm, ops, ws, root = dock_env
    dock._goal.setPlainText("carry me")
    dock._seed.setValue(17)
    dock._budget.setText("1e9n")
    form = dock.form_dict()

    other = CampaignDock(vm, ops, store=None)
    qtbot.addWidget(other)
    other.apply_form(form)
    assert other.form_dict() == form
    assert other._goal.toPlainText() == "carry me" and other._seed.value() == 17


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
                             "grid_state": {"state": "continue", "reasons": [], "per_q": []}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    assert "press Resume" in dock._status_line.text()


def test_selected_master_shows_revision_consistent_reasoning(dock_env):
    dock, vm, _ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "spec", "sha256": "x"}})
    log_event(path, "decide", {"event": "model_update", "action": "model_update", "per_q": [{
        "hkl": [2.5, 0, 0], "assessment_state": "uncertified", "omega0": 6.25,
        "calibrated_halfwidth": 0.12, "tolerance": 0.05, "master_revision": "rev-7",
        "uncertainty_method": "model_averaged_predictive", "track_state": "confirmed",
        "linewidth_state": "resolution_limited", "pool_consistent": True,
        "expected_next_information": {"expected_information_nats": 0.031},
        "diagnostics": ["diminishing_returns"],
        "reason": "continue — master interval remains above tolerance",
    }]})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock.set_selected_fit(SimpleNamespace(q=(2.5, 0.0, 0.0)))
    text = dock._master_detail.text()
    assert "revision rev-7" in text
    assert "ω: 6.25 meV" in text and "target ±0.05 meV" in text
    assert "Γ resolution_limited" in text and "pool consistency pass" in text
    assert "continue — master interval remains above tolerance" in text
    log_event(path, "stop", {"state": "pending_recovery", "reasons": ["job still running"],
                             "grid_state": {"state": "continue", "reasons": [], "per_q": []}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    assert "no new scan was submitted" in dock._status_line.text()
    assert "press Resume" in dock._status_line.text()


def _feed_lines(dock):
    return [dock._feed.topLevelItem(i).text(0) for i in range(dock._feed.topLevelItemCount())]


def test_feed_renders_and_filters_exceptions(dock_env):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 0})
    log_event(path, "timing", {"stage": "propose", "seconds": 0.1})
    log_event(path, "branch_track_update", {"revision": "rev-2", "iter": 0})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    # Profiling and audit-trail kinds never reach the feed; the JSONL on disk keeps them.
    assert dock._feed.topLevelItemCount() == 2
    dock._exceptions_only.setChecked(True)
    assert dock._feed.topLevelItemCount() == 1
    assert "proposal clamped" in dock._feed.topLevelItem(0).text(0)


def test_feed_row_expands_onto_its_raw_payload(dock_env):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 0,
                                  "reason": "outside the accessible window"})
    ws.refresh()
    vm.on_workspace_changed({"events"})

    row = dock._feed.topLevelItem(0)
    assert row.childCount() == 1                     # exactly one detail child per rendered line
    assert "proposal_clamped" in row.child(0).text(0)   # the RAW kind/action, verbatim
    assert not row.isExpanded()
    dock._toggle_expanded(row)
    assert row.isExpanded()
    dock._toggle_expanded(row)
    assert not row.isExpanded()
    dock._toggle_expanded(row.child(0))              # clicking the payload must not collapse it
    assert not row.isExpanded()


def test_loud_rows_survive_the_problems_only_filter_and_are_styled(dock_env):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "escalate_fit", {"phase": "promote", "hkl": [2.75, 0.0, 0.0],
                                     "from_tier": "fast", "to_tier": "full", "iter": 21})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    dock._exceptions_only.setChecked(True)
    lines = _feed_lines(dock)
    assert len(lines) == 1 and "Refitting more carefully" in lines[0]
    row = dock._feed.topLevelItem(0)
    assert not row.icon(0).isNull()                  # loud rows carry an icon…
    assert row.font(0).bold()                        # …and are bold
    assert row.foreground(0).color().name() == "#b26a00"


def test_budget_status_line_is_synthesized_into_the_feed(dock_env):
    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {
        "event": "campaign_start", "action": "campaign_start",
        "objective": {"budget": {"kind": "neutrons", "amount": 1e10},
                      "analysis_limit_seconds": 3600.0},
        "source": {"kind": "goal", "sha256": "x"}})
    ws.refresh()
    vm.on_workspace_changed({"events"})
    budget = [line for line in _feed_lines(dock) if line.startswith("Budget —")]
    assert len(budget) == 1
    assert "neutrons left" in budget[0] and "analysis time left" in budget[0]


def test_feed_hydrates_events_consumed_before_dock_connected(qtbot, make_ws):
    root = make_ws(rows=[])
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 0})
    log_event(path, "timing", {"stage": "propose", "seconds": 0.1})

    vm = CampaignViewModel(Workspace.open(root))
    dock = CampaignDock(vm, _Ops(), store=None)
    qtbot.addWidget(dock)

    assert dock._feed.topLevelItemCount() == 2
    assert "proposal clamped" in dock._feed.topLevelItem(0).text(0)
    assert all("timing" not in line.lower() for line in _feed_lines(dock))


def test_feed_copy_selection_copy_all_and_save_text(dock_env, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QFileDialog

    dock, vm, ops, ws, root = dock_env
    path = root / "report" / "session_log.jsonl"
    log_event(path, "decide", {"event": "campaign_start", "action": "campaign_start",
                               "objective": {}, "source": {"kind": "goal", "sha256": "x"}})
    log_event(path, "exception", {"action": "proposal_clamped", "iter": 0})
    ws.refresh()
    vm.on_workspace_changed({"events"})

    dock._feed.topLevelItem(0).setSelected(True)  # newest-first visible order
    dock._copy_feed_selection()
    assert "proposal clamped" in QApplication.clipboard().text()
    dock._copy_feed_all()
    visible = "\n".join(_feed_lines(dock))
    assert QApplication.clipboard().text() == visible

    destination = tmp_path / "campaign-log.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *_args, **_kwargs: (str(destination), "Text (*.txt)"))
    dock._save_feed()
    assert destination.read_text(encoding="utf-8") == visible + "\n"


def test_form_round_trips_through_gui_store(qtbot, dock_env, tmp_path):
    dock, vm, ops, ws, root = dock_env
    store = GuiStore.load(root)
    dock.set_store(store)
    dock._goal.setPlainText("persist me")
    dock._seed.setValue(42)
    dock._save_timer.stop()
    dock._save_form()          # flush the debounce deterministically

    store2 = GuiStore.load(root)
    dock2 = CampaignDock(vm, ops, store=store2)
    qtbot.addWidget(dock2)
    assert dock2._goal.toPlainText() == "persist me"
    assert dock2._seed.value() == 42

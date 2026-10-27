"""Virtual campaign from a cold GUI: reserve → launch → promote → adopt (no data required).

The drive engine is cold-start capable (it lazily runs `isar init` after its first scan); these
tests cover the GUI side: the Campaign dock exists before any workspace does, Start with no
workspace reserves a temporary one and launches into it, and the workspace the campaign creates
is promoted/adopted mid-run — including the awkward orderings (policy-only mid-init state,
transient open failure, finish-before-tick, finish-before-init, rebind mid-promotion).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from synthetic_ws import write_policy, write_session, write_state

from isar_gui import operation_coordinator
from isar_gui.gui_store import GuiStore
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


class _Sig:
    def __init__(self):
        self._cbs = []

    def connect(self, cb):
        self._cbs.append(cb)

    def disconnect(self, cb=None):
        if cb is None:
            self._cbs.clear()
        elif cb in self._cbs:
            self._cbs.remove(cb)

    def emit(self, *a):
        for cb in list(self._cbs):
            cb(*a)


class _FakeRunner:
    """ProcessRunner stand-in: captures argv, completes on demand, never spawns a process."""

    instances: list = []

    def __init__(self, *_a, **_k):
        self.line = _Sig()
        self.finished = _Sig()
        self.argv = None
        self.running = False
        _FakeRunner.instances.append(self)

    def start(self, argv, **_k):
        self.argv = list(argv)
        self.running = True

    def is_running(self):
        return self.running

    def kill(self):
        self.running = False

    def kill_tree(self):
        self.running = False

    def terminate(self):
        self.running = False

    def deleteLater(self):
        pass

    def complete(self, code: int):
        self.running = False
        self.finished.emit(code)


@pytest.fixture
def cold(qtbot, monkeypatch):
    """A cold (no-workspace) window with a fake drive runner. temp_root is already isolated by the
    autouse LOCALAPPDATA redirect in conftest. These tests leave content-bearing temporary
    workspaces behind, so the unsaved-temp close prompt (a modal — silent offscreen hang) is
    resolved as 'keep for recovery', the house pattern from test_shell."""
    monkeypatch.setattr(operation_coordinator, "ProcessRunner", _FakeRunner)
    _FakeRunner.instances.clear()
    w = MainWindow(None, session=Session(), watch=False)
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)
    qtbot.addWidget(w)
    return w


def _start_virtual(w, goal="map the branch"):
    from PySide6.QtWidgets import QApplication

    dock = w.campaign_dock
    dock._confirm_objective = lambda _program: True
    dock.set_backend_status("tavi", "ok", "up")
    dock._goal.setPlainText(goal)
    assert dock._start_btn.isEnabled()
    dock._start_btn.click()
    compile_runner = _FakeRunner.instances[-1]
    argv = compile_runner.argv
    assert "--compile-only" in argv
    program_path = Path(argv[argv.index("--compile-only") + 1])
    program_path.parent.mkdir(parents=True, exist_ok=True)
    program_path.write_text(json.dumps({
        "schema": "isar.campaign-program/1",
        "source_text": goal,
        "source": {"kind": "goal", "sha256": "test-fingerprint"},
        "steps": [{"id": "targeted-1", "kind": "targeted", "payload": {
            "schema": "isar.campaign-step.targeted/1",
            "path": [[2.0, 0.0, 0.0], [2.5, 0.0, 0.0]],
            "tolerance_meV": 0.5,
            "budget": {"kind": "neutrons", "amount": 1e9},
            "tier": "deterministic", "name": "test", "meta": {"goal": goal},
            "target_branch": "acoustic", "target_label": "acoustic",
            "energy_search_meV": [-2.0, 7.0],
        }}],
    }), encoding="utf-8")
    compile_runner.complete(0)
    QApplication.processEvents()
    QApplication.processEvents()
    # Keep the historical fixture contract: instances contains campaign runners, not the completed
    # compile-only preview process.
    _FakeRunner.instances.remove(compile_runner)


def _complete_init(root):
    """What the campaign's lazy `isar init` leaves behind (policy → session → state)."""
    write_policy(root)
    write_session(root)
    write_state(root)


# ---- the entry point exists before any workspace ------------------------------
def test_cold_window_has_the_campaign_entry(cold):
    w = cold
    assert w.campaign_dock is not None
    assert "Campaign" in w._docks
    # G19: the Campaign tab exists in the right area with no experiment open.
    from PySide6.QtCore import Qt
    assert w.dockWidgetArea(w.campaign_dock) == Qt.RightDockWidgetArea
    w.set_right_panel_collapsed(False)        # the Campaign tab is there beside Processing
    assert w.campaign_dock in w.tabifiedDockWidgets(w.processing_dock)


def test_pending_recovery_completion_status_is_resumable(cold):
    cold._on_drive_finished(5, True)
    message = cold.statusBar().currentMessage()
    assert "recovery pending" in message
    assert "Resume" in message
    assert "failed" not in message.lower()


# ---- reserve → launch → carry the form ----------------------------------------
def test_virtual_start_reserves_a_temp_workspace_and_launches(cold):
    w = cold
    _start_virtual(w, goal="map the acoustic branch")
    assert w._wsm.state() == "pending"
    root = w._wsm.root()
    assert root is not None
    assert w._operations.is_running_campaign()
    argv = _FakeRunner.instances[0].argv
    assert "--program" in argv
    assert str(root) in argv
    # The typed form survived the none→pending rebind (a NEW dock was constructed).
    assert w.campaign_dock._goal.toPlainText() == "map the acoustic branch"
    assert w.campaign_vm.process_alive
    assert w._promote_identity == (str(root), w._wsm.generation())
    assert "workspace appears after the first scan" in w.campaign_dock._status_line.text()


# ---- promotion: init-order honesty ---------------------------------------------
def test_policy_alone_does_not_promote_the_full_trio_does(cold):
    w = cold
    _start_virtual(w)
    root = w._wsm.root()

    write_policy(root)                      # isar init writes the policy FIRST — mid-init state
    w._try_promote_campaign_workspace()
    assert w._wsm.state() == "pending"

    write_session(root)
    write_state(root)
    w._try_promote_campaign_workspace()
    assert w._wsm.state() == "ready"        # promoted: the full window is now bound to it
    assert not w.is_empty
    # The running campaign was adopted across the generation bump — not stale.
    assert w._operations._active is not None
    assert w._operations._active.generation == w._wsm.generation()
    # The form seeded at drive start is visible in the post-promotion dock.
    assert w.campaign_dock._goal.toPlainText() == "map the branch"


def test_transient_open_failure_retries_on_the_next_tick(cold, monkeypatch):
    w = cold
    _start_virtual(w)
    root = w._wsm.root()
    _complete_init(root)

    real_open = w._wsm.open
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return False                    # one transient failure — must NOT disarm the watch
        return real_open(path)

    monkeypatch.setattr(w._wsm, "open", flaky)
    w._try_promote_campaign_workspace()
    assert w._wsm.state() == "pending" and w._promote_identity is not None
    w._try_promote_campaign_workspace()     # the retry succeeds
    assert w._wsm.state() == "ready"
    assert w._operations.is_running_campaign()   # the drive stayed controllable throughout


# ---- completion races ------------------------------------------------------------
def test_finish_after_init_but_before_a_tick_still_promotes(cold):
    w = cold
    _start_virtual(w)
    root = w._wsm.root()
    _complete_init(root)                    # the campaign initialized the workspace …
    _FakeRunner.instances[0].complete(0)    # … and exited before the next 1 s tick

    assert w._wsm.state() == "ready"        # the final attempt in _on_drive_finished promoted
    assert w.campaign_vm.process_alive is False   # and the POST-rebind VM ended idle
    assert not w._promote_timer.isActive()


def test_finish_before_init_stays_pending_and_retryable(cold):
    w = cold
    _start_virtual(w)
    _FakeRunner.instances[0].complete(3)    # died before any scan — nothing to promote

    assert w._wsm.state() == "pending"
    assert not w._promote_timer.isActive() and w._promote_identity is None
    assert not w._operations.is_busy()
    # The same reserved root accepts a retry (the pending gate allows it).
    _start_virtual(w)
    assert w._operations.is_running_campaign()
    assert len(_FakeRunner.instances) == 2


# ---- staleness ---------------------------------------------------------------------
def test_rebind_mid_promotion_cancels_the_watch(cold, make_ws):
    w = cold
    _start_virtual(w)
    pending_root = w._wsm.root()
    ident = w._promote_identity

    # The normal open path already refuses while a campaign runs (single-writer guard), so the
    # realistic binding switch is: force-stop, then open another workspace.
    assert w.load_workspace(make_ws(rows=[])) is False   # guarded while the campaign is busy
    w._operations.cancel_drive()
    _FakeRunner.instances[0].complete(1)                 # the killed tree exits (nothing to promote)

    other = make_ws(rows=[])
    assert w.load_workspace(other)
    assert not w._promote_timer.isActive() and w._promote_identity is None

    # The contentless reserved dir was auto-discarded on rebind; even if the abandoned
    # campaign's files rematerialized, the stale identity must not promote them.
    pending_root.mkdir(parents=True, exist_ok=True)
    _complete_init(pending_root)
    w._try_promote_campaign_workspace()       # no identity -> no-op
    assert str(w._wsm.root()) == str(other)   # still on the operator's workspace
    # And the manager itself refuses the stale identity outright.
    assert w._wsm.promote_pending_if_ready(*ident) is False


# ---- form seeding honesty ------------------------------------------------------------
def test_seeding_never_overwrites_a_recovered_form(cold, qtbot):
    w = cold
    w.new_workspace()                         # a pending workspace (e.g. recovered) …
    root = w._wsm.root()
    store = GuiStore.load(root)               # … that already carries a campaign form
    store.set_campaign_form({"source": "goal", "goal": "the recovered goal"})
    store.save()

    _start_virtual(w, goal="a newly typed goal")   # pending state -> direct request_drive
    assert GuiStore.load(root).campaign_form()["goal"] == "the recovered goal"


# ---- operator control across the promotion --------------------------------------------
def test_stop_targets_the_same_token_before_and_after_promotion(cold):
    w = cold
    _start_virtual(w)
    root = w._wsm.root()
    token = w._operations._active.payload[0].control_token
    assert w._operations.request_drive_stop() is True
    marker = root / "report" / "control" / f"stop-{token}.json"
    assert marker.exists()

    _complete_init(root)
    w._try_promote_campaign_workspace()
    assert w._wsm.state() == "ready"
    assert w._operations._active.payload[0].control_token == token
    assert w._operations.request_drive_stop() is True    # idempotent, same marker
    assert marker.exists()

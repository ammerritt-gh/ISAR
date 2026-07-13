"""Shell tests: MainWindow builds against a synthetic workspace; the Docks menu syncs both ways."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.commands import GuiIngestReport
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from synthetic_ws import scan_entry, stamp_ws


def _open(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(root), root


class _FakeDrop:
    """A minimal drop event carrying local-file URLs for MainWindow.dropEvent."""

    def __init__(self, paths):
        from PySide6.QtCore import QMimeData, QUrl

        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


def test_main_window_builds(qtbot, make_ws):
    ws, root = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.windowTitle() == f"ISAR — {root.name}"
    assert w.centralWidget() is not None
    # The log dock is scaffolded and registered so the Docks menu can find it.
    assert "Log" in w._docks and w._docks["Log"] is w.log_dock
    w.show()
    assert w.log_dock.isVisible()


def test_empty_start_window(qtbot):
    """No workspace (empty-start): the window opens with the Open-Workspace hint as its central
    widget, no watch bridge (even with watch enabled), only data-entry/diagnostic docks, and 'no
    workspace' in the status bar."""
    w = MainWindow(None, session=Session(), watch=True,
                   watch_kwargs={"poll_interval_s": 0.02, "debounce_ms": 50})
    qtbot.addWidget(w)
    assert w.is_empty
    assert w._bridge is None                       # never watch a workspace that isn't there
    assert w.centralWidget() is w._empty_hint
    assert "Open Workspace" in w._empty_hint.text()
    assert w.windowTitle() == "ISAR — (no workspace)"
    assert "No workspace" in w.statusBar().currentMessage()
    # Workspace-specific docks are withheld until data has a workspace.  Files is the useful
    # first-time surface for adding/dropping data; Log remains available for visible failures.
    assert set(w._docks) == {"Log", "Files"}
    assert w.files_dock.isVisible() is False  # the window itself has not been shown yet
    assert not hasattr(w, "browser_dock")
    assert not hasattr(w, "card_dock")
    assert not hasattr(w, "scans_dock")
    # The empty viewmodels remain honest and available to the shell, even though their docks are
    # intentionally not presented before a workspace binds.
    assert w.browser_vm.group_rows == []
    assert w.heatmap_vm.spec.strips == []
    # workspace_path stays None (not the string "None") in the hidden browser model.
    assert w.browser_vm.card_summary["workspace_path"] is None


def test_empty_start_keeps_log_compact_and_files_prominent(qtbot):
    """A first-time window must lead with data entry, not a giant mostly-empty log."""
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.files_dock.isVisible())
    # The queued empty-start layout runs after any restored state.  On the 800px default window the
    # requested 180px log height leaves the useful Files dock with the clear majority of the space.
    qtbot.waitUntil(lambda: w.log_dock.height() <= 240)
    assert w.files_dock.height() > w.log_dock.height()


def test_open_workspace_from_empty_binds(qtbot, make_ws):
    """Open Workspace… from the empty state does a full bind from None: the inspector becomes the
    central widget, the title/status update, and the workspace's data reaches the browser."""
    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.is_empty

    assert w.load_workspace(str(root)) is True
    assert not w.is_empty
    assert w.centralWidget() is w.inspector
    assert w.inspector is not None
    assert w.windowTitle() == f"ISAR — {root.name}"
    assert len(w.browser_vm.group_rows) == 1
    # A ready workspace restores the full normal view; the sparse welcome scaffold is not sticky.
    for name in ("Browser", "Card", "Scans", "Plane (q, E)", "Dispersion ω(q)",
                 "Waterfall / Comparison"):
        assert name in w._docks


def test_empty_start_autosave_preserves_full_workspace_layout(qtbot):
    """The sparse welcome scaffold must never replace a previously saved full dock layout."""
    saved_layout = "persisted-full-layout"
    w = MainWindow(None, session=Session(dock_layout_b64=saved_layout), watch=False)
    qtbot.addWidget(w)
    snap = w._snapshot_session()           # would raise on Path(None) if note_workspace ran
    assert snap.dock_layout_b64 == saved_layout
    assert snap.recent_workspaces == []


def test_no_qapplication_on_import():
    """In a clean interpreter, importing isar_gui (+ submodules) must not create a QApplication."""
    import subprocess
    import sys

    script = (
        "import isar_gui, isar_gui.main_window, isar_gui.watch_bridge, isar_gui.session\n"
        "from PySide6.QtWidgets import QApplication\n"
        "assert QApplication.instance() is None, 'import created a QApplication'\n"
        "print('NO_QAPP_OK')\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
    assert out.returncode == 0, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "NO_QAPP_OK" in out.stdout


def test_docks_menu_toggles_log_dock(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()

    # Find the Log action in the Docks menu.
    action = None
    for a in w.menuBar().actions():
        if a.menu() and a.text().replace("&", "") == "Docks":
            for sub in a.menu().actions():
                if sub.text() == "Log":
                    action = sub
    assert action is not None
    assert action.isCheckable()
    assert action.isChecked()  # dock starts visible -> action checked

    # Menu -> dock: a real user click (trigger() flips checked then emits triggered) hides the dock.
    # Uses trigger()/`triggered`, not `toggled`, because _toggle is now wired to triggered so the
    # programmatic visibility sync can't hide docks — see test_docks_menu.py for why.
    assert action.isChecked()
    action.trigger()
    assert not w.log_dock.isVisible()

    # Dock -> menu: hiding/showing the dock updates the action (visibilityChanged sync).
    w.log_dock.show()
    assert action.isChecked()


def test_watch_disabled_has_no_bridge(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._bridge is None


# ---- live watch wiring (Task 14) --------------------------------------------
_FAST_WATCH = {"poll_interval_s": 0.02, "debounce_ms": 50}


def _live_window(qtbot, ws):
    w = MainWindow(ws, session=Session(), watch=True, watch_kwargs=_FAST_WATCH)
    qtbot.addWidget(w)
    return w


def test_live_label_starts_watching_and_updates_on_change(qtbot, make_ws):
    from synthetic_ws import write_results

    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    ws = Workspace.open(root)
    w = _live_window(qtbot, ws)
    assert w._live_label.text() == "watching (no changes yet)"

    with qtbot.waitSignal(w._bridge.changed, timeout=6000):
        write_results(root, [
            {"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
             "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0},
            {"scan": 2, "detector": "det", "sample": "s", "T": 100.0,
             "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 6.0},
        ])

    assert w._live_label.text().startswith("live — last update ")


def test_live_label_blank_when_watch_disabled(qtbot, make_ws):
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._live_label.text() == ""


def test_watch_grows_browser_and_heatmap(qtbot, make_ws):
    """The item-2 'watch the heatmap grow' proof: a new fitted row landing on disk (as a running
    campaign would write it) reaches the browser tree and the (q, E) plane through the bridge."""
    from synthetic_ws import write_data_file, write_results

    x = list(range(-2, 9))
    counts1 = [100.0] * len(x)

    root = make_ws(
        rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
              "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}],
        data_files=[{"scan_number": 1, "energy": x, "counts": counts1}],
    )
    ws = Workspace.open(root)
    w = _live_window(qtbot, ws)

    assert len(w.browser_vm.group_rows) == 1
    assert len(w.heatmap_vm.spec.strips) == 1

    with qtbot.waitSignal(w._bridge.changed, timeout=6000):
        write_results(root, [
            {"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
             "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0},
            {"scan": 2, "detector": "det", "sample": "s", "T": 100.0,
             "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 6.0},
        ])
        write_data_file(root, 2, x, counts1)

    # The two file writes above may debounce into separate ticks rather than one — wait until both
    # have actually landed across all four views instead of asserting right after a single signal.
    qtbot.waitUntil(
        lambda: len(w.browser_vm.group_rows) == 2
        and len(w.heatmap_vm.spec.strips) == 2
        and len(w.dispersion_vm.spec.meta["omega_hat_entropy_all"]) == 2
        and len(w.waterfall_vm.q_values) == 2,
        timeout=6000,
    )

    assert len(w.browser_vm.group_rows) == 2
    assert len(w.heatmap_vm.spec.strips) == 2
    # The dispersion + waterfall views read the same refreshed facade.
    assert len(w.dispersion_vm.spec.meta["omega_hat_entropy_all"]) == 2
    assert len(w.waterfall_vm.q_values) == 2


def test_inspector_refreshes_selection_still_present(qtbot, make_ws):
    from synthetic_ws import write_results

    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    ws = Workspace.open(root)
    w = _live_window(qtbot, ws)

    w.browser_vm.select(w._ws.results[0])
    assert not w.inspector_vm.is_empty
    assert w.inspector_vm.current_fit.omega0 == 4.0

    with qtbot.waitSignal(w._bridge.changed, timeout=6000):
        write_results(root, [{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                              "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 5.0}])

    assert not w.inspector_vm.is_empty
    assert w.inspector_vm.current_fit.omega0 == 5.0


def test_card_state_change_rebuilds_dispersion_prior(qtbot, make_ws):
    """A card.state.json change (a new dispersion_prior) must reach the dispersion view: the
    changed-key gate includes 'card_state', which carries the prior / branch belief / lattice
    epochs the component views read (not just 'results'/'snapshot')."""
    from isar.plotspec.spec import Role
    from synthetic_ws import write_state

    def _oc(h, omega0):
        return {"detector_id": "det", "verdict": "resolved", "disposition": "resolved",
                "confidence": 0.9, "omega0": omega0, "gamma": 1.0, "q": [h, 0.0, 0.0], "T": 200.0,
                "sample": "tavi",
                "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
                "interval": [omega0 - 0.4, omega0 + 0.4], "interval_reliable": True}

    grid = [(1, 2.0, 4.0), (2, 2.2, 4.0)]
    scans = {f"tavi:{n}:det": {"scan_id": f"tavi:{n}:det", "scan_number": n,
                               "scan_type": "measurement", "status": "fitted",
                               "detectors": {"det": _oc(h, w0)}, "provenance": {"M": 200}}
             for n, h, w0 in grid}
    rows = [{"scan": n, "detector": "det", "sample": "tavi", "T": 200.0,
             "h": h, "k": 0.0, "l": 0.0, "status": "fitted"} for n, h, w0 in grid]
    root = make_ws(scans=scans, rows=rows)
    ws = Workspace.open(root)
    w = _live_window(qtbot, ws)

    assert not any(s.role is Role.PRIOR for s in w.dispersion_vm.spec.series)

    prior = [
        {"sample": "tavi", "t_bin": 200.0, "q": [2.0, 0.0, 0.0], "omega0": 4.0, "sigma": 0.2},
        {"sample": "tavi", "t_bin": 200.0, "q": [2.2, 0.0, 0.0], "omega0": 4.0, "sigma": 0.2},
    ]
    with qtbot.waitSignal(w._bridge.changed, timeout=6000):
        write_state(root, dispersion_prior=prior)

    assert any(s.role is Role.PRIOR for s in w.dispersion_vm.spec.series)


def test_inspector_clears_when_selection_removed(qtbot, make_ws):
    from synthetic_ws import write_results

    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    ws = Workspace.open(root)
    w = _live_window(qtbot, ws)

    w.browser_vm.select(w._ws.results[0])
    assert not w.inspector_vm.is_empty

    with qtbot.waitSignal(w._bridge.changed, timeout=6000):
        write_results(root, [{"scan": 2, "detector": "det", "sample": "s", "T": 100.0,
                              "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 6.0}])

    assert w.inspector_vm.is_empty


def test_dock_location_change_triggers_autosave(qtbot, make_ws, monkeypatch):
    from PySide6.QtCore import Qt

    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)

    calls = []
    monkeypatch.setattr(w._autosaver, "request", lambda: calls.append(1))
    w.log_dock.dockLocationChanged.emit(Qt.DockWidgetArea.BottomDockWidgetArea)
    assert calls == [1]


# ---- Phase C: workspace-first shell (temp / title / close / recents) --------
def _stamp_temp_ws(temproot, name="ws-1"):
    root = temproot / name
    root.mkdir(parents=True)
    return stamp_ws(root, rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])


def _install_fake_msgbox(monkeypatch, choice):
    """Replace main_window.QMessageBox with a fake that auto-clicks ``choice`` on exec and counts
    instantiations, so a closeEvent prompt can be asserted without a real modal dialog."""
    from PySide6.QtWidgets import QMessageBox as RealMB

    state = {"count": 0}

    class FakeMB:
        Icon = RealMB.Icon
        ButtonRole = RealMB.ButtonRole

        def __init__(self, parent=None):
            self._btns = {}
            self._clicked = None
            state["count"] += 1

        def setIcon(self, *a):
            pass

        def setWindowTitle(self, *a):
            pass

        def setText(self, *a):
            pass

        def setInformativeText(self, *a):
            pass

        def addButton(self, label, role):
            btn = object()
            self._btns[label] = btn
            return btn

        def setDefaultButton(self, *a):
            pass

        def exec(self):
            self._clicked = self._btns[choice]

        def clickedButton(self):
            return self._clicked

    monkeypatch.setattr("isar_gui.main_window.QMessageBox", FakeMB)
    return state


def test_new_workspace_pending_title_is_unsaved(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm

    monkeypatch.setattr(wm, "temp_root", lambda: tmp_path / "temp")
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)

    w.new_workspace()
    assert w._wsm.state() == "pending"
    assert w._wsm.is_temporary()
    assert "Unsaved" in w.windowTitle()
    assert w.is_empty                                   # pending is an empty bind (docks show empty)
    assert "temporary workspace" in w._empty_hint.text().lower()


def test_close_prompts_and_discards_content_bearing_temp(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm
    from PySide6.QtGui import QCloseEvent

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    root = _stamp_temp_ws(temproot)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.load_workspace(str(root)) is True
    assert w._wsm.is_temporary() and w._wsm.has_content()

    counter = _install_fake_msgbox(monkeypatch, choice="Discard")
    monkeypatch.setattr(w._autosaver, "flush_now", lambda: None)  # don't touch the real session file
    w.closeEvent(QCloseEvent())
    assert counter["count"] == 1                        # a content-bearing temp workspace prompts
    assert not root.exists()                            # Discard removed the temp analysis


def test_close_does_not_prompt_for_saved_workspace(qtbot, tmp_path, monkeypatch, make_ws):
    import isar_gui.workspace_manager as wm
    from PySide6.QtGui import QCloseEvent

    monkeypatch.setattr(wm, "temp_root", lambda: tmp_path / "temp")  # make_ws roots elsewhere
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert not w._wsm.is_temporary()

    counter = _install_fake_msgbox(monkeypatch, choice="Discard")
    monkeypatch.setattr(w._autosaver, "flush_now", lambda: None)
    w.closeEvent(QCloseEvent())
    assert counter["count"] == 0                        # a saved workspace closes without a prompt


def test_open_valid_workspace_can_keep_temporary_work_for_recovery(
        qtbot, tmp_path, monkeypatch, make_ws):
    """Keeping is a real fourth choice: opening another workspace leaves recoverable temp work."""
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    temp = _stamp_temp_ws(temproot)
    target = make_ws(rows=[])
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.load_workspace(temp)

    counter = _install_fake_msgbox(monkeypatch, choice="Keep for Recovery")
    assert w.load_workspace(target)
    assert counter["count"] == 1
    assert w._wsm.root() == target
    assert temp.exists()                                 # left for the startup recovery dialog


def test_invalid_open_does_not_prompt_or_discard_temporary_work(qtbot, tmp_path, monkeypatch):
    """Candidate validation happens before asking the operator what to do with current analysis."""
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    temp = _stamp_temp_ws(temproot)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.load_workspace(temp)

    counter = _install_fake_msgbox(monkeypatch, choice="Discard")
    assert not w.load_workspace(tmp_path / "not-a-workspace")
    assert counter["count"] == 0
    assert temp.exists() and w._wsm.root() == temp
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)


def test_new_resolves_temporary_work_and_cleans_empty_reservation(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.new_workspace()
    first = w._wsm.root()
    assert first is not None and first.exists()

    # A second New leaves no dead pending directory behind.
    w.new_workspace()
    second = w._wsm.root()
    assert second is not None and second.exists()
    # The allocator may reuse the same timestamped name after the empty reservation was removed;
    # either way exactly one pending directory remains.
    assert list(temproot.iterdir()) == [second]


def test_new_cancel_keeps_current_temporary_analysis(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    temp = _stamp_temp_ws(temproot)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.load_workspace(temp)
    counter = _install_fake_msgbox(monkeypatch, choice="Cancel")

    w.new_workspace()
    assert counter["count"] == 1
    assert w._wsm.root() == temp and temp.exists()
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)


def test_temp_workspace_not_added_to_recents(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    root = _stamp_temp_ws(temproot)
    session = Session()
    w = MainWindow(None, session=session, watch=False)
    qtbot.addWidget(w)

    assert w.load_workspace(str(root)) is True
    assert w._wsm.is_temporary()
    assert session.recent_workspaces == []              # binding a temp workspace never notes recents
    w._snapshot_session()                               # nor does an autosave snapshot
    assert session.recent_workspaces == []
    # The window still holds a content-bearing temp workspace here; qtbot's teardown close would
    # otherwise raise the real (modal) Save/Discard prompt and hang the offscreen run.
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)


def test_save_as_notes_dest_in_recents(qtbot, tmp_path, monkeypatch):
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    root = _stamp_temp_ws(temproot)
    session = Session()
    w = MainWindow(None, session=session, watch=False)
    qtbot.addWidget(w)
    assert w.load_workspace(str(root)) is True

    dest = tmp_path / "kept"

    class _FakeDialog:
        @staticmethod
        def getExistingDirectory(*a, **k):
            return str(dest)

    monkeypatch.setattr("isar_gui.main_window.QFileDialog", _FakeDialog)
    assert w.save_workspace_as() is True
    assert not w._wsm.is_temporary()
    assert str(dest) in session.recent_workspaces       # the saved copy IS noted in recents


def test_ready_drop_routes_to_ingest_controller(qtbot, make_ws, monkeypatch):
    """Dropping raw data onto a ready workspace hands it to the IngestController (which queues rather
    than refuses a concurrent drop) — the window no longer runs the inline single-flight ingest."""
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)

    requested = []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    w.dropEvent(_FakeDrop([r"some/data/path"]))
    assert requested == [[r"some/data/path"]]


def test_pending_gui_store_write_flushes_to_old_workspace_on_load(qtbot, make_ws):
    """A watched-folder mutation made just before loading another workspace must persist into the OLD
    workspace's gui.state.json (the debounced timer would otherwise re-save into the NEW store)."""
    import json

    ws_a, root_a = _open(make_ws)
    root_b = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = MainWindow(ws_a, session=Session(), watch=False)
    qtbot.addWidget(w)

    watched = str(root_a / "incoming")
    w.files_vm.add_watch(watched)                  # mutates A's store + arms the debounced save timer
    assert w._store_save_timer.isActive()

    assert w.load_workspace(str(root_b)) is True    # rebind flushes the pending write into A first
    assert not w._store_save_timer.isActive()       # timer disarmed, cannot fire into B's store

    saved = json.loads((root_a / "gui.state.json").read_text(encoding="utf-8"))
    assert watched in saved.get("watched_folders", [])
    # The freshly-bound B store is untouched by A's pending mutation.
    assert w._gui_store.watched_folders() == []
    assert not (root_b / "gui.state.json").exists()


def test_is_file_known_uses_cached_manifest_index(qtbot, tmp_path, make_ws):
    """The folder-watcher is_known guard is an O(1) lookup into a prebuilt {resolved: (size,mtime)}
    cache — it never resolves the manifest per call (the beamtime-scale poll-freeze fix)."""
    from pathlib import Path

    data = tmp_path / "numor_1.dat"
    data.write_bytes(b"counts")
    st = data.stat()
    root = make_ws(
        rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}],
        files={str(data): {"size": st.st_size, "mtime": st.st_mtime, "format": "measurement",
                           "n_scans": 1, "scan_ids": ["exp:1"], "n_context": 0}},
    )
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)

    rp = str(Path(str(data)).resolve())
    assert w._known_files_cache.get(rp) == (st.st_size, st.st_mtime)
    assert w._is_file_known(str(data), st.st_size, st.st_mtime) is True
    assert w._is_file_known(str(data), st.st_size + 10, st.st_mtime) is False   # a resend re-queues
    # The guard reads only the prebuilt cache: emptying it makes everything unknown though ws.files
    # is unchanged — proof it does not resolve the manifest on each poll.
    w._known_files_cache = {}
    assert w._is_file_known(str(data), st.st_size, st.st_mtime) is False


def test_dock_layout_round_trips_through_session(qtbot, make_ws):
    ws1, _ = _open(make_ws)
    w1 = MainWindow(ws1, session=Session(), watch=False)
    qtbot.addWidget(w1)
    w1.show()
    w1.log_dock.hide()
    saved = w1._snapshot_session()
    assert saved.dock_layout_b64

    ws2, _ = _open(make_ws)
    w2 = MainWindow(ws2, session=Session(dock_layout_b64=saved.dock_layout_b64), watch=False)
    qtbot.addWidget(w2)
    w2.show()
    # Normally fired via QTimer.singleShot(0, ...) once the event loop settles; call it directly so
    # the test doesn't depend on pumping the event loop.
    w2._restore_layout()
    assert not w2.log_dock.isVisible()


def test_empty_start_then_open_reapplies_saved_full_dock_layout(qtbot, make_ws):
    """A sparse welcome scaffold must not consume the saved layout for the later full workspace.

    ``restoreState`` ignores dock ids that do not exist yet.  Opening a workspace after the empty
    start must therefore restore the same full-layout preference that a direct workspace launch
    would have used.
    """
    ws, _ = _open(make_ws)
    source = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(source)
    source.show()
    source.browser_dock.show()
    source.browser_dock.raise_()
    qtbot.waitUntil(source.browser_dock.isVisible)
    saved = source._snapshot_session()

    root = make_ws(rows=[{"scan": 2, "detector": "det", "status": "fitted", "omega0": 6.0}])
    w = MainWindow(None, session=Session(dock_layout_b64=saved.dock_layout_b64), watch=False)
    qtbot.addWidget(w)
    w.show()
    assert "Browser" not in w._docks                  # sparse start cannot restore it yet

    assert w.load_workspace(str(root)) is True
    qtbot.waitUntil(w.browser_dock.isVisible)


def test_new_pending_reapplies_compact_empty_log_geometry(qtbot, tmp_path, monkeypatch):
    """The compact welcome geometry also applies to a later New -> pending transition."""
    import isar_gui.workspace_manager as wm
    from PySide6.QtCore import Qt

    monkeypatch.setattr(wm, "temp_root", lambda: tmp_path / "temp")
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    # Make the initial dock obviously large, then make a fresh pending workspace.  The queued
    # welcome layout must shrink it again rather than leaving the old allocation in place.
    w.resizeDocks([w.log_dock], [500], Qt.Vertical)
    w.new_workspace()
    qtbot.waitUntil(lambda: w._wsm.state() == "pending")
    qtbot.waitUntil(lambda: w.log_dock.height() <= 240)
    assert w.files_dock.height() > w.log_dock.height()


def test_pending_autosave_preserves_layout_then_ingest_promotes_scans(
        qtbot, tmp_path, monkeypatch, make_ws):
    """New may sit pending beyond the autosave debounce without losing the full dock layout.

    When bootstrap subsequently binds the ready workspace, its queued layout restoration may hide
    Scans. The queued post-ingest promotion must run afterward and expose the parsed rows.
    """
    import isar_gui.workspace_manager as wm

    # Build a real full layout in which Scans was hidden, matching a prior operator session.
    source_root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted",
                                 "omega0": 5.0}])
    source = MainWindow(Workspace.open(source_root), session=Session(), watch=False)
    qtbot.addWidget(source)
    source.show()
    source.scans_dock.hide()
    saved_layout = source._snapshot_session().dock_layout_b64
    assert saved_layout

    temproot = tmp_path / "temp"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    session = Session(dock_layout_b64=saved_layout)
    w = MainWindow(None, session=session, watch=False)
    qtbot.addWidget(w)
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)
    # Keep the debounce proof isolated from the real per-user GUI session.
    w._autosaver._path = tmp_path / "session.json"
    w.show()
    w.new_workspace()
    pending_root = w._wsm.root()
    assert pending_root is not None

    qtbot.wait(650)  # exceed the production 500 ms session-autosave debounce
    assert session.dock_layout_b64 == saved_layout

    scan_id = "tavi:41:det"
    stamp_ws(
        pending_root,
        scans={scan_id: scan_entry(41, status="ingested")},
        files={"C:/incoming/scan41.dat": {
            "format": "measurement", "n_scans": 1, "scan_ids": [scan_id], "n_context": 0,
        }},
        rows=[],
    )
    assert w._wsm.open(pending_root)
    w._on_ingest_report(GuiIngestReport(
        files=({"path": "C:/incoming/scan41.dat", "scan_ids": [scan_id]},),
        added=(scan_id,),
    ))

    # Pump both queued callbacks: restore the old layout first, then make parsed Scans prominent.
    qtbot.wait(50)
    assert [row.key for row in w.scans_vm.rows()] == [scan_id]
    assert w.scans_dock.isVisible()


def test_recovered_raw_temp_promotes_scans_after_layout_restore(
        qtbot, tmp_path, monkeypatch, make_ws):
    """Relaunch recovery has no ingest signal, but raw parsed rows must still be made visible."""
    import isar_gui.workspace_manager as wm

    source_root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted",
                                 "omega0": 5.0}])
    source = MainWindow(Workspace.open(source_root), session=Session(), watch=False)
    qtbot.addWidget(source)
    source.show()
    source.scans_dock.hide()
    saved_layout = source._snapshot_session().dock_layout_b64
    assert saved_layout

    temproot = tmp_path / "recovery"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    recovered_root = temproot / "ws-recovered"
    recovered_root.mkdir(parents=True)
    scan_id = "tavi:52:det"
    stamp_ws(
        recovered_root,
        scans={scan_id: scan_entry(52, status="ingested")},
        files={"C:/incoming/scan52.dat": {
            "format": "measurement", "n_scans": 1, "scan_ids": [scan_id], "n_context": 0,
        }},
        rows=[],
    )

    w = MainWindow(
        Workspace.open(recovered_root), session=Session(dock_layout_b64=saved_layout), watch=False)
    qtbot.addWidget(w)
    monkeypatch.setattr(w, "_resolve_unsaved_temp_on_close", lambda: True)
    w.show()
    qtbot.wait(50)  # restore the hidden layout, then run the recovery promotion

    assert w._wsm.is_temporary()
    assert [row.key for row in w.scans_vm.rows()] == [scan_id]
    assert w._ws.results == []
    assert w.scans_dock.isVisible()


def test_fitted_only_ingest_does_not_promote_scans(qtbot, make_ws):
    """A context/dedup ingest into analyzed work must not steal focus from the operator's view."""
    root = make_ws(rows=[{"scan": 7, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    w.scans_dock.hide()

    w._on_ingest_report(GuiIngestReport(files=({"path": "C:/incoming/context.log"},)))
    qtbot.wait(50)

    assert all(row.status == "fitted" for row in w.scans_vm.rows())
    assert not w.scans_dock.isVisible()


def test_stale_ingest_promotion_cannot_touch_new_workspace(qtbot, make_ws):
    old_root = make_ws(
        scans={"tavi:41:det": scan_entry(41, status="ingested")}, rows=[])
    new_root = make_ws(rows=[])
    w = MainWindow(Workspace.open(old_root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    old_generation = w._wsm.generation()

    assert w.load_workspace(new_root)
    w.scans_dock.hide()
    w._promote_scans_if_current(old_generation, old_root)

    assert not w.scans_dock.isVisible()


# ---- persistent log dock across rebinds -------------------------------------
def test_log_dock_persists_across_rebind(qtbot, make_ws):
    """The Log dock is created once and survives a workspace rebind: same object, scrollback kept,
    and a `── HH:MM:SS workspace: <name> ──` separator appended for the new workspace. (Regression:
    _teardown_binding used to delete every dock incl. Log, wiping the log on the drop-to-ingest
    rebind.)"""
    ws_a, _ = _open(make_ws)
    w = MainWindow(ws_a, session=Session(), watch=False)
    qtbot.addWidget(w)

    log = w.log_dock
    w.log("MARKER-BEFORE-REBIND")
    assert "MARKER-BEFORE-REBIND" in log._view.toPlainText()

    root_b = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    assert w.load_workspace(str(root_b)) is True

    # Same object (never recreated), and the pre-rebind line is still there.
    assert w.log_dock is log
    text = log._view.toPlainText()
    assert "MARKER-BEFORE-REBIND" in text
    # A boundary separator for the new workspace was appended.
    assert f"workspace: {root_b.name}" in text
    assert text.count("workspace:") >= 2   # one at first bind, one after the rebind


def test_rebind_does_not_duplicate_log_dock_autosave_wiring(qtbot, make_ws):
    """The persistent Log dock is re-registered on every rebind; its dockLocationChanged autosave
    connection must be wired exactly once, not once per bind (a double-wire fires _autosave twice
    per drag)."""
    from PySide6.QtCore import Qt

    ws_a, _ = _open(make_ws)
    w = MainWindow(ws_a, session=Session(), watch=False)
    qtbot.addWidget(w)

    root_b = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    assert w.load_workspace(str(root_b)) is True

    calls = {"n": 0}
    w._autosave = lambda: calls.__setitem__("n", calls["n"] + 1)
    w.log_dock.dockLocationChanged.emit(Qt.BottomDockWidgetArea)
    assert calls["n"] == 1

"""Shell tests: MainWindow builds against a synthetic workspace; the Docks menu syncs both ways."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


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
    """No workspace (empty-start): the window opens with its one arrangement, the display built over
    the null workspace (never a hint page that vanishes when data arrives), no watch bridge (even
    with watch enabled), no scan-centric docks, and 'No experiment open' in the status bar."""
    w = MainWindow(None, session=Session(), watch=True,
                   watch_kwargs={"poll_interval_s": 0.02, "debounce_ms": 50})
    qtbot.addWidget(w)
    assert w.is_empty
    assert w._bridge is None                       # never watch a workspace that isn't there
    # The central widget is the frame (inspector column | display stack, then the rail); the display
    # is built in the empty state too, and the hint page is only the parking page.
    assert w.centralWidget() is not w._center_stack
    assert w._center_stack.isAncestorOf(w._center_host) and w._center_host is not None
    assert w._center_stack.currentWidget() is w._center_host
    assert "Nothing to show yet" in w._empty_hint.text()
    assert "Nothing to show yet" in w.inspector._empty_label.text()
    assert w.windowTitle() == "ISAR — no experiment"
    assert "No experiment open" in w.statusBar().currentMessage()
    # Workspace-specific docks are withheld until data has a workspace.  The old Files dock stays
    # reachable from View > Panels (hidden); Campaign is the OTHER way in (a virtual campaign creates
    # its own workspace); Log and Processing are always there.
    assert set(w._docks) == {"Log", "Files", "Campaign", "Processing"}
    assert w.files_dock.isHidden()
    assert not hasattr(w, "browser_dock")
    assert not hasattr(w, "card_dock")
    assert not hasattr(w, "scans_dock")
    # The empty viewmodels remain honest and available to the shell, even though their docks are
    # intentionally not presented before a workspace binds.
    assert w.browser_vm.group_rows == []
    assert w.heatmap_vm.spec.strips == []
    # workspace_path stays None (not the string "None") in the hidden browser model.
    assert w.browser_vm.card_summary["workspace_path"] is None


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
    # The inspector is now the CenterHost's "scan" page (the stack shows the host, not the inspector).
    assert w._center_host is not None
    assert w._center_stack.currentWidget() is w._center_host
    assert w._center_host.pane("scan") is w.inspector
    assert w.inspector is not None
    assert w.windowTitle() == f"ISAR — {root.name}"
    assert len(w.browser_vm.group_rows) == 1
    # A ready workspace restores the full normal view; the sparse welcome scaffold is not sticky. The
    # scan-centric docks remain docks; the four plot surfaces are CenterHost pages.
    for name in ("Browser", "Card", "Scans"):
        assert name in w._docks
    for key in ("scan", "waterfall", "plane", "dispersion", "sandbox"):
        assert w._center_host.pane(key) is not None


def test_empty_start_autosave_saves_the_one_arrangement(qtbot):
    """The empty state has the same arrangement as a ready one, so it saves it (and its needs)."""
    w = MainWindow(None, session=Session(dock_layout_b64="persisted-full-layout"), watch=False)
    qtbot.addWidget(w)
    snap = w._snapshot_session()           # would raise on Path(None) if note_workspace ran
    assert snap.dock_layout_b64 != "persisted-full-layout"   # replaced by this window's own state
    assert snap.view_state["window_layout"]["version"] == 4
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

    # Find the Log action in the View > Panels menu.
    action = None
    for a in w._view_menu.actions():
        if a.menu() and a.text().replace("&", "") == "Panels":
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


# ---- the experiment is a real folder (title / close / recents) ---------------
def test_new_experiment_is_a_pending_real_folder_named_in_the_title(qtbot, tmp_path):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)

    w.new_workspace()
    root = w._wsm.root()
    assert w._wsm.state() == "pending"
    assert root is not None and root.is_dir() and root.parent == tmp_path / "experiments"
    assert w.windowTitle() == f"ISAR — {root.name}"   # never "Unsaved"
    assert w.is_empty                                   # pending is an empty bind (docks show empty)
    assert w.experiment_strip._name.full_text() == root.name


def test_close_never_prompts(qtbot, monkeypatch, make_ws):
    from PySide6.QtGui import QCloseEvent

    class _NoBox:
        def __init__(self, *a, **k):
            raise AssertionError("closing ISAR must not ask anything")

    ws, _ = _open(make_ws)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.new_workspace()                                   # a pending experiment …
    assert w.load_workspace(str(ws.root))               # … left for a ready one, nothing asked
    monkeypatch.setattr("isar_gui.main_window.QMessageBox", _NoBox)
    monkeypatch.setattr(w._autosaver, "flush_now", lambda: None)  # don't touch the real session file
    event = QCloseEvent()
    w.closeEvent(event)
    assert event.isAccepted()


def test_invalid_open_keeps_the_open_experiment(qtbot, tmp_path, make_ws):
    """A folder that is not an experiment (here an empty one) is refused, not adopted."""
    ws, root = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    empty = tmp_path / "just-a-folder"
    empty.mkdir()
    assert not w.load_workspace(str(empty))
    assert w._wsm.root() == root
    assert "not an ISAR experiment" in w.statusBar().currentMessage()
    assert list(empty.iterdir()) == []                  # nothing was started in it


def test_pending_experiment_is_noted_in_recents(qtbot, tmp_path, monkeypatch):
    import tempfile

    # note_workspace refuses recents under the OS temp dir; the experiments root here lives under
    # tmp_path, so point the perceived temp dir elsewhere to model a real (recordable) location.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "_not_temp"))
    session = Session()
    w = MainWindow(None, session=session, watch=False)
    qtbot.addWidget(w)
    w.new_workspace()
    assert session.recent_workspaces == [str(w._wsm.root())]


def test_ready_drop_routes_to_ingest_controller(qtbot, make_ws, monkeypatch):
    """Dropping raw data onto a ready workspace hands it to the IngestController (which queues rather
    than refuses a concurrent drop) — the window no longer runs the inline single-flight ingest."""
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)

    requested = []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    w.dropEvent(_FakeDrop([r"some/data/path"]))
    assert requested == [[str(Path("some/data/path"))]]     # native separators, in drop order


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


def test_failed_manifest_file_is_not_known_to_watcher(qtbot, tmp_path, make_ws):
    data = tmp_path / "bad.dat"
    data.write_bytes(b"bad")
    st = data.stat()
    root = make_ws(
        files={str(data): {"size": st.st_size, "mtime": st.st_mtime, "format": "measurement",
                           "n_scans": 0, "scan_ids": [], "n_context": 0,
                           "error": "ValueError: bad header"}},
    )
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)

    assert not w._known_files_cache
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
    # The blob travels with its ``window_layout`` record (version and what it needs of a screen):
    # a blob alone is not restorable.
    w2 = MainWindow(ws2, session=Session(dock_layout_b64=saved.dock_layout_b64,
                                         view_state=dict(saved.view_state)), watch=False)
    qtbot.addWidget(w2)
    w2.show()
    # The window restores its layout in __init__; call it again directly to prove it is idempotent.
    w2._restore_layout()
    assert not w2.log_dock.isVisible()


def test_empty_start_then_open_keeps_the_saved_arrangement(qtbot, make_ws, monkeypatch):
    """The empty start and the later open share one arrangement: a saved one (right panel open, a
    wide left column) is restored on the empty start, and opening a workspace carries it across."""
    from PySide6.QtCore import Qt

    monkeypatch.setattr(MainWindow, "_available_size", lambda self: (1400, 900))
    ws, _ = _open(make_ws)
    source = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(source)
    source.show()
    source.set_right_panel_collapsed(False)
    source.resizeDocks([source.left_column_dock], [420], Qt.Horizontal)
    qtbot.wait(50)
    saved = source._snapshot_session()
    left = source.left_column_dock.width()

    root = make_ws(rows=[{"scan": 2, "detector": "det", "status": "fitted", "omega0": 6.0}])
    w = MainWindow(None, session=Session(dock_layout_b64=saved.dock_layout_b64,
                                         view_state=dict(saved.view_state)), watch=False)
    qtbot.addWidget(w)
    w.show()
    qtbot.wait(50)
    assert "Browser" not in w._docks
    assert not w._right_panel_collapsed()
    assert abs(w.left_column_dock.width() - left) <= 4

    assert w.load_workspace(str(root)) is True
    qtbot.wait(50)
    assert "Browser" in w._docks
    assert not w._right_panel_collapsed()
    assert abs(w.left_column_dock.width() - left) <= 4


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


def test_activity_signal_after_pending_rebind_does_not_touch_dead_card_dock(qtbot, make_ws):
    """An operation-activity signal that arrives after a rebind to the pending/empty state must not
    reach the previous binding's Card dock. (Regression: _teardown_binding deleteLater()'d the docks
    but left ``self.card_dock`` pointing at the dead C++ widget; the empty rebind returns early from
    _init_docks without recreating it, so _on_operation_activity_changed called
    ``card_dock.set_operation_busy`` on a deleted QPlainTextEdit and raised RuntimeError
    'Internal C++ object already deleted' — surfaced as the modal unexpected-error dialog during a
    drop-to-ingest.)"""
    ws, _ = _open(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.card_dock is not None                 # ready bind builds the Card dock

    w.new_workspace()                              # rebind to pending: analysis docks are torn down
    assert w._wsm.state() == "pending"
    assert w.is_empty
    assert w.card_dock is None                     # nulled by _teardown_binding, not a dead wrapper

    # The signal that used to crash: an ingest start arriving while bound to the pending state.
    w._on_operation_activity_changed({"kind": "ingest", "queued": 0, "cancelling": False})
    w._on_operation_activity_changed({"kind": None, "queued": 0, "cancelling": False})


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

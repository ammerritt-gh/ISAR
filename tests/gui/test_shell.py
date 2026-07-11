"""Shell tests: MainWindow builds against a synthetic workspace; the Docks menu syncs both ways."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


def _open(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(root), root


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
    widget, no watch bridge (even with watch enabled), the docks scaffolded, and 'no workspace' in
    the status bar."""
    w = MainWindow(None, session=Session(), watch=True,
                   watch_kwargs={"poll_interval_s": 0.02, "debounce_ms": 50})
    qtbot.addWidget(w)
    assert w.is_empty
    assert w._bridge is None                       # never watch a workspace that isn't there
    assert w.centralWidget() is w._empty_hint
    assert "Open Workspace" in w._empty_hint.text()
    assert w.windowTitle() == "ISAR — (no workspace)"
    assert "No workspace" in w.statusBar().currentMessage()
    # All docks are still present (empty/graceful), so the layout is stable across a later bind.
    for name in ("Log", "Browser", "Plane (q, E)", "Dispersion ω(q)", "Waterfall / Comparison"):
        assert name in w._docks
    # The empty viewmodels render their own honest empty views (no fitted scans / empty tree).
    assert w.browser_vm.group_rows == []
    assert w.heatmap_vm.spec.strips == []
    # workspace_path stays None (not the string "None") so the Browser summary renders blank.
    assert w.browser_vm.card_summary["workspace_path"] is None
    assert w.browser_dock._summary_labels["workspace_path"].text() == ""


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


def test_empty_start_autosave_does_not_crash(qtbot):
    """A dock toggle in the empty state fires the debounced autosave; the snapshot must not choke on
    the absent workspace root (note_workspace is skipped when empty)."""
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    snap = w._snapshot_session()           # would raise on Path(None) if note_workspace ran
    assert snap.dock_layout_b64 is not None
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

    # Menu -> dock: unchecking hides the dock.
    action.setChecked(False)
    action.toggled.emit(False)
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

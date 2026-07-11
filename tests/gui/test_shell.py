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

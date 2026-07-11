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

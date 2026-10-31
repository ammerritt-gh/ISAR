"""Floating panels are Qt's standard floating windows (operator ruling 2026-10-31): our code adds no frame controls."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


@pytest.fixture
def window(qtbot, make_ws):
    window = MainWindow(Workspace.open(make_ws(rows=[])), session=Session(), watch=False)
    qtbot.addWidget(window)
    window.resize(1108, 851)
    window.show()
    QApplication.processEvents()
    return window


@pytest.mark.parametrize('name', ['Log', 'Files', 'Campaign'])
def test_floating_panel_has_qt_default_flags_and_redocks_with_its_content(window, name):
    dock = window._docks[name]
    window._show_panel(dock)
    dock.setFloating(True)
    QApplication.processEvents()
    assert dock.isFloating() and dock.isVisible()
    assert not dock.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    assert not dock.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    content = dock.widget()
    dock.setFloating(False)
    QApplication.processEvents()
    assert not dock.isFloating() and dock.isVisible() and dock.widget() is content


def test_a_restored_floating_panel_is_still_floating_and_visible(qtbot, window):
    window.log_dock.setFloating(True)
    window.log_dock.resize(500, 300)
    QApplication.processEvents()
    session = Session.from_dict(window._snapshot_session().to_dict())
    restored = MainWindow(window._ws, session=session, watch=False)
    qtbot.addWidget(restored)
    restored.show_saved_window()
    QApplication.processEvents()
    assert restored.log_dock.isFloating() and restored.log_dock.isVisible()


def test_persistent_log_dock_survives_a_workspace_switch(window, make_ws):
    dock = window.log_dock
    dock.setFloating(True)
    QApplication.processEvents()
    assert window.load_workspace(str(make_ws(rows=[])))
    QApplication.processEvents()
    assert window.log_dock is dock and dock.isVisible()

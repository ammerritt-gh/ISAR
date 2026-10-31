"""Floating panels behave as normal windows without losing their dock identity."""
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


def _assert_controls(dock):
    assert dock.isFloating()
    assert dock.windowType() == Qt.WindowType.Window
    assert dock.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert dock.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint


@pytest.mark.parametrize('name', ['Log', 'Files', 'Campaign', 'Analyzer array'])
def test_floating_panels_keep_controls_through_redocking_and_resize(window, name):
    dock = window._docks[name]
    if name == 'Analyzer array':
        assert dock.isHidden()
    window._show_panel(dock)
    if not dock.isFloating():
        dock.setFloating(True)
    QApplication.processEvents()
    _assert_controls(dock)
    assert dock.parentWidget() is window and dock.isVisible()
    content = dock.widget()
    dock.showMaximized()
    QApplication.processEvents()
    assert dock.isMaximized()
    dock.showNormal()
    QApplication.processEvents()
    dock.setFloating(False)
    QApplication.processEvents()
    assert not dock.isFloating() and dock.isVisible() and dock.widget() is content
    dock.setFloating(True)
    QApplication.processEvents()
    _assert_controls(dock)
    assert dock.isVisible() and dock.widget() is content


def test_panels_menu_restores_a_minimized_maximized_dock(window):
    dock = window.log_dock
    dock.setFloating(True)
    dock.showMaximized()
    QApplication.processEvents()
    dock.showMinimized()
    QApplication.processEvents()
    assert dock.isMinimized()
    action = next(a for a in window._docks_menu.actions() if a.text() == 'Log')
    action.trigger()
    QApplication.processEvents()
    assert dock.isVisible() and not dock.isMinimized() and dock.isMaximized()
    assert action.isChecked()
    _assert_controls(dock)


def test_restored_floating_dock_has_controls_and_hidden_array_stays_hidden(qtbot, window):
    dock = window.log_dock
    dock.setFloating(True)
    dock.resize(500, 300)
    QApplication.processEvents()
    session = Session.from_dict(window._snapshot_session().to_dict())
    restored = MainWindow(window._ws, session=session, watch=False)
    qtbot.addWidget(restored)
    restored.show_saved_window()
    QApplication.processEvents()
    _assert_controls(restored.log_dock)
    assert restored.log_dock.isVisible()
    _assert_controls(restored.array_dock)
    assert restored.array_dock.isHidden()


def test_persistent_log_dock_keeps_controls_after_workspace_switch(window, make_ws):
    dock = window.log_dock
    dock.setFloating(True)
    QApplication.processEvents()
    assert window.load_workspace(str(make_ws(rows=[])))
    QApplication.processEvents()
    assert window.log_dock is dock and dock.isVisible()
    _assert_controls(dock)

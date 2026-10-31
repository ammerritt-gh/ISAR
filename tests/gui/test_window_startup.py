"""The real startup window restores one coherent native state and dock layout."""
import pytest
from PySide6.QtWidgets import QApplication

from isar_gui.main_window import MainWindow
from isar_gui.session import Session


def _window(qtbot, session=None):
    window = MainWindow(None, session=session or Session(), watch=False)
    qtbot.addWidget(window)
    return window


def _saved(window):
    return Session.from_dict(window._snapshot_session().to_dict())


def _assert_restored_size(window, saved):
    area = window.screen().availableGeometry()
    # The default offscreen screen is smaller than the real GUI's minimum width.
    # Qt must bring an oversized saved window onto that screen, too.
    if saved.width() <= area.width() and saved.height() <= area.height():
        assert window.size() == saved
    else:
        assert window.width() <= area.width() and window.height() <= area.height()
        assert window.geometry().intersects(area)


def test_normal_window_geometry_and_preferences_survive_startup(qtbot):
    first = _window(qtbot)
    first.resize(720, 620)
    first.move(30, 40)
    first.show()
    QApplication.processEvents()
    first._scan_numbering_actions['both'].trigger()
    first.set_right_panel_collapsed(True)
    position, size = first.pos(), first.size()
    session = _saved(first)
    first.close()

    second = _window(qtbot, session)
    assert not second.isVisible()
    second.show_saved_window()
    QApplication.processEvents()
    assert second.isVisible() and not second.isMinimized() and not second.isMaximized()
    _assert_restored_size(second, size)
    if size.width() + position.x() <= second.screen().availableGeometry().right():
        assert (second.pos() - position).manhattanLength() <= 4
    assert second.scan_list_vm.analyzer_numbering == 'both'
    assert second._scan_numbering_actions['both'].isChecked()
    assert second._right_panel_collapsed()


@pytest.mark.parametrize('minimized', [False, True])
def test_maximized_startup_keeps_its_normal_restore_size(qtbot, minimized):
    first = _window(qtbot)
    first.resize(720, 620)
    first.show()
    QApplication.processEvents()
    normal = first.geometry()
    first.showMaximized()
    QApplication.processEvents()
    if minimized:
        first.showMinimized()
        QApplication.processEvents()
    session = _saved(first)
    first.close()

    second = _window(qtbot, session)
    second.show_saved_window()
    QApplication.processEvents()
    assert second.isMaximized() and not second.isMinimized()
    second.showNormal()
    QApplication.processEvents()
    _assert_restored_size(second, normal.size())


def test_normal_window_closed_minimized_reopens_visible(qtbot):
    first = _window(qtbot)
    first.resize(720, 620)
    first.show()
    QApplication.processEvents()
    size = first.size()
    first.showMinimized()
    QApplication.processEvents()
    session = _saved(first)
    first.close()
    second = _window(qtbot, session)
    second.show_saved_window()
    QApplication.processEvents()
    assert second.isVisible() and not second.isMinimized() and not second.isMaximized()
    _assert_restored_size(second, size)


@pytest.mark.parametrize('blob', [None, 'not-a-Qt-geometry', '\u2603'])
def test_first_start_or_invalid_geometry_has_a_complete_maximized_window(qtbot, blob):
    session = Session(view_state={'window_geometry_b64': blob})
    window = _window(qtbot, session)
    window.show_saved_window()
    QApplication.processEvents()
    assert window.isVisible() and window.isMaximized() and not window.isMinimized()
    assert window.centralWidget().width() > 0 and window.centralWidget().height() > 0


def test_resize_and_move_request_geometry_autosave(qtbot, monkeypatch):
    window = _window(qtbot)
    window.show()
    QApplication.processEvents()
    requests = []
    monkeypatch.setattr(window._autosaver, 'request', lambda: requests.append(True))
    window.resize(720, 620)
    window.move(30, 40)
    QApplication.processEvents()
    assert requests
    assert _saved(window).view_state['window_geometry_b64']


def test_application_entrypoint_preserves_a_normal_saved_window(qtbot, monkeypatch):
    from isar_gui import app, main_window

    first = _window(qtbot)
    first.resize(1100, 740)
    first.show()
    QApplication.processEvents()
    session = _saved(first)
    first.close()
    launched = []

    class StartupWindow(MainWindow):
        def __init__(self, ws):
            super().__init__(ws, session=session, watch=False)
            qtbot.addWidget(self)
            launched.append(self)

    monkeypatch.setattr(main_window, 'MainWindow', StartupWindow)
    monkeypatch.setattr(app, '_resolve_recent_workspace', lambda: None)
    monkeypatch.setattr(app, '_legacy_temp_note', lambda: None)
    monkeypatch.setattr(app, '_install_exception_hook', lambda path: None)
    monkeypatch.setattr(QApplication, 'exec', lambda self: 0)
    assert app.main([]) == 0
    QApplication.processEvents()
    assert launched[0].isVisible()
    assert not launched[0].isMaximized() and not launched[0].isMinimized()

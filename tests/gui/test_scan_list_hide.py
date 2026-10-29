"""Scan-list right-click menu on the real window (offscreen Qt): Remove from list, Restore to list and
Show removed. The menu is opened and its actions triggered as an operator would, through the list's own
handler; nothing here runs ``isar``.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from scans_view_fixture import ONE_MODE

REMOVE, RESTORE = "Remove from list", "Restore to list"


def _window(qtbot, monkeypatch, ws_root, size=(1108, 851)):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    window = MainWindow(Workspace.open(ws_root), session=Session(), watch=False)
    qtbot.addWidget(window)
    window.resize(*size)
    window.show()
    window.activateWindow()
    qtbot.waitExposed(window)
    return window


def _menu(window, key, text):
    """Right-click the key's row and trigger ``text`` in the menu the list opens there."""
    panel = window.scan_list
    index = panel.index_for_key(key)
    parent = index.parent()
    while parent.isValid():
        panel._view.expand(parent)
        parent = parent.parent()
    rect = panel._view.visualRect(index)
    panel._on_context_menu(rect.center())
    action = next(a for a in panel._row_menu.actions() if a.text() == text)
    action.trigger()


def test_remove_from_list_hides_the_row_and_survives_reopening(qtbot, monkeypatch, scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    panel, vm = window.scan_list, window.scan_list_vm
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(window, ONE_MODE, REMOVE)
    assert vm.is_removed(ONE_MODE) and ONE_MODE not in vm.selection.keys
    assert ONE_MODE not in panel._by_key
    assert vm.snapshot.spectrum(ONE_MODE).fit is not None      # hidden, not excluded: the fit stays
    window._save_gui_store()

    reopened = _window(qtbot, monkeypatch, scans_view_ws)
    assert reopened.scan_list_vm.is_removed(ONE_MODE)
    assert ONE_MODE not in reopened.scan_list._by_key
    reopened.scan_list._removed_button.click()                  # Show removed
    greyed = reopened.scan_list._by_key[ONE_MODE]
    assert "(removed)" in greyed.text() and greyed.data(Qt.ItemDataRole.ForegroundRole) is not None
    reopened.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(reopened, ONE_MODE, RESTORE)
    assert not reopened.scan_list_vm.is_removed(ONE_MODE)
    assert "(removed)" not in reopened.scan_list._by_key[ONE_MODE].text()

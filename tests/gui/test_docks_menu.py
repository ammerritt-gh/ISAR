"""Docks-menu regression tests: a tabified dock going behind its sibling must NOT vanish.

The bug: the Docks-menu action was wired to `toggled`. A tabified dock going behind its sibling
fires `visibilityChanged(False)` -> `action.setChecked(False)`, and under `toggled` that
programmatic state change re-entered the handler and *hid* the dock — permanently losing the buried
tab. The fix wires the handler to `triggered` (user click only), so the visibility sync no longer
hides anything. These tests pin both halves: programmatic setChecked / visibilityChanged never hide,
while an actual user trigger still toggles.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


def _open(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(root)


def _dock_action(w, label):
    # Read off the held _docks_menu attribute, not a temporary `menuBar().actions()[i].menu()`
    # wrapper — the temporary parent wrapper is GC'd on return and shiboken then invalidates the
    # child QAction wrapper we returned.
    for sub in w._docks_menu.actions():
        if sub.text() == label:
            return sub
    return None


def test_visibility_sync_does_not_hide_dock(qtbot, make_ws):
    """The core regression: a programmatic `visibilityChanged(False)` (what a tabified dock going
    behind emits) must NOT hide the dock. Under the old `toggled` wiring it did; under `triggered`
    it doesn't."""
    w = MainWindow(_open(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()

    # Browser and Card are tabified siblings; simulate Card going behind.
    dock = w.card_dock
    action = _dock_action(w, "Card")
    assert action is not None

    dock.visibilityChanged.emit(False)  # what Qt emits when the tab is buried

    # The action reflects the (buried) visibility state...
    assert not action.isChecked()
    # ...but the dock was NOT explicitly hidden — a buried tab is not a hidden widget. The bug hid
    # it (isHidden() would be True), which is exactly what deletes the tab from the tab bar.
    assert not dock.isHidden()


def test_programmatic_setchecked_does_not_hide_dock(qtbot, make_ws):
    """Directly driving `action.setChecked(False)` (the tail of the visibility sync) leaves the dock
    visible: only a user `triggered` may hide it."""
    w = MainWindow(_open(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()

    action = _dock_action(w, "Log")
    assert action is not None and action.isChecked()

    action.setChecked(False)  # emits toggled(False), NOT triggered
    assert not w.log_dock.isHidden()  # untouched by the handler


def test_user_trigger_toggles_dock(qtbot, make_ws):
    """A genuine menu click (triggered) still hides and shows the dock."""
    w = MainWindow(_open(make_ws), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()

    action = _dock_action(w, "Log")
    assert action is not None and action.isChecked()

    # trigger() flips the checkable action then emits triggered(checked) — the real user-click path.
    action.trigger()  # user unchecks
    assert not action.isChecked()
    assert w.log_dock.isHidden()

    action.trigger()  # user re-checks
    assert action.isChecked()
    assert not w.log_dock.isHidden()

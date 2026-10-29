"""Shared assertions for the inter-spectra GUI tests.

``assert_described`` is the house rule of the GUI operator contract: every control a 3 AM user meets has a
tooltip and a name, and every menu shows its tooltips and its actions have tooltips.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QListWidget, QMenu,
                               QWidget)

_CONTROLS = (QAbstractButton, QComboBox, QAbstractSpinBox, QListWidget, QLineEdit)


def assert_described(widget: QWidget) -> None:
    """Assert every control under ``widget`` (and ``widget`` itself) is described; raise AssertionError naming the first."""
    for kind in _CONTROLS:
        found = list(widget.findChildren(kind))
        if isinstance(widget, kind):
            found.insert(0, widget)
        for control in found:
            label = control.text() if isinstance(control, QAbstractButton) else ""
            name = control.objectName() or type(control).__name__
            assert control.accessibleName() or label, f"{name} has no accessible name or text"
            assert control.toolTip(), f"{name} has no tooltip"
    for menu in widget.findChildren(QMenu):
        name = menu.objectName() or menu.title() or "menu"
        assert menu.toolTipsVisible(), f"{name} does not show its tooltips"
        for action in menu.actions():
            if not action.isSeparator():
                assert action.toolTip(), f"action {action.text()!r} in {name} has no tooltip"

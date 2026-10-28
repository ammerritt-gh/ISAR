"""Shared geometry checks for the window's one arrangement (a plain helper, not a test module).

``assert_frame_geometry`` is the single definition of "the frame holds": used by
``test_window_frame.py`` on the foundation's stub mounts, and by the combined geometry check with real
content at the hardest laptop combination.
"""
from __future__ import annotations

import itertools

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

from isar_gui.views.elided_label import ElidedButton, ElidedLabel

_DISPLAY_MIN = (300, 250)


def rect_in(window, widget) -> QRect:
    """``widget``'s rectangle in ``window`` coordinates."""
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def assert_frame_geometry(window, available: tuple[int, int]) -> None:
    """The frame of the one arrangement holds at an ``available`` (width, height) screen:

    * every frame dock (left column, Log, Output) and the Processing dock when open lie inside the window;
    * the display (the central stack) keeps at least 300 x 250 px;
    * the left column runs past the bottom bar: its bottom is level with or below the Log's, and the
      Log starts to the right of the column, with the Output to the right of the Log;
    * the window is no larger than the screen (nothing forced it past its minimum size).
    """
    assert window.isVisible(), "the window must be shown to measure its geometry"
    inside = window.rect()
    left = rect_in(window, window.left_column_dock)
    log = rect_in(window, window.log_dock)
    output = rect_in(window, window.output_dock)
    for name, rect in (("left column", left), ("Log", log), ("Output", output)):
        assert inside.contains(rect), f"{name} {rect} leaves the window {inside}"
    # Only Processing, the front tab: a buried tab keeps a stale geometry.
    if window.processing_dock.isVisible():
        assert inside.contains(rect_in(window, window.processing_dock)), "Processing leaves the window"
    display = window._center_stack
    assert display.width() >= _DISPLAY_MIN[0] and display.height() >= _DISPLAY_MIN[1], (
        f"the display shrank to {display.width()}x{display.height()}")
    assert left.bottom() >= log.bottom(), "the left column must run to (or past) the bottom bar"
    assert log.left() > left.right(), "the Log must start right of the left column"
    assert output.left() >= log.right(), "the Output must sit right of the Log"
    assert window.width() <= available[0] and window.height() <= available[1], (
        f"the window {window.width()}x{window.height()} outgrew the screen {available}")


def assert_apart(window, widgets, what: str) -> None:
    """No two visible widgets share a pixel of the window (mapped rectangles)."""
    rects = [(widget, rect_in(window, widget)) for widget in widgets if widget.isVisible()]
    for (a, ra), (b, rb) in itertools.combinations(rects, 2):
        assert not ra.intersects(rb), (
            f"{what}: {a.objectName() or a} {ra} overlaps {b.objectName() or b} {rb}")


def assert_reachable(window, control, scroll=None, *, enabled=True) -> None:
    """A whole, enabled control inside the window, after scrolling its own content if needed."""
    if scroll is not None:
        QApplication.processEvents()
        scroll.verticalScrollBar().setValue(
            max(0, control.mapTo(scroll.widget(), QPoint(0, 0)).y() - 20))
        QApplication.processEvents()
    rect = rect_in(window, control)
    name = control.objectName() or type(control).__name__
    assert control.isVisible() and control.isEnabled() == enabled and rect.width() > 0, name
    assert window.rect().contains(rect), (name, rect)
    assert control.visibleRegion().boundingRect().size() == control.size(), (
        name, "is clipped or covered", control.visibleRegion().boundingRect(), control.size())


def assert_elided_have_tooltips(root) -> None:
    """Visible shortened labels and buttons expose their complete text on hover."""
    for control_type in (ElidedLabel, ElidedButton):
        for control in root.findChildren(control_type):
            if control.isVisible() and control.text() != control.full_text():
                assert control.full_text() and control.full_text() in control.toolTip(), (
                    control.objectName() or type(control).__name__, control.full_text(), control.toolTip())

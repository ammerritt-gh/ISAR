"""ElidedLabel -- a one-line label that never widens its column.

"Nothing clips": a long name is shortened with "…" to the width it is given and shown in full as the
tooltip. Its minimum width is zero, so a long experiment name or folder path cannot push the left
column (or any mount) past its minimum width.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._mode = mode
        self._full = ""
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self):  # noqa: N802 (Qt override)
        hint = super().sizeHint()
        hint.setWidth(0)        # the column decides the width, never the text
        return hint

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, self._mode, max(self.width(), 1)))


class ElidedButton(QPushButton):
    """A button that never pins its column's width: its text is shortened with "…" to the room it has
    (the full text stays the accessible name), as :class:`ElidedLabel` does for a label."""
    _PAD = 24       # px the button style adds around the text

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setAccessibleName(text)
        # Preferred, not the default Minimum: only a policy that may shrink lets the layout use the 0 minimum
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setAccessibleName(text)
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight,
                                                    max(self.width() - self._PAD, 1)))
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802 (Qt override)
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full) + self._PAD)
        return hint

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                    max(self.width() - self._PAD, 1)))

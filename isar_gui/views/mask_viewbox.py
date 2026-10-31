"""PUFFIN's click/rectangle exclusion gesture, in the displayed coordinate system."""
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtWidgets import QGraphicsRectItem
import pyqtgraph as pg


class MaskViewBox(pg.ViewBox):
    box_preview = Signal(object)   # view rectangle, or None when cancelled
    box_selected = Signal(object)
    mode_changed = Signal(bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask_mode = False
        self.selecting = False
        self._rectangle = QGraphicsRectItem()
        self._rectangle.setPen(pg.mkPen('#1864ab', width=1))
        self._rectangle.setBrush(pg.mkBrush(24, 100, 171, 25))
        self._rectangle.setZValue(100)
        self.addItem(self._rectangle, ignoreBounds=True)
        self._rectangle.hide()

    def set_mask_mode(self, enabled):
        self.mask_mode = bool(enabled)
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.cancel_selection()
        self.mode_changed.emit(self.mask_mode)

    def cancel_selection(self):
        self.selecting = False
        self._rectangle.hide()
        self.box_preview.emit(None)

    def mouseClickEvent(self, event):  # noqa: N802
        if self.mask_mode and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mouseClickEvent(event)

    def mouseDragEvent(self, event, axis=None):  # noqa: N802
        if not self.mask_mode or event.button() != Qt.MouseButton.LeftButton:
            return super().mouseDragEvent(event, axis=axis)
        event.accept()
        start = self.mapSceneToView(event.buttonDownScenePos())
        end = self.mapSceneToView(event.scenePos())
        rect = QRectF(start, end).normalized()
        self.selecting = True
        self._rectangle.setRect(rect)
        self._rectangle.show()
        self.box_preview.emit(rect)
        if event.isFinish():
            self.selecting = False
            self._rectangle.hide()
            self.box_selected.emit(rect)
            self.box_preview.emit(None)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.mask_mode:
            self.set_mask_mode(False)
            event.accept()
        else:
            super().keyPressEvent(event)

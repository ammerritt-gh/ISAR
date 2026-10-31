"""Viewport decorations for measured points outside the visible axes; never affect bounds."""
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPen, QColor, QPolygonF
import pyqtgraph as pg


class EdgeIndicators(pg.GraphicsObject):
    def __init__(self, viewbox):
        super().__init__()
        self.vb = viewbox
        self.points = ()
        self.counts = {}
        self.enabled = True
        self.setParentItem(viewbox)
        self.setZValue(200)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.vb.sigRangeChanged.connect(self.refresh)
        self.vb.sigResized.connect(self.refresh)

    def set_points(self, points, enabled=True):
        """``(x, y, kept)`` triples, including waterfall offsets."""
        self.points = tuple(points)
        self.enabled = enabled
        self.refresh()

    def refresh(self, *_args):
        self.prepareGeometryChange()
        (lo, hi), (bottom, top) = self.vb.viewRange()
        counts = {edge: [0, 0] for edge in ('left', 'right', 'bottom', 'top')}
        for x, y, kept in self.points:
            x, y, kept = np.asarray(x), np.asarray(y), np.asarray(kept, dtype=bool)
            finite = np.isfinite(x) & np.isfinite(y)
            for edge, outside in (('left', x < lo), ('right', x > hi),
                                  ('bottom', y < bottom), ('top', y > top)):
                counts[edge][0] += int(np.count_nonzero(finite & outside & kept))
                counts[edge][1] += int(np.count_nonzero(finite & outside & ~kept))
        self.counts = {k: tuple(v) for k, v in counts.items() if sum(v)}
        self.setToolTip('\n'.join(f'{k}: {v[0]} unmasked, {v[1]} masked points outside the view'
                                 for k, v in self.counts.items()))
        self.setVisible(self.enabled and bool(self.counts))
        self.update()

    def boundingRect(self):  # noqa: N802
        return QRectF(self.vb.rect())

    def paint(self, painter, option, widget=None):
        rect = self.boundingRect()
        w, h = rect.width(), rect.height()
        half = min(22.0, w / 5, h / 5)
        painter.setPen(QPen(QColor('black'), 2))
        painter.setBrush(QColor('black'))
        for edge in self.counts:
            painter.save()
            # Local x points outward; the bar is perpendicular to the arrow.
            cx, cy, angle = {'left': (4, h / 2, 180), 'right': (w - 4, h / 2, 0),
                             'top': (w / 2, 4, -90), 'bottom': (w / 2, h - 4, 90)}[edge]
            painter.translate(cx, cy)
            painter.rotate(angle)
            painter.drawLine(QPointF(-7, -half), QPointF(-7, half))
            painter.drawLine(QPointF(-14, 0), QPointF(0, 0))
            painter.drawPolygon(QPolygonF([QPointF(0, 0), QPointF(-4, -3), QPointF(-4, 3)]))
            painter.restore()

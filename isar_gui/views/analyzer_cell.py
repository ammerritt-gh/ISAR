"""One analyzer's cell in the Analyzer array: names, a small plot, the hkl and |Q| below it.

The cell draws what its ``CellState`` says and nothing else (hard rule 7); it is updated in place, so the 28
cells of a grid are built once and a scan switch repaints only the plots whose data changed. It paints its own
outlines (anchor solid, lock dashed, both nested), focus and the batch ring, so it carries an accessible name and
description. A batch mark is a word with a dot in the name row (the tone repeats it), a dimmed plot while waiting, and
a ring while a worker holds the spectrum; the dock's timer only moves the ring's brightness. An absence word ("no fit")
sits in a strip under the plot, never over the data.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStackedLayout, QVBoxLayout, QWidget
import pyqtgraph as pg
from isar_gui.views.edge_indicators import EdgeIndicators

from isar.plotspec.spec import Role
from isar_gui.inter_words import BATCH_TIPS, BATCH_WORDS
from isar_gui.plotspec_pg import ROLE_COLOR
from isar_gui.theme import TONE_COLOR, Tone, dot_icon, marker_icon
from isar_gui.viewmodels.analyzer_array import CellState
from isar_gui.views.elided_label import ElidedLabel

CELL_MIN = (132, 150)        # px: a dock smaller than the grid scrolls, the cells never shrink below this
AXIS_COLOR = "#404040"
VEIL_ALPHA = 170             # of 255: the window-coloured veil over a waiting cell's plot
RING_STILL = 0.6             # the ring's brightness with the timer stopped (the reduced-motion look)
BATCH_TONES = {"waiting": Tone.IDLE, "fitting": Tone.ACTIVE, "done": Tone.OK, "refused": Tone.WARN,
               "failed": Tone.ERR, "not_reached": Tone.IDLE}
MARKERS = {"anchor": "Anchor: a spectrum you trust. An anchored batch grows outward from it.",
           "lock": "Locked: this spectrum's fit is fixed, so no batch will refit it."}


def _small(widget: QWidget, bold: bool = False, shrink: float = 1.0) -> None:
    font = widget.font()
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(max(6.5, size - shrink))
    font.setBold(bold)
    widget.setFont(font)


class _Veil(QWidget):
    """A translucent sheet in the window colour over the plot while a cell waits. A graphics effect on the embedded plot renders it
    dark offscreen and on some drivers, so the dim is painted instead; the word beside it stays at full strength."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName("batch_veil")
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        veil = self.palette().color(QPalette.ColorRole.Window)
        veil.setAlpha(VEIL_ALPHA)
        painter.fillRect(self.rect(), veil)
        painter.end()


class AnalyzerCell(QFrame):
    clicked = Signal(object, bool)          # (spectrum key or None, ctrl held)

    def __init__(self, beamline: str, internal: str, show_x_ticks: bool, parent=None) -> None:
        super().__init__(parent)
        self.beamline, self.internal = beamline, internal
        self.key: str | None = None
        self.selected = False
        self.outline_kind = "none"          # none | anchor | lock | both
        self.batch_state: str | None = None
        self.dimmed = False                 # waiting: a veil fades the plot, the word stays
        self.ringing = False                # a worker holds the spectrum: the ring is painted
        self.ring_phase = RING_STILL        # 0..1, the dock's timer moves it only while some cell rings
        self.redraws = 0                    # how many times the plot items were refilled (the in-place check)
        self.cell: CellState | None = None
        self._token = None
        self._ranges = None
        self.setObjectName(f"cell_{beamline}")
        self.setMinimumSize(*CELL_MIN)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)       # room for the 2 px outline and the nested one
        outer.setSpacing(1)
        self.name_row = QWidget(self)
        row = QHBoxLayout(self.name_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.icons = {}
        for kind in ("anchor", "lock"):
            icon = QLabel(self.name_row)
            icon.setPixmap(marker_icon(kind).pixmap(12, 12))
            icon.setToolTip(MARKERS[kind])
            icon.setAccessibleName(kind.title())
            icon.setObjectName(f"{kind}_icon")
            icon.hide()
            row.addWidget(icon)
            self.icons[kind] = icon
        self.internal_label = QLabel(internal, self.name_row)
        self.internal_label.setObjectName("internal_name")
        _small(self.internal_label, bold=True, shrink=0)
        self.beamline_label = QLabel(beamline, self.name_row)
        self.beamline_label.setObjectName("beamline_name")
        _small(self.beamline_label)
        self.beamline_label.setToolTip(f"Beamline name of analyzer {internal}")
        self.internal_label.setToolTip(f"Internal name: row {internal[0]} (A is the scattering plane), "
                                       f"position {internal[1:]} (1 is the lowest 2θ)")
        row.addWidget(self.internal_label)
        row.addWidget(self.beamline_label)
        self.stale_label = QLabel("stale", self.name_row)
        _small(self.stale_label)
        self.stale_label.hide()
        row.addWidget(self.stale_label)
        row.addStretch(1)
        self.state_dot = QLabel(self.name_row)       # the batch word's tone, so colour never stands alone
        self.state_dot.setObjectName("state_dot")
        self.state_dot.hide()
        row.addWidget(self.state_dot)
        self.state_caption = ElidedLabel("", self.name_row, Qt.TextElideMode.ElideRight)   # the batch word
        self.state_caption.setObjectName("state_caption")
        _small(self.state_caption)
        self.state_caption.hide()
        row.addWidget(self.state_caption, 1)
        outer.addWidget(self.name_row)

        box = self.plot_box = QWidget(self)
        stack = QStackedLayout(box)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        stack.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(box, background="w")
        self._setup_plot(show_x_ticks)
        stack.addWidget(self.plot)
        self.veil = _Veil(box)
        stack.addWidget(self.veil)
        outer.addWidget(box, 1)
        self.absence_label = ElidedLabel("", self, Qt.TextElideMode.ElideRight)   # a strip under the plot: never over data
        self.absence_label.setObjectName("absence_word")
        self.absence_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _small(self.absence_label, bold=True, shrink=0)
        policy = self.absence_label.sizePolicy()
        policy.setRetainSizeWhenHidden(True)           # the strip keeps its height, so the plots of a row line up
        self.absence_label.setSizePolicy(policy)
        self.absence_label.hide()
        outer.addWidget(self.absence_label)

        self.hkl_label = ElidedLabel("", self, Qt.TextElideMode.ElideRight)
        self.hkl_label.setObjectName("hkl_label")
        self.q_label = ElidedLabel("", self, Qt.TextElideMode.ElideRight)
        self.q_label.setObjectName("q_label")
        _small(self.hkl_label)
        _small(self.q_label)
        outer.addWidget(self.hkl_label)
        outer.addWidget(self.q_label)

    # ---- the plot ------------------------------------------------------------------------------
    def _setup_plot(self, show_x_ticks: bool) -> None:
        pw = self.plot
        pw.setObjectName("cell_plot")
        pw.setMenuEnabled(False)
        pw.setMouseEnabled(False, False)
        pw.hideButtons()
        pw.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)   # a click reaches the cell
        pw.setMinimumHeight(56)
        item = pw.getPlotItem()
        item.setContentsMargins(0, 0, 0, 0)
        item.vb.enableAutoRange(False)
        self.edges = EdgeIndicators(item.vb)
        font = self.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
        for name in ("left", "bottom"):
            axis = item.getAxis(name)
            axis.setPen(AXIS_COLOR)
            axis.setTextPen(AXIS_COLOR)
            axis.setStyle(tickFont=font, tickTextOffset=2, autoExpandTextSpace=False)
            axis.enableAutoSIPrefix(False)
        item.getAxis("left").setWidth(34)
        item.getAxis("bottom").setHeight(14)         # every cell keeps the strip, so plot areas line up
        item.getAxis("bottom").setStyle(showValues=show_x_ticks)
        data, fit = QColor(ROLE_COLOR[Role.DATA]), QColor(ROLE_COLOR[Role.FIT])
        self._kept = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(data))
        self._masked = pg.ScatterPlotItem(size=4, pen=pg.mkPen(data), brush=None)    # masked points stay, hollow
        self._total = pg.PlotDataItem(pen=pg.mkPen(fit, width=1.4, style=Qt.PenStyle.DashLine))
        self._parts: list = []
        self._component_pen = pg.mkPen(QColor(ROLE_COLOR[Role.COMPONENT]), width=1)
        for added in (self._total, self._kept, self._masked):
            item.addItem(added)

    def _draw(self, plot) -> None:
        self.redraws += 1
        if plot is None:
            self.edges.set_points((), self.edges.enabled)
            for scatter in (self._kept, self._masked):
                scatter.setData([], [])
            self._total.setData([], [])
            for part in self._parts:
                part.setData([], [])
            return
        x, y, kept = plot.x, plot.y, plot.kept
        self.edges.set_points(((x, y, kept),), self.edges.enabled)
        self._kept.setData(x[kept], y[kept])
        self._masked.setData(x[~kept], y[~kept])
        if plot.total is None:
            self._total.setData([], [])
        else:
            self._total.setData(plot.fit_x, plot.total)
        while len(self._parts) < len(plot.components):
            part = pg.PlotDataItem(pen=self._component_pen)
            self.plot.getPlotItem().addItem(part)
            self._parts.append(part)
        for index, part in enumerate(self._parts):
            if index < len(plot.components):
                part.setData(plot.fit_x, plot.components[index])
            else:
                part.setData([], [])

    # ---- state ---------------------------------------------------------------------------------
    def apply(self, cell: CellState) -> None:
        """Draw ``cell``; the plot items are refilled only when its data token changed."""
        self.cell, self.key = cell, cell.key
        self.selected = cell.selected
        self.stale_label.setVisible(bool(cell.stale_tip))
        self.stale_label.setToolTip(cell.stale_tip or "")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if cell.key else Qt.FocusPolicy.NoFocus)
        self.outline_kind = ("both" if cell.anchor and cell.lock else "anchor" if cell.anchor
                             else "lock" if cell.lock else "none")
        self.icons["anchor"].setVisible(cell.anchor)
        self.icons["lock"].setVisible(cell.lock)
        self._apply_batch(cell)
        token = None if cell.plot is None else cell.plot.token
        if token != self._token:
            self._draw(cell.plot)
            self._token = token
        ranges = (cell.x_range, cell.y_range)
        if ranges != self._ranges:
            self._ranges = ranges
            vb = self.plot.getPlotItem().vb
            if cell.y_range is not None:
                vb.setYRange(*cell.y_range, padding=0)
            if cell.x_range is not None:
                vb.setXRange(*cell.x_range, padding=0)
        font = self.internal_label.font()
        font.setUnderline(cell.selected)
        self.internal_label.setFont(font)

        self.plot.setVisible(cell.plot is not None)   # no empty axes where nothing is drawn: they would read as zero
        self.absence_label.setVisible(cell.absence is not None)
        self.absence_label.set_full_text(cell.absence or "")
        self.absence_label.setToolTip(cell.absence_tip or "")

        if cell.key is None:
            self.hkl_label.set_full_text("")
            self.q_label.set_full_text("")
        else:
            hkl = f"hkl {cell.hkl}" if cell.hkl else "hkl missing"
            self.hkl_label.set_full_text(hkl)
            self.hkl_label.setToolTip(f"{hkl}\n{cell.hkl_tip}")
            self.q_label.set_full_text(cell.q)
            self.q_label.setToolTip(f"{cell.q}\n{cell.q_tip}")
        self.setToolTip(self._tooltip(cell))
        self.setAccessibleName(f"Analyzer {cell.internal}, beamline {cell.beamline}")
        self.setAccessibleDescription(self._description(cell))
        self.update()

    def _apply_batch(self, cell: CellState) -> None:
        """The caption word and dot, the dim and the ring for ``cell.batch`` (None clears all of them)."""
        mark = cell.batch
        state = None if mark is None else mark.state
        self.batch_state = state
        self.ringing = state == "fitting"
        self.dimmed = state == "waiting"
        self.veil.setVisible(self.dimmed)
        if self.dimmed:
            self.veil.raise_()                       # the stacked layout raises the absence word; the veil goes over it
        if mark is None:
            self.state_caption.set_full_text("")
            self.state_caption.hide()
            self.state_dot.hide()
            return
        word = BATCH_WORDS.get(state, state)
        self.state_caption.set_full_text(word)
        self.state_caption.setToolTip(f"{word}: {mark.reason or BATCH_TIPS.get(state, '')}")
        self.state_caption.show()
        self.state_dot.setPixmap(dot_icon(BATCH_TONES.get(state, Tone.IDLE)).pixmap(10, 10))
        self.state_dot.setToolTip(self.state_caption.toolTip())
        self.state_dot.show()

    def set_ring_phase(self, phase: float) -> None:
        self.ring_phase = phase
        if self.ringing:
            self.update()

    @staticmethod
    def _tooltip(cell: CellState) -> str:
        lines = [f"Analyzer {cell.internal} · beamline {cell.beamline}"]
        if cell.key is None:
            return "\n".join(lines + [cell.absence_tip or ""])
        if cell.fit_label:
            lines.append(f"Fit drawn: {cell.fit_label} (the fit ISAR uses for this spectrum)"
                         + ("; energy axis is the beamline's zero" if cell.beamline_zero else ""))
        if cell.absence and cell.absence_tip:
            lines.append(f"{cell.absence}: {cell.absence_tip}")
        if cell.stale_tip:
            lines.append(cell.stale_tip)
        if cell.anchor:
            lines.append(MARKERS["anchor"])
        if cell.lock:
            lines.append(MARKERS["lock"])
        if cell.batch is not None:
            word = BATCH_WORDS.get(cell.batch.state, cell.batch.state)
            lines.append(f"Batch: {word}. {cell.batch.reason or BATCH_TIPS.get(cell.batch.state, '')}")
        lines.append("Click to select this spectrum in the scan list; Ctrl-click adds or removes it.")
        return "\n".join(lines)

    @staticmethod
    def _description(cell: CellState) -> str:
        parts = []
        if cell.key is None:
            return cell.absence or "no file"
        parts.append("selected" if cell.selected else "not selected")
        if cell.anchor:
            parts.append("anchor")
        if cell.lock:
            parts.append("locked fit")
        if cell.absence:
            parts.append(cell.absence)
        parts += [f"hkl {cell.hkl}" if cell.hkl else "hkl missing", cell.q]
        if cell.batch is not None:
            parts.append("batch: " + BATCH_WORDS.get(cell.batch.state, cell.batch.state))
        return ", ".join(parts)

    # ---- gestures ------------------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit(self.key, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusOutEvent(event)
        self.update()

    # ---- painting ------------------------------------------------------------------------------
    def outline_rects(self) -> list[QRectF]:
        """The rectangles the anchor/lock outlines are drawn on (for the composed-geometry check)."""
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.outline_kind == "none":
            return []
        return [r, r.adjusted(3, 3, -3, -3)] if self.outline_kind == "both" else [r]

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().paintEvent(event)
        painter = QPainter(self)
        palette = self.palette()
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.selected:               # the window's ordinary selection: a fill, a different channel from the outlines
            tint = palette.color(QPalette.ColorRole.Highlight)
            tint.setAlpha(70)
            painter.fillRect(self.rect().adjusted(1, 1, -1, -1), tint)
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        painter.drawRect(frame)
        ink = palette.color(QPalette.ColorRole.WindowText)
        rects = self.outline_rects()
        for rect, style in zip(rects, {"anchor": ("solid",), "lock": ("dashed",),
                                       "both": ("solid", "dashed")}.get(self.outline_kind, ())):
            pen = QPen(ink, 2, Qt.PenStyle.SolidLine if style == "solid" else Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect)
        if self.ringing:                 # a worker holds this spectrum; the brightness, not the position, is the pulse
            ring = QColor(TONE_COLOR[Tone.ACTIVE])
            ring.setAlphaF(0.35 + 0.65 * self.ring_phase)
            painter.setPen(QPen(ring, 2))
            painter.drawRect(QRectF(self.rect()).adjusted(2, 2, -2, -2))
        if self.hasFocus():
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 1, Qt.PenStyle.DotLine))
            painter.drawRect(QRectF(self.rect()).adjusted(4.5, 4.5, -4.5, -4.5))
        painter.end()

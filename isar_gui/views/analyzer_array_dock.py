"""The Analyzer array dock: one scan's analyzers as a grid of small plots, where the analyzers sit.

A viewer. Its one interaction is selection, shared both ways with the scan list through the window's
``Selection``; it holds no selection, scale reference or fit of its own (hard rule 7). The header says which scan
and layout are shown and carries the intensity scale, the y-axis mode and the typed shared limit, which affect
every cell. Cells are built once per layout and updated in place; the dock does nothing while hidden.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QButtonGroup, QDockWidget, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from isar_gui.viewmodels.analyzer_array import SCALES, Y_MODES, ArrayState, AnalyzerArrayViewModel
from isar_gui.views.analyzer_cell import CELL_MIN, AnalyzerCell
from isar_gui.views.elided_label import ElidedLabel

_log = logging.getLogger(__name__)

DEFAULT_SIZE = (1060, 800)           # px: the floating window's first size, clamped to the screen by the window
AXIS_THICKNESS = 20
RING_TICK_MS = 100
RING_PERIOD_TICKS = 24               # a slow pulse: 2.4 s from dim to bright to dim
SCALE_TIPS = {
    "normalised": "Normalised: counts per one experiment-wide reference monitor, the same reference as the "
                  "Scan(s) plot. Affects every cell's intensity.",
    "raw": "Raw: counts as recorded. Affects every cell's intensity."}
MODE_TIPS = {
    "auto": "Each cell scales its own y axis to its own data. Affects every cell's y axis.",
    "shared": "All cells use one y limit, so heights compare between cells. Affects every cell's y axis."}
LIMIT_TIP = ("The y limit all cells share when 'One limit for all' is chosen (0 to this value). Empty uses the "
             "largest value shown. Kept separately for Normalised and Raw, since their units differ.")


class AxisLabel(QWidget):
    """A one-line axis caption, drawn shortened with "…" to the room it has; the full text is the tooltip."""

    def __init__(self, vertical: bool, parent=None) -> None:
        super().__init__(parent)
        self.vertical = vertical
        self._full = ""
        if vertical:
            self.setFixedWidth(AXIS_THICKNESS)
            self.setMinimumHeight(0)
        else:
            self.setFixedHeight(AXIS_THICKNESS)

    def set_full_text(self, text: str, tip: str = "") -> None:
        self._full = text
        self.setToolTip(f"{text}\n{tip}" if tip else text)
        self.setAccessibleName(text)
        self.update()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(AXIS_THICKNESS, 0) if self.vertical else QSize(0, AXIS_THICKNESS)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        length = self.height() if self.vertical else self.width()
        text = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(length - 4, 1))
        if self.vertical:
            painter.translate(0, self.height())
            painter.rotate(-90)
            painter.drawText(0, 0, self.height(), self.width(), int(Qt.AlignmentFlag.AlignCenter), text)
        else:
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()


class AnalyzerArrayDock(QDockWidget):
    def __init__(self, vm: AnalyzerArrayViewModel, parent=None) -> None:
        super().__init__("Analyzer array", parent)
        self.setObjectName("analyzer_array_dock")
        self.vm = vm
        self.last_redraw = None
        self.cells: dict[str, AnalyzerCell] = {}
        self._layout_key = None
        self._applied: ArrayState | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._redraw)
        self.ring_timer = QTimer(self)      # moves the fitting ring's brightness; runs only while a visible cell rings
        self.ring_timer.setInterval(RING_TICK_MS)
        self.ring_timer.timeout.connect(self._ring_tick)
        self._ring_t = 0

        root = QWidget(self)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(3)
        self.title_label = ElidedLabel("", root, Qt.TextElideMode.ElideRight)
        self.title_label.setObjectName("array_title")
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        outer.addWidget(self.title_label)
        self.note_label = self._line(outer, "array_selection_note", root)
        self.layout_label = self._line(outer, "array_layout_line", root)
        self.unplaced_label = self._line(outer, "array_unplaced", root)
        self.batch_label = self._line(outer, "array_batch_line", root)

        scale_row, self.scale_group, self.scale_buttons = self._segment(
            root, "Intensity", SCALES, {"normalised": "Normalised", "raw": "Raw"}, SCALE_TIPS)
        self.scale_group.idClicked.connect(lambda i: self.vm.set_scale(SCALES[i]))
        scale_row.addStretch(1)
        outer.addLayout(scale_row)
        mode_row, self.mode_group, self.mode_buttons = self._segment(
            root, "Y axis", Y_MODES, {"auto": "Auto per cell", "shared": "One limit for all"}, MODE_TIPS)
        self.mode_group.idClicked.connect(lambda i: self.vm.set_y_mode(Y_MODES[i]))
        self.limit_box = QLineEdit(root)
        self.limit_box.setObjectName("array_limit")
        self.limit_box.setAccessibleName("Shared y limit")
        self.limit_box.setFixedWidth(118)
        self.limit_box.editingFinished.connect(self._limit_typed)
        mode_row.addWidget(self.limit_box)
        self.limit_unit = ElidedLabel("", root, Qt.TextElideMode.ElideRight)
        self.limit_unit.setObjectName("array_limit_unit")
        mode_row.addWidget(self.limit_unit, 1)
        outer.addLayout(mode_row)
        self.limit_note = self._line(outer, "array_limit_note", root)

        self.caption_label = self._line(outer, "array_caption", root)
        self.body = QStackedWidget(root)
        self.message = QLabel("", self.body)
        self.message.setObjectName("array_message")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.body.addWidget(self.message)
        self.scroll = QScrollArea(self.body)
        self.scroll.setObjectName("array_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setMinimumSize(120, 100)
        self.scroll.setAccessibleName("Analyzer grid")
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(4)
        self.x_axis = AxisLabel(False, self.grid_host)
        self.x_axis.setObjectName("array_x_axis")
        self.y_axis = AxisLabel(True, self.grid_host)
        self.y_axis.setObjectName("array_y_axis")
        self.scroll.setWidget(self.grid_host)
        self.body.addWidget(self.scroll)
        outer.addWidget(self.body, 1)
        self.setWidget(root)

        vm.selection.changed.connect(self.refresh)
        vm.scan_list.snapshot_changed.connect(self.refresh)
        vm.refreshed.connect(self.refresh)
        vm.marks_moved.connect(self.refresh)

    @staticmethod
    def _line(layout, name, parent) -> ElidedLabel:
        label = ElidedLabel("", parent, Qt.TextElideMode.ElideRight)
        label.setObjectName(name)
        label.hide()
        layout.addWidget(label)
        return label

    @staticmethod
    def _segment(parent, title, values, words, tips):
        row = QHBoxLayout()
        row.setSpacing(2)
        caption = QLabel(title, parent)
        caption.setFixedWidth(max(52, caption.fontMetrics().horizontalAdvance(title) + 6))
        row.addWidget(caption)
        group = QButtonGroup(parent)
        group.setExclusive(True)
        buttons = {}
        for index, value in enumerate(values):
            button = QToolButton(parent)
            button.setText(words[value])
            button.setCheckable(True)
            button.setToolTip(tips[value])
            button.setAccessibleName(f"{title}: {words[value]}")
            button.setStyleSheet("QToolButton {padding: 2px 6px;}")
            group.addButton(button, index)
            row.addWidget(button)
            buttons[value] = button
        return row, group, buttons

    # ---- redraw ----------------------------------------------------------------------------------
    def refresh(self, *_args) -> None:
        self._timer.start(0)

    def flush_redraw(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._redraw()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self.vm.set_active(True)
        self.refresh()
        self._sync_ring()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self.vm.set_active(False)
        self._sync_ring()

    def _redraw(self) -> None:
        if not self.vm.active:
            return
        started = perf_counter()
        state = self.vm.recompute()
        if state is self._applied:
            return
        self.apply(state)
        if state.timing is not None:
            self.last_redraw = replace(state.timing, render_s=perf_counter() - started - state.timing.total_s,
                                       total_s=perf_counter() - started)
            _log.debug("Analyzer array redraw: read %.3f s, build %.3f s, apply %.3f s, total %.3f s",
                       self.last_redraw.read_s, self.last_redraw.build_s, self.last_redraw.render_s,
                       self.last_redraw.total_s)
        self._applied = state

    def _sync_ring(self) -> None:
        """Run the ring timer exactly while a visible cell is fitting; a stopped ring stays painted, still."""
        want = self.isVisible() and any(c.ringing for c in self.cells.values())
        if want and not self.ring_timer.isActive():
            self.ring_timer.start()
        elif not want and self.ring_timer.isActive():
            self.ring_timer.stop()

    def _ring_tick(self) -> None:
        self._ring_t = (self._ring_t + 1) % RING_PERIOD_TICKS
        x = self._ring_t / RING_PERIOD_TICKS
        phase = 1.0 - abs(2.0 * x - 1.0)          # a triangle wave, 0 to 1 and back
        for cell in self.cells.values():
            if cell.ringing:
                cell.set_ring_phase(phase)

    def apply(self, state: ArrayState) -> None:
        self._header(state)
        self.body.setCurrentIndex(1 if state.body == "grid" else 0)
        self.message.setText(state.message)
        if state.body != "grid":
            for cell in self.cells.values():       # no grid shown: nothing may keep ringing
                cell.ringing = False
            self._sync_ring()
            return
        self._ensure_cells(state)
        for cell_state in state.cells:
            self.cells[cell_state.beamline].apply(cell_state)
        self.x_axis.set_full_text(state.x_label, state.x_tip)
        self.y_axis.set_full_text(state.y_label, state.y_tip)
        self._sync_ring()

    def _header(self, state: ArrayState) -> None:
        for label, text, tip in ((self.title_label, state.title, state.title_tip),
                                 (self.note_label, state.selection_note,
                                  "The grid shows the scan of the spectrum you picked first."),
                                 (self.layout_label, state.layout_line,
                                  "The analyzer layout this scan is drawn on, and how it was chosen."),
                                 (self.unplaced_label, state.unplaced_line, state.unplaced_tip),
                                 (self.batch_label, state.batch_line, state.batch_line_tip),
                                 (self.caption_label, state.caption if state.body == "grid" else "",
                                  "Every cell shares the energy axis; the y axis is linear.")):
            label.set_full_text(text)
            if text:
                label.setToolTip(f"{text}\n{tip}" if tip else text)
            label.setVisible(bool(text))
        for buttons, current in ((self.scale_buttons, state.scale), (self.mode_buttons, state.y_mode)):
            for value, button in buttons.items():
                with QSignalBlocker(button):
                    button.setChecked(value == current)
        box = self.limit_box
        box.setEnabled(state.limit_enabled)
        box.setPlaceholderText(state.limit_placeholder)
        if not box.hasFocus():                 # never rewrite a limit the user is typing
            box.setText(state.limit_text)
        reason = state.limit_reason or ""
        box.setToolTip(LIMIT_TIP + (f"\n{reason}" if reason else ""))
        self.limit_unit.set_full_text(state.limit_unit)
        self.limit_note.set_full_text(reason if state.limit_enabled else "")
        self.limit_note.setVisible(bool(reason) and state.limit_enabled)

    def _limit_typed(self) -> None:
        text = self.limit_box.text()
        if not self.vm.set_limit(self.vm.scale, text):
            self.limit_box.setText(self._applied.limit_text if self._applied is not None else "")

    def _ensure_cells(self, state: ArrayState) -> None:
        key = (state.n_rows, state.n_cols, tuple((c.beamline, c.internal, c.screen_row, c.screen_col)
                                                 for c in state.cells))
        if key == self._layout_key:
            return
        for cell in self.cells.values():
            self.grid.removeWidget(cell)
            cell.setParent(None)
            cell.deleteLater()
        self.cells = {}
        self.grid.removeWidget(self.x_axis)
        self.grid.removeWidget(self.y_axis)
        for c in state.cells:
            cell = AnalyzerCell(c.beamline, c.internal, c.screen_row == state.n_rows - 1, self.grid_host)
            cell.clicked.connect(self.vm.click_cell)
            self.grid.addWidget(cell, c.screen_row, c.screen_col + 1)
            self.cells[c.beamline] = cell
        self.grid.addWidget(self.y_axis, 0, 0, state.n_rows, 1)
        self.grid.addWidget(self.x_axis, state.n_rows, 1, 1, state.n_cols)
        for row in range(state.n_rows):
            self.grid.setRowStretch(row, 1)
        for col in range(1, state.n_cols + 1):
            self.grid.setColumnStretch(col, 1)
        self._layout_key = key
        self._applied = None

    def cell_at(self, beamline: str) -> AnalyzerCell:
        return self.cells[beamline]

    def grid_minimum(self) -> tuple[int, int]:
        """The least size of the grid itself: the cells never shrink below ``CELL_MIN``; a smaller dock scrolls."""
        return (self.grid.columnCount() - 1) * CELL_MIN[0], (self.grid.rowCount() - 1) * CELL_MIN[1]

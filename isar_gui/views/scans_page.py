"""The shared-selection Scan(s) page, with display controls and honest missing-data states."""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from time import perf_counter

from PySide6.QtCore import QSize, Qt, Signal, QSignalBlocker, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QButtonGroup,
    QDoubleSpinBox, QHBoxLayout, QLabel, QStackedWidget, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget)
import pyqtgraph as pg

from isar_gui.plotspec_pg import clear_render, disable_view_drops, render_into
from isar_gui.viewmodels.scans_page import (
    EMPTY, PLOT, LIST, LAYOUTS, PASSES, SCALES, PageState, RedrawTiming, ScansPageViewModel)
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.group_chips import GroupChips
from isar_gui.views.inter_fit_bar import InterFitBar

EMPTY_TEXT = 'No spectra selected. Select them in the scan list.'
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DrawnCurve:
    key: str
    role: str
    name: str
    style: str


@dataclass(frozen=True)
class LegendLine:
    entry: str
    text: str
    bold: bool


@dataclass(frozen=True)
class Drawn:
    body: int
    keys: tuple[str, ...]
    not_drawn: tuple[tuple[str, str], ...]
    not_drawn_line: str
    layout: str | None
    fit: str
    scale: str
    ylabel: str
    curves_header: str | None
    legend: tuple[LegendLine, ...]
    curves: tuple[DrawnCurve, ...]
    view_range: tuple[tuple[float, float], tuple[float, float]]


class _OffsetBox(QDoubleSpinBox):
    def wheelEvent(self, event):  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ScansPage(QWidget):
    store_dirty = Signal()

    def __init__(self, scan_list_vm, ws, parent=None):
        super().__init__(parent)
        self.scan_list_vm = scan_list_vm
        self.ws = ws
        self._store = None
        self.vm = ScansPageViewModel(ws, scan_list_vm, self)
        self.last_redraw: RedrawTiming | None = None
        self._drawn_state = PageState()
        self._rendered_state = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._redraw)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        self.fit_mount = self._mount('scans_page_fit', header)
        self.group_mount = self._mount('scans_page_groups', self)
        header_layout.addWidget(self.fit_mount)
        header_layout.addStretch(1)
        outer.addWidget(header)
        outer.addWidget(self.group_mount)
        # The viewed fit of the open spectrum; hidden until the window gives the page its inter-spectra state.
        self.inter_mount = self._mount('scans_page_inter', self)
        self.inter_bar = InterFitBar(self.inter_mount)
        self.inter_mount.layout().addWidget(self.inter_bar, 1)
        self.inter_mount.hide()
        outer.addWidget(self.inter_mount)
        self.inter_bar.fit_chosen.connect(self.vm.choose_fit)
        self.inter_bar.make_authoritative_requested.connect(self.vm.make_authoritative)
        self.inter_bar.lock_requested.connect(self.vm.toggle_lock)
        self.inter_bar.waterfall_toggled.connect(self.vm.set_waterfall)
        self.fit_mount.layout().addWidget(QLabel('Fit', self.fit_mount))
        self.fit_group, self.fit_buttons = self._switch(self.fit_mount, PASSES, 'Fit')
        self.fit_group.idClicked.connect(self._fit_clicked)
        self.groups = GroupChips(scan_list_vm, self.group_mount)
        self.group_mount.layout().addWidget(self.groups, 1)
        self.groups.store_dirty.connect(self._on_store_dirty)

        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        self.layout_mount = self._mount('scans_page_layout', controls)
        self.scale_mount = self._mount('scans_page_scale', controls)
        controls_layout.addWidget(self.layout_mount)
        controls_layout.addWidget(self.scale_mount)
        controls_layout.addStretch(1)
        outer.addWidget(controls)
        self.layout_group, self.layout_buttons = self._switch(self.layout_mount, LAYOUTS, 'Layout')
        self.layout_group.idClicked.connect(self._layout_clicked)
        self.scale_group, self.scale_buttons = self._switch(self.scale_mount, SCALES, 'Intensity scale')
        self.scale_group.idClicked.connect(self._scale_clicked)

        # A separate offset row preserves the 300 px centre-column agreement.
        self.offset_mount = self._mount('scans_page_offset', self)
        self.offset_mount.layout().addWidget(QLabel('Offset', self.offset_mount))
        self.offset_box = _OffsetBox(self.offset_mount)
        self.offset_box.setObjectName('scansOffset')
        self.offset_box.setAccessibleName('Trace offset in the current intensity unit')
        self.offset_box.setRange(0, 1e12)
        self.offset_box.setDecimals(3)
        self.offset_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.offset_box.setFixedWidth(90)
        self.offset_box.valueChanged.connect(self._offset_changed)
        self.offset_mount.layout().addWidget(self.offset_box)
        self.auto_button = QToolButton(self.offset_mount)
        self.auto_button.setText('Auto')
        self.auto_button.setCheckable(True)
        self.auto_button.setAccessibleName('Automatic trace offset')
        self.auto_button.setToolTip('Space traces automatically using their data range')
        self.auto_button.clicked.connect(self._auto_clicked)
        self.offset_mount.layout().addWidget(self.auto_button)
        self.offset_mount.layout().addStretch(1)
        outer.addWidget(self.offset_mount)

        self.hint = ElidedLabel('', self, Qt.TextElideMode.ElideRight)
        self.hint.setObjectName('scansPageControlHint')
        outer.addWidget(self.hint)
        self.not_drawn = ElidedLabel('', self, Qt.TextElideMode.ElideRight)
        self.not_drawn.setObjectName('scansPageNotDrawn')
        self.not_drawn.hide()
        outer.addWidget(self.not_drawn)
        self.body = QStackedWidget(self)
        self.body.setObjectName('scansPageBody')
        self.empty_label = QLabel(EMPTY_TEXT, self.body)
        self.empty_label.setObjectName('scansPageEmptyText')
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.body.addWidget(self.empty_label)

        self.plot_area = QWidget(self.body)
        plot_layout = QVBoxLayout(self.plot_area)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        self._glw = pg.GraphicsLayoutWidget(self.plot_area)
        self._glw.setBackground('white')
        self._glw.setObjectName('scansPageGraphics')
        disable_view_drops(self._glw)
        self._main_plot = self._glw.addPlot(row=0, col=0)
        self._resid_plot = self._glw.addPlot(row=1, col=0)
        self._resid_plot.setXLink(self._main_plot)
        for plot in (self._main_plot, self._resid_plot):
            for name in ('left', 'bottom'):
                axis = plot.getAxis(name)
                axis.setPen('#404040')
                axis.setTextPen('#404040')
                axis.setLabel(color='#404040')
        self._glw.ci.layout.setRowStretchFactor(0, 3)
        self._glw.ci.layout.setRowStretchFactor(1, 0)
        self._glw.ci.removeItem(self._resid_plot)
        self._residual_mounted = False
        self._resid_plot.hide()
        plot_layout.addWidget(self._glw)
        self.body.addWidget(self.plot_area)
        self._layout_actions = {}
        self._layout_action_group = QActionGroup(self)
        self._layout_action_group.setExclusive(True)
        menu = self._main_plot.getViewBox().menu
        menu.addSection('Layout')
        for value in LAYOUTS:
            action = QAction(value.title(), self)
            action.setCheckable(True)
            action.setData(value)
            self._layout_action_group.addAction(action)
            menu.addAction(action)
            self._layout_actions[value] = action
        self._layout_action_group.triggered.connect(self._layout_action)

        self.list_area = QWidget(self.body)
        list_layout = QVBoxLayout(self.list_area)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.limit_note = QLabel(self.list_area)
        self.limit_note.setWordWrap(True)
        list_layout.addWidget(self.limit_note)
        caption = QLabel('Selection (read only; change it in the scan list)', self.list_area)
        caption.setWordWrap(True)
        list_layout.addWidget(caption)
        self.limit_tree = QTreeWidget(self.list_area)
        self.limit_tree.setHeaderHidden(True)
        self.limit_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.limit_tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.limit_tree.setAccessibleName('Selected spectra, read only')
        list_layout.addWidget(self.limit_tree, 1)
        self.body.addWidget(self.list_area)
        self.body.setCurrentIndex(EMPTY)
        outer.addWidget(self.body, 1)
        self.selection.changed.connect(self._on_selection_changed)
        self.scan_list_vm.snapshot_changed.connect(self.refresh)
        self.vm.refreshed.connect(self.refresh)
        self.vm.store_dirty.connect(self._on_store_dirty)
        self.refresh()

    @staticmethod
    def _mount(name, parent):
        mount = QWidget(parent)
        mount.setObjectName(name)
        layout = QHBoxLayout(mount)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        return mount

    def _switch(self, mount, values, name):
        group = QButtonGroup(self)
        group.setExclusive(True)
        buttons = {}
        for index, value in enumerate(values):
            button = QToolButton(mount)
            button.setText(value.title())
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setAccessibleName(f'{name}: {value.title()}')
            button.setStyleSheet('QToolButton {padding: 2px 4px;}')
            group.addButton(button, index)
            mount.layout().addWidget(button)
            buttons[value] = button
        return group, buttons

    @property
    def selection(self):
        return self.scan_list_vm.selection

    @property
    def store(self):
        return self._store

    def set_store(self, store):
        self._store = store
        self.groups.set_store(store)
        self.vm.set_store(store)

    def set_inter(self, inter):
        self.vm.set_inter(inter)

    def refresh(self):
        self._timer.start(0)

    def flush_redraw(self):
        if self._timer.isActive():
            self._timer.stop()
            self._redraw()

    def load_view_state(self, session):
        self.vm.load_view_state(session)

    def save_view_state(self, session):
        self.vm.save_view_state(session)

    def _on_selection_changed(self, change):
        self.refresh()

    def _on_store_dirty(self):
        self.store_dirty.emit()

    def _fit_clicked(self, index):
        self.vm.set_fit(PASSES[index])

    def _layout_clicked(self, index):
        self.vm.set_layout(LAYOUTS[index])

    def _scale_clicked(self, index):
        self.vm.set_scale(SCALES[index])

    def _layout_action(self, action):
        self.vm.set_layout(action.data())

    def _offset_changed(self, value):
        self.vm.set_step(value)

    def _auto_clicked(self, checked):
        self.vm.set_step(None if checked else self.offset_box.value())

    def _controls(self, state):
        for buttons, current, control, tip in (
            (self.fit_buttons, self.vm.fit, state.fit,
             'Fit curves on every Scan(s) plot; one setting for all of ISAR'),
            (self.layout_buttons, self.vm.layout, state.layout, 'Layout of the current plot'),
            (self.scale_buttons, self.vm.scale, None, 'Intensity scale of every Scan(s) plot')):
            for value, button in buttons.items():
                with QSignalBlocker(button):
                    button.setChecked(value == current)
                button.setEnabled(control is None or control.enabled)
                suffix = f'. {control.reason}' if control and control.reason else ''
                button.setToolTip(tip + suffix)
        self.fit_buttons['deep'].setToolTip(self.fit_buttons['deep'].toolTip() + '. ' + state.deep_tooltip)
        self.offset_box.setEnabled(state.offset.enabled)
        self.auto_button.setEnabled(state.offset.enabled)
        with QSignalBlocker(self.offset_box), QSignalBlocker(self.auto_button):
            self.offset_box.setValue(state.step or 0)
            self.auto_button.setChecked(state.step_auto)
        self.offset_box.setToolTip('Trace spacing in the current intensity unit' +
                                  (f'; {state.offset.reason}' if state.offset.reason else ''))
        for value, action in self._layout_actions.items():
            action.setChecked(value == self.vm.layout)
            action.setEnabled(state.layout.enabled)
            action.setText(value.title() + (f' ({state.layout.reason})' if state.layout.reason else ''))
        hints = [f'{title}: {control.reason}' for title, control in
                 (('Fit', state.fit), ('Offset | Overlay', state.layout), ('Offset box', state.offset))
                 if not control.enabled and control.reason]
        self.hint.set_full_text(' · '.join(hints))
        self.hint.setVisible(bool(hints) and state.body == PLOT)
        self.inter_mount.setVisible(state.inter is not None)
        if state.inter is not None:
            self.inter_bar.show_state(state.inter)

    def _redraw(self):
        started = perf_counter()
        state = self.vm.recompute()
        if state is self._rendered_state:
            return
        self._controls(state)
        self.body.setCurrentIndex(state.body)
        self.not_drawn.set_full_text(state.not_drawn_text)
        self.not_drawn.setToolTip(state.not_drawn_text + '\n' + state.not_drawn_tip)
        self.not_drawn.setVisible(bool(state.not_drawn_text))
        self.limit_note.setText(state.over_limit_note)
        self.limit_tree.clear()
        for group in state.over_limit:
            item = QTreeWidgetItem(self.limit_tree, [group.title])
            item.setToolTip(0, group.title)
            for name in group.names:
                QTreeWidgetItem(item, [name])
        render_started = perf_counter()
        show_residual = state.residual_spec is not None
        if show_residual != self._residual_mounted:
            if show_residual:
                self._glw.ci.addItem(self._resid_plot, row=1, col=0)
            else:
                self._glw.ci.removeItem(self._resid_plot)
            self._glw.ci.layout.setRowStretchFactor(1, int(show_residual))
            self._residual_mounted = show_residual
        for plot, spec in ((self._main_plot, state.main_spec), (self._resid_plot, state.residual_spec)):
            if spec is None:
                clear_render(plot)
            else:
                render_into(plot, spec)
        self._resid_plot.setVisible(state.residual_spec is not None)
        if state.reset_range:
            self._main_plot.enableAutoRange()
            self._resid_plot.enableAutoRange()
        timing = state.timing or RedrawTiming(len(self.selection.keys), 0, 0, 0, 0)
        self.last_redraw = replace(timing, render_s=perf_counter() - render_started,
                                   total_s=perf_counter() - started)
        self._rendered_state = state
        self._drawn_state = state
        _log.debug('Scan(s) redraw: %d spectra, read %.3f s, build %.3f s, render %.3f s, total %.3f s',
                   self.last_redraw.n, self.last_redraw.read_s, self.last_redraw.build_s,
                   self.last_redraw.render_s, self.last_redraw.total_s)

    def drawn(self):
        state = self._drawn_state
        legend, curves = [], []
        if state.main_spec is not None:
            for key, trace in zip(state.keys, state.traces):
                legend.append(LegendLine(key, trace.identity + (f' · {trace.note}' if trace.note else ''), trace.active))
            rank = {'total': 0, 'elastic': 1, 'quasielastic': 2, 'mode': 3, 'background': 4}
            entries = [(key, trace, curve) for key, trace in zip(state.keys, state.traces) for curve in trace.curves]
            entries.sort(key=lambda item: (rank[item[2].kind], item[2].number or 0))
            names = set()
            for key, trace, curve in entries:
                style = 'dotted' if curve.kind == 'background' else 'dashed' if trace.deep else 'solid'
                curves.append(DrawnCurve(key, curve.kind, curve.name, style))
                if curve.kind != 'background' and curve.name not in names:
                    legend.append(LegendLine(curve.name, curve.name, False))
                    names.add(curve.name)
        return Drawn(state.body, state.keys, state.not_drawn, state.not_drawn_text, state.view,
                     state.fit_value, state.scale_value, state.main_spec.ylabel if state.main_spec else '',
                     state.curves_header, tuple(legend), tuple(curves),
                     tuple(tuple(axis) for axis in self._main_plot.getViewBox().viewRange()))

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, max(240, super().minimumSizeHint().height()))

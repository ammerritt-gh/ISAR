"""Point selection against the exact displayed traces, including waterfall offsets."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Signal, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from isar.intake.point_masks import save_selection
from isar_gui.selection import identity
from isar_gui.views.edge_indicators import EdgeIndicators
from isar_gui.views.masked_points import MaskedPoints


class ScanMaskControls(QWidget):
    saved = Signal()

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.vb = page._main_plot.getViewBox()
        self.points = []
        self.rows = []
        self.unit = ''
        self.dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        row = QHBoxLayout()
        self.mode = self._button(row, 'Mask points',
            'Click a point or drag an E/I rectangle across any displayed spectra. Mixed selection: mask all; all masked: restore all. Escape exits masking.')
        self.mode.setCheckable(True)
        self.mode.toggled.connect(self.vb.set_mask_mode)
        self.vb.mode_changed.connect(self.mode.setChecked)
        self.list_button = self._button(row, 'Masked (0)', 'List masked points by energy and intensity, and restore them.')
        self.list_button.clicked.connect(self.show_list)
        self.all_button = self._button(row, 'Show all', 'Reveal every displayed point, including masked points outside the current axes.')
        self.all_button.clicked.connect(self.show_all)
        self.auto_button = self._button(row, 'Auto range', 'Size the axes using unmasked data only.')
        self.auto_button.clicked.connect(self.auto_range)
        row.addStretch(1)
        layout.addLayout(row)
        self.message = QLabel(self)
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        self.preview = pg.ScatterPlotItem(size=11, pen=pg.mkPen('#1864ab', width=2), brush=None)
        self.preview.setZValue(90)
        self.vb.addItem(self.preview, ignoreBounds=True)
        # The renderer's curves, annotations and masked markers do not enter autosizing.
        self.bounds = pg.PlotDataItem(pen=pg.mkPen(0, 0, 0, 0))
        page._main_plot.addItem(self.bounds)
        self.edges = EdgeIndicators(self.vb)
        self.vb.box_preview.connect(self.preview_box)
        self.vb.box_selected.connect(self.select_box)
        # ScatterPlotItem accepts clicks itself. The scene signal reaches us for clicks on
        # both ordinary and masked symbols, as well as in the surrounding hit tolerance.
        page._glw.scene().sigMouseClicked.connect(self._scene_clicked)

    def _button(self, row, title, tip):
        button = QToolButton(self)
        button.setText(title)
        button.setToolTip(tip)
        row.addWidget(button)
        return button

    def apply(self, state):
        self.vb.cancel_selection()  # a refresh never retargets an unfinished gesture
        self.points, self.rows = [], []
        bounds_x, bounds_y, edge_points = [], [], []
        seen = set()
        for i, (key, trace) in enumerate(zip(state.keys, state.traces)):
            info = identity(self.page.scan_list_vm.snapshot, key)
            label = f'{info.set_label} · {info.analyzer} · {info.t}' if info else key
            offset = (len(state.traces) - 1 - i) * (state.step or 0) if state.view == 'offset' else 0
            x, y, kept = np.asarray(trace.x), np.asarray(trace.y), np.asarray(trace.kept, dtype=bool)
            displayed_y = y + offset
            edge_points.append((x, displayed_y, kept))
            finite = np.isfinite(x) & np.isfinite(y)
            accepted = finite & kept
            err = np.where(np.isfinite(trace.yerr), trace.yerr, 0)
            bounds_x.extend([x[accepted], x[accepted]])
            bounds_y.extend([(displayed_y - err)[accepted], (displayed_y + err)[accepted]])
            manual = trace.manual_mask if trace.manual_mask is not None else np.zeros(x.shape, dtype=bool)
            for j, point_id in enumerate(trace.point_ids):
                if not finite[j]:
                    continue
                self.points.append((key, point_id, float(x[j]), float(displayed_y[j]), bool(manual[j])))
                if manual[j] and (key, point_id) not in seen:
                    self.rows.append((key, point_id, label, float(x[j]), float(y[j])))
                    seen.add((key, point_id))
        self.bounds.setData(np.concatenate(bounds_x) if bounds_x else [],
                            np.concatenate(bounds_y) if bounds_y else [])
        self.edges.set_points(edge_points, self.page.vm.out_of_range)
        self.mode.setEnabled(bool(self.points))
        self.all_button.setEnabled(bool(state.traces))
        self.auto_button.setEnabled(bool(state.traces))
        if not self.points:
            self.mode.setChecked(False)
        self.list_button.setText(f'Masked ({len(self.rows)})')
        self.list_button.setEnabled(bool(state.traces))
        self.unit = state.main_spec.ylabel.split(', traces offset')[0] if state.main_spec else ''
        if self.dialog is not None:
            self.dialog.show_points(self.rows, self.unit)

    def _inside(self, rect):
        rect = rect.intersected(self.vb.viewRect())
        if rect.isEmpty():
            return []
        return [p for p in self.points if rect.left() <= p[2] <= rect.right()
                and rect.top() <= p[3] <= rect.bottom()]

    @staticmethod
    def _selection(points):
        out = {}
        for key, point_id, *_ in points:
            out.setdefault(key, set()).add(point_id)
        return {k: sorted(v) for k, v in out.items()}

    def preview_box(self, rect):
        selected = [] if rect is None else self._inside(rect)
        self.preview.setData([p[2] for p in selected], [p[3] for p in selected])
        if rect is not None:
            count = sum(len(v) for v in self._selection(selected).values())
            action = 'Restore' if selected and all(p[4] for p in selected) else 'Mask'
            self.message.setText(f'{action} {count} points in the rectangle (all displayed spectra).')
            self.message.show()

    def select_box(self, rect):
        self.save(self._selection(self._inside(rect)))

    def select_point(self, scene_point):
        candidates = []
        for point in self.points:
            if not self.vb.viewRect().contains(QPointF(point[2], point[3])):
                continue
            mapped = self.vb.mapViewToScene(QPointF(point[2], point[3]))
            distance = (mapped.x() - scene_point.x()) ** 2 + (mapped.y() - scene_point.y()) ** 2
            if distance <= 8 ** 2:
                candidates.append((distance, point))
        if candidates:
            nearest = min(d for d, _ in candidates)
            # Exactly overlapping points are indistinguishable on screen: select them together.
            self.save(self._selection([p for d, p in candidates if abs(d - nearest) < 0.01]))

    def _scene_clicked(self, event):
        if not self.vb.mask_mode or event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.scenePos()
        if not self.vb.sceneBoundingRect().contains(position):
            return
        rendered = getattr(self.page._main_plot, '_isar_rendered', None)
        legend = rendered.legend if rendered else None
        if legend is not None and legend.sceneBoundingRect().contains(position):
            return
        self.select_point(position)

    def save(self, selection, *, masked=None):
        if not selection:
            return
        result = save_selection(self.page.ws.root, selection, masked=masked)
        if not result.ok:
            self.message.setText(result.reason)
            self.message.show()
            return
        count = sum(len(v) for v in selection.values())
        self.message.setText(f'{"Masked" if result.value else "Restored"} {count} points. Existing fits keep their original inputs; changed fits are stale.')
        self.message.show()
        self.page.ws.refresh()
        self.page.scan_list_vm.on_workspace_changed({'intake_entries'})
        self.saved.emit()

    def show_list(self):
        if self.dialog is None:
            self.dialog = MaskedPoints(self)
            self.dialog.restore_requested.connect(lambda points: self.save(points, masked=False))
        self.dialog.show_points(self.rows, self.unit)
        self.dialog.show()

    def show_all(self):
        x = np.concatenate([np.asarray(t.x) for t in self.page._drawn_state.traces])
        y = np.concatenate([p[1] for p in self.edges.points])
        finite = np.isfinite(x) & np.isfinite(y)
        if finite.any():
            self.vb.setRange(xRange=(float(x[finite].min()), float(x[finite].max())),
                             yRange=(float(y[finite].min()), float(y[finite].max())), padding=0.05)

    def auto_range(self):
        self.vb.enableAutoRange()
        self.vb.autoRange()

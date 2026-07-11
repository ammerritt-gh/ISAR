"""WaterfallDock — the C4 waterfall / comparison view (Task 13, A-6).

Purely presentational: the whole view (per-T waterfall stack, or the per-T dispersion comparison
overlay) is a :class:`~isar.plotspec.spec.PlotSpec` computed by
:class:`~isar_gui.viewmodels.waterfall.WaterfallViewModel`; this dock only renders it through
:func:`isar_gui.plotspec_pg.render_into`, offers a mode toggle, a q selector (waterfall + comparison
share the same q axis but only waterfall fixes a single q — the selector still narrows which q's
family of scans populate the "modal q" default), and an offset spinbox (waterfall mode only, since
the offset is meaningless in comparison mode). No geometry, no workspace access — the dock is dumb,
same shape as the heatmap/dispersion docks.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import render_into


class WaterfallDock(QDockWidget):
    """Waterfall/comparison over a :class:`WaterfallViewModel`: mode toggle, q selector, offset
    spinbox (waterfall mode only), and click -> ``VM.activate_at``."""

    def __init__(self, vm, parent=None):
        super().__init__("Waterfall / Comparison", parent)
        self._vm = vm

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(4, 4, 4, 4)

        # --- controls row ----------------------------------------------------
        controls = QWidget()
        c_l = QHBoxLayout(controls)
        c_l.setContentsMargins(0, 0, 0, 0)

        self._waterfall_radio = QRadioButton("Waterfall")
        self._comparison_radio = QRadioButton("Comparison")
        self._waterfall_radio.setChecked(True)
        self._waterfall_radio.toggled.connect(self._on_mode_toggled)
        c_l.addWidget(self._waterfall_radio)
        c_l.addWidget(self._comparison_radio)

        c_l.addWidget(QLabel("q:"))
        self._q_combo = QComboBox()
        self._q_combo.currentIndexChanged.connect(self._on_q_changed)
        c_l.addWidget(self._q_combo)

        self._offset_label = QLabel("offset:")
        c_l.addWidget(self._offset_label)
        self._offset_spin = QDoubleSpinBox()
        self._offset_spin.setRange(0.0, 1.0e9)
        self._offset_spin.setDecimals(3)
        self._offset_spin.setSingleStep(1.0)
        self._offset_spin.valueChanged.connect(self._on_offset_changed)
        c_l.addWidget(self._offset_spin)
        c_l.addStretch(1)
        outer.addWidget(controls)

        # --- plot -------------------------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        self._plot = self._glw.addPlot(row=0, col=0)
        outer.addWidget(self._glw)

        self.setWidget(body)

        self._plot.scene().sigMouseClicked.connect(self._on_click)
        vm.spec_changed.connect(self.refresh)
        self.refresh()

    # ---- rendering ------------------------------------------------------------
    def refresh(self) -> None:
        spec = self._vm.spec
        self._sync_controls()
        if spec is not None:
            render_into(self._plot, spec)

    def _sync_controls(self) -> None:
        is_waterfall = self._vm.mode == "waterfall"
        self._waterfall_radio.blockSignals(True)
        self._comparison_radio.blockSignals(True)
        self._waterfall_radio.setChecked(is_waterfall)
        self._comparison_radio.setChecked(not is_waterfall)
        self._waterfall_radio.blockSignals(False)
        self._comparison_radio.blockSignals(False)

        self._offset_label.setVisible(is_waterfall)
        self._offset_spin.setVisible(is_waterfall)
        self._offset_spin.blockSignals(True)
        self._offset_spin.setValue(self._vm.offset_step)
        self._offset_spin.blockSignals(False)

        qs = self._vm.q_values
        current = self._vm.selected_q
        self._q_combo.blockSignals(True)
        self._q_combo.clear()
        for q in qs:
            self._q_combo.addItem(f"{q:g}", q)
        if current is not None and current in qs:
            self._q_combo.setCurrentIndex(qs.index(current))
        self._q_combo.blockSignals(False)

    # ---- interaction ------------------------------------------------------------
    def _on_mode_toggled(self, checked: bool) -> None:
        if checked:
            self._vm.set_mode("waterfall")
        else:
            self._vm.set_mode("comparison")

    def _on_q_changed(self, idx: int) -> None:
        if idx < 0:
            return
        q = self._q_combo.itemData(idx)
        if q is not None:
            self._vm.set_selected_q(q)

    def _on_offset_changed(self, value: float) -> None:
        self._vm.set_offset_step(value)

    def _on_click(self, event) -> None:
        vb = self._plot.getViewBox()
        if vb is None:
            return
        pt = vb.mapSceneToView(event.scenePos())
        self._vm.activate_at(pt.x(), pt.y())

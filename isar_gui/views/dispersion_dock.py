"""DispersionDock — the C5 ω̂(q) dispersion view (Task 12, A-7).

Purely presentational: the whole view (ω̂ scatter + certifying whiskers + entropy colour + verdict
glyphs + prior/branch curves + off-curve flags + the always-on projection legend) is a
:class:`~isar.plotspec.spec.PlotSpec` computed by
:class:`~isar_gui.viewmodels.dispersion.DispersionViewModel`; this dock only renders it through
:func:`isar_gui.plotspec_pg.render_into`, offers a temperature selector when more than one T is
present, and forwards a plot click to the VM's nearest-ω̂ hit-test (which emits ``point_activated``).
No geometry, no workspace access — the dock is dumb, the heatmap dock's 1-D sibling.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into


class DispersionDock(QDockWidget):
    """ω̂(q) over a :class:`DispersionViewModel`: scatter + overlays + projection legend, a T
    selector, and click -> ``VM.activate_at``."""

    def __init__(self, vm, parent=None):
        super().__init__("Dispersion ω(q)", parent)
        self.setObjectName("dispersion_dock")  # stable name for saveState()/restoreState()
        self._vm = vm

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(4, 4, 4, 4)

        # --- temperature selector (only meaningful with >1 T) ---------------
        self._t_row = QWidget()
        t_l = QHBoxLayout(self._t_row)
        t_l.setContentsMargins(0, 0, 0, 0)
        t_l.addWidget(QLabel("T (K):"))
        self._t_combo = QComboBox()
        self._t_combo.currentIndexChanged.connect(self._on_t_changed)
        t_l.addWidget(self._t_combo)
        t_l.addStretch(1)
        outer.addWidget(self._t_row)

        # --- plot -----------------------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        self._plot = self._glw.addPlot(row=0, col=0)
        outer.addWidget(self._glw)

        self.setWidget(body)

        self._plot.scene().sigMouseClicked.connect(self._on_click)
        vm.spec_changed.connect(self.refresh)
        self.refresh()

    # ---- rendering --------------------------------------------------------
    def refresh(self) -> None:
        spec = self._vm.spec
        self._sync_t_combo()
        if spec is not None:
            render_into(self._plot, spec)
        else:
            clear_render(self._plot)  # no spec -> don't retain the previous (stale) render

    def _sync_t_combo(self) -> None:
        temps = self._vm.temperatures
        self._t_row.setVisible(len(temps) > 1)
        current = self._vm.temperature
        self._t_combo.blockSignals(True)
        self._t_combo.clear()
        for t in temps:
            self._t_combo.addItem(f"{t:g}", t)
        if current is not None and current in temps:
            self._t_combo.setCurrentIndex(temps.index(current))
        self._t_combo.blockSignals(False)

    # ---- interaction ------------------------------------------------------
    def _on_t_changed(self, idx: int) -> None:
        if idx < 0:
            return
        t = self._t_combo.itemData(idx)
        if t is not None:
            self._vm.set_temperature(t)

    def _on_click(self, event) -> None:
        vb = self._plot.getViewBox()
        if vb is None:
            return
        pt = vb.mapSceneToView(event.scenePos())
        self._vm.activate_at(pt.x(), pt.y())

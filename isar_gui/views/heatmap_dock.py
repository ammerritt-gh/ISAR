"""HeatmapDock — the C3 growing (q, E) plane view (Task 11, A-5).

Purely presentational: the whole plane (measured strips + honesty overlays + the always-on
projection legend) is a :class:`~isar.plotspec.spec.PlotSpec` computed by
:class:`~isar_gui.viewmodels.heatmap.HeatmapViewModel`; this dock only renders it through
:func:`isar_gui.plotspec_pg.render_into`, offers a temperature selector when more than one T is
present, shows a minimal colorbar labelled from ``spec.value_label``, and forwards a plot click to
the VM's nearest-ω̂ hit-test (which emits ``point_activated``). No geometry, no workspace access —
the dock is dumb.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into


class HeatmapDock(QDockWidget):
    """(q, E) plane over a :class:`HeatmapViewModel`: strips + overlays + projection legend, a T
    selector, a colorbar, and click -> ``VM.activate_at``."""

    def __init__(self, vm, parent=None):
        super().__init__("Plane (q, E)", parent)
        self.setObjectName("heatmap_dock")  # stable name for saveState()/restoreState()
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

        # --- plot + colorbar -----------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        self._plot = self._glw.addPlot(row=0, col=0)
        self._colorbar = pg.HistogramLUTItem()
        self._colorbar.setImageItem(pg.ImageItem())  # placeholder until a strip drives it
        self._glw.addItem(self._colorbar, row=0, col=1)
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
            self._sync_colorbar(spec)
        else:
            clear_render(self._plot)  # no spec -> don't retain the previous (stale) render
            # …and detach the colorbar from the now-removed ImageItem (a fresh placeholder).
            self._colorbar.setImageItem(pg.ImageItem())

    def _sync_colorbar(self, spec) -> None:
        label = spec.value_label or "counts"
        self._colorbar.axis.setLabel(label)
        # Point the colorbar at the first strip's ImageItem so its levels/LUT match the plane.
        rendered = getattr(self._plot, "_isar_rendered", None)
        img = None
        if rendered is not None:
            img = next((it for it in rendered.items if isinstance(it, pg.ImageItem)), None)
        if img is not None:
            self._colorbar.setImageItem(img)
        else:
            # No strip in this spec -> the previous strip's ImageItem was just removed; don't leave
            # the colorbar pointing at a detached item. Reset to a fresh placeholder.
            self._colorbar.setImageItem(pg.ImageItem())

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

"""HeatmapPane — the C3 growing (q, E) plane view (Task 11, A-5).

Purely presentational: the whole plane (measured strips + honesty overlays + the always-on
projection legend) is a :class:`~isar.plotspec.spec.PlotSpec` computed by
:class:`~isar_gui.viewmodels.heatmap.HeatmapViewModel`; this pane only renders it through
:func:`isar_gui.plotspec_pg.render_into`, offers a temperature selector when more than one T is
present, shows a minimal colorbar labelled from ``spec.value_label``, and forwards a plot click to
the VM's nearest-ω̂ hit-test (which emits ``point_activated``). No geometry, no workspace access —
the pane is dumb.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into
from isar_gui.views.group_drop import GroupDropMixin


class HeatmapPane(GroupDropMixin, QWidget):
    """(q, E) plane over a :class:`HeatmapViewModel`: strips + overlays + projection legend, a T
    selector, a colorbar, click -> ``VM.activate_at``, and a group-drop restriction (2A, refuses
    multi-T)."""

    def __init__(self, vm, parent=None, entropy_action=None):
        super().__init__(parent)
        self._vm = vm

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        # --- temperature selector (only meaningful with >1 T) ---------------
        self._t_row = QWidget()
        t_l = QHBoxLayout(self._t_row)
        t_l.setContentsMargins(0, 0, 0, 0)
        t_l.addWidget(QLabel("T (K):"))
        self._t_combo = QComboBox()
        self._t_combo.currentIndexChanged.connect(self._on_t_changed)
        t_l.addWidget(self._t_combo)
        if entropy_action is not None:
            entropy_button = QToolButton(self._t_row)
            entropy_button.setDefaultAction(entropy_action)
            entropy_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            t_l.addWidget(entropy_button)
        t_l.addStretch(1)
        outer.addWidget(self._t_row)

        # --- plot + colorbar -----------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        self._plot = self._glw.addPlot(row=0, col=0)
        self._colorbar = pg.HistogramLUTItem()
        self._colorbar_proxy = pg.ImageItem()
        self._colorbar_proxy.setImage(np.asarray([[0.0]], dtype=float))
        self._colorbar.setImageItem(self._colorbar_proxy)
        self._strip_images: list[pg.ImageItem] = []
        self._syncing_colorbar = False
        self._colorbar.sigLevelsChanged.connect(self._apply_colorbar_levels)
        self._colorbar.gradient.sigGradientChanged.connect(self._apply_colorbar_lut)
        self._glw.addItem(self._colorbar, row=0, col=1)
        outer.addWidget(self._glw)

        self._init_group_drop(outer)   # group-drop restriction + Show-all banner (2A)

        self._plot.scene().sigMouseClicked.connect(self._on_click)
        vm.spec_changed.connect(self.refresh)
        self.refresh()

    # ---- rendering --------------------------------------------------------
    def refresh(self) -> None:
        spec = self._vm.spec
        self._sync_t_combo()
        if spec is not None:
            # Drop references to the outgoing render before its items are detached. If rendering
            # the replacement raises, colourbar interaction still cannot reach stale strip items.
            self._strip_images = []
            render_into(self._plot, spec)
            self._sync_colorbar(spec)
        else:
            clear_render(self._plot)  # no spec -> don't retain the previous (stale) render
            self._clear_colorbar_target()

    def _sync_colorbar(self, spec) -> None:
        label = spec.value_label or "counts"
        self._colorbar.axis.setLabel(label)
        rendered = getattr(self._plot, "_isar_rendered", None)
        self._strip_images = list(rendered.strip_images) if rendered is not None else []
        if not self._strip_images:
            self._clear_colorbar_target()
            return
        self._syncing_colorbar = True
        try:
            cmap = pg.colormap.get(rendered.strip_colormap or spec.strip_colormap)
            self._colorbar.gradient.setColorMap(cmap)
            self._colorbar_proxy.setLookupTable(cmap.getLookupTable(nPts=256))
            if rendered.strip_levels is not None:
                self._colorbar_proxy.setLevels(rendered.strip_levels)
                self._colorbar.setLevels(*rendered.strip_levels)
        finally:
            self._syncing_colorbar = False
        self._apply_colorbar_levels()

    def _clear_colorbar_target(self) -> None:
        self._strip_images = []
        self._colorbar_proxy.clear()
        self._colorbar_proxy.setImage(np.asarray([[0.0]], dtype=float))

    def _apply_colorbar_levels(self) -> None:
        if self._syncing_colorbar:
            return
        levels = self._colorbar.getLevels()
        self._syncing_colorbar = True
        try:
            self._colorbar_proxy.setLevels(levels)
            for image in self._strip_images:
                image.setLevels(levels)
        finally:
            self._syncing_colorbar = False

    def _apply_colorbar_lut(self) -> None:
        if self._syncing_colorbar:
            return
        lut = self._colorbar.gradient.getLookupTable(nPts=256)
        self._colorbar_proxy.setLookupTable(lut)
        for image in self._strip_images:
            image.setLookupTable(lut)

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

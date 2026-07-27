"""DispersionPane — the C5 ω̂(q) dispersion view (Task 12, A-7).

Purely presentational: the whole view (ω̂ scatter + certifying whiskers + entropy colour + verdict
glyphs + prior/branch curves + off-curve flags + the always-on projection legend) is a
:class:`~isar.plotspec.spec.PlotSpec` computed by
:class:`~isar_gui.viewmodels.dispersion.DispersionViewModel`; this pane only renders it through
:func:`isar_gui.plotspec_pg.render_into`, offers a temperature selector when more than one T is
present, and forwards a plot click to the VM's nearest-ω̂ hit-test (which emits ``point_activated``).
No geometry, no workspace access — the pane is dumb, the heatmap pane's 1-D sibling.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into
from isar_gui.views.group_drop import GroupDropMixin


class DispersionPane(GroupDropMixin, QWidget):
    """ω̂(q) over a :class:`DispersionViewModel`: scatter + overlays + projection legend, a T
    selector, click -> ``VM.activate_at``, and a group-drop restriction (2A, refuses multi-T)."""

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

        self._status_scroll = QScrollArea(self)
        self._status_scroll.setWidgetResizable(True)
        self._status_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._status_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._status_host = QWidget(self._status_scroll)
        self._status_layout = QHBoxLayout(self._status_host)
        self._status_layout.setContentsMargins(2, 2, 2, 2)
        self._status_scroll.setWidget(self._status_host)
        self._status_scroll.setMaximumHeight(48)
        outer.addWidget(self._status_scroll)

        # --- plot -----------------------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        self._plot = self._glw.addPlot(row=0, col=0)
        # Reciprocal-lattice coordinates are conventionally read as plain decimals.  Pyqtgraph's
        # default SI-prefix formatter turns a K range such as 0..0.5 into milli-scaled tick labels
        # (0..500 with a ×10⁻³-style suffix), which looks like a thousand-fold coordinate error.
        self._plot.getAxis("bottom").enableAutoSIPrefix(False)
        outer.addWidget(self._glw)

        self._init_group_drop(outer)   # group-drop restriction + Show-all banner (2A)

        self._plot.scene().sigMouseClicked.connect(self._on_click)
        vm.spec_changed.connect(self.refresh)
        self.refresh()

    # ---- rendering --------------------------------------------------------
    def refresh(self) -> None:
        spec = self._vm.spec
        self._sync_t_combo()
        self._sync_status_ribbon()
        if spec is not None:
            render_into(self._plot, spec)
        else:
            clear_render(self._plot)  # no spec -> don't retain the previous (stale) render

    def _sync_status_ribbon(self) -> None:
        while self._status_layout.count():
            item = self._status_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        reasons = {
            None: "in progress", "met": "converged", "interval_floored": "interval floored",
            "info_saturated": "information saturated",
            "uncertifiable_interval": "uncertifiable interval", "fast_tier_only": "fast tier only",
            "infeasible": "instrument infeasible", "signal_unmodeled": "signal unmodeled",
            "branch_ambiguity": "branch ambiguity", "posterior_rejected": "posterior rejected",
            "no_signal": "no signal",
        }
        points = self._vm.status_points
        state_colors = {
            "master_target": "#2e7d32",
            "master_ambiguous": "#b8860b",
            "master_mismatch": "#c62828",
            "master_certified": "#1565c0",
            "master_unassessed": "#808080",
        }
        for index, point in enumerate(points):
            reason = point.get("reason")
            q = point.get("q")
            button = QPushButton("?" if q is None else f"{float(q):g}", self._status_host)
            button.setToolTip(reasons.get(reason, str(reason)) + "\n" + str(point))
            color = state_colors.get(
                point.get("display_state"),
                "#c62828" if point.get("stuck") else "#b8860b")
            button.setStyleSheet(f"border-bottom: 4px solid {color}; padding: 2px 5px;")
            button.clicked.connect(lambda _checked=False, i=index: self._vm.activate_status(i))
            self._status_layout.addWidget(button)
        self._status_layout.addStretch(1)
        self._status_scroll.setVisible(bool(points))

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

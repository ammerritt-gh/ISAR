"""CampaignMonitor — the compact, glanceable campaign view (GUI Pass 3, M3-1).

The small-window mode's whole content: what the campaign is doing right now (current scan, last
decision), how far along it is (iteration + budget bar), a miniature plane view that grows as
results land, a three-line log ticker, and cooperative Stop / Expand buttons. It is a read-only
projection over the same CampaignViewModel + HeatmapViewModel the full window uses — no new state,
no new physics, just less of everything (AD-14: same window, saveState swap, read-only).
"""
from __future__ import annotations

import logging

import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into
from isar_gui.viewmodels.campaign import budget_readout

log = logging.getLogger(__name__)

_TICKER_LINES = 3


class CampaignMonitor(QWidget):
    """Glanceable campaign card: status, mini plane, ticker, Stop + Expand."""

    expand_requested = Signal()

    def __init__(self, campaign_vm, heatmap_vm, operations, parent=None):
        super().__init__(parent)
        self._vm = campaign_vm
        self._heatmap_vm = heatmap_vm
        self._operations = operations
        self._ticker: list[str] = []
        self._scan_line = "no scan yet"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        self._status = QLabel("no campaign yet", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._status)

        self._current = QLabel(self._scan_line, self)
        self._current.setWordWrap(True)
        outer.addWidget(self._current)

        self._budget_bar = QProgressBar(self)
        self._budget_bar.setRange(0, 1000)
        self._budget_bar.setTextVisible(True)
        outer.addWidget(self._budget_bar)

        self._glw = pg.GraphicsLayoutWidget(self)
        self._glw.setMinimumHeight(120)
        self._plot = self._glw.addPlot()
        outer.addWidget(self._glw, 1)

        self._ticker_label = QLabel("", self)
        self._ticker_label.setWordWrap(False)
        self._ticker_label.setStyleSheet("color: #616161; font-size: 8pt;")
        # A plain QLabel demands its longest line's width as a minimum — one logged workspace
        # path would pin the whole compact window wide. Ignore the hint; long lines just clip.
        policy = self._ticker_label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self._ticker_label.setSizePolicy(policy)
        outer.addWidget(self._ticker_label)

        buttons = QHBoxLayout()
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.setToolTip("Cooperative: the campaign finishes the current scan, then stops.")
        self._stop_btn.clicked.connect(self._on_stop)
        self._expand_btn = QPushButton("Expand", self)
        self._expand_btn.clicked.connect(self.expand_requested.emit)
        buttons.addWidget(self._stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._expand_btn)
        outer.addLayout(buttons)

        campaign_vm.status_changed.connect(self._sync)
        campaign_vm.feed_appended.connect(self._on_feed)
        heatmap_vm.spec_changed.connect(self._render_plane)
        self._sync()
        self._render_plane()

    # ---- feed-driven "current scan" + ticker ---------------------------------
    def _on_feed(self, items: list) -> None:
        for item in items:
            if item.kind in ("propose", "run"):
                self._scan_line = item.text
        self._current.setText(self._scan_line)

    def append_log_line(self, line: str) -> None:
        """The window feeds LogDock lines here; keep the last few as a ticker."""
        self._ticker.append(str(line))
        del self._ticker[:-_TICKER_LINES]
        self._ticker_label.setText("\n".join(self._ticker))

    # ---- status ---------------------------------------------------------------
    def _sync(self, *_a) -> None:
        state = self._vm.state
        running = self._operations.is_running_campaign()
        if running:
            it = state.iteration
            text = f"campaign running — iteration {it if it is not None else '…'}"
            if self._operations.is_stop_pending():
                text += " — stopping after the current scan…"
        elif state.stop_state:
            text = f"campaign {state.stop_state}"
        elif state.has_campaign:
            text = "campaign idle"
        else:
            text = "no campaign yet"
        if state.converged is not None and state.n_grid:
            text += f"   ({state.converged}/{state.n_grid} converged)"
        self._status.setText(text)
        permille, fmt = budget_readout(state)
        self._budget_bar.setValue(permille)
        self._budget_bar.setFormat(fmt)
        self._stop_btn.setEnabled(running and not self._operations.is_stop_pending())

    def _on_stop(self) -> None:
        self._operations.request_drive_stop()
        self._sync()

    # ---- mini plane -------------------------------------------------------------
    def _render_plane(self) -> None:
        spec = self._heatmap_vm.spec
        if spec is not None:
            render_into(self._plot, spec)
        else:
            clear_render(self._plot)

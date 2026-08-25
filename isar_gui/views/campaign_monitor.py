"""CampaignMonitor — the compact, glanceable campaign view (GUI Pass 3, M3-1).

The small-window mode's whole content: what the campaign is doing right now (current scan, last
decision), how far along it is (iteration + budget bar), a miniature plane view that grows as
results land, a three-line log ticker, and cooperative Stop / Expand buttons. It is a read-only
projection over the same CampaignViewModel + HeatmapViewModel the full window uses — no new state,
no new physics, just less of everything (AD-14: same window, saveState swap, read-only).

Also carries the scoring-fallback banner (SCENE-5 5.2, D7): a non-blocking red alert shown
whenever the latest discovery decision fell back to lexicographic placement because scene scoring
itself failed — the campaign keeps running, but the operator needs to know.
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
from isar_gui.viewmodels.campaign import (analysis_readout, authority_iter_text, budget_readout,
                                          current_activity_text, terminal_status_text)

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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        self._status = QLabel("no campaign yet", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._status)

        # Scoring-fallback banner (SCENE-5 5.2, D7): the beam never idles on a scoring fault, but
        # the operator must know the campaign fell back to basic placement. Hidden by default;
        # shown/cleared from CampaignViewModel.scoring_fallback, never blocking.
        self._fallback_banner = QLabel("", self)
        self._fallback_banner.setWordWrap(True)
        self._fallback_banner.setStyleSheet(
            "background-color: #b71c1c; color: white; font-weight: bold; padding: 4px;")
        self._fallback_banner.setVisible(False)
        outer.addWidget(self._fallback_banner)

        self._current = QLabel("no scan yet", self)
        self._current.setWordWrap(True)
        outer.addWidget(self._current)

        self._budget_bar = QProgressBar(self)
        self._budget_bar.setRange(0, 1000)
        self._budget_bar.setTextVisible(True)
        outer.addWidget(self._budget_bar)
        self._analysis_bar = QProgressBar(self)
        self._analysis_bar.setRange(0, 1000)
        self._analysis_bar.setTextVisible(True)
        outer.addWidget(self._analysis_bar)

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
        campaign_vm.scoring_fallback_changed.connect(self._sync_fallback_banner)
        heatmap_vm.spec_changed.connect(self._render_plane)
        self._sync()
        self._sync_fallback_banner()
        self._render_plane()

    # ---- ticker ---------------------------------------------------------------
    def append_log_line(self, line: str) -> None:
        """The window feeds LogDock lines here; keep the last few as a ticker."""
        self._ticker.append(str(line))
        del self._ticker[:-_TICKER_LINES]
        self._ticker_label.setText("\n".join(self._ticker))

    # ---- status ---------------------------------------------------------------
    def _sync(self, *_a) -> None:
        state = self._vm.state
        running = self._operations.is_running_campaign()
        # The current-scan line is state-derived (not feed-driven): this widget is created lazily
        # on compact-entry, possibly mid-scan, and must not say "no scan yet" until the next event.
        self._current.setText(current_activity_text(state))
        if running:
            it = state.iteration
            # state.iteration is the log's zero-based index; operators count from 1.
            text = f"campaign running — iteration {it + 1 if it is not None else '…'}"
            if self._operations.is_stop_pending():
                text += " — stopping after the current scan…"
        elif state.stop_state:
            text = terminal_status_text(state)
        elif state.has_campaign:
            text = "campaign idle"
        else:
            text = "no campaign yet"
        if state.converged is not None and state.n_grid:
            text += f"   ({state.converged}/{state.n_grid} converged)"
        authority = state.scan_design_authority or {}
        if authority:
            text += (f"   EIG: {authority.get('selected_role', '—')} "
                     f"({authority.get('decision_outcome', '—')})"
                     f"{authority_iter_text(authority)}")
        self._status.setText(text)
        permille, fmt = budget_readout(state)
        self._budget_bar.setVisible(state.budget_amount is not None)
        self._budget_bar.setValue(permille)
        self._budget_bar.setFormat(fmt)
        analysis_permille, analysis_fmt = analysis_readout(state)
        self._analysis_bar.setVisible(state.analysis_limit_seconds is not None)
        self._analysis_bar.setValue(analysis_permille)
        self._analysis_bar.setFormat(analysis_fmt)
        self._stop_btn.setEnabled(running and not self._operations.is_stop_pending())

    def _sync_fallback_banner(self, *_a) -> None:
        fallback = self._vm.scoring_fallback
        if not fallback:
            self._fallback_banner.setVisible(False)
            self._fallback_banner.setText("")
            return
        reason = fallback.get("reason", "unknown reason")
        self._fallback_banner.setText(
            "Scene scoring failed — campaign continuing on basic placement; needs attention"
            f"<br><span style='font-size: 8pt; font-weight: normal;'>reason: {reason}</span>")
        self._fallback_banner.setVisible(True)

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

"""InspectorWidget — the C2 fit inspector central widget (Task 10, A-4).

Left: a :class:`pyqtgraph.GraphicsLayoutWidget` with two x-linked rows — the data+fit+components
overlay and the residuals — rendered through :func:`isar_gui.plotspec_pg.render_into` (so the app and
the report draw the same spec with the same meaning). Right: the interval-ladder table (tier /
interval / annotations, certifying tier visually primary), the AIC candidate table (winner bolded),
and the verdict + provenance panels.

Purely presentational: every value shown is computed by :class:`~isar_gui.viewmodels.inspector.
InspectorViewModel`. The widget only lays out and renders VM outputs and reacts to ``spec_changed``.
No matplotlib, no workspace access.
"""
from __future__ import annotations

import html

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from isar_gui.plotspec_pg import clear_render, render_into

_EMPTY_HINT = "Select a scan in the Browser."


def _bold(item: QTableWidgetItem) -> QTableWidgetItem:
    f = item.font()
    f.setBold(True)
    item.setFont(f)
    return item


class InspectorWidget(QWidget):
    """Data+fit+residuals plot, interval ladder, AIC candidates, verdict + provenance."""

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self._vm = vm

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._splitter = QSplitter(Qt.Horizontal, self)
        outer.addWidget(self._splitter)

        # --- left: plots -------------------------------------------------
        self._glw = pg.GraphicsLayoutWidget()
        from isar_gui.plotspec_pg import disable_view_drops
        disable_view_drops(self._glw)   # no drops on the plot view (silence the dragLeave warning)
        self._main_plot = self._glw.addPlot(row=0, col=0)
        self._resid_plot = self._glw.addPlot(row=1, col=0)
        self._resid_plot.setXLink(self._main_plot)
        self._glw.ci.layout.setRowStretchFactor(0, 3)
        self._glw.ci.layout.setRowStretchFactor(1, 1)
        self._splitter.addWidget(self._glw)

        # --- right: tables + panels -------------------------------------
        side = QWidget()
        side_l = QVBoxLayout(side)
        self._empty_label = QLabel(_EMPTY_HINT)
        self._empty_label.setContentsMargins(12, 12, 12, 12)
        side_l.addWidget(self._empty_label)

        # Unit-mode metadata panel (a raw parsed scan × analyzer): shown only in "unit" kind, hidden
        # in "fit" kind where the ladder/candidates/verdict tell the story instead.
        self._metadata_header = QLabel("<b>Scan metadata</b>")
        side_l.addWidget(self._metadata_header)
        self._metadata_label = QLabel()
        self._metadata_label.setWordWrap(True)
        self._metadata_label.setTextFormat(Qt.RichText)
        side_l.addWidget(self._metadata_label)

        self._ladder_header = QLabel("<b>Interval ladder</b>")
        side_l.addWidget(self._ladder_header)
        self._ladder = self._make_table(["Tier", "Interval", "Notes"])
        side_l.addWidget(self._ladder)

        self._candidates_header = QLabel("<b>AIC candidates</b>")
        side_l.addWidget(self._candidates_header)
        self._candidates = self._make_table(["Model", "ΔAIC", "Weight", "n_modes"])
        side_l.addWidget(self._candidates)

        self._verdict_header = QLabel("<b>Verdict</b>")
        side_l.addWidget(self._verdict_header)
        self._verdict_label = QLabel()
        self._verdict_label.setWordWrap(True)
        self._verdict_label.setTextFormat(Qt.RichText)
        side_l.addWidget(self._verdict_label)

        self._provenance_header = QLabel("<b>Provenance</b>")
        side_l.addWidget(self._provenance_header)
        self._provenance_label = QLabel()
        self._provenance_label.setWordWrap(True)
        self._provenance_label.setTextFormat(Qt.RichText)
        side_l.addWidget(self._provenance_label)
        side_l.addStretch(1)

        self._splitter.addWidget(side)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)

        vm.spec_changed.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _make_table(headers) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return t

    # ---- rendering --------------------------------------------------------
    def refresh(self) -> None:
        st = self._vm.state
        empty = st.empty
        unit = (not empty) and st.kind == "unit"
        self._empty_label.setVisible(empty)
        # Unit mode shows only the metadata panel; fit mode shows the ladder/candidates/verdict/
        # provenance. Header labels are toggled alongside their widgets so a hidden section leaves no
        # orphan heading.
        for w in (self._metadata_header, self._metadata_label):
            w.setVisible(unit)
        for w in (self._ladder_header, self._ladder, self._candidates_header, self._candidates,
                  self._verdict_header, self._verdict_label,
                  self._provenance_header, self._provenance_label):
            w.setVisible((not empty) and not unit)
        self._render_plots(st)
        self._render_metadata(st.metadata if unit else {})
        self._render_ladder(st.ladder_rows)
        self._render_candidates(st)
        self._render_verdict({} if (empty or unit) else st.verdict)
        self._render_provenance({} if (empty or unit) else st.provenance)

    def _render_plots(self, st) -> None:
        if st.main_spec is not None:
            render_into(self._main_plot, st.main_spec)
        else:
            self._clear_plot(self._main_plot)
            if not st.empty and st.curve_error:
                self._main_plot.setTitle(f"curve unavailable ({st.curve_error})")
        if st.residuals_spec is not None:
            render_into(self._resid_plot, st.residuals_spec)
        else:
            self._clear_plot(self._resid_plot)

    @staticmethod
    def _clear_plot(plot_item) -> None:
        clear_render(plot_item)  # shared "nothing to show" clear (same idiom as the docks)

    def _render_ladder(self, rows) -> None:
        self._ladder.setRowCount(len(rows))
        for i, r in enumerate(rows):
            tier = QTableWidgetItem(r.tier + (" *" if r.certifying else ""))
            interval = QTableWidgetItem(r.interval_text)
            note = QTableWidgetItem(r.annotation)
            if r.certifying:  # exactly one certifying tier is visually primary
                _bold(tier)
                _bold(interval)
            self._ladder.setItem(i, 0, tier)
            self._ladder.setItem(i, 1, interval)
            self._ladder.setItem(i, 2, note)

    def _render_candidates(self, st) -> None:
        if not st.candidates_recorded:
            self._candidates.setRowCount(1)
            item = QTableWidgetItem("not recorded")
            self._candidates.setItem(0, 0, item)
            for c in range(1, 4):
                self._candidates.setItem(0, c, QTableWidgetItem(""))
            return
        rows = st.candidate_rows
        self._candidates.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [
                QTableWidgetItem(r.label),
                QTableWidgetItem("" if r.delta_aic is None else f"{r.delta_aic:.2f}"),
                QTableWidgetItem("" if r.weight is None else f"{r.weight:.3f}"),
                QTableWidgetItem("" if r.n_modes is None else str(r.n_modes)),
            ]
            for c, cell in enumerate(cells):
                if r.winner:
                    _bold(cell)
                self._candidates.setItem(i, c, cell)

    def _render_metadata(self, md) -> None:
        if not md:
            self._metadata_label.setText("")
            return
        # Escape workspace-derived values (command / file / q …) so markup in the data can't inject
        # into the RichText label — same discipline as the verdict/provenance panels.
        parts = [f"<b>{html.escape(str(k))}</b>: {html.escape(str(v))}" for k, v in md.items()]
        self._metadata_label.setText("<br>".join(parts))

    def _render_verdict(self, v) -> None:
        if not v:
            self._verdict_label.setText("")
            return
        order = ["verdict", "disposition", "confidence", "best_label", "modality", "fit_kind",
                 "branch_id", "assignment_entropy"]
        parts = []
        for k in order:
            if k in v and v[k] is not None:
                val = v[k]
                if isinstance(val, float):
                    val = f"{val:.3g}"
                # Escape workspace-derived values (best_label/branch_id/…) so markup in the data
                # can't inject into or corrupt the RichText label.
                parts.append(f"<b>{k}</b>: {html.escape(str(val))}")
        self._verdict_label.setText("<br>".join(parts))

    def _render_provenance(self, p) -> None:
        if not p:
            self._provenance_label.setText("")
            return
        # Escape workspace-derived values so markup in the data can't inject into the RichText label.
        parts = []
        if "prior_used" in p:
            parts.append(f"<b>prior</b>: {html.escape(str(p['prior_used']))}")
        if "calib" in p:
            parts.append(f"<b>calib</b>: {html.escape(str(p['calib']))}")
        if "pooled" in p:
            parts.append(f"<b>{html.escape(str(p['pooled']))}</b>")
        if "members" in p:
            parts.append("members: " + ", ".join(html.escape(str(m)) for m in p["members"]))
        self._provenance_label.setText("<br>".join(parts))

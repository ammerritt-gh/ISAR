"""Collapsed presentation of the former fit tables, driven by InspectorViewModel state."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QToolButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

def _bold(item: QTableWidgetItem) -> QTableWidgetItem:
    f = item.font()
    f.setBold(True)
    item.setFont(f)
    return item


class _CollapsibleSection(QWidget):
    def __init__(self, name: str, content: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName(f"fit_table_{name.casefold().replace(' ', '_')}_section")
        self.header = QToolButton(self)
        self.header.setObjectName(f"fit_table_{name.casefold().replace(' ', '_')}_toggle")
        self.header.setText(name)
        self.header.setAccessibleName(name)
        self.header.setToolTip(f"Show or hide {name.lower()}")
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setCheckable(True)
        self.header.setChecked(False)
        self.content = content
        self.content.setObjectName(f"fit_table_{name.casefold().replace(' ', '_')}_content")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.content)
        self.header.toggled.connect(self._toggle)
        self._toggle(False)

    def _toggle(self, expanded: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)


class FitTables(QWidget):
    """The former inspector tables, shown below the stored Fast/Deep summary."""

    _NO_FIT = ("No stored fit: the verdict, interval ladder, model candidates and provenance "
               "appear once a fit is stored.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fit_tables")
        self.setMinimumWidth(0)
        self._ladder = self._make_table(["Tier", "Interval", "Notes"])
        self._candidates = self._make_table(["Model", "P(model)", "ΔAIC", "n_modes"])
        self._verdict_label = QLabel()
        self._verdict_label.setWordWrap(True)
        self._verdict_label.setTextFormat(Qt.TextFormat.RichText)
        self._provenance_label = QLabel()
        self._provenance_label.setWordWrap(True)
        self._provenance_label.setTextFormat(Qt.TextFormat.RichText)
        self._sections = {
            "Interval ladder": _CollapsibleSection("Interval ladder", self._ladder),
            "Model candidates": _CollapsibleSection("Model candidates", self._candidates),
            "Verdict": _CollapsibleSection("Verdict", self._verdict_label),
            "Provenance": _CollapsibleSection("Provenance", self._provenance_label),
        }
        self._no_fit = QLabel(self._NO_FIT, self)
        self._no_fit.setObjectName("fit_tables_no_fit")
        self._no_fit.setWordWrap(True)
        self._no_fit.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for section in self._sections.values():
            layout.addWidget(section)
        layout.addWidget(self._no_fit)
        layout.addStretch(1)
        self.render(None)

    @staticmethod
    def _make_table(headers) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setMinimumWidth(0)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def render(self, state) -> None:
        """Render the stored fit tables; ``None`` means that this spectrum has no fit."""
        if state is None or state.empty:
            for section in self._sections.values():
                section.hide()
            self._no_fit.show()
            return
        self._no_fit.hide()
        for section in self._sections.values():
            section.show()
        self._render_ladder(state.ladder_rows)
        self._render_candidates(state)
        self._render_verdict(state.verdict)
        self._render_provenance(state.provenance)

    def _render_ladder(self, rows) -> None:
        self._ladder.setRowCount(len(rows))
        for i, row in enumerate(rows):
            tier = QTableWidgetItem(row.tier + (" *" if row.certifying else ""))
            interval = QTableWidgetItem(row.interval_text)
            note = QTableWidgetItem(row.annotation)
            if row.certifying:
                _bold(tier)
                _bold(interval)
            for col, item in enumerate((tier, interval, note)):
                item.setToolTip(item.text())
                self._ladder.setItem(i, col, item)
        self._size_table(self._ladder)

    def _render_candidates(self, state) -> None:
        if not state.candidates_recorded:
            self._candidates.setRowCount(1)
            item = QTableWidgetItem("not recorded")
            item.setToolTip(item.text())
            self._candidates.setItem(0, 0, item)
            for col in range(1, 4):
                blank = QTableWidgetItem("")
                blank.setToolTip("not recorded")
                self._candidates.setItem(0, col, blank)
            self._size_table(self._candidates)
            return
        rows = state.candidate_rows
        self._candidates.setRowCount(len(rows))
        for i, row in enumerate(rows):
            items = [
                QTableWidgetItem(row.label),
                QTableWidgetItem("" if row.posterior is None else f"{row.posterior:.3f}"),
                QTableWidgetItem("" if row.delta_aic is None else f"{row.delta_aic:.2f}"),
                QTableWidgetItem("" if row.n_modes is None else str(row.n_modes)),
            ]
            for col, item in enumerate(items):
                if row.winner:
                    _bold(item)
                item.setToolTip(item.text() or "not recorded")
                self._candidates.setItem(i, col, item)
        self._size_table(self._candidates)

    @staticmethod
    def _size_table(table: QTableWidget) -> None:
        table.resizeRowsToContents()
        header = table.horizontalHeader().height()
        rows = sum(table.rowHeight(i) for i in range(table.rowCount()))
        table.setFixedHeight(header + rows + 2 * table.frameWidth())

    def _render_verdict(self, verdict) -> None:
        order = ["verdict", "disposition", "confidence", "best_label", "modality", "fit_kind",
                 "branch_id", "assignment_entropy"]
        parts = []
        for key in order:
            if key in verdict and verdict[key] is not None:
                value = verdict[key]
                if isinstance(value, float):
                    value = f"{value:.3g}"
                parts.append(f"<b>{key}</b>: {html.escape(str(value))}")
        text = "<br>".join(parts)
        self._verdict_label.setText(text)
        self._verdict_label.setToolTip(html.unescape(text).replace("<br>", "\n"))

    def _render_provenance(self, provenance) -> None:
        parts = []
        if "prior_used" in provenance:
            parts.append(f"<b>prior</b>: {html.escape(str(provenance['prior_used']))}")
        if "calib" in provenance:
            parts.append(f"<b>calib</b>: {html.escape(str(provenance['calib']))}")
        if "pooled" in provenance:
            parts.append(f"<b>{html.escape(str(provenance['pooled']))}</b>")
        if "members" in provenance:
            members = ", ".join(html.escape(str(member)) for member in provenance["members"])
            parts.append("members: " + members)
        text = "<br>".join(parts)
        self._provenance_label.setText(text)
        self._provenance_label.setToolTip(html.unescape(text).replace("<br>", "\n"))

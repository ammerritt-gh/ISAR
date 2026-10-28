"""ScanInspectorPanel -- the optional scan inspector, a second part of the left panel.

The inspector extends the left panel outward beside the scan list when a scan is inspected and pulls
back on its close button (the window owns the placement, visibility and width; this widget owns the
content). For the inspected spectrum it shows the file, the scan numbers, the analyzer, the points and
energy range, the parse status, each pass's state and the small raw-counts plot, and the fact table
(temperature, slit, Q and the count origin, each with the file's value, the user's and the one used;
:mod:`isar_gui.views.fact_table`). Where the backend holds no facts for the scan (a workspace it did
not import) the stage-one Temperature, Q and Slit lines stay, so the facts are never shown twice.

Every line comes from the scan-list snapshot (``vm.snapshot``) and, for the energy range and the plot,
the facade's stored unit data; nothing is computed or re-fitted here (hard rule 7). :func:`inspector_lines`
is a pure function so the lines are testable without the plot. Accepts no drops: a drop reaches the main
window by Qt's propagation, which is the one drop route.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import NamedTuple

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from isar.workspace_api.scan_list import Fact, PooledRow, ScanSetRow, SpectrumRow
from isar_gui import status_words
from isar_gui.plain_words import drop_echo
from isar_gui.plotspec_pg import clear_render, disable_view_drops, render_into
from isar_gui.theme import DEEP, FAST, PARSED, dot_icon, status_tone
from isar_gui.viewmodels.inspector import CONFLICTS_SEP, MISSING_FACT_TEXT, InspectorViewModel
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.fact_table import FactTable

log = logging.getLogger(__name__)

_NOT_IN_THE_FILE = "not in the file"
_LABEL_WIDTH = 96          # px of the label column; the value column gets the rest
_PLOT_MIN_HEIGHT = 130     # the plot must leave the lines visible at the laptop size
_NO_SPECTRUM = "This spectrum is no longer in the experiment"
_NOTHING_PICKED = "Select an analyzer row (for example a06) under a scan set to inspect it."
_RAW_AXIS_TIP = "The energy axis is the file's raw axis: no energy zero has been applied yet."
_POOLED_NOTE = "pooled spectrum: shown in the display"
_STAGE_ONE_FACTS = frozenset({"Temperature", "Q", "Slit"})   # the lines the fact table replaces
# Unit metadata keys the inspector draws on its own lines, or the hashed unit key: never repeated below.
_SHOWN_ELSEWHERE = frozenset({"unit", "analyzer", "T", "points", "file", "slit", "scan_numbers",
                              "n_merged_scans", "T_K", "Q_nm"})


class Line(NamedTuple):
    """One line of the inspector. ``column`` (PARSED, FAST or DEEP) makes it a status: a dot plus the
    word, with ``reason`` beneath. ``tooltip`` is the value's full text."""
    label: str
    text: str
    tooltip: str = ""
    column: str | None = None
    reason: str | None = None
    group: str = "facts"           # "facts", "processing" or "metadata"
    word: str | None = None        # a status line: the fitting code's word (``text`` is the plain word shown)


def _number(value: float) -> str:
    return f"{value:g}"


def fact_text(fact: Fact) -> tuple[str, str]:
    """(text, tooltip) of a fact: ``"97 K — file (header)"``, or ``"— <reason>"`` when it is missing."""
    if fact.value is None:
        reason = fact.reason or _NOT_IN_THE_FILE
        return f"— {reason}", reason
    value = fact.value
    if isinstance(value, (tuple, list)):
        shown = "[" + ", ".join(_number(v) for v in value) + "]"
    elif isinstance(value, float):
        shown = _number(value)
    else:
        shown = str(value)
    if fact.unit:
        shown += f" {fact.unit}"
    if fact.origin and fact.origin != "missing":
        shown += f" — {fact.origin}"
        if fact.source and fact.source != fact.origin:
            shown += f" ({fact.source})"
    return shown, shown


def set_summary_text(set_row: ScanSetRow) -> str:
    """What the inspector says when a scan-set row (not a spectrum) is picked: the set's name, its analyzers,
    how many of its files were read and its temperature."""
    n = len(set_row.spectra)
    names = ", ".join(sp.analyzer or "—" for sp in set_row.spectra) or "none read"
    read = max(set_row.files_received - len(set_row.unread), 0)
    files = f"Files: {read} of {set_row.files_received} read" + (
        f", {len(set_row.unread)} not read" if set_row.unread else "")
    return "\n".join((f"Scan set {set_row.label}", f"{n} analyzer{'' if n == 1 else 's'}: {names}", files,
                      f"Temperature: {fact_text(set_row.temperature)[0]}",
                      "Select one of its analyzer rows in the list to inspect that analyzer's counts and facts."))


def scan_numbers_text(set_row: ScanSetRow) -> str:
    """The scan numbers of a set: its one number, or what a summed set says about its members."""
    numbers = ", ".join(str(n) for n in set_row.scan_numbers)
    if set_row.summed:
        if numbers:
            return f"summed: scans {numbers}"
        if set_row.n_merged:
            return f"summed ({set_row.n_merged} scans merged; the files do not list their numbers)"
        return "summed"
    if len(set_row.scan_numbers) == 1:
        return numbers
    if numbers:
        return f"scans {numbers}"
    return "— not in the files"


def energy_range_text(unit) -> tuple[str, str]:
    """(text, tooltip) of the range of ``unit_data``'s abscissa. The abscissa is the raw one, so when
    its axis is not absolute the line says the energy zero is not applied (hard rule 4)."""
    x = None if unit is None else getattr(unit, "x", None)
    if x is None or len(x) == 0:
        reason = "no parsed counts for this spectrum"
        return f"— {reason}", reason
    axis = dict(getattr(unit, "axis", None) or {})
    text = f"{_number(float(min(x)))} to {_number(float(max(x)))} {axis.get('units') or 'meV'}"
    if axis.get("calibration_status") != "absolute":
        text += " — raw axis, zero not applied"
        return text, f"{text}\n{_RAW_AXIS_TIP}"
    return text, text


def inspector_lines(row: SpectrumRow | PooledRow, set_row: ScanSetRow | None, unit=None,
                    metadata: dict | None = None) -> list[Line]:
    """The lines of the inspector for ``row`` (a spectrum or a pooled master), in display order.
    ``set_row`` is the spectrum's scan set, ``unit`` its stored unit data and ``metadata`` the unit
    metadata of the view model (the :data:`_KEPT_METADATA` fields are taken from it); each may be ``None``."""
    def fact(label: str, f: Fact) -> Line:
        text, tip = fact_text(f)
        return Line(label, text, tip)

    def status(label: str, column: str, st, group: str) -> Line:
        reason = drop_echo(st.word, st.reason)         # "resolved" over "resolved: count 1" said the word twice
        return Line(label, status_words.plain_word(column, st.word, st.reason, st.provisional),
                    status_words.tooltip(column, st.word, reason, st.provisional), column, reason, group, st.word)

    if isinstance(row, PooledRow):
        members = "—" if row.n_members is None else str(row.n_members)
        return [Line("Pooled", row.label, row.label),
                fact("Temperature", row.temperature), fact("Q", row.q),
                Line("Members", members, members),
                status("Fast pass", FAST, row.fast, "processing"),
                status("Deep pass", DEEP, row.deep, "processing")]
    no_set = Fact(None, None, "missing", "missing", "its scan set is not in the experiment")
    lines = []
    if row.file_name:
        lines.append(Line("File", row.file_name, row.file_path or row.file_name))
    else:
        reason = "the file of this analyzer is not recorded"
        lines.append(Line("File", f"— {reason}", reason))
    if set_row is None:
        lines.append(Line("Scan numbers", "—", no_set.reason))
    else:
        text = scan_numbers_text(set_row)
        lines.append(Line("Scan numbers", text, text))
    lines.append(Line("Analyzer", row.analyzer or "—", row.analyzer or ""))
    lines.append(fact("Temperature", set_row.temperature if set_row else no_set))
    lines.append(fact("Q", row.q))
    lines.append(fact("Slit", set_row.slit if set_row else no_set))
    points = "—" if row.n_points is None else str(row.n_points)
    lines.append(Line("Points", points, "" if row.n_points is not None else "number of points not recorded"))
    text, tip = energy_range_text(unit)
    lines.append(Line("Energy range", text, tip))
    lines.append(status("Parsed", PARSED, row.parsed, "facts"))
    for name, value in (metadata or {}).items():      # the view model's order; a missing fact has no row
        # a fact whose sources disagree keeps its row, so the conflicting value and source stay visible
        if CONFLICTS_SEP in value or (name not in _SHOWN_ELSEWHERE and not value.startswith(MISSING_FACT_TEXT)):
            # a long value is shown shortened; its tooltip is the whole text (_fmt_fact)
            lines.append(Line(name, value, getattr(value, "full", value), group="metadata"))
    lines.append(status("Fast pass", FAST, row.fast, "processing"))
    lines.append(status("Deep pass", DEEP, row.deep, "processing"))
    return lines


def _muted(widget: QWidget) -> None:
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.WindowText,
                     palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText))
    widget.setPalette(palette)


class _Note(QLabel):
    """A wrapped line (muted unless asked) whose width never pins its column."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, muted: bool = True) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        if muted:
            _muted(self)

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint


class ScanInspectorPanel(QWidget):
    close_requested = Signal()          # the ✕ button in its header

    def __init__(self, vm: ScanListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scan_inspector")
        self.setAcceptDrops(False)
        self._vm = vm
        self._key: str | None = None
        self._set_key: str | None = None   # a scan-set row was picked: its summary stands in for a spectrum
        self._stale = False             # content changed while hidden: redraw when shown
        self._plot_vm: InspectorViewModel | None = None   # the panel's own: the centre's never moves
        self._fact_table: FactTable | None = None
        self._fact_lines: list[Line] = []

        self._title = ElidedLabel("Scan inspector", self)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._close = QToolButton(self)
        self._close.setObjectName("scan_inspector_close")
        self._close.setText("✕")
        self._close.setToolTip("Close the inspector")
        self._close.setAccessibleName("Close inspector")
        self._close.clicked.connect(self.close_requested)
        header = QHBoxLayout()
        header.addWidget(self._title, 1)
        header.addWidget(self._close, 0)

        self._message = _Note(_NOTHING_PICKED, self)
        self._message.setObjectName("scan_inspector_message")

        self._body = QWidget(self)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        self._facts = self._form()
        self._processing = self._form()
        self._metadata = self._form()
        self._metadata_header = self._section("Scan metadata")
        rows = QWidget()
        rows_l = QVBoxLayout(rows)
        rows_l.setContentsMargins(0, 0, 0, 0)
        rows_l.addLayout(self._facts)
        self._rows_layout = rows_l      # the fact table goes right under the facts (set_intake)
        rows_l.addWidget(self._section("Processing"))
        rows_l.addLayout(self._processing)
        rows_l.addWidget(self._metadata_header)
        rows_l.addLayout(self._metadata)
        rows_l.addStretch(1)
        # The rows scroll, so a unit with many metadata rows never squeezes the plot below them.
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("scan_inspector_rows")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(rows)
        body.addWidget(self._scroll, 1)
        body.addWidget(self._section("Raw counts"))
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setObjectName("scan_inspector_plot")
        self._glw.setMinimumHeight(_PLOT_MIN_HEIGHT)
        disable_view_drops(self._glw)   # a drop must reach the window, not the plot view
        self._plot = self._glw.addPlot(row=0, col=0)
        body.addWidget(self._glw, 1)
        self._plot_note = _Note("", self)
        self._plot_note.setObjectName("scan_inspector_plot_note")
        body.addWidget(self._plot_note)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(header)
        layout.addWidget(self._message)
        layout.addWidget(self._body, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        vm.inspect_requested.connect(self.show_key)
        vm.current_changed.connect(self._on_current_changed)
        vm.set_focused.connect(self._on_set_focused)
        vm.snapshot_changed.connect(self._on_snapshot_changed)
        self._render()

    # ---- building blocks -----------------------------------------------------------------
    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(2)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def set_intake(self, intake) -> None:
        """The window's IntakeState: builds the fact table from it and places it under the facts."""
        self._intake = intake
        if self._fact_table is not None:
            self._rows_layout.removeWidget(self._fact_table)
            self._fact_table.deleteLater()
        self._fact_table = FactTable(intake, self)
        self._fact_table.shown_changed.connect(self._fill_fact_lines)
        self._rows_layout.insertWidget(1, self._fact_table)
        self._render()

    # ---- following the list --------------------------------------------------------------
    def show_key(self, key: str | None) -> None:
        """Show the facts of spectrum ``key`` (``None`` = nothing selected)."""
        self._key = key
        self._set_key = None
        self._render()

    def _on_set_focused(self, set_key) -> None:
        if self.isHidden():
            return                      # showEvent follows the list's focus
        self._set_key = set_key
        if set_key is not None:
            self._key = None
        self._render()

    def _on_current_changed(self, key) -> None:
        # A None or a key that is not a spectrum or pooled master keeps what is shown.
        if self.isHidden() or key is None or self._vm.snapshot.spectrum(key) is None:
            return
        self.show_key(key)

    def _on_snapshot_changed(self) -> None:
        if self.isHidden():
            self._stale = True
        else:
            self._render()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        current, focus = self._vm.current_key, self._vm.focus_set
        if focus is not None and focus != self._set_key:
            self._key, self._set_key, self._stale = None, focus, True   # opened on a scan-set row: summarise it
        elif focus is None and current is not None and current != self._key and self._vm.snapshot.spectrum(current):
            self._key, self._set_key, self._stale = current, None, True   # opened without a key: follow the list
        if self._stale:
            self._render()

    # ---- rendering -----------------------------------------------------------------------
    def _render(self) -> None:
        self._stale = False
        snapshot = self._vm.snapshot
        row = None if self._key is None else snapshot.spectrum(self._key)
        if row is None:
            self._set_table_unit(None)
            set_row = None if self._set_key is None else snapshot.scan_set(self._set_key)
            self._title.set_full_text("Scan inspector" if set_row is None else f"Scan set {set_row.label}")
            self._message.setText(set_summary_text(set_row) if set_row is not None else
                                  _NOTHING_PICKED if self._key is None else _NO_SPECTRUM)
            self._message.show()
            self._body.hide()
            return
        self._message.hide()
        self._body.show()
        pooled = isinstance(row, PooledRow)
        set_row = None if pooled else snapshot.scan_set(row.set_key)
        unit = None if pooled else self._read_unit(self._key)
        self._title.set_full_text(row.label if pooled else
                                  f"{row.analyzer or '—'} · {set_row.label if set_row else '—'}")
        state = None if pooled else self._plot_state()
        lines = inspector_lines(row, set_row, unit, None if state is None else state.metadata)
        self._set_table_unit(None if pooled else self._key, row, set_row)
        self._fact_lines = [ln for ln in lines if ln.group == "facts"]
        self._fill_fact_lines()
        self._fill(self._processing, [ln for ln in lines if ln.group == "processing"])
        self._fill(self._metadata, [ln for ln in lines if ln.group == "metadata"])
        self._metadata_header.setVisible(self._metadata.rowCount() > 0)
        self._render_plot(row, set_row, pooled, state)

    def _set_table_unit(self, key, row=None, set_row=None) -> None:
        if self._fact_table is not None:
            self._fact_table.set_unit(key, row, set_row)

    def _fill_fact_lines(self) -> None:
        """The facts form, without the stage-one Temperature, Q and Slit lines while the table shows them."""
        replaced = self._fact_table is not None and self._fact_table.has_facts
        self._fill(self._facts, [ln for ln in self._fact_lines if not (replaced and ln.label in _STAGE_ONE_FACTS)])

    def _read_unit(self, key: str):
        try:
            return self._vm.workspace.unit_data(key)
        except (AttributeError, KeyError, ValueError, OSError) as exc:   # the facade degrades; stay up
            log.warning("scan inspector: no unit data for %s: %s", key, exc)
            return None

    def _plot_state(self):
        """The inspected unit's state in the panel's own view model (the centre's never moves)."""
        if self._plot_vm is None or self._plot_vm.workspace is not self._vm.workspace:
            self._plot_vm = InspectorViewModel(self._vm.workspace)
        self._plot_vm.set_unit(self._key)
        return self._plot_vm.state

    def _render_plot(self, row, set_row, pooled: bool, state) -> None:
        note = None
        if pooled:
            clear_render(self._plot)
            note = _POOLED_NOTE
        else:
            if state.main_spec is None:
                clear_render(self._plot)
                note = state.curve_error or "no parsed counts for this spectrum"
            else:
                # The VM titles a unit "(parsed, not fitted)" with the hashed number: retitle from the
                # snapshot, on a copy, so the VM's own state is never mutated.
                title = (f"{set_row.label} · {row.analyzer} · raw counts"
                         if set_row is not None and row.analyzer else "raw counts")
                render_into(self._plot, dataclasses.replace(state.main_spec, title=title))
        self._glw.setVisible(note is None)
        self._plot_note.setVisible(note is not None)
        self._plot_note.setText(note or "")

    def _fill(self, form: QFormLayout, lines: list[Line]) -> None:
        while form.rowCount():
            form.removeRow(0)
        for line in lines:
            label = ElidedLabel(line.label, mode=Qt.TextElideMode.ElideRight)   # full name in the tooltip
            # Fixed, not Ignored: QFormLayout sizes the label column from the hint, and Ignored zeroes it
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            label.setFixedWidth(_LABEL_WIDTH)
            _muted(label)
            form.addRow(label, self._status_widget(line) if line.column else self._value(line))

    @staticmethod
    def _value(line: Line) -> QLabel:
        if line.label == "Energy range":          # its caveat ("zero not applied") must not be cut off
            note = _Note(line.text, muted=False)
            note.setToolTip(line.tooltip or line.text)
            return note
        mode = Qt.TextElideMode.ElideMiddle if line.label == "File" else Qt.TextElideMode.ElideRight
        value = ElidedLabel(line.text, mode=mode)
        value.setToolTip(line.tooltip or line.text)
        return value

    @staticmethod
    def _status_widget(line: Line) -> QWidget:
        """A dot plus the word, with the reason beneath in muted text when there is one."""
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        dot = QLabel()
        dot.setPixmap(dot_icon(status_tone(line.column, line.word or line.text)).pixmap(10, 10))
        word = ElidedLabel(line.text, mode=Qt.TextElideMode.ElideRight)
        word.setToolTip(line.tooltip or line.text)
        top.addWidget(dot, 0)
        top.addWidget(word, 1)
        outer.addLayout(top)
        if line.reason:
            reason = _Note(line.reason)
            reason.setToolTip(line.reason)
            outer.addWidget(reason)
        return box

"""ScanInspectorPanel -- the optional scan inspector, a second part of the left panel.

The inspector extends the left panel outward beside the scan list when a scan is inspected and pulls
back on its close button (the window owns the placement, visibility and width; this widget owns the
content). For the inspected spectrum it shows the file, the scan numbers, the analyzer, the points and
energy range, the parse status, the stored Fast and Deep summaries, the former fit tables, and the fact
table (temperature, slit, Q and the count origin, each with the file's value, the user's and the one
used; :mod:`isar_gui.views.fact_table`). Where the backend holds no facts for the scan (a workspace it
did not import) the stage-one Temperature, Q and Slit lines stay, so the facts are never shown twice.

The metadata lines come from the scan-list snapshot (``vm.snapshot``); the energy range and stored fit
summary come from the shared facade reads. Nothing is computed or re-fitted here (hard rule 7).
:func:`inspector_lines` is a pure function so the metadata lines are testable independently. Accepts no
drops: a drop reaches the main window by Qt's propagation, which is the one drop route.
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import NamedTuple

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from isar.workspace_api.scan_list import Fact, PooledRow, ScanSetRow, SpectrumRow
from isar.workspace_api.spectra import (AMPLITUDE_HEADING, ENERGY_HEADING, LINEWIDTH_HEADING,
                                        OUTDATED_TEXT, Measured, PassSummary,
                                        SpectrumBundle, spectrum_bundles)
from isar_gui import status_words
from isar_gui.plain_words import drop_echo
from isar_gui.selection import unique_labels
from isar_gui.theme import DEEP, FAST, PARSED, dot_icon, status_tone
from isar_gui.viewmodels.inspector import CONFLICTS_SEP, MISSING_FACT_TEXT, InspectorViewModel
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.fact_table import FactTable
from isar_gui.views.inspector_widget import FitTables

log = logging.getLogger(__name__)

_NOT_IN_THE_FILE = "not in the file"
_LABEL_WIDTH = 96          # px of the label column; the value column gets the rest
_NO_SPECTRUM = "This spectrum is no longer in the experiment"
EMPTY_TEXT = "No spectrum selected"
_RAW_AXIS_TIP = "The energy axis is the file's raw axis: no energy zero has been applied yet."
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


def inspector_title(snapshot, keys, active: str | None) -> tuple[str, str]:
    """The C-identity title and its position/path tooltip for the active selected spectrum."""
    row = None if active is None else snapshot.spectrum(active)
    if row is None:
        return "Scan inspector", ""
    labels = unique_labels(snapshot, keys, lambda ident: (
        f"Scan {ident.scan}" if ident.numbered else ident.scan, ident.analyzer, ident.t))
    title = labels.get(active, row.label if isinstance(row, PooledRow) else row.analyzer or "Spectrum")
    ordered = tuple(keys)
    position = ordered.index(active) + 1 if active in ordered else 1
    tip = f"{title} · {position} of {len(ordered)} selected"
    file_name = getattr(row, "file_name", None)
    if file_name:
        tip += f"\n{file_name}"
    return title, tip


def measured_text(value: Measured) -> tuple[str, str]:
    """Render one stored value and uncertainty without making missing values look numeric."""
    number = _finite_number(value.value)
    if number is None:
        return "—", "not stored"
    if value.kind not in ("hesse", "minos"):
        return f"{number:.4g} ± —", f"{number!r}; uncertainty not stored"
    lo, hi = _finite_number(value.lo), _finite_number(value.hi)
    if lo is None or hi is None or lo > number or number > hi:
        return f"{number:.4g} ± —", f"{number!r}; uncertainty not stored"
    decimal_value = Decimal(str(value.value))
    decimal_lo = Decimal(str(value.lo))
    decimal_hi = Decimal(str(value.hi))
    if value.kind == "hesse":
        sigma = (decimal_hi - decimal_lo) / 2
        if sigma == 0:
            return f"{number:.4g} ± 0", f"{number!r} (HESSE interval {lo!r} to {hi!r})"
        digits = max(1 - sigma.adjusted(), 0)
        return (f"{number:.{digits}f} ± {float(sigma):.{digits}f}",
                f"{number!r} (HESSE interval {lo!r} to {hi!r})")
    plus, minus = decimal_hi - decimal_value, decimal_value - decimal_lo
    smallest = min(plus, minus)
    digits = max(1 - smallest.adjusted(), 0) if smallest > 0 else 3
    return (f"{number:.{digits}f} +{float(plus):.{digits}f} −{float(minus):.{digits}f}",
            f"{number!r} (MINOS interval {lo!r} to {hi!r})")


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


class _SnapshotIndex:
    """O(1) row lookup for one immutable scan-list snapshot."""

    def __init__(self, snapshot):
        self._rows = {row.key: row for scan_set in snapshot.sets for row in scan_set.spectra}
        self._rows.update({row.key: row for row in snapshot.pooled})
        self._sets = {scan_set.key: scan_set for scan_set in snapshot.sets}

    def spectrum(self, key):
        return self._rows.get(key)

    def scan_set(self, key):
        return self._sets.get(key)


class FitSection(QWidget):
    """Compact Fast | Deep summary sourced from the shared spectrum bundle."""

    _SUMMARY_ROWS = (("modes", "Modes"), ("state", "State"),
                     ("trust", "Trust"), ("inputs", "Inputs"))
    _QUANTITIES = (
        ("energy", "E₀ (meV)", ENERGY_HEADING),
        ("linewidth", "Γ (meV, HWHM)", LINEWIDTH_HEADING),
        ("amplitude", "h (fit units)", AMPLITUDE_HEADING),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scan_inspector_fit")
        self._title = QLabel("Fit", self)
        self._title.setObjectName("scan_inspector_fit_title")
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._grid_widget = QWidget(self)
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(2)
        self._grid.setColumnStretch(0, 0)
        self._grid.setColumnStretch(1, 1)
        self._grid.setColumnStretch(2, 1)
        self._grid_values: dict[str, _Note] = {}
        for col, text in enumerate(("", "Fast", "Deep")):
            header = QLabel(text, self._grid_widget)
            header.setObjectName(f"fit_header_{text.casefold()}" if text else "fit_header_label")
            if text:
                header_font = header.font()
                header_font.setBold(True)
                header.setFont(header_font)
            self._grid.addWidget(header, 0, col)
        for row, (key, label_text) in enumerate(self._SUMMARY_ROWS, start=1):
            label = self._cell(label_text, f"fit_label_{key}", muted=True)
            self._grid.addWidget(label, row, 0)
            for col, pass_name in ((1, "fast"), (2, "deep")):
                name = f"fit_{pass_name}_{key}"
                value = self._cell("—", name, muted=False)
                self._grid_values[name] = value
                self._grid.addWidget(value, row, col)
        self._dynamic_widgets = []
        self._base_rows = 1 + len(self._SUMMARY_ROWS)
        self._note = _Note("Stored fit values. Raw | Normalised and the plot's Fit switch do not change them.",
                           self)
        self._note.setObjectName("scan_inspector_fit_note")
        self._unavailable = _Note("", self)
        self._unavailable.setObjectName("scan_inspector_fit_unavailable")
        self._unavailable.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addWidget(self._grid_widget)
        layout.addWidget(self._note)
        layout.addWidget(self._unavailable)
        self.setMinimumWidth(0)

    @staticmethod
    def _cell(text: str, name: str, *, muted: bool) -> _Note:
        cell = _Note(text, muted=muted)
        cell.setObjectName(name)
        cell.setMinimumWidth(0)
        return cell

    def set_unavailable(self, message: str) -> None:
        self._clear_modes()
        self._grid_widget.hide()
        self._note.hide()
        self._unavailable.setText(message)
        self._unavailable.setToolTip(message)
        self._unavailable.show()

    def render(self, summaries, *, exposure_source: str | None = None) -> None:
        self._unavailable.hide()
        self._grid_widget.show()
        self._note.show()
        fast, deep = summaries["fast"], summaries["deep"]
        for pass_name, summary in (("fast", fast), ("deep", deep)):
            self._set_summary(pass_name, summary)
        self._clear_modes()
        for number in range(1, max(len(fast.modes), len(deep.modes)) + 1):
            mode_row = self._base_rows + (number - 1) * (len(self._QUANTITIES) + 1)
            heading = self._cell(f"Mode {number}", f"fit_mode_{number}_header", muted=True)
            heading_font = heading.font()
            heading_font.setBold(True)
            heading.setFont(heading_font)
            self._grid.addWidget(heading, mode_row, 1, 1, 2)
            self._dynamic_widgets.append(heading)
            for quantity_index, (quantity, label_text, heading_tip) in enumerate(self._QUANTITIES, start=1):
                row = mode_row + quantity_index
                label = self._cell(label_text, f"fit_mode_{number}_{quantity}_label", muted=True)
                label.setToolTip(heading_tip)
                if quantity == "amplitude" and exposure_source:
                    label.setToolTip(f"{heading_tip} ({exposure_source})")
                self._grid.addWidget(label, row, 0)
                self._dynamic_widgets.append(label)
                for col, pass_name, summary in ((1, "fast", fast), (2, "deep", deep)):
                    name = f"fit_{pass_name}_m{number}_{quantity}"
                    value = self._cell("—", name, muted=False)
                    if number <= len(summary.modes):
                        measured = getattr(summary.modes[number - 1], quantity)
                        text, tooltip = measured_text(measured)
                        value.setText(text)
                        value.setToolTip(tooltip)
                    else:
                        value.setToolTip(f"this pass has no Mode {number}")
                    self._grid.addWidget(value, row, col)
                    self._dynamic_widgets.append(value)

    def _set_summary(self, pass_name: str, summary: PassSummary) -> None:
        mode_count = str(len(summary.modes)) if summary.present and summary.label is not None else "—"
        if pass_name == "fast":
            trust = "—"
            trust_tip = "the fast pass carries no trust flag"
        else:
            trust = ("untrusted" if summary.trusted is False
                     else "trusted" if summary.trusted is True else "—")
            trust_tip = summary.trust_reason or trust
        if summary.current is True:
            inputs, inputs_tip = "current", "the stored fit inputs match"
        elif summary.current is False:
            inputs = summary.outdated_text or OUTDATED_TEXT
            inputs_tip = "\n".join(f"{name}: {used} → {now}" for name, used, now in summary.changed)
            if not inputs_tip:
                inputs_tip = inputs
        elif summary.present:
            inputs = summary.unjudged_text or "not judged"
            inputs_tip = "no finished input record for this pass"
        else:
            inputs, inputs_tip = "—", ""
        entries = {
            "modes": (mode_count, summary.label or summary.state_reason or ""),
            "state": (summary.state, summary.state_reason or summary.state),
            "trust": (trust, trust_tip),
            "inputs": (inputs, inputs_tip),
        }
        for key, (text, tooltip) in entries.items():
            cell = self._grid_values[f"fit_{pass_name}_{key}"]
            cell.setText(text)
            cell.setToolTip(tooltip)

    def _clear_modes(self) -> None:
        for widget in self._dynamic_widgets:
            self._grid.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._dynamic_widgets.clear()


class ScanInspectorPanel(QWidget):
    close_requested = Signal()          # the ✕ button in its header

    def __init__(self, vm: ScanListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scan_inspector")
        self.setAcceptDrops(False)
        self._vm = vm
        self._stale = True              # content changed while hidden: redraw when shown
        self._snapshot_source = None
        self._snapshot_index: _SnapshotIndex | None = None
        self._meta_vm: InspectorViewModel | None = None
        self._tables_vm: InspectorViewModel | None = None
        self.current_bundle: SpectrumBundle | None = None
        self._fact_table: FactTable | None = None
        self._fact_lines: list[Line] = []

        self._title = ElidedLabel("Scan inspector", self)
        self._title.setObjectName("scan_inspector_title")
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

        self._position_row = QWidget(self)
        position_layout = QHBoxLayout(self._position_row)
        position_layout.setContentsMargins(0, 0, 0, 0)
        self._previous = QToolButton(self._position_row)
        self._previous.setObjectName("scan_inspector_prev")
        self._previous.setText("◀")
        self._previous.setAccessibleName("Previous selected spectrum")
        self._next = QToolButton(self._position_row)
        self._next.setObjectName("scan_inspector_next")
        self._next.setText("▶")
        self._next.setAccessibleName("Next selected spectrum")
        self._position = QLabel(self._position_row)
        self._position.setObjectName("scan_inspector_position")
        self._position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        position_layout.addWidget(self._previous)
        position_layout.addWidget(self._position, 1)
        position_layout.addWidget(self._next)
        self._previous.clicked.connect(self._step_previous)
        self._next.clicked.connect(self._step_next)
        self._previous_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        self._previous_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._previous_shortcut.activated.connect(self._step_previous)
        self._next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        self._next_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._next_shortcut.activated.connect(self._step_next)

        self._message = _Note(EMPTY_TEXT, self)
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
        self._fit_section = FitSection(rows)
        rows_l.addWidget(self._fit_section)
        self._fit_tables = FitTables(self)
        rows_l.addWidget(self._fit_tables)
        rows_l.addStretch(1)
        # The facts and former fit tables share one scroll area.
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("scan_inspector_rows")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(rows)
        body.addWidget(self._scroll, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(header)
        layout.addWidget(self._position_row)
        layout.addWidget(self._message)
        layout.addWidget(self._body, 1)
        self._position_row.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._previous.setToolTip("Only one spectrum is selected")
        self._next.setToolTip("Only one spectrum is selected")

        vm.inspect_requested.connect(self._on_inspect)
        vm.selection.changed.connect(self._on_selection_changed)
        vm.snapshot_changed.connect(self._on_snapshot_changed)

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
        self._install_text_shortcut_guards()
        self._request_render()

    # ---- following the list --------------------------------------------------------------
    def _on_inspect(self, key: str | None) -> None:
        if key in self._vm.selection.keys:
            self._vm.selection.set_active(key)

    def _on_selection_changed(self, _change) -> None:
        self._request_render()

    def _request_render(self) -> None:
        if self.isHidden():
            self._stale = True
            return
        self._render()

    def _on_snapshot_changed(self) -> None:
        self._request_render()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._stale:
            self._render()

    # ---- rendering -----------------------------------------------------------------------
    def _render(self) -> None:
        self._stale = False
        snapshot = self._vm.snapshot
        if self._snapshot_source is not snapshot:
            self._snapshot_source = snapshot
            self._snapshot_index = _SnapshotIndex(snapshot)
        index = self._snapshot_index
        selection = self._vm.selection
        key = selection.active
        position = selection.position()
        if position is None:
            self._position_row.hide()
            self._title.set_full_text("Scan inspector")
            self._title.setToolTip("")
            self._title.setAccessibleName("Scan inspector")
            self._position.setText("")
            self._message.setText(EMPTY_TEXT)
            self._message.show()
            self._body.hide()
            self._set_table_unit(None)
            self.current_bundle = None
            self._fit_section.set_unavailable(EMPTY_TEXT)
            self._fit_tables.render(None)
            return
        self._position_row.show()
        self._position.setText(f"{position[0]} of {position[1]} selected")
        self._set_arrow_state(position)
        title, title_tip = inspector_title(index, selection.keys, key)
        self._title.set_full_text(title)
        self._title.setToolTip(title_tip)
        self._title.setAccessibleName(title_tip.split("\n", 1)[0])
        row = None if key is None else index.spectrum(key)
        if row is None:
            self._set_table_unit(None)
            self._message.setText(_NO_SPECTRUM)
            self._message.show()
            self._body.hide()
            self.current_bundle = None
            self._fit_section.set_unavailable(_NO_SPECTRUM)
            self._fit_tables.render(None)
            return
        self._message.hide()
        self._body.show()
        pooled = isinstance(row, PooledRow)
        set_row = None if pooled else index.scan_set(row.set_key)
        unit = None if pooled else self._read_unit(key)
        state = None if pooled else self._metadata_state(key)
        lines = inspector_lines(row, set_row, unit, None if state is None else state.metadata)
        self._set_table_unit(None if pooled else key, row, set_row)
        self._fact_lines = [ln for ln in lines if ln.group == "facts"]
        self._fill_fact_lines()
        self._fill(self._processing, [ln for ln in lines if ln.group == "processing"])
        self._fill(self._metadata, [ln for ln in lines if ln.group == "metadata"])
        self._metadata_header.setVisible(self._metadata.rowCount() > 0)
        self._render_bundle(row, key)
        self._render_tables(row)

    def _set_arrow_state(self, position: tuple[int, int]) -> None:
        current, total = position
        if total <= 1:
            previous_tip = next_tip = "Only one spectrum is selected"
        else:
            previous_tip = ("Previous spectrum in the selection (Left arrow)" if current > 1 else
                            "This is the first spectrum in the selection")
            next_tip = ("Next spectrum in the selection (Right arrow)" if current < total else
                        "This is the last spectrum in the selection")
        self._previous.setEnabled(current > 1)
        self._next.setEnabled(current < total)
        self._previous.setToolTip(previous_tip)
        self._next.setToolTip(next_tip)

    def _step_previous(self) -> None:
        self._vm.selection.step(-1)

    def _step_next(self) -> None:
        self._vm.selection.step(1)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_Left:
            self._step_previous()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self._step_next()
            event.accept()
            return
        super().keyPressEvent(event)

    def _set_table_unit(self, key, row=None, set_row=None) -> None:
        if self._fact_table is not None:
            self._fact_table.set_unit(key, row, set_row)

    def _fill_fact_lines(self) -> None:
        """The facts form, without the stage-one Temperature, Q and Slit lines while the table shows them."""
        replaced = self._fact_table is not None and self._fact_table.has_facts
        self._fill(self._facts, [ln for ln in self._fact_lines if not (replaced and ln.label in _STAGE_ONE_FACTS)])
        self._install_text_shortcut_guards()

    def _read_unit(self, key: str):
        try:
            return self._vm.workspace.unit_data(key)
        except (AttributeError, KeyError, ValueError, OSError) as exc:   # the facade degrades; stay up
            log.warning("scan inspector: no unit data for %s: %s", key, exc)
            return None

    def _metadata_state(self, key: str):
        workspace = self._vm.workspace
        if self._meta_vm is None or self._meta_vm.workspace is not workspace:
            self._meta_vm = InspectorViewModel(workspace, specs=False)
        self._meta_vm.set_unit(key)
        return self._meta_vm.state

    def _render_bundle(self, row, key: str) -> None:
        try:
            bundles = spectrum_bundles(self._vm.workspace, [row], cache=self._vm.spectra_cache)
            self.current_bundle = bundles[0] if bundles else None
        except (ValueError, OSError, KeyError) as exc:
            log.warning("scan inspector: fit values unavailable for %s: %s", key, exc)
            self.current_bundle = None
            self._fit_section.set_unavailable(f"Fit values unavailable ({exc})")
            return
        if self.current_bundle is None:
            self._fit_section.set_unavailable("Fit values unavailable (no spectrum values returned)")
            return
        self._fit_section.render(self.current_bundle.summaries,
                                 exposure_source=self.current_bundle.exposure_source)

    def _render_tables(self, row) -> None:
        fit = getattr(row, "fit", None)
        if fit is None:
            self._fit_tables.render(None)
            return
        workspace = self._vm.workspace
        if self._tables_vm is None or self._tables_vm.workspace is not workspace:
            self._tables_vm = InspectorViewModel(workspace, specs=False)
        self._tables_vm.set_fit(fit)
        self._fit_tables.render(self._tables_vm.state)

    def _install_text_shortcut_guards(self) -> None:
        if self._fact_table is None:
            return
        for edit in self._fact_table.findChildren(QLineEdit):
            edit.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if (isinstance(watched, QLineEdit) and event.type() == QEvent.Type.ShortcutOverride
                and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)):
            event.accept()
            return False
        return super().eventFilter(watched, event)

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

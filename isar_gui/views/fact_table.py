"""FactTable -- the scan inspector's table of facts: the file's value, the user's, and the one used.

For the inspected spectrum it shows, from ``intake.fact_rows`` (the intake backend's own read, never
recomputed here): temperature and slit (facts of the whole scan set), this analyzer's Q and count origin,
the analyzer's state (from ``intake.conditions``) and any other fact the read returns, each as three
cells -- *From the files* (value and source), *Yours* (value and reason) and *Used* (the value in force
and whose it is, or the fallback named) -- and, beneath, this spectrum's readiness per use. A disagreement
between the file's value and the user's is drawn in the warning colour and says both values on hover.

Row actions are the operator's amendment 1 and nothing else: temperature and slit offer *Set my value...*
and *Use the file's value* (the whole scan set); the analyzer offers *Mark dead* / *In use* and, where its
counts may not be raw, *Accept counts...* / *Withdraw acceptance*. Q, the count origin and every other fact
are display only. No widget edits a file value. Each fact is a title line over its three cells, not a
fourth column: the inspector is 260 px wide at the laptop size, too narrow for four columns of text.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from isar.intake.readiness import USES
from isar.workspace_api import scan_prefix, split_key
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.value_entry import SetValueDialog

NO_VALUE_OF_YOURS = "No value of yours is in force"
FILES_ONLY = "Comes from the files only"
NO_HKL = "no hkl in the files: steering needs it"
_FALLBACKS = {"bose_t0_limit": "none: T → 0⁺ limit", "slit_inferred": "inferred from the elastic lines",
              "slit_broadest_curve": "broadest curve", "slit_pending": "chosen when analysis starts"}
_USE_NAMES = {"read": "Read", "reduce": "Reduce / export", "calibrate": "Calibrate", "fit": "Fit",
              "steer": "Steer"}
_TITLES = {"temperature": "Temperature", "slit": "Slit", "q": "Q", "counts": "Count origin"}
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"


def _number(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _value_text(value, unit: str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        shown = "[" + ", ".join(_number(v) for v in value) + "]"
    else:
        shown = _number(value)
    return f"{shown} {unit}" if unit else shown


def _plural(n: int, one: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


def _with(first: str, second: str | None) -> str:
    return f"{first}\n{second}" if second else first


def counts_text(record) -> tuple[str, str]:
    """(text, tooltip) of a count-origin record: bins tested, bins off an integer, largest deviation."""
    if not isinstance(record, dict):
        return "—", "missing"
    origin = {"y_over_e_squared": "N = (y/e)²"}.get(record.get("origin"), str(record.get("origin") or "counts"))
    off = record.get("bins_off")
    verdict = "none off an integer" if not off else f"{off} off (largest deviation {record.get('max_deviation'):.2g})"
    tip = f"family {record.get('family')}, level {record.get('level') or 'none'}"
    return f"{origin}, {record.get('bins_tested')} bins tested, {verdict}", tip


class _Cell(QLabel):
    """A wrapped cell whose width never pins its column (the column decides)."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, muted: bool = False) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)      # a plain setSizePolicy would drop the flag word wrap just set
        self.setSizePolicy(policy)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        if muted:
            palette = self.palette()
            palette.setColor(palette.ColorRole.WindowText,
                             palette.color(palette.ColorGroup.Disabled, palette.ColorRole.WindowText))
            self.setPalette(palette)

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint


class _ActionButton(QPushButton):
    """A button that never pins the inspector's width: its text is shortened with "…" to the room it has
    (the full text stays the accessible name), as :class:`ElidedLabel` does for a label."""
    _PAD = 24       # px the button style adds around the text

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setAccessibleName(text)
        # Preferred, not the default Minimum: only a policy that may shrink lets the layout use the 0 minimum
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def full_text(self) -> str:
        return self._full

    def sizeHint(self):  # noqa: N802 (Qt override)
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full) + self._PAD)
        return hint

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                    max(self.width() - self._PAD, 1)))


@dataclass
class _Action:
    text: str
    slot: Callable[[], None]
    name: str
    enabled: bool = True
    tip: str = ""


@dataclass
class _Row:
    key: str                        # "temperature" | "slit" | "q" | "counts" | "analyzer" | another fact's name
    title: str
    files: tuple[str, str] = ("", "")        # (text, tooltip) of each cell
    yours: tuple[str, str] = ("", "")
    used: tuple[str, str] = ("", "")
    warn: bool = False              # a disagreement: Yours and Used in the warning colour
    tip: str = ""                   # the row's hover
    actions: list[_Action] = field(default_factory=list)


class _ReasonDialog(QDialog):
    """An optional reason, then ``save(reason)``; a refused save shows its reason and the dialog stays."""

    def __init__(self, title: str, prompt: str, action: str, save: Callable[[str | None], object],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fact_reason_dialog")
        self.setWindowTitle(title)
        self._save_fn = save
        prompt_label = QLabel(prompt, self)
        prompt_label.setWordWrap(True)
        self.reason_edit = QLineEdit(self)
        self.reason_edit.setObjectName("fact_reason")
        self.reason_edit.setPlaceholderText("Reason (optional)")
        self.reason_edit.setAccessibleName("Reason (optional)")
        self.status = QLabel("", self)
        self.status.setObjectName("fact_reason_status")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(_WARN)
        self.status.hide()
        buttons = QDialogButtonBox(self)
        self.action_button = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        self.action_button.setObjectName("fact_reason_action")
        self.action_button.setDefault(True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.action_button.clicked.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        self.reason_edit.textChanged.connect(lambda *_a: self.status.hide())
        layout = QVBoxLayout(self)
        layout.addWidget(prompt_label)
        layout.addWidget(self.reason_edit)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        outcome = self._save_fn(self.reason_edit.text().strip() or None)
        if outcome.ok:
            self.accept()
        else:
            self.status.setText(outcome.message)
            self.status.show()


class FactTable(QWidget):
    shown_changed = Signal(bool)    # the table started or stopped having facts to show for the unit

    def __init__(self, intake, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fact_table")
        self._intake = intake
        self._unit: str | None = None
        self._row = None            # the scan-list SpectrumRow and ScanSetRow of the unit, when given
        self._set_row = None
        self._shown = False
        self._body: QWidget | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)
        intake.changed.connect(self._render)
        intake.worker_changed.connect(self._render)
        self._render()

    @property
    def has_facts(self) -> bool:
        """True while the table shows this unit's facts (so the stage-one fact lines are not repeated)."""
        return self._shown

    def set_unit(self, unit_key: str | None, row=None, set_row=None) -> None:
        """Show the facts of spectrum ``unit_key`` (``None``: a pooled row or nothing picked). ``row`` and
        ``set_row`` are its scan-list rows: the file's |Q| when the hkl is missing, the scan set's label."""
        self._unit, self._row, self._set_row = unit_key, row, set_row
        self._render()

    # ---- what the unit's facts say -------------------------------------------------------
    @property
    def _scan(self) -> str:
        return scan_prefix(self._unit)

    @property
    def _analyzer(self) -> str | None:
        return split_key(self._unit)[2]

    def _applies_to(self) -> str:
        if self._set_row is None:
            return "Applies to the whole scan set"
        return f"Applies to the whole scan set {self._set_row.label} ({_plural(len(self._set_row.spectra), 'analyzer')})"

    def _fact_cells(self, r) -> _Row:
        """One fact row of the read as a display row (no actions yet)."""
        unit = r.unit or ("rlu" if r.fact == "q" and r.file_value is not None else None)
        row = _Row(r.fact, _TITLES.get(r.fact, r.fact.replace("_", " ").capitalize()))
        if r.fact == "counts":
            row.files = counts_text(r.file_value)
            if r.user_value is True:
                row.yours = (_with("accepted", r.user_reason),
                             f"You accepted these counts: {r.user_reason}" if r.user_reason
                             else "You accepted these counts")
            row.used = ("as in the file", "Accepting the counts changes none of them")
            return row
        if r.file_value is None:
            row.files = ("—\nmissing", "missing")
        else:
            row.files = (f"{_value_text(r.file_value, unit)}\n{r.file_source}", r.file_source)
        if r.file_conflicts:
            row.files = (row.files[0], "; ".join(str(c) for c in r.file_conflicts))
        if r.user_value is not None:
            row.yours = (_with(_value_text(r.user_value, unit), r.user_reason),
                         f"Yours ({r.user_entry}): {r.user_reason}" if r.user_reason else f"Yours ({r.user_entry})")
        if r.used_source == "missing":
            named = _FALLBACKS.get(r.fallback, r.fallback) if r.fallback else None
            row.used = (f"—\n{named or 'missing'}", f"No value is in force; {named}" if named else "missing")
        else:
            whose = "yours" if r.used_source == "user" else "file"
            row.used = (f"{_value_text(r.used_value, unit)}\n{whose}", f"Used: {whose}")
        if r.disagreement:
            both = (f"Your value ({_value_text(r.user_value, unit)}) differs from the file's "
                    f"({_value_text(r.file_value, unit)}); {'yours' if r.used_source == 'user' else 'the file'}'s is used")
            row.warn, row.yours, row.used = True, (row.yours[0], both), (row.used[0], both)
        return row

    def _q_row(self, r) -> _Row:
        row = self._fact_cells(r)
        row.tip = FILES_ONLY
        if r.file_value is None:      # no hkl: the file's |Q|, as the scan list shows it
            q = None if self._row is None else self._row.q
            if q is not None and q.value is not None:
                where = f", {q.source}" if q.source and q.source != "missing" else ""
                row.files = (f"{_value_text(q.value, q.unit)}\n(|Q|{where})", NO_HKL)
            else:
                row.files = (row.files[0], NO_HKL)
            row.used = ("—\nno hkl", NO_HKL)
        return row

    def _analyzer_row(self, scan: str, analyzer: str) -> _Row:
        mine = {}
        for c in self._intake.conditions or ():
            if c.scan == scan and (c.condition == "excluded" or c.analyzer == analyzer):
                mine[c.condition] = c
        row = _Row("analyzer", f"Analyzer {analyzer}", tip=f"{analyzer} of scan {scan}")
        excluded, dead, accepted = mine.get("excluded"), mine.get("dead"), mine.get("counts_accepted")
        if excluded is not None:      # the scan's exclusion is the user's, shown here and not changed here
            why = _with("scan excluded", excluded.user_reason)
            row.yours, row.used, row.tip = (why, why.replace("\n", ": ")), ("left out", "The scan is excluded"), FILES_ONLY
            return row
        if dead is not None:
            why = _with("declared dead", dead.user_reason)
            row.yours, row.used = (why, why.replace("\n", ": ")), ("not used", "Declared dead")
            row.actions.append(_Action("In use", self._mark_in_use, "in_use", tip=f"Use {analyzer} again"))
        else:
            row.used = ("in use", "In use")
            row.actions.append(_Action("Mark dead", self._mark_dead, "mark_dead",
                                       tip=f"Leave {analyzer} out of this scan"))
        att = self._attention_for(scan, analyzer)
        if accepted is not None:
            row.actions.append(_Action("Withdraw acceptance", self._withdraw_counts, "withdraw_counts",
                                       tip="Take back your acceptance of these counts"))
        elif att is not None and att.action == "accept_counts":
            row.actions.append(_Action("Accept counts…", self._accept_counts, "accept_counts",
                                       tip="The counts may not be raw: accept them as they are"))
        return row

    def _attention_for(self, scan: str, analyzer: str):
        attention = self._intake.attention
        if attention is None or not attention.evaluated:
            return None
        sr = attention.scans.get(scan)
        return None if sr is None else sr.analyzers.get(analyzer)

    def _rows(self) -> list[_Row]:
        scan, analyzer = self._scan, self._analyzer
        mine = [r for r in (self._intake.fact_rows or ()) if r.scan == scan and r.analyzer in (None, analyzer)]
        by = {(r.fact, r.analyzer): r for r in mine}
        out: list[_Row] = []
        for fact in ("temperature", "slit"):
            r = by.get((fact, None))
            if r is None:
                continue
            row = self._fact_cells(r)
            row.title, row.tip = f"{row.title} — whole scan set", self._applies_to()
            row.actions = [
                _Action("Set my value…", lambda f=fact: self._set_value(f), f"set_{fact}", tip=self._applies_to()),
                _Action("Use the file's value", lambda f=fact: self._use_file_value(f), f"use_file_{fact}",
                        enabled=r.user_value is not None,
                        tip=f"Use the file's {fact} again. {self._applies_to()}" if r.user_value is not None
                        else NO_VALUE_OF_YOURS)]
            out.append(row)
        if (r := by.get(("q", analyzer))) is not None:
            out.append(self._q_row(r))
        if (r := by.get(("counts", analyzer))) is not None:
            row = self._fact_cells(r)
            row.tip = FILES_ONLY
            out.append(row)
        if mine:
            out.append(self._analyzer_row(scan, analyzer))
        for r in mine:
            if r.fact not in _TITLES:
                row = self._fact_cells(r)
                row.tip = FILES_ONLY
                out.append(row)
        return out

    # ---- drawing ---------------------------------------------------------------------------
    def _render(self) -> None:
        if self._body is not None:
            self._layout.removeWidget(self._body)
            self._body.setParent(None)      # out of the tree at once, destroyed on the next event pass
            self._body.deleteLater()
            self._body = None
        rows = self._rows() if self._unit is not None and self._analyzer is not None else []
        shown = bool(rows)
        self.setVisible(shown)
        if shown != self._shown:
            self._shown = shown
            self.shown_changed.emit(shown)
        if not shown:
            return
        self._body = QWidget(self)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)
        body.addWidget(self._heading("Facts"))
        body.addWidget(self._table(rows))
        line = _Cell(f"Temperature and slit apply to the whole scan set {self._set_label()}".rstrip() + ".",
                     self._body, muted=True)
        line.setObjectName("fact_applies_to")
        line.setToolTip(self._applies_to())
        body.addWidget(line)
        self._readiness(body)
        self._layout.addWidget(self._body)

    def _heading(self, text: str) -> QLabel:
        label = QLabel(text, self._body)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _set_label(self) -> str:
        if self._set_row is None:
            return ""
        return f"{self._set_row.label} ({_plural(len(self._set_row.spectra), 'analyzer')})"

    def _table(self, rows: list[_Row]) -> QWidget:
        table = QWidget(self._body)
        table.setObjectName("fact_grid")
        grid = QGridLayout(table)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(1)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        for col, name in enumerate(("From the files", "Yours", "Used")):
            head = _Cell(name, table, muted=True)
            head.setObjectName("fact_head")
            grid.addWidget(head, 0, col)
        n = 1
        for row in rows:
            title = ElidedLabel(row.title, table, Qt.TextElideMode.ElideRight)
            title.setObjectName(f"fact_title_{row.key}")
            font = title.font()
            font.setBold(True)
            title.setFont(font)
            title.setToolTip(row.tip or row.title)
            grid.addWidget(title, n, 0, 1, 3)
            for col, (name, (text, tip)) in enumerate((("files", row.files), ("yours", row.yours),
                                                       ("used", row.used))):
                cell = _Cell(text, table)
                cell.setObjectName(f"fact_{row.key}_{name}")
                cell.setToolTip(tip or row.tip)
                if row.warn and name != "files":
                    cell.setStyleSheet(_WARN)
                grid.addWidget(cell, n + 1, col)
            n += 2
            if row.actions:
                box = QWidget(table)
                stack = QVBoxLayout(box)
                stack.setContentsMargins(0, 0, 0, 2)
                stack.setSpacing(2)
                for a in row.actions:
                    button = _ActionButton(a.text, box)
                    button.setObjectName(f"fact_action_{a.name}")
                    button.setEnabled(a.enabled)
                    button.setToolTip(a.tip)
                    button.clicked.connect(lambda _checked=False, slot=a.slot: slot())
                    stack.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
                grid.addWidget(box, n, 0, 1, 3)
                n += 1
        return table

    def _readiness(self, body: QVBoxLayout) -> None:
        attention = self._intake.attention
        if attention is None:
            lines = ["Readiness: checking…"]
        elif not attention.evaluated:
            lines = [f"Readiness: not judged — {attention.note or 'not evaluated'}"]
        else:
            ar = self._attention_for(self._scan, self._analyzer)
            if ar is None:
                lines = ["Readiness: this analyzer is not in the import record"]
            else:
                lines = []
                for use in USES:
                    st = ar.uses[use]
                    why = st.reason or (_FALLBACKS.get(st.fallback, st.fallback) if st.fallback else None)
                    lines.append(f"{_USE_NAMES.get(use, use)}: {st.state}" + (f" — {why}" if why else ""))
                if ar.running:
                    lines.append("running")
                body.addWidget(self._heading("Readiness"))
        for text in lines:
            cell = _Cell(text, self._body)
            cell.setObjectName("fact_readiness")
            cell.setToolTip(text)
            body.addWidget(cell)

    # ---- the actions -----------------------------------------------------------------------
    def _run(self, dialog: QDialog) -> int:
        return dialog.exec()

    def _set_value(self, fact: str) -> None:
        self._run(SetValueDialog(self._intake, fact, [self._scan], self._applies_to(), self))

    def _use_file_value(self, fact: str) -> None:
        self._run(_ReasonDialog(
            "Use the file's value", f"Use the file's {fact} again. {self._applies_to()}.", "Use the file's value",
            lambda reason: self._intake.use_file_value(fact, [self._scan], reason), self))

    def _mark_dead(self) -> None:
        self._run(_ReasonDialog(
            "Mark analyzer dead", f"Leave {self._analyzer} out of this scan's analysis.", "Mark dead",
            lambda reason: self._intake.set_dead([self._unit], True, reason), self))

    def _mark_in_use(self) -> None:
        self._run(_ReasonDialog(
            "Analyzer in use", f"Use {self._analyzer} in this scan's analysis again.", "In use",
            lambda reason: self._intake.set_dead([self._unit], False, reason), self))

    def _accept_counts(self) -> None:
        self._run(_ReasonDialog(
            "Accept counts", f"The counts of {self._analyzer} may not be raw. Accept them as they are.",
            "Accept counts", lambda reason: self._intake.set_counts_accepted([self._unit], True, reason), self))

    def _withdraw_counts(self) -> None:
        self._run(_ReasonDialog(
            "Withdraw acceptance", f"Take back your acceptance of the counts of {self._analyzer}.",
            "Withdraw acceptance", lambda reason: self._intake.set_counts_accepted([self._unit], False, reason),
            self))

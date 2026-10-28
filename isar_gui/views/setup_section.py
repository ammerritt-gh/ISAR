"""SetupSection -- the top of the left column under the experiment strip: what was read, and the curves.

Two or three compact lines from the intake backend's import summary and settings:

* what was read: "2 scan sets · 56 analyzers read · 1 incomplete set · 3 files not read", where the
  incomplete sets and the files not read are links that open, in place, the list behind them (each set
  with its missing analyzers and their causes and its sidecar state; each file with its reason), or
  "Nothing read yet";
* where the resolution curves were found, with *Choose curves folder…*, and below it, in the warning colour, the
  last curve check's problem (one line, the full text on hover), a note when the saved folder is not
  checked yet, and each other ``rf/`` folder found beside new data with *Use this folder*.

The section only shows what the backend returns. *Choose curves folder…* leaves through ``locate_requested``, and the
window routes it to :func:`locate_resolution_folder`, the one route that saves a folder the user picks.
"""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from isar_gui.plain_words import NO_CURVES, NO_CURVES_TIP, curve_problem, plain_reason
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedButton, ElidedLabel

NOTHING_READ = "Nothing read yet. Add data files below."
NOT_BL43 = "No import summary: this experiment is not BL43LXU data."
POLICY_FILE = "card.policy.toml"
STALE_NOTE = "The curves folder has not been checked yet; it is checked when you add data or start the analysis."
COUNTS_LEGEND = ("A scan set is one scan with one file per analyzer; a spectrum is one analyzer of one set, "
                 "and 'analyzers read' counts the spectra.\nAn incomplete set has analyzer files that did not "
                 "arrive or could not be read. Click a count in blue to list them.")
LOCATE_TEXT = "Choose curves folder…"               # the setup line is tight at 1108 px; elsewhere there is room
LOCATE_LONG = "Choose resolution-curve folder…"     # attention list, readiness list, Details dialog
LOCATE_TIP = ("Pick the folder holding the measured resolution curves. It is saved with this experiment "
              "at once and the analysis fits with it.")
_SOURCE_WORDS = {"discovered": "found beside the data", "user": "chosen by you"}
_LIST_ROWS = 6                  # an opened list shows this many rows, then scrolls
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"
_QUIET = "color: #6b7280;"
RUN_TEXT = "Fast-pass all ready"
RUN_TIP = ("Add a fast pass for every ready analyzer file that has no current fast pass. They join the fast queue "
           "in the Processing panel and start as cores free up; a number in brackets is how many would be added now.")


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def resolution_text(summary, settings) -> tuple[str | None, bool]:
    """Where the resolution curves are, as ``(text, warn)``: ``"<folder> (found beside the data)"``,
    ``"not found"`` or ``"not found (<folder>, chosen by you)"`` in the warning colour when the last check
    found no curve there, or ``(None, False)`` when nothing is known (no card and nothing saved)."""
    curves = None if summary is None else summary.curves
    if curves is not None:
        if curves.source == "none":
            return "not found", True
        where = f"{curves.folder}, {_SOURCE_WORDS.get(curves.source, curves.source)}"
        if curves.n_curves == 0 and curves.problem is not None:     # checked, and nothing usable there
            return f"not found ({where})", True
        return f"{curves.folder} ({_SOURCE_WORDS.get(curves.source, curves.source)})", False
    if settings is not None and settings.resolution_folder is not None:
        return f"{settings.resolution_folder.path} (chosen by you)", False
    return None, False


def _start_folder(intake) -> str:
    settings, summary = intake.settings, intake.summary
    if settings is not None and settings.resolution_folder is not None:
        return settings.resolution_folder.path
    curves = None if summary is None else summary.curves
    return (curves.folder if curves is not None and curves.folder else "") or ""


def locate_resolution_folder(parent: QWidget, intake):
    """*Choose resolution-curve folder…*, wherever it is clicked: pick a folder and save it at once as the user's resolution
    folder. Returns the :class:`~isar_gui.viewmodels.intake.SaveOutcome`, or ``None`` when cancelled."""
    folder = QFileDialog.getExistingDirectory(parent, "Choose the resolution-curve folder",
                                              _start_folder(intake))
    if not folder:
        return None
    return intake.save_resolution_folder(Path(folder))


class _Line(ElidedLabel):
    """A free-text line of the section: one line, shortened with "…" to the column's width, the full
    text as its tooltip. Two wrapped lines left the scan list 390 px high at 1108 x 851 (offscreen)."""

    def __init__(self, parent: QWidget | None = None, *, warn: bool = False, quiet: bool = False) -> None:
        super().__init__("", parent, Qt.TextElideMode.ElideRight)
        if warn:
            self.setStyleSheet(_WARN)
        elif quiet:
            self.setStyleSheet(_QUIET)

    def set_line(self, text: str | None, tip: str | None = None) -> None:
        self.set_full_text(text or "")
        if tip:
            self.setToolTip(tip)
        self.setVisible(bool(text))


class _RunButton(ElidedButton):
    """*Fast-pass all ready (N)*: when the row is short the count in brackets goes before the words are cut, and
    the button beside it shrinks before the words do."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(RUN_TEXT) + self._PAD)

    def _show(self) -> None:
        room = max(self.width() - self._PAD, 1)
        text = self._full
        if self.fontMetrics().horizontalAdvance(text) > room:
            text = RUN_TEXT
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, room))

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setAccessibleName(text)
        self._show()
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        QPushButton.resizeEvent(self, event)
        self._show()


class SetupSection(QWidget):
    locate_requested = Signal()
    run_requested = Signal()            # "Fast-pass all ready (N)" -> MainWindow.start_analysis (the button holds no logic)

    def __init__(self, intake, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("setup_section")
        self._intake = intake
        self._root: Path | None = None
        self._open_list: str | None = None      # "incomplete" | "not_read" while its list is open
        self._listed: tuple[str | None, list[tuple[str, str]]] = (None, [])   # what the list widget holds
        self._curves_only = False               # the Run reason is the missing resolution curves

        self._summary = QLabel("", self)
        self._summary.setObjectName("setup_summary")
        self._summary.setWordWrap(True)
        self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                              | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self._summary.linkActivated.connect(self.toggle_list)
        self._list = QListWidget(self)
        self._list.setObjectName("setup_list")
        self._list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._list.setWordWrap(False)
        self._list.setUniformItemSizes(True)
        self._list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._list.hide()

        self._resolution = ElidedLabel("", self)
        self._resolution.setObjectName("setup_resolution")
        self._locate = ElidedButton(LOCATE_TEXT, self)       # shares its row with Run: it gives way first
        self._locate.setObjectName("setup_locate")
        self._locate.setToolTip(LOCATE_TIP)
        self._locate.clicked.connect(self.locate_requested)
        self._run = _RunButton(RUN_TEXT, self)
        self._run.setObjectName("setup_run")
        self._run.setToolTip(RUN_TIP)
        self._run.clicked.connect(self.run_requested)
        self._run_note = _Line(self, quiet=True)          # how many are ready, or why Run is off
        self._run_note.setObjectName("setup_run_note")
        self._problem = _Line(self, warn=True)
        self._problem.setObjectName("setup_problem")
        self._stale = _Line(self)
        self._stale.setObjectName("setup_stale")
        self._record_problem = _Line(self, warn=True)
        self._record_problem.setObjectName("setup_record_problem")
        self._outcome = _Line(self, warn=True)
        self._outcome.setObjectName("setup_outcome")
        self._others = QVBoxLayout()
        self._others.setContentsMargins(0, 0, 0, 0)
        self._others.setSpacing(2)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(0)       # the Run row costs a line; the 400 px scan list has no slack
        self._grid.setColumnStretch(0, 1)
        self._grid.addWidget(self._summary, 0, 0)
        self._grid.addWidget(self._resolution, 1, 0)
        # Under the path, not beside it: the button names its object, and beside it the path got no room at 1108 px.
        # Fast-pass all ready leads the row, so it is on screen at every window size whatever the right panel does.
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self._run, 0)
        buttons.addWidget(self._locate, 0)
        buttons.addStretch(1)
        self._grid.addLayout(buttons, 2, 0)
        self._grid.addWidget(self._run_note, 3, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(1)
        layout.addLayout(self._grid)
        layout.addWidget(self._list)
        for line in (self._problem, self._stale, self._record_problem, self._outcome):
            layout.addWidget(line)
        layout.addLayout(self._others)

        intake.changed.connect(self._render)
        self._render()

    def set_run_state(self, enabled: bool, note: str, *, curves_only: bool = False, label: str = RUN_TEXT) -> None:
        """The button on or off, with the muted line beside it: the readiness count, or the reason it is off.
        ``label`` carries the count of analyzer files a press would add. ``curves_only``: the reason is the
        missing resolution curves, which the amber line says."""
        self._run.set_full_text(label)
        self._run.setEnabled(enabled)
        self._run.setToolTip(RUN_TIP if enabled else (note or RUN_TIP))
        self._run_note.set_line(note, note)
        self._curves_only = curves_only
        self._fit_run_note()

    def _fit_run_note(self) -> None:
        # At 1108 px a second line squeezes the counts, so the amber curves line stands in for the reason when it
        # is that reason (a ready count is no reason at all); any other reason keeps its line.
        said_above = self._problem.isVisibleTo(self) and (
            self._run.isEnabled() or (self._curves_only and self._problem.full_text() == NO_CURVES))
        self._run_note.setVisible(bool(self._run_note.full_text()) and not said_above)

    # ---- the outcome of a save made from here --------------------------------------------
    def show_save_outcome(self, outcome) -> None:
        """Show a refused folder save (``outcome.ok`` False) under the section; ok or ``None`` clears it."""
        self._outcome.set_line(None if outcome is None or outcome.ok else outcome.message)

    def toggle_list(self, which: str) -> None:
        """Open (or close) the list behind a count: ``"incomplete"`` or ``"not_read"``."""
        self._open_list = None if self._open_list == which else which
        self._render()

    # ---- rendering -----------------------------------------------------------------------
    def _render(self) -> None:
        intake = self._intake
        root, summary, settings = intake.root, intake.summary, intake.settings
        if root != self._root:              # another experiment: nothing of the last one stays open
            self._root, self._open_list = root, None
            self._outcome.set_line(None)
        has_card = root is not None and (Path(root) / POLICY_FILE).exists()
        curves = None if summary is None else summary.curves
        bl43 = root is not None and not (has_card and summary is not None and curves is None
                                         and summary.record_problem is None)

        warn_summary = record_in_summary = False
        if root is None:
            text = NOTHING_READ
        elif summary is None:
            text, warn_summary = f"What was read could not be shown: {intake.error or 'unknown error'}", True
        elif not bl43:
            text = NOT_BL43
        elif summary.n_scans == 0 and not summary.files_not_read:
            if has_card and summary.record_problem:      # an experiment that received files, not an empty one
                text, warn_summary, record_in_summary = html.escape(summary.record_problem, quote=False), True, True
            else:
                text = NOTHING_READ
        else:
            text = self._counts_html(summary)
        self._summary.setText(text)
        self._summary.setToolTip(COUNTS_LEGEND if text not in (NOTHING_READ, NOT_BL43) else "")
        self._summary.setStyleSheet(_WARN if warn_summary else "")
        self._render_list(summary if bl43 else None)

        where, warn = resolution_text(summary, settings)
        resolution = None if where is None else f"Resolution curves: {where}"
        self._locate.setVisible(bl43)
        self._resolution.set_full_text(resolution or "")
        self._resolution.setStyleSheet(_WARN if warn else "")
        # "not found" and the sentence under it say the same: the sentence stands alone (the row is kept for the tests
        # and the screen reader, hidden) so the section keeps the height it had before the button got its own row.
        self._resolution.setVisible(bool(resolution) and bl43 and curve_problem(
            curves.problem if curves is not None else None) != NO_CURVES)
        if curves is not None:
            self._resolution.setToolTip(f"{resolution}\n{self._checked_text(curves)}")

        problem = curves.problem if bl43 and curves is not None else None
        shown = curve_problem(problem)
        self._problem.set_line(shown, f"{NO_CURVES_TIP}\n\n{problem}" if shown != problem else None)
        self._stale.set_line(STALE_NOTE if bl43 and curves is not None and curves.check_stale else None)
        record = None if summary is None else summary.record_problem
        self._record_problem.set_line(record if has_card and not record_in_summary else None)
        self._render_others(curves.other_folders if bl43 and curves is not None else ())
        self._fit_run_note()

    @staticmethod
    def _counts_html(summary) -> str:
        parts = [_n(summary.n_scans, "scan set", "scan sets"),
                 _n(summary.n_analyzers, "analyzer read", "analyzers read")]
        for key, count, one, many in (("incomplete", len(summary.incomplete_sets), "incomplete set",
                                       "incomplete sets"),
                                      ("not_read", len(summary.files_not_read), "file not read",
                                       "files not read")):
            words = _n(count, one, many)
            parts.append(f'<a href="{key}">{words}</a>' if count else words)
        return " · ".join(parts)

    @staticmethod
    def _checked_text(curves) -> str:
        if curves.checked_at is None:
            return "Not checked yet: the folder is checked when you add data or start the analysis."
        return (f"Last checked {curves.checked_at}. A curve file added or replaced in the folder since "
                "then is seen at the next import or when analysis starts.")

    def _render_list(self, summary) -> None:
        items: list[tuple[str, str]] = []          # (row text, tooltip)
        if summary is not None and self._open_list == "incomplete":
            for s in summary.incomplete_sets:
                missing = ", ".join(f"{a} ({cause})" for a, cause in s.missing_analyzers.items())
                bits = [f"missing {missing}"] if missing else []
                if s.sidecar in ("missing", "unreadable"):
                    bits.append(f"sidecar {s.sidecar}" + (f" ({s.sidecar_reason})" if s.sidecar_reason else ""))
                line = f"{s.set or s.scan}: {'; '.join(bits) or 'incomplete'}"
                items.append((line, line))
        elif summary is not None and self._open_list == "not_read":
            # the full path is the tooltip: two folders can each hold a notes.txt
            items = [(f"{Path(f.path).name}: {plain_reason(f.reason)}", f"{f.path}\n{f.reason}")
                     for f in summary.files_not_read]
        if not items:
            self._open_list = None
        if (self._open_list, items) != self._listed:     # a re-read that changed nothing leaves the list alone
            bar = self._list.verticalScrollBar()
            keep = bar.value() if self._open_list == self._listed[0] else 0   # another list starts at the top
            self._list.clear()
            for line, tip in items:
                item = QListWidgetItem(line)
                item.setToolTip(tip)
                self._list.addItem(item)
            if items:
                rows = min(len(items), _LIST_ROWS)
                self._list.setFixedHeight(rows * max(self._list.sizeHintForRow(0), 1)
                                          + 2 * self._list.frameWidth())
                self._list.doItemsLayout()               # the scroll range is current before the place is restored
                bar.setValue(keep)
            self._listed = (self._open_list, items)
        self._list.setVisible(bool(items))

    def _render_others(self, others) -> None:
        while self._others.count():
            item = self._others.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                row = item.layout()
                while row.count():
                    w = row.takeAt(0).widget()
                    if w is not None:
                        w.deleteLater()
        for o in others:
            row = QHBoxLayout()
            row.setSpacing(6)
            label = ElidedLabel(f"Another rf/ folder with curves was found beside new data: {o.path}", self)
            label.setObjectName("setup_other_folder")
            label.setToolTip(f"{o.path} (beside {o.beside}) also holds curves; it is not used unless you choose it")
            use = QPushButton("Use this curves folder", self)
            use.setObjectName("setup_use_other")
            use.setToolTip("Replace the saved resolution-curve folder with this one; the analysis will refit "
                           f"with it.\n{o.path}")
            use.clicked.connect(lambda _checked=False, p=o.path: self._use(p))
            row.addWidget(label, 1)
            row.addWidget(use, 0)
            self._others.addLayout(row)

    def _use(self, path: str) -> None:
        self.show_save_outcome(self._intake.save_resolution_folder(Path(path)))

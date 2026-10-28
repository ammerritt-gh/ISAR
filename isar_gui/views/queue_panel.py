"""QueuePanel -- the analysis queue as the Processing panel shows it, and the words the rail uses.

The panel shows what the :class:`~isar_gui.queue_controller.QueueController` reports and sends the user's
actions back to it; it computes and fits nothing. From the top: the switch that fast-passes new scans as they
arrive, the selection with the two add buttons, a banner when the queue is paused or cannot start, the fast and
the deep queue (each under its scan-set headings), the Finished list, and a footer with Pause or Resume. Only
the queue part scrolls; the switch, the selection and the add buttons stay in view.

The threshold for every line here is a reader who knows inelastic scattering and the beamtime but has never
used this window, at 3 AM, with only the screen: every control, word, count and mark has a hover in plain words,
a control that cannot be used says why in a visible line, and the essential actions have visible labels.

Progress is drawn from the steps the engine reports and the GUI's own clock (design step 12a): no percentage,
no estimate, and no verdict that an item has stalled. A background update rebuilds the queue part in place and
keeps its scroll position; it never touches the user's selection (only *Show* does).
"""
from __future__ import annotations

import time
from collections import Counter

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from isar.workspace_api import currentness as _currentness
from isar_gui.theme import QUEUE_STATES, TONE_COLOR, Tone, dot_icon

_ITEMS_SHOWN = 6            # a display setting: items shown per scan-set group before "… N more"
_now = time.monotonic       # the GUI's clock for elapsed time (the controller stamps events with the same one)
_COUNT_ORDER = ("done", "done_warning", "held", "failed", "stopped")
_FAST_PHASES = {"seed": "starting values", "evidence": "comparing models", "audits": "checking the fit",
                "audit": "checking the fit", "classify": "choosing the peak count", "errors": "error bars"}
_WAITS_FOR_FAST = "waits for this analyzer's fast pass"
_WARN_STYLE = f"color: {TONE_COLOR[Tone.WARN]};"

_FAST_TIP = ("Fast passes: the quick fit that tries every peak count and says how settled the count is. "
             "Whenever cores free up, the oldest waiting fast pass starts before any deep pass.")
_DEEP_TIP = ("Deep passes: the slower, sampled fit for a spectrum whose fast pass is not settled. A running deep "
             "pass is never interrupted by the fast queue; only you can stop it. Deep passes are only ever added by you.")
_FINISHED_TIP = ("What has finished since you opened this experiment: one line per scan set and pass, then each "
                 "analyzer that needs a look (held, failed, stopped or done with a warning) and each finished deep "
                 "pass. The saved results themselves are in the scan list.")


def fmt_elapsed(seconds: float) -> str:
    """``3:00``, and ``1:02:05`` from an hour on."""
    minutes, secs = divmod(int(max(0.0, seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def progress_text(item, now: float) -> tuple[str, str]:
    """(the progress line of a running item, its hover). Only steps the engine reported, and the time since the
    latest one; ``item.progress`` is the engine's event as received."""
    p = item.progress
    if p is None:
        return "starting", "ISAR has started this item and has not reported a step yet."
    since = "" if item.step_started_at is None else fmt_elapsed(now - item.step_started_at)
    phase = p.get("phase")
    if p.get("pass") == "deep":
        if phase == "sampling":
            stage, stages, step, of = p.get("stage"), p.get("stages"), p.get("step"), p.get("of")
            where = "stage" if stage is None else f"stage {stage}"
            if stage is not None and stages is not None and stage < stages:
                where += f" of {stages}"          # the last stage's length is known only when it starts
            text = f"{where} · model {step} of {of}" if step is not None and of is not None else where
            if since:
                text += f" · this model {since}"
            tip = ("The deep pass works through its models stage by stage. \"This model\" is how long ISAR has "
                   "been on the current step; a model taking far longer than the earlier ones may be stuck, and "
                   "Stop ends it. The number of models in the last stage is known only when it starts.")
            return text, tip
        text = "error bars" + (f" · {since}" if since else "")
        return text, "The deep pass has chosen its model and is fitting it once more to get the error bars."
    # a fast pass; a deep item that is doing its fast pass first reads the same, with a prefix
    if phase == "ladder":
        step, of = p.get("step"), p.get("of")
        if step is None:
            text = "trying peak counts"
        else:
            tried = "no peaks" if step == 0 else f"{step} {_plural(step, 'peak', 'peaks')}"
            text = f"trying {tried}" + (f" (up to {of})" if of is not None else "")
    else:
        text = _FAST_PHASES.get(phase, str(phase or "working"))
    if since:
        text += f" · {since}"
    if item.pass_kind == "deep":
        text = f"fast pass first: {text}"
    tip = ("The fast pass tries one peak count after another, up to a ceiling; it may stop before the ceiling. "
           "The time is how long ISAR has been on this step.")
    return text, tip


def rail_status(view) -> tuple[str, str]:
    """(what is happening, what needs a look) for the rail and its hover; ``""`` for either when nothing."""
    running = sum(view.n_running.values())
    waiting = sum(view.n_waiting.values())
    parts = ["Paused"] if view.paused else []
    if running:
        parts.append(f"{running} running")
    if view.n_saving:
        parts.append(f"{view.n_saving} saving")
    if waiting:
        parts.append(f"{waiting} waiting")
    look = []
    if view.n_look:
        look.append(f"{view.n_look} {_plural(view.n_look, 'needs', 'need')} a look")
    if view.service == "failed":
        look.append("analysis process failed")
    return " · ".join(parts), " · ".join(look)


def _confirm_stop(parent: QWidget, title: str, text: str, stop_label: str) -> bool:
    """The modal that asks before a deep pass is stopped (a test replaces this)."""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, parent)
    stop = box.addButton(stop_label, QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.exec()
    return box.clickedButton() is stop


def _take_the_width_offered(lab: QLabel) -> None:
    """A wrapping label asks for no width of its own, so it never pins the panel; the policy is changed in place
    because a fresh one would drop the height-for-width flag that word wrap set."""
    policy = lab.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
    lab.setSizePolicy(policy)


def _label(text: str, tip: str, *, bold: bool = False, wrap: bool = True, name: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setToolTip(tip)
    lab.setWordWrap(wrap)
    if bold:
        font = lab.font()
        font.setBold(True)
        lab.setFont(font)
    if name:
        lab.setObjectName(name)
    if wrap:
        _take_the_width_offered(lab)
    return lab


def _button(text: str, tip: str, name: str, *, tool: bool = False) -> QPushButton | QToolButton:
    button = QToolButton() if tool else QPushButton(text)
    if tool:
        button.setText(text)
        button.setAutoRaise(True)
    button.setToolTip(tip)
    button.setObjectName(name)
    return button


class _SwitchLabel(QLabel):
    """The switch's visible words. A QCheckBox never wraps its text, so at a narrow panel it would pin the panel's
    width; the check box carries no text and this wrapping label, which a click toggles, carries it."""

    def __init__(self, text: str, box: QCheckBox) -> None:
        super().__init__(text)
        self._box = box
        self.setWordWrap(True)
        _take_the_width_offered(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton and self._box.isEnabled():
            self._box.click()
        super().mouseReleaseEvent(event)


class QueuePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("queue_panel")
        self._controller = None
        self._expanded: set[tuple[str, str]] = set()      # (queue, scan set) groups shown in full
        self._live: dict[str, tuple[QLabel, QLabel, object]] = {}   # item_id -> (progress, elapsed, ItemView)
        self._shape: object = object()            # what the rows were last built from (see _signature)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_labels)
        self._soon = QTimer(self)                 # coalesces a burst of `changed` into one rebuild
        self._soon.setSingleShot(True)
        self._soon.setInterval(0)
        self._soon.timeout.connect(self.refresh)
        self._keep = 0                            # the scroll position a rebuild restores
        self._restore = QTimer(self)              # the range is final only after the next layout pass
        self._restore.setSingleShot(True)
        self._restore.setInterval(0)
        self._restore.timeout.connect(lambda: self._scroll.verticalScrollBar().setValue(self._keep))

        self._auto = QCheckBox()
        self._auto.setObjectName("queue_auto")
        self._auto.setAccessibleName("Fast-pass new scans as they arrive")
        self._auto.clicked.connect(self._on_auto)
        self._auto_text = _SwitchLabel("Fast-pass new scans as they arrive", self._auto)
        self._auto_text.setObjectName("queue_auto_text")
        self._auto_note = _label("", "", name="queue_auto_note")
        self._selection = _label("", "", name="queue_selection")
        self._add_caption = _label("Add to queue:", "The two buttons add the analyzers selected in the scan list "
                                   "to the fast or the deep queue. Selecting alone never queues anything.")
        self._fast = _button("Fast pass", "", "queue_add_fast")
        self._deep = _button("Deep pass", "", "queue_add_deep")
        self._fast.clicked.connect(lambda: self._add("fast"))
        self._deep.clicked.connect(lambda: self._add("deep"))
        self._add_reason = _label("", "Why the Fast pass and Deep pass buttons cannot be used now.",
                                  name="queue_add_reason")
        self._banner = QWidget()
        self._banner.setObjectName("queue_banner")
        self._banner_text = _label("", "Why nothing new is starting.", name="queue_banner_text")
        self._banner_resume = _button("Resume", "Lets the queue start waiting analyzers again.", "queue_banner_resume")
        self._banner_resume.clicked.connect(self._pause_or_resume)
        banner = QHBoxLayout(self._banner)
        banner.setContentsMargins(0, 0, 0, 0)
        banner.addWidget(self._banner_text, 1)
        banner.addWidget(self._banner_resume, 0)

        self._content = QWidget()
        self._content.setObjectName("queue_content")
        self._rows = QVBoxLayout(self._content)
        self._rows.setContentsMargins(0, 0, 4, 0)
        self._rows.setSpacing(2)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("queue_scroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._content)

        self._footer_text = _label("", "", name="queue_footer_text")
        self._pause = _button("Pause", "", "queue_pause")
        self._pause.clicked.connect(self._pause_or_resume)

        switch = QHBoxLayout()
        switch.addWidget(self._auto, 0, Qt.AlignmentFlag.AlignTop)
        switch.addWidget(self._auto_text, 1)
        add_row = QHBoxLayout()
        add_row.addWidget(self._fast, 1)
        add_row.addWidget(self._deep, 1)
        foot = QHBoxLayout()
        foot.addWidget(self._footer_text, 1)
        foot.addWidget(self._pause, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(switch)
        for part in (self._auto_note, self._selection, self._add_caption):
            layout.addWidget(part)
        layout.addLayout(add_row)
        layout.addWidget(self._add_reason)
        layout.addWidget(self._banner)
        layout.addWidget(self._scroll, 1)
        layout.addLayout(foot)
        self.refresh()

    # ---- the controller --------------------------------------------------------------------------------
    def set_controller(self, controller) -> None:
        """The controller to show and drive (``None``: none). Group expansions belong to one experiment."""
        if self._controller is not None:
            self._controller.changed.disconnect(self._schedule)
        self._controller = controller
        self._expanded.clear()
        self._shape = object()                    # another controller's rows are never reused
        if controller is not None:
            controller.changed.connect(self._schedule)
        self.refresh()

    def _schedule(self) -> None:
        if not self._soon.isActive():
            self._soon.start()

    # ---- actions ---------------------------------------------------------------------------------------
    def _on_auto(self, on: bool) -> None:
        if self._controller is not None:
            self._controller.set_auto_fast(bool(on))

    def _add(self, pass_kind: str) -> None:
        if self._controller is not None:
            self._controller.add_selection(pass_kind)

    def _pause_or_resume(self) -> None:
        c = self._controller
        if c is not None:
            c.resume() if c.view().paused else c.pause()

    def _remove(self, item) -> None:
        if self._controller is not None:
            self._controller.remove(item.item_id)         # asks nothing: one click on the add button undoes it

    def _stop(self, item) -> None:
        if self._controller is None:
            return
        item = self._live.get(item.item_id, (None, None, item))[2]       # a row that was not rebuilt has newer data
        if item.pass_kind == "deep":
            if item.progress is not None and item.progress.get("pass") == "fast":
                lost = f"{item.analyzer} is still doing the fast pass its deep pass starts with."
                tail = "."
            else:
                lost = (f"{item.analyzer} has been in its deep pass for "
                        f"{max(0, int((_now() - item.started_at) // 60))} min." if item.started_at is not None
                        else f"{item.analyzer} is in its deep pass.")
                tail = "; its fast-pass result stays once it has been saved."
            text = f"{lost} Stopping now discards that work{tail}"
            if not _confirm_stop(self, "Stop the deep pass?", text, f"Stop {item.analyzer}"):
                return
        self._controller.stop(item.item_id)

    def _show(self, item) -> None:
        if self._controller is not None:
            self._controller.show(item.unit_key)

    def _toggle(self, key: tuple[str, str]) -> None:
        self._expanded ^= {key}
        self.refresh()

    # ---- drawing ---------------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Redraw everything from the controller now (a change normally gets here through ``_schedule``)."""
        self._soon.stop()
        c = self._controller
        view = c.view() if c is not None else None
        sel = c.selection() if c is not None else None
        bound = view is not None and view.bound
        self._head(c, view, sel, bound)
        self._banner_for(view)
        self._queues(view)
        self._foot(view, bound)

    def _head(self, c, view, sel, bound: bool) -> None:
        self._auto.setEnabled(bound)
        self._auto_text.setEnabled(bound)
        tip = (
            "When on, every analyzer file ISAR reads in from now on (from the watched folder or a drop) is added to "
            "the fast queue and marked auto. It starts on for a new experiment and belongs to this experiment. It "
            "never adds deep passes, never touches what you added yourself, and never sweeps back over older files."
            if bound else "Open or create an experiment first.")
        self._auto.setToolTip(tip)
        self._auto_text.setToolTip(tip)
        if c is not None:
            self._auto.setChecked(c.auto_fast())
        self._auto_note.setText("For this experiment. Deep passes are only ever added by you." if bound
                                else "No experiment is open.")
        self._auto_note.setToolTip("The switch above belongs to the open experiment. Deep passes are slow, so only "
                                   "you ever queue one.")
        shown = sel.text if sel is not None and sel.keys else "No analyzers selected"
        if sel is not None and sel.n_pooled:
            shown += f" ({sel.n_pooled} pooled {_plural(sel.n_pooled, 'master', 'masters')} not queued)"
        self._selection.setText(shown)
        self._selection.setToolTip("The analyzer files highlighted in the scan list. Pooled masters are not "
                                   "analyzer files and cannot be queued.")
        can_add = bound and sel is not None and bool(sel.keys)
        reason = ""
        if not bound:
            reason = "Open or create an experiment to queue analysis."
        elif not can_add:
            reason = ("Pooled masters cannot be queued: select analyzer files in the scan list."
                      if sel is not None and sel.n_pooled else
                      "Select analyzer files in the scan list, then press Fast pass or Deep pass.")
        for button, pass_name, what in ((self._fast, "fast", "a fast pass"), (self._deep, "deep", "a deep pass")):
            button.setEnabled(can_add)
            button.setToolTip(reason or (
                f"Adds {what} for each selected analyzer file. Files that already have a current {pass_name} pass, "
                f"or are already waiting or running for it, are left out, and the Log says which."
                + (" Use it for a spectrum whose fast pass is not settled; it is slower." if pass_name == "deep" else "")))
        self._add_reason.setText(reason)
        self._add_reason.setVisible(bool(reason))

    def _banner_for(self, view) -> None:
        lines = []
        if view is not None and view.service == "failed" and view.service_error:
            lines.append(view.service_error)
        if view is not None and view.paused:
            busy = sum(view.n_running.values()) + view.n_saving
            lines.append(f"Paused. {busy} {_plural(busy, 'analyzer', 'analyzers')} finishing; nothing new starts; "
                         f"{sum(view.n_waiting.values())} waiting.")
        elif view is not None and view.blocked:
            lines.append(f"Waiting: {view.blocked}. Nothing starts until it ends.")
        self._banner_text.setText("\n".join(lines))
        self._banner_text.setStyleSheet(_WARN_STYLE)
        self._banner_resume.setVisible(view is not None and view.paused)
        self._banner.setVisible(bool(lines))

    def _foot(self, view, bound: bool) -> None:
        if not bound:
            text, tip = "No experiment open", "Open an experiment to use the analysis queue."
        else:
            running = sum(view.n_running.values())
            text = (f"{running} {_plural(running, 'analyzer', 'analyzers')} running on {view.cores_in_use} "
                    f"{_plural(view.cores_in_use, 'core', 'cores')}" if running else "Nothing running")
            if view.n_saving:
                text += f" · {view.n_saving} saving"
            tip = (f"{view.cores_in_use} of the {view.cores} cores ISAR may use are busy. Change the limit under "
                   "Config > Cores… in the top row.")
        self._footer_text.setText(text)
        self._footer_text.setToolTip(tip)
        paused = bound and view.paused
        self._pause.setText("Resume" if paused else "Pause")
        self._pause.setEnabled(bound)
        self._pause.setToolTip(
            "Open an experiment first." if not bound else
            "Lets the queue start waiting analyzers again." if paused else
            "Lets running analyzers finish and starts nothing new until you press Resume. New arrivals still join "
            "the queue and wait. Use it before renaming the experiment folder.")

    def _signature(self, view):
        """Everything a rebuilt row would show except the running items' progress and clocks, which live in `_live`.
        Equal signatures mean the widgets stand; a hover, a press or the focus on one is then never lost."""
        if view is None or not view.bound:
            return None

        def items(its):
            return tuple((i.item_id, i.status, i.reason, i.runnable) for i in its)

        def queue(groups):
            return tuple((g.set_key, g.set_label, g.automatic, g.n_done, g.n_total, items(g.items)) for g in groups)

        finished = tuple((g.set_key, g.set_label, g.pass_kind, tuple(g.counts.items()), items(g.look))
                         for g in view.finished)
        return (queue(view.fast), queue(view.deep), finished, frozenset(self._expanded),
                _currentness.deep_calibration_note())

    def _queues(self, view) -> None:
        self._expanded &= {(kind, g.set_key) for kind, groups in (("fast", view.fast), ("deep", view.deep))
                           for g in groups} if view is not None else set()
        shape = self._signature(view)
        if shape == self._shape:
            if shape is not None:                 # swap in the newer progress; the tick and this call redraw it
                fresh = {i.item_id: i for groups in (view.fast, view.deep) for g in groups for i in g.items}
                for iid, (progress, elapsed, old) in list(self._live.items()):
                    self._live[iid] = (progress, elapsed, fresh.get(iid, old))
                self._tick_labels()
            return
        self._shape = shape
        bar = self._scroll.verticalScrollBar()
        keep = bar.value()
        self._content.setUpdatesEnabled(False)
        while self._rows.count():
            part = self._rows.takeAt(0).widget()
            if part is not None:
                part.setParent(None)
                part.deleteLater()
        self._live.clear()
        now = _now()
        if view is not None and view.bound:
            self._queue("fast", "Fast queue", _FAST_TIP, view.fast, view, now)
            self._queue("deep", "Deep queue", _DEEP_TIP, view.deep, view, now)
            self._finished(view, now)
        else:
            self._rows.addWidget(_label("Nothing queued: no experiment is open.",
                                        "Open an experiment to see its analysis queue here."))
        self._rows.addStretch(1)
        self._content.setUpdatesEnabled(True)
        self._content.layout().activate()
        bar.setValue(keep)
        self._keep = keep
        self._restore.start()
        if self._live and not self._tick.isActive():
            self._tick.start()
        elif not self._live:
            self._tick.stop()

    def _heading(self, text: str, tip: str, counts: str, counts_tip: str, name: str) -> QWidget:
        row = QWidget()
        row.setObjectName(name)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(_label(text, tip, bold=True, wrap=False), 0)
        if counts:
            box.addWidget(_label(counts, counts_tip, name=f"{name}_counts"), 1)
        else:
            box.addStretch(1)
        return row

    def _queue(self, kind: str, title: str, tip: str, groups, view, now: float) -> None:
        running, waiting = view.n_running[kind], view.n_waiting[kind]
        saving = sum(1 for g in groups for i in g.items if i.status == "saving")
        parts = ([f"{running} running"] if running else []) + ([f"{saving} saving"] if saving else []) + (
            [f"{waiting} waiting"] if waiting else [])
        self._rows.addWidget(self._heading(
            title, tip, " · ".join(parts) or "nothing queued",
            "How much work was asked of this queue: items running now, items being saved, and items waiting for "
            "cores.", f"queue_{kind}_heading"))
        if kind == "deep":
            note = _currentness.deep_calibration_note()
            if note:
                self._rows.addWidget(_label(
                    note, "This applies to every deep result, not to any one item. It is why the scan list marks "
                          "deep counts (provisional): the deep pass's sampler is not yet calibrated, so its "
                          "probabilities are not yet to be trusted.", name="queue_deep_note"))
        if not groups:
            self._rows.addWidget(_label("Nothing here", f"No {kind} passes are waiting or running."))
        for group in groups:
            self._group(kind, group, now)

    def _group(self, kind: str, group, now: float) -> None:
        auto = " · auto" if group.automatic else ""
        self._rows.addWidget(_label(
            f"{group.set_label}{auto} · {group.n_done} of {group.n_total} done",
            f"{group.n_done} of the {group.n_total} analyzer files of this scan set added for a {kind} pass since "
            "you opened the experiment are saved. "
            + ("Auto: ISAR added them as they arrived. " if group.automatic else "") + "The analyzers below travel "
            "together only as a heading; each is added, stopped and finished on its own.",
            bold=True, name="queue_group"))
        key = (kind, group.set_key)
        items = group.items
        full = key in self._expanded or len(items) <= _ITEMS_SHOWN
        for item in (items if full else items[:_ITEMS_SHOWN]):
            self._rows.addWidget(self._item_row(item, now))
        if len(items) > _ITEMS_SHOWN:
            if full:
                text, tip = "Show fewer", "Collapses this scan set to its first few analyzers"
            else:
                rest = Counter(i.status for i in items[_ITEMS_SHOWN:])
                names = [f"{rest[s]} {QUEUE_STATES[s][0]}" for s in ("running", "saving", "waiting") if rest[s]]
                text = (f"… {sum(rest.values())} more " + names[0].split(" ", 1)[1] if len(names) == 1 else
                        f"… {sum(rest.values())} more: " + ", ".join(names))
                tip = "Show every analyzer of this scan set"
            more = _button(text, tip, "queue_more")
            more.setFlat(True)
            more.setStyleSheet("text-align: left;")
            more.clicked.connect(lambda _=False, k=key: self._toggle(k))
            self._rows.addWidget(more)

    def _item_row(self, item, now: float) -> QWidget:
        word, tone, meaning = QUEUE_STATES[item.status]
        row = QWidget()
        row.setObjectName(f"queue_row_{item.item_id}")
        grid = QGridLayout(row)
        grid.setContentsMargins(8, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(0)
        dot = QLabel()
        dot.setPixmap(dot_icon(tone).pixmap(10, 10))      # a dot and a word, never colour alone
        dot.setToolTip(f"{word}: {meaning}")
        grid.addWidget(dot, 0, 0)
        grid.addWidget(_label(item.analyzer, f"Analyzer file {item.analyzer} of {item.set_label}", wrap=False), 0, 1)
        grid.addWidget(_label(word, f"{word}: {meaning}", wrap=False, name="queue_item_word"), 0, 2)
        grid.setColumnStretch(3, 1)
        what = f"{item.analyzer}'s {item.pass_kind} pass"
        if item.status == "running":
            elapsed = _label("", "How long this item has been running, in total.", wrap=False)
            grid.addWidget(elapsed, 0, 3, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(self._stop_button(item, what), 0, 4)
            text, tip = progress_text(item, now)
            progress = _label(text, tip, name="queue_item_progress")
            grid.addWidget(progress, 1, 1, 1, 4)
            self._live[item.item_id] = (progress, elapsed, item)
            self._set_elapsed(elapsed, item, now)
        elif item.status == "waiting":
            remove = _button("✕ Remove", f"Takes {what} out of the {item.pass_kind} queue. It asks nothing; "
                             "press the add button to put it back.", "queue_item_remove", tool=True)
            remove.setAccessibleName(f"Remove {item.analyzer} from the {item.pass_kind} queue")
            remove.clicked.connect(lambda _=False, i=item: self._remove(i))
            grid.addWidget(remove, 0, 4)
            if not item.runnable:
                grid.addWidget(_label(_WAITS_FOR_FAST, "A deep pass starts only after the fast pass of the same "
                                      "analyzer file has finished; other files' deep passes may start meanwhile.",
                                      name="queue_item_progress"), 1, 1, 1, 4)
        elif item.status == "saving":
            grid.addWidget(_label("writing the result into the experiment folder",
                                  "The fit is done; it counts as done once it is saved.",
                                  name="queue_item_progress"), 1, 1, 1, 4)
        else:                                              # a finished item in the look list
            if item.reason:
                grid.addWidget(_label(item.reason, f"Why this analyzer is {word}.", name="queue_item_reason"),
                               1, 1, 1, 4)
            if item.pass_kind == "deep":
                shows = ("its deep result" if item.status in ("done", "done_warning") else
                         "the analyzer, so its reason and any missing value can be found")
                show = _button("Show", f"Selects this analyzer in the scan list and shows {shows} in the centre "
                               "display. This is the only button in the panel that changes your selection.",
                               "queue_item_show", tool=True)
                show.clicked.connect(lambda _=False, i=item: self._show(i))
                grid.addWidget(show, 0, 4)
        return row

    def _stop_button(self, item, what: str):
        button = _button("■ Stop", (f"Stops {what} now and frees its cores; other items carry on. "
                                         + ("It asks first, because the work done so far is discarded. Its fast-pass "
                                            "result stays once it has been saved." if item.pass_kind == "deep" else
                                            "Nothing already saved is lost; press the add button to run it again.")),
                         "queue_item_stop", tool=True)
        button.setAccessibleName(f"Stop {item.analyzer}'s {item.pass_kind} pass")
        button.clicked.connect(lambda _=False, i=item: self._stop(i))
        return button

    @staticmethod
    def _set_elapsed(label: QLabel, item, now: float) -> None:
        label.setText("" if item.started_at is None else fmt_elapsed(now - item.started_at))

    def _tick_labels(self) -> None:
        now = _now()
        for progress, elapsed, item in self._live.values():
            text, tip = progress_text(item, now)
            progress.setText(text)
            if progress.toolTip() != tip:
                progress.setToolTip(tip)
            self._set_elapsed(elapsed, item, now)

    def _finished(self, view, now: float) -> None:
        head = self._heading("Finished", _FINISHED_TIP, "", _FINISHED_TIP, "queue_finished_heading")
        clear = _button("Clear list", "Tidies this list only. It changes no result, no count in the scan list and "
                        "no check for work already done.", "queue_clear")
        clear.setEnabled(bool(view.finished))
        if not view.finished:
            clear.setToolTip("Nothing has finished yet, so there is nothing to clear.")
        clear.clicked.connect(lambda: self._controller.clear_finished())
        head.layout().addWidget(clear, 0)
        self._rows.addWidget(head)
        if not view.finished:
            self._rows.addWidget(_label("Nothing finished yet", _FINISHED_TIP))
        for group in view.finished:
            counts = " · ".join(f"{group.counts[s]} {QUEUE_STATES[s][0]}" for s in _COUNT_ORDER
                                     if group.counts.get(s))
            self._rows.addWidget(_label(
                f"{group.set_label} · {group.pass_kind} pass · {counts}",
                f"Analyzer files of this scan set whose {group.pass_kind} pass has finished, by outcome. Done means "
                "saved in the experiment folder.", bold=True, name="queue_finished_line"))
            for item in group.look:
                self._rows.addWidget(self._item_row(item, now))

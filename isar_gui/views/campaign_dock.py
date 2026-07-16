"""CampaignDock — set up, launch, watch, and stop an autonomous measurement campaign.

The right-hand campaign workspace (GUI Pass 3, M2-6): a Setup form that maps 1:1 onto the
``isar drive`` CLI, Start/Resume + cooperative-Stop/Force-stop controls routed through the
single-writer coordinator, an aggregates-only Progress strip, and the live decision feed rendered
from the session event log. The dock OBSERVES the campaign exclusively through the
:class:`~isar_gui.viewmodels.campaign.CampaignViewModel` (workspace artifacts) and CONTROLS it
exclusively through coordinator requests — it never touches the workspace itself (G19).

Resume honesty (AD-10): goals compile through the LLM per invocation, so "run the same goal
again" is NOT the same campaign. The Start button compares the form's source fingerprint against
the log's last campaign_start fingerprint: a match relabels the button "Resume" and launches
``--resume-latest`` (the frozen objective, no LLM); a mismatch over an existing campaign shows an
inline warning — the whole-log spend ledger carries over — plus an explicit "Start anyway".
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from isar_gui.commands import DriveOptions, source_fingerprint
from isar_gui.viewmodels.campaign import budget_readout

log = logging.getLogger(__name__)

_SEVERITY_BG = {"warn": QColor("#fff3e0"), "terminal": QColor("#e8eaf6"),
                "notice": QColor("#e3f2fd")}
_MAX_FEED_ROWS = 500


class CampaignDock(QDockWidget):
    """The campaign workspace dock. All engine interaction goes through the coordinator."""

    #: Emitted when the operator asks to launch TAVI (the window owns the .bat plumbing).
    tavi_launch_requested = Signal()
    #: Emitted instead of request_drive when Start is clicked with NO workspace at all ("none"
    #: state): the window must reserve a temporary workspace first, which rebinds (and tears this
    #: dock down), so the launch has to happen from the window side. Carries (DriveOptions, env).
    virtual_start_requested = Signal(object, dict)

    def __init__(self, vm, operations, store=None, llm_env=None, workspace_state=None,
                 parent=None):
        super().__init__("Campaign", parent)
        self.setObjectName("campaign_dock")
        self._vm = vm
        self._operations = operations
        self._store = store
        self._llm_env = llm_env or (lambda: {})
        # The manager's live state ("none" | "pending" | "ready"), injected like llm_env so the
        # dock stays coordinator/manager-agnostic. Default suits tests and ready-only embeddings.
        self._workspace_state = workspace_state or (lambda: "ready")
        self._tavi_status = "down"
        self._feed_items: list = []
        self._loading_form = False   # _load_form sets widgets; their signals must not write back
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)   # debounce: no per-keystroke json writes
        self._save_timer.timeout.connect(self._save_form)

        body = QWidget(self)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # ---- Setup -----------------------------------------------------------
        self._src_goal = QRadioButton("Goal", body)
        self._src_spec = QRadioButton("Spec file", body)
        self._src_goal.setChecked(True)
        src_row = QHBoxLayout()
        src_row.addWidget(self._src_goal)
        src_row.addWidget(self._src_spec)
        src_row.addStretch(1)
        outer.addLayout(src_row)

        self._goal = QPlainTextEdit(body)
        self._goal.setPlaceholderText("e.g. map the acoustic branch from (2,0,0) to (3,0,0) …")
        self._goal.setFixedHeight(58)
        outer.addWidget(self._goal)

        spec_row = QHBoxLayout()
        self._spec_path = QLineEdit(body)
        self._spec_path.setPlaceholderText("objective spec .json")
        self._spec_browse = QPushButton("Browse…", body)
        self._spec_browse.clicked.connect(self._browse_spec)
        spec_row.addWidget(self._spec_path, 1)
        spec_row.addWidget(self._spec_browse)
        outer.addLayout(spec_row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._tier = QComboBox(body)
        self._tier.addItems(["deterministic", "mcstas"])
        self._budget = QLineEdit(body)
        self._budget.setPlaceholderText("8h | 1e10n (optional)")
        self._seed = QSpinBox(body)
        self._seed.setRange(0, 99_999_999)
        self._max_iters = QSpinBox(body)
        self._max_iters.setRange(1, 100_000)
        self._max_iters.setValue(200)
        self._max_iters.setToolTip("Iteration cap PER INVOCATION — a resume gets a fresh cap.")
        form.addRow("Tier", self._tier)
        form.addRow("Budget", self._budget)
        form.addRow("Seed", self._seed)
        form.addRow("Max iters", self._max_iters)
        outer.addLayout(form)

        self._mismatch = QLabel(body)
        self._mismatch.setWordWrap(True)
        self._mismatch.setStyleSheet("color: #b26a00;")
        self._mismatch.setVisible(False)
        outer.addWidget(self._mismatch)

        # ---- Controls --------------------------------------------------------
        controls = QHBoxLayout()
        self._start_btn = QPushButton("Start", body)
        self._start_btn.clicked.connect(self._on_start_clicked)
        # The explicit resume route (fingerprints decide CONFIDENCE of sameness, never whether a
        # resume is possible): the frozen objective in the session log is always sufficient, so
        # this button works when the form can't produce a comparable fingerprint at all — a
        # pre-fingerprint campaign log, a moved/deleted spec file, or a freshly typed goal.
        self._resume_btn = QPushButton("Resume last campaign", body)
        self._resume_btn.setFlat(True)
        self._resume_btn.setStyleSheet("color: #1565c0;")
        self._resume_btn.clicked.connect(self._on_resume_clicked)
        self._resume_btn.setVisible(False)
        self._stop_btn = QPushButton("Stop", body)
        self._stop_btn.setToolTip("Cooperative: the campaign finishes the current scan, then stops.")
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        self._force_btn = QPushButton("Force stop", body)
        self._force_btn.setToolTip("Kills the campaign's process tree NOW. The in-flight scan may "
                                   "be stranded and re-measured on resume.")
        self._force_btn.setStyleSheet("color: #c62828;")
        self._force_btn.clicked.connect(self._on_force_clicked)
        controls.addWidget(self._start_btn)
        controls.addWidget(self._resume_btn)
        controls.addWidget(self._stop_btn)
        controls.addWidget(self._force_btn)
        controls.addStretch(1)
        outer.addLayout(controls)

        tavi_row = QHBoxLayout()
        self._tavi_dot = QLabel("TAVI ●", body)
        self._tavi_btn = QPushButton("Launch TAVI", body)
        self._tavi_btn.clicked.connect(self.tavi_launch_requested.emit)
        tavi_row.addWidget(self._tavi_dot)
        tavi_row.addWidget(self._tavi_btn)
        tavi_row.addStretch(1)
        outer.addLayout(tavi_row)

        self._status_line = QLabel("no campaign yet", body)
        self._status_line.setWordWrap(True)
        outer.addWidget(self._status_line)

        # ---- Progress --------------------------------------------------------
        self._iter_label = QLabel("", body)
        self._budget_bar = QProgressBar(body)
        self._budget_bar.setTextVisible(True)
        self._budget_bar.setRange(0, 1000)
        self._budget_bar.setValue(0)
        self._converge_label = QLabel("", body)
        outer.addWidget(self._iter_label)
        outer.addWidget(self._budget_bar)
        outer.addWidget(self._converge_label)

        # ---- Feed ------------------------------------------------------------
        self._exceptions_only = QCheckBox("exceptions only", body)
        self._exceptions_only.toggled.connect(self._rebuild_feed)
        outer.addWidget(self._exceptions_only)
        self._feed = QListWidget(body)
        self._feed.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        outer.addWidget(self._feed, 1)

        self.setWidget(body)

        # ---- wiring ----------------------------------------------------------
        vm.status_changed.connect(self._sync_status)
        vm.feed_appended.connect(self._append_feed)
        vm.feed_reset.connect(self._clear_feed)
        # Qt drops these automatically when this dock (the receiver) is destroyed on rebind, so
        # connecting to the persistent coordinator here does not accumulate across bindings.
        operations.drive_started.connect(self._sync_status)
        operations.drive_finished.connect(self._on_drive_finished)
        operations.activity_changed.connect(self._on_activity)
        self._goal.textChanged.connect(self._on_form_edited)
        self._spec_path.textChanged.connect(self._on_form_edited)
        self._budget.textChanged.connect(self._on_form_edited)
        self._src_goal.toggled.connect(self._on_form_edited)
        self._tier.currentTextChanged.connect(self._on_form_edited)
        self._seed.valueChanged.connect(self._on_form_edited)
        self._max_iters.valueChanged.connect(self._on_form_edited)

        self._load_form()
        self._sync_status()

    # ---- form persistence (typed GuiStore accessors) --------------------------
    def set_store(self, store) -> None:
        """Late-bind the per-workspace GuiStore (loaded after dock construction on a ready bind)."""
        self._store = store
        self._load_form()

    def _load_form(self) -> None:
        if self._store is None:
            return
        form = self._store.campaign_form()
        if not form:
            return
        self._loading_form = True
        try:
            self._apply_form(form)
        finally:
            self._loading_form = False

    def _apply_form(self, form: dict) -> None:
        self._src_spec.setChecked(form.get("source") == "spec")
        self._src_goal.setChecked(form.get("source") != "spec")
        self._goal.setPlainText(str(form.get("goal", "")))
        self._spec_path.setText(str(form.get("spec_path", "")))
        tier = str(form.get("tier", "deterministic"))
        if self._tier.findText(tier) >= 0:
            self._tier.setCurrentText(tier)
        self._budget.setText(str(form.get("budget", "")))
        try:
            self._seed.setValue(int(form.get("seed", 0)))
            self._max_iters.setValue(max(1, int(form.get("max_iters", 200))))
        except (TypeError, ValueError):
            log.debug("campaign form carried non-numeric seed/max_iters; defaults kept")

    def form_dict(self) -> dict:
        """The current form as the plain dict the GuiStore persists — the window uses this to
        carry the operator's typed setup across a workspace rebind (virtual-campaign start)."""
        return {
            "source": "spec" if self._src_spec.isChecked() else "goal",
            "goal": self._goal.toPlainText(),
            "spec_path": self._spec_path.text(),
            "tier": self._tier.currentText(),
            "budget": self._budget.text(),
            "seed": self._seed.value(),
            "max_iters": self._max_iters.value(),
        }

    def apply_form(self, form: dict) -> None:
        """Populate the form programmatically (a rebind carry-over) without write-back churn."""
        self._loading_form = True
        try:
            self._apply_form(form)
        finally:
            self._loading_form = False

    def _save_form(self) -> None:
        if self._store is None:
            return
        self._store.set_campaign_form(self.form_dict())
        self._store.save()

    def _on_form_edited(self, *_a) -> None:
        if self._loading_form:
            return   # widget-population during a load must not write back or churn status
        self._save_timer.start()
        self._sync_status()

    def _browse_spec(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Objective spec", "", "JSON (*.json)")
        if path:
            self._spec_path.setText(path)
            self._src_spec.setChecked(True)

    # ---- Start / Resume decision (AD-10) --------------------------------------
    def _form_fingerprint(self) -> str | None:
        try:
            if self._src_spec.isChecked():
                p = self._spec_path.text().strip()
                return source_fingerprint(spec_path=p) if p else None
            text = self._goal.toPlainText().strip()
            return source_fingerprint(goal=text) if text else None
        except (OSError, ValueError):
            return None   # unreadable spec file etc. — treated as "no fingerprint yet"

    def _resume_decision(self) -> tuple[str, bool]:
        """(button label, resume_latest?) from the fingerprint comparison."""
        state = self._vm.state
        last = (state.source or {}).get("sha256") if state.has_campaign else None
        mine = self._form_fingerprint()
        if last is not None and mine is not None and mine == last:
            return "Resume", True
        if last is not None and mine is not None:
            return "Start anyway", False
        return "Start", False

    # ---- controls --------------------------------------------------------------
    def _launch_resume(self) -> None:
        """Launch ``--resume-latest``: the frozen objective, no LLM. Tier is frozen with the
        objective (the CLI ignores it on resume); budget/seed/max-iters still apply per run."""
        opts = DriveOptions(resume_latest=True, tier=self._tier.currentText(),
                            budget=self._budget.text().strip() or None,
                            seed=self._seed.value(), max_iters=self._max_iters.value())
        if not self._operations.request_drive(opts, env=dict(self._llm_env())):
            self._status_line.setText("could not start — workspace not ready")
            return
        self._sync_status()

    def _on_resume_clicked(self) -> None:
        self._launch_resume()

    def _on_start_clicked(self) -> None:
        _label, resume = self._resume_decision()
        budget = self._budget.text().strip() or None
        if resume:
            self._launch_resume()
            return
        if self._src_spec.isChecked():
            opts = DriveOptions(spec_path=self._spec_path.text().strip(),
                                tier=self._tier.currentText(), budget=budget,
                                seed=self._seed.value(), max_iters=self._max_iters.value())
        else:
            opts = DriveOptions(goal=self._goal.toPlainText().strip(),
                                tier=self._tier.currentText(), budget=budget,
                                seed=self._seed.value(), max_iters=self._max_iters.value())
        if self._workspace_state() == "none":
            # No workspace at all: the window must reserve a temporary one first, and that
            # rebind tears THIS dock down — so hand the launch over instead of requesting it.
            self.virtual_start_requested.emit(opts, dict(self._llm_env()))
            return
        if not self._operations.request_drive(opts, env=dict(self._llm_env())):
            self._status_line.setText("could not start — workspace not ready")
            return
        self._sync_status()

    def _on_stop_clicked(self) -> None:
        if self._operations.request_drive_stop():
            self._status_line.setText("stopping after the current scan…")
        self._sync_status()

    def _on_force_clicked(self) -> None:
        self._operations.cancel_drive()
        self._sync_status()

    def _on_drive_finished(self, _code: int, _live: bool) -> None:
        self._sync_status()

    def _on_activity(self, _state: dict) -> None:
        self._sync_status()

    # ---- backend health (window-fed) --------------------------------------------
    def set_backend_status(self, backend: str, status: str, detail: str = "") -> None:
        if backend != "tavi":
            return
        self._tavi_status = status
        color = {"ok": "#2e7d32", "reachable_error": "#b26a00"}.get(status, "#c62828")
        self._tavi_dot.setStyleSheet(f"color: {color};")
        self._tavi_dot.setToolTip(detail)
        self._sync_status()

    # ---- status/progress ----------------------------------------------------------
    def _sync_status(self, *_a) -> None:
        state = self._vm.state
        running = self._operations.is_running_campaign()
        busy = self._operations.is_busy()

        label, _resume = self._resume_decision()
        self._start_btn.setText(label)
        mismatch = label == "Start anyway"
        self._mismatch.setVisible(mismatch)
        if mismatch:
            self._mismatch.setText(
                "Different objective than the existing campaign log — starting anyway continues "
                "the SAME spend ledger. Use a fresh workspace for a clean campaign.")

        can_start = (not busy) and self._tavi_status == "ok" and self._form_fingerprint() is not None
        self._start_btn.setEnabled(can_start)
        if running:
            self._start_btn.setToolTip("A campaign is already running")
        elif busy:
            self._start_btn.setToolTip("Another workspace operation is running")
        elif self._tavi_status != "ok":
            self._start_btn.setToolTip("TAVI backend is not reachable — launch it first")
        elif self._form_fingerprint() is None:
            self._start_btn.setToolTip("Enter a goal or pick a spec file")
        else:
            self._start_btn.setToolTip("")

        # The explicit resume route needs NO form fingerprint — the frozen objective in the log is
        # sufficient. Shown whenever a campaign exists and the primary isn't already a verified
        # Resume (fingerprints decide confidence of sameness, never resumability).
        self._resume_btn.setVisible(state.has_campaign and label != "Resume")
        self._resume_btn.setEnabled((not busy) and self._tavi_status == "ok")
        self._resume_btn.setToolTip(
            "Continues the frozen objective from the campaign log (no LLM). Goal/spec and tier "
            "are ignored; budget/seed/max-iters still apply to this run."
            + ("" if self._tavi_status == "ok" else "\nTAVI backend is not reachable — launch it first."))

        # Tier is frozen with a resumed objective — a live combo implying otherwise would lie.
        resuming = label == "Resume"
        self._tier.setEnabled(not resuming)
        self._tier.setToolTip("Tier is frozen with the campaign objective — it applies to new "
                              "starts only." if resuming else "")

        self._stop_btn.setEnabled(running)
        self._force_btn.setVisible(running and self._stop_pending())

        if running and self._workspace_state() == "pending":
            # The campaign is initializing its own workspace (the card lands after the first
            # scan); until then there is no event log to report iterations from — say so.
            self._status_line.setText(
                "campaign starting — the workspace appears after the first scan lands"
                + ("  — stopping after the current scan…" if self._stop_pending() else ""))
        elif running:
            it = state.iteration
            # Launch-captured cap: the spinbox can be edited mid-run and no longer describes the
            # subprocess. Iterations are logged zero-based; operators count from 1.
            opts = self._operations.drive_options()
            cap = opts.max_iters if opts is not None else "—"
            self._status_line.setText(
                f"running — iteration {it + 1 if it is not None else '…'} of {cap} (this run)"
                + ("  — stopping after the current scan…" if self._stop_pending() else ""))
        elif state.stop_state == "operator_stopped":
            self._status_line.setText("stopped by operator — press Resume to continue")
        elif state.stop_state:
            self._status_line.setText(
                f"{state.stop_state}: " + "; ".join(state.stop_reasons))
        elif state.has_campaign:
            self._status_line.setText("campaign idle — press Resume to continue")
        else:
            self._status_line.setText("no campaign yet")

        it = state.iteration
        self._iter_label.setText(
            f"iteration: {it + 1 if it is not None else '—'}   scans: {state.scan_count}   "
            f"observations: {state.n_observations if state.n_observations is not None else '—'}")
        permille, text = budget_readout(state)
        self._budget_bar.setValue(permille)
        self._budget_bar.setFormat(text)
        verdicts = getattr(self._vm, "scan_verdicts", {}) or {}
        conv = (f"converged {state.converged}/{state.n_grid}"
                if state.converged is not None and state.n_grid else "convergence: —")
        vt = "  ·  scan verdicts: " + ", ".join(
            f"{k} {v}" for k, v in sorted(verdicts.items())) if verdicts else ""
        self._converge_label.setText(conv + vt)

    def _stop_pending(self) -> bool:
        return bool(self._operations.is_stop_pending())

    # ---- feed -----------------------------------------------------------------------
    def _append_feed(self, items: list) -> None:
        self._feed_items.extend(items)
        del self._feed_items[:-_MAX_FEED_ROWS]
        show_all = not self._exceptions_only.isChecked()
        for item in items:
            if not show_all and item.severity not in ("warn", "terminal"):
                continue
            self._add_row(item)
        self._feed.scrollToBottom()

    def _clear_feed(self) -> None:
        self._feed_items.clear()
        self._feed.clear()

    def _rebuild_feed(self) -> None:
        self._feed.clear()
        show_all = not self._exceptions_only.isChecked()
        for item in self._feed_items:
            if show_all or item.severity in ("warn", "terminal"):
                self._add_row(item)
        self._feed.scrollToBottom()

    def _add_row(self, item) -> None:
        row = QListWidgetItem(item.text)
        bg = _SEVERITY_BG.get(item.severity)
        if bg is not None:
            row.setBackground(bg)
        if item.severity == "terminal":
            font = row.font()
            font.setBold(True)
            row.setFont(font)
        row.setToolTip(f"{item.kind} @ {item.ts:.0f}")
        self._feed.addItem(row)
        while self._feed.count() > _MAX_FEED_ROWS:
            self._feed.takeItem(0)
        # QListWidgetItem alignment default is fine; keep rows compact.
        row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)

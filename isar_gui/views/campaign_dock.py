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
import json

import shiboken6

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QAbstractItemView,
    QComboBox,
    QDockWidget,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from isar_gui.commands import DriveOptions, source_fingerprint
from isar_gui.log_render import BUDGET_KIND, BudgetAnnouncer, RenderedEvent, format_payload
from isar_gui.viewmodels.campaign import (analysis_readout, budget_readout,
                                          current_activity_text, terminal_status_text)

log = logging.getLogger(__name__)

_SEVERITY_BG = {"warn": QColor("#fff3e0"), "terminal": QColor("#e8eaf6"),
                "notice": QColor("#e3f2fd")}
#: Loud rows (exceptions, fit escalations) are coloured as well as backgrounded — they are the two
#: things an operator is actually watching for, and they survive every feed filter.
_LOUD_FG = {"exception": QColor("#b71c1c"), "escalate_fit": QColor("#b26a00")}
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
        self._selected_hkl: tuple[float, ...] | None = None
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
        self._src_program = QRadioButton("Program file", body)
        self._src_goal.setChecked(True)
        src_row = QHBoxLayout()
        src_row.addWidget(self._src_goal)
        src_row.addWidget(self._src_program)
        src_row.addStretch(1)
        outer.addLayout(src_row)

        self._goal = QPlainTextEdit(body)
        self._goal.setPlaceholderText("e.g. map the acoustic branch from (2,0,0) to (3,0,0) …")
        self._goal.setFixedHeight(58)
        outer.addWidget(self._goal)

        program_row = QHBoxLayout()
        self._program_path = QLineEdit(body)
        self._program_path.setPlaceholderText("campaign program .json")
        self._program_browse = QPushButton("Browse…", body)
        self._program_browse.clicked.connect(self._browse_program)
        program_row.addWidget(self._program_path, 1)
        program_row.addWidget(self._program_browse)
        outer.addLayout(program_row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._tier = QComboBox(body)
        self._tier.addItems(["deterministic", "mcstas"])
        self._budget = QLineEdit(body)
        # The GUI may not import isar.drive (G16), so no literal default is quoted
        # here — blank inherits the driver's per-tier default.
        self._budget.setPlaceholderText("blank = tier default; e.g. 2h or 1e10n")
        self._analysis_limit = QLineEdit(body)
        self._analysis_limit.setPlaceholderText("analysis: 1h | none")
        self._seed = QSpinBox(body)
        self._seed.setRange(0, 99_999_999)
        form.addRow("Tier", self._tier)
        form.addRow("Measurement budget", self._budget)
        form.addRow("Analysis limit", self._analysis_limit)
        form.addRow("Seed", self._seed)
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
        self._analysis_bar = QProgressBar(body)
        self._analysis_bar.setTextVisible(True)
        self._analysis_bar.setRange(0, 1000)
        self._analysis_bar.setValue(0)
        self._converge_label = QLabel("", body)
        self._master_detail = QLabel("Select a pooled master to inspect its decision state.", body)
        self._master_detail.setWordWrap(True)
        self._master_detail.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #c7c7c7; padding: 5px;")
        outer.addWidget(self._iter_label)
        outer.addWidget(self._budget_bar)
        outer.addWidget(self._analysis_bar)
        outer.addWidget(self._converge_label)
        outer.addWidget(self._master_detail)

        # ---- Feed ------------------------------------------------------------
        self._now_label = QLabel("Now: campaign idle", body)
        self._now_label.setWordWrap(True)
        self._now_label.setStyleSheet(
            "font-weight: bold; background: #e8f1fb; border: 1px solid #9abbd8; padding: 5px;")
        outer.addWidget(self._now_label)
        self._exceptions_only = QCheckBox("problems only", body)
        self._exceptions_only.setToolTip(
            "Show only exceptions, fit escalations, blocked checks and terminal outcomes.")
        self._exceptions_only.toggled.connect(self._rebuild_feed)
        feed_tools = QHBoxLayout()
        feed_tools.addWidget(self._exceptions_only)
        self._copy_selection_btn = QPushButton("Copy selection", body)
        self._copy_all_btn = QPushButton("Copy all", body)
        self._save_log_btn = QPushButton("Save log…", body)
        self._copy_selection_btn.clicked.connect(self._copy_feed_selection)
        self._copy_all_btn.clicked.connect(self._copy_feed_all)
        self._save_log_btn.clicked.connect(self._save_feed)
        feed_tools.addStretch(1)
        feed_tools.addWidget(self._copy_selection_btn)
        feed_tools.addWidget(self._copy_all_btn)
        feed_tools.addWidget(self._save_log_btn)
        outer.addLayout(feed_tools)
        # A tree, not a list: every row carries one child holding its raw JSON payload, so the
        # operator-facing sentence stays short and the audit detail is one click away (the log on
        # disk is never abridged — this dock only chooses what to say out loud).
        self._feed = QTreeWidget(body)
        self._feed.setHeaderHidden(True)
        self._feed.setColumnCount(1)
        self._feed.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._feed.setUniformRowHeights(False)
        self._feed.setWordWrap(True)
        self._feed.itemClicked.connect(self._toggle_expanded)
        outer.addWidget(self._feed, 1)
        self._budget_announcer = BudgetAnnouncer()

        self.setWidget(body)

        # ---- wiring ----------------------------------------------------------
        vm.status_changed.connect(self._sync_status)
        vm.feed_appended.connect(self._append_feed)
        vm.feed_reset.connect(self._clear_feed)
        initial_feed = vm.feed_snapshot()
        if initial_feed:
            self._append_feed(initial_feed[-_MAX_FEED_ROWS:])
        # Qt drops these automatically when this dock (the receiver) is destroyed on rebind, so
        # connecting to the persistent coordinator here does not accumulate across bindings.
        operations.drive_started.connect(self._sync_status)
        operations.drive_finished.connect(self._on_drive_finished)
        operations.activity_changed.connect(self._on_activity)
        if hasattr(operations, "drive_compiled"):
            operations.drive_compiled.connect(self._on_objective_compiled)
            operations.drive_compile_failed.connect(self._on_objective_compile_failed)
        self._goal.textChanged.connect(self._on_form_edited)
        self._program_path.textChanged.connect(self._on_form_edited)
        self._budget.textChanged.connect(self._on_form_edited)
        self._analysis_limit.textChanged.connect(self._on_form_edited)
        self._src_goal.toggled.connect(self._on_form_edited)
        self._tier.currentTextChanged.connect(self._on_form_edited)
        self._seed.valueChanged.connect(self._on_form_edited)

        self._load_form()
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._sync_status)
        self._activity_timer.start()
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
        self._src_program.setChecked(form.get("source") == "program")
        self._src_goal.setChecked(form.get("source") != "program")
        self._goal.setPlainText(str(form.get("goal", "")))
        self._program_path.setText(str(form.get("program_path", "")))
        tier = str(form.get("tier", "deterministic"))
        if self._tier.findText(tier) >= 0:
            self._tier.setCurrentText(tier)
        self._budget.setText(str(form.get("budget", "")))
        self._analysis_limit.setText(str(form.get("analysis_limit", "")))
        try:
            self._seed.setValue(int(form.get("seed", 0)))
        except (TypeError, ValueError):
            log.debug("campaign form carried a non-numeric seed; the default is kept")

    def form_dict(self) -> dict:
        """The current form as the plain dict the GuiStore persists — the window uses this to
        carry the operator's typed setup across a workspace rebind (virtual-campaign start)."""
        return {
            "source": "program" if self._src_program.isChecked() else "goal",
            "goal": self._goal.toPlainText(),
            "program_path": self._program_path.text(),
            "tier": self._tier.currentText(),
            "budget": self._budget.text(),
            "analysis_limit": self._analysis_limit.text(),
            "seed": self._seed.value(),
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

    def _browse_program(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Campaign program", "", "JSON (*.json)")
        if path:
            self._program_path.setText(path)
            self._src_program.setChecked(True)

    # ---- Start / Resume decision (AD-10) --------------------------------------
    def _form_fingerprint(self) -> str | None:
        try:
            if self._src_program.isChecked():
                p = self._program_path.text().strip()
                return source_fingerprint(program_path=p) if p else None
            text = self._goal.toPlainText().strip()
            return source_fingerprint(goal=text) if text else None
        except (OSError, ValueError):
            return None   # unreadable program file etc. — treated as "no fingerprint yet"

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
        objective (the CLI ignores it on resume); budget/seed still apply per run."""
        opts = DriveOptions(resume_latest=True, tier=self._tier.currentText(),
                            budget=self._budget.text().strip() or None,
                            analysis_limit=self._analysis_limit.text().strip() or None,
                            seed=self._seed.value())
        if not self._operations.request_drive(opts, env=dict(self._llm_env())):
            self._status_line.setText("could not start — workspace not ready")
            return
        self._sync_status()

    def _on_resume_clicked(self) -> None:
        self._launch_resume()

    def _on_start_clicked(self) -> None:
        _label, resume = self._resume_decision()
        budget = self._budget.text().strip() or None
        analysis_limit = self._analysis_limit.text().strip() or None
        if resume:
            self._launch_resume()
            return
        if self._src_program.isChecked():
            opts = DriveOptions(program_path=self._program_path.text().strip(),
                                tier=self._tier.currentText(), budget=budget,
                                analysis_limit=analysis_limit, seed=self._seed.value())
        else:
            opts = DriveOptions(goal=self._goal.toPlainText().strip(),
                                tier=self._tier.currentText(), budget=budget,
                                analysis_limit=analysis_limit, seed=self._seed.value())
        if not hasattr(self._operations, "request_drive_compile"):
            self._status_line.setText("could not compile objective — coordinator is unavailable")
            return
        if not self._operations.request_drive_compile(opts, env=dict(self._llm_env())):
            self._status_line.setText("could not compile objective")
            return
        self._status_line.setText("compiling objective for confirmation…")
        self._sync_status()

    def _on_objective_compiled(self, program: dict, path: str) -> None:
        # A workspace rebind replaces this dock with deleteLater(); a compile signal already queued
        # for the retired receiver can arrive before Qt finishes disconnecting it. Never let that
        # stale Python wrapper open a second modal or launch a duplicate campaign.
        if not shiboken6.isValid(self):
            return
        if not self._confirm_objective(program):
            self._status_line.setText("campaign start cancelled — no measurements submitted")
            return
        # Branch, tier, energy window, tolerance, and budget are exactly what the operator just
        # confirmed. Only invocation controls that are outside the objective remain mutable here.
        opts = DriveOptions(compiled_program_path=path, seed=self._seed.value())
        if self._workspace_state() == "none":
            self.virtual_start_requested.emit(opts, dict(self._llm_env()))
            return
        if not self._operations.request_drive(opts, env=dict(self._llm_env())):
            self._status_line.setText("could not start — workspace not ready")

    def _on_objective_compile_failed(self, message: str) -> None:
        if not shiboken6.isValid(self):
            return
        self._status_line.setText(str(message))

    def dispose(self) -> None:
        """Disconnect persistent coordinator signals before a workspace rebind deletes this dock."""
        self._save_timer.stop()
        self._activity_timer.stop()
        connections = [
            (self._operations.drive_started, self._sync_status),
            (self._operations.drive_finished, self._on_drive_finished),
            (self._operations.activity_changed, self._on_activity),
        ]
        if hasattr(self._operations, "drive_compiled"):
            connections.extend([
                (self._operations.drive_compiled, self._on_objective_compiled),
                (self._operations.drive_compile_failed, self._on_objective_compile_failed),
            ])
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError) as exc:
                log.debug("campaign dock signal already disconnected during dispose: %s", exc)

    def _confirm_objective(self, program: dict) -> bool:
        """Copyable, modal measurement preview. Tests must stub this method."""
        steps = program.get("steps") or [{}]
        objective = steps[0].get("payload") or {}
        path = objective.get("path") or []
        budget = objective.get("budget") or {}
        lines = [
            f"Target: {objective.get('target_label') or objective.get('target_branch', 'acoustic')}",
            f"Branch certified: {objective.get('target_branch', 'acoustic')}",
            f"Q path: {path[0] if path else '?'} → {path[-1] if path else '?'} ({len(path)} points)",
            f"Energy search: {objective.get('energy_search_meV')} meV",
            f"Tolerance: ±{objective.get('tolerance_meV')} meV",
            f"Budget: {budget.get('amount')} {budget.get('kind')}",
            f"Analysis limit: {objective.get('analysis_limit_seconds')} seconds",
            "",
            "Frozen campaign program:",
            json.dumps(program, indent=2),
        ]
        dialog = QDialog(self)
        dialog.setWindowTitle("Confirm campaign objective")
        dialog.resize(620, 500)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(dialog)
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines))
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start measurements")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

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

    def set_selected_fit(self, fit) -> None:
        """Follow browser/plot selection with one revision-consistent master assessment."""
        q = getattr(fit, "q", None) if fit is not None else None
        self._selected_hkl = (tuple(float(value) for value in q)
                              if q is not None and len(q) >= 3 else None)
        self._sync_status()

    def _selected_master_text(self, state) -> str:
        if self._selected_hkl is None:
            return "Select a pooled master to inspect its decision state."
        selected = next((row for row in state.per_q
                         if len(row.get("hkl") or ()) >= 3
                         and all(abs(float(left) - float(right)) <= 1e-6
                                 for left, right in zip(row["hkl"], self._selected_hkl))), None)
        q_text = ", ".join(f"{value:g}" for value in self._selected_hkl[:3])
        if selected is None:
            return f"Selected q ({q_text}) is outside the current campaign objective."
        omega = selected.get("omega0")
        halfwidth = selected.get("calibrated_halfwidth")
        tolerance = selected.get("tolerance")
        interval = ("unavailable" if halfwidth is None else f"±{float(halfwidth):.4g} meV")
        target = "—" if tolerance is None else f"±{float(tolerance):.4g} meV"
        consistency = selected.get("pool_consistent")
        consistency_text = ("pass" if consistency is True else
                            "FAIL — review required" if consistency is False else "not assessed")
        info = selected.get("expected_next_information") or {}
        info_text = (f"{float(info.get('expected_information_nats')):.4g} nats"
                     if info.get("expected_information_nats") is not None else "unavailable")
        revision = selected.get("master_revision") or "—"
        diagnostics = ", ".join(selected.get("diagnostics") or ()) or "none"
        return (f"Master at ({q_text}) — revision {revision}\n"
                f"ω: {'—' if omega is None else f'{float(omega):.5g} meV'}; interval {interval}; "
                f"target {target}; method {selected.get('uncertainty_method') or '—'}\n"
                f"track {selected.get('track_state') or '—'}; Γ {selected.get('linewidth_state') or '—'}; "
                f"pool consistency {consistency_text}; next information {info_text}\n"
                f"{selected.get('reason') or 'continue'}; diagnostics: {diagnostics}")

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
            self._start_btn.setToolTip("Enter a goal or pick a program file")
        else:
            self._start_btn.setToolTip("")

        # The explicit resume route needs NO form fingerprint — the frozen objective in the log is
        # sufficient. Shown whenever a campaign exists and the primary isn't already a verified
        # Resume (fingerprints decide confidence of sameness, never resumability).
        self._resume_btn.setVisible(state.has_campaign and label != "Resume")
        self._resume_btn.setEnabled((not busy) and self._tavi_status == "ok")
        self._resume_btn.setToolTip(
            "Continues the frozen objective from the campaign log (no LLM). Goal/program and tier "
            "are ignored; budget and seed still apply to this run."
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
            # Iterations are logged zero-based; operators count from 1. There is no iteration cap
            # any more — the budgets are the only stop conditions, and each has its own bar.
            self._status_line.setText(
                f"running — iteration {it + 1 if it is not None else '…'} (this run)"
                + ("  — stopping after the current scan…" if self._stop_pending() else ""))
        elif state.stop_state == "operator_stopped":
            self._status_line.setText("stopped by operator — press Resume to continue")
        elif state.stop_state == "pending_recovery":
            self._status_line.setText(
                "scan recovery pending — no new scan was submitted; press Resume to retry")
        elif state.stop_state:
            self._status_line.setText(terminal_status_text(state))
        elif state.has_campaign:
            self._status_line.setText("campaign idle — press Resume to continue")
        else:
            self._status_line.setText("no campaign yet")

        it = state.iteration
        self._iter_label.setText(
            f"iteration: {it + 1 if it is not None else '—'}   scans: {state.scan_count}   "
            f"observations: {state.n_observations if state.n_observations is not None else '—'}")
        permille, text = budget_readout(state)
        self._budget_bar.setVisible(state.budget_amount is not None)
        self._budget_bar.setValue(permille)
        self._budget_bar.setFormat(text)
        analysis_permille, analysis_text = analysis_readout(state)
        self._analysis_bar.setVisible(state.analysis_limit_seconds is not None)
        self._analysis_bar.setValue(analysis_permille)
        self._analysis_bar.setFormat(analysis_text)
        verdicts = getattr(self._vm, "scan_verdicts", {}) or {}
        conv = (f"converged {state.converged}/{state.n_grid}"
                if state.converged is not None and state.n_grid else "convergence: —")
        vt = "  ·  scan verdicts: " + ", ".join(
            f"{k} {v}" for k, v in sorted(verdicts.items())) if verdicts else ""
        self._converge_label.setText(conv + vt)
        self._now_label.setText(current_activity_text(state))
        self._master_detail.setText(self._selected_master_text(state))

    def _stop_pending(self) -> bool:
        return bool(self._operations.is_stop_pending())

    # ---- feed -----------------------------------------------------------------------
    @staticmethod
    def _is_visible(item, show_all: bool) -> bool:
        """Loud rows (exception / escalate_fit) are never filtered out — that is the point of them."""
        return show_all or bool(getattr(item, "loud", False)) or item.severity in ("warn", "terminal")

    def _append_feed(self, items: list) -> None:
        items = list(items)
        show_all = not self._exceptions_only.isChecked()
        # The budget line is paced by rows the operator actually SEES, so a filtered feed does not
        # get budget updates at the unfiltered rate.
        shown = [item for item in items if self._is_visible(item, show_all)]
        budget = self._budget_line(len(shown))
        if budget is not None:
            items.append(budget)
            shown.append(budget)
        self._feed_items = list(reversed(items)) + self._feed_items
        del self._feed_items[_MAX_FEED_ROWS:]
        for item in shown:
            self._add_row(item, prepend=True)
        self._feed.scrollToTop()

    def _budget_line(self, rows: int):
        """A synthesized budget-status row, when one is due (see :class:`BudgetAnnouncer`)."""
        text = self._budget_announcer.maybe_line(self._vm.state, now=time.time(), rows=rows)
        if text is None:
            return None
        state = self._vm.state
        return RenderedEvent(
            ts=time.time(), kind=BUDGET_KIND, text=text, severity="notice", loud=False,
            payload={"budget_kind": state.budget_kind, "budget_amount": state.budget_amount,
                     "spent_neutrons": state.spent_neutrons, "spent_seconds": state.spent_seconds,
                     "analysis_limit_seconds": state.analysis_limit_seconds,
                     "analysis_spent_seconds": state.analysis_spent_seconds,
                     "scan_count": state.scan_count,
                     "note": "synthesized by the GUI from campaign state; not a logged event"})

    def _clear_feed(self) -> None:
        self._feed_items.clear()
        self._feed.clear()
        self._budget_announcer.reset()

    def _rebuild_feed(self) -> None:
        self._feed.clear()
        show_all = not self._exceptions_only.isChecked()
        for item in self._feed_items:
            if self._is_visible(item, show_all):
                self._add_row(item)
        self._feed.scrollToTop()

    def _add_row(self, item, *, prepend=False) -> None:
        row = QTreeWidgetItem([item.text])
        bg = _SEVERITY_BG.get(item.severity)
        if bg is not None:
            row.setBackground(0, bg)
        fg = _LOUD_FG.get(item.kind) if getattr(item, "loud", False) else None
        if fg is not None:
            row.setForeground(0, fg)
        if item.severity == "terminal" or getattr(item, "loud", False):
            font = row.font(0)
            font.setBold(True)
            row.setFont(0, font)
        icon = self._row_icon(item)
        if icon is not None:
            row.setIcon(0, icon)
        row.setToolTip(0, f"{item.kind} @ {item.ts:.0f} — click to show the raw payload")
        detail = QTreeWidgetItem([format_payload(getattr(item, "payload", {}) or {})])
        detail.setFont(0, QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        detail.setFlags(Qt.ItemFlag.ItemIsEnabled)   # payload text is context, not a selectable row
        row.addChild(detail)
        if prepend:
            self._feed.insertTopLevelItem(0, row)
        else:
            self._feed.addTopLevelItem(row)
        while self._feed.topLevelItemCount() > _MAX_FEED_ROWS:
            self._feed.takeTopLevelItem(self._feed.topLevelItemCount() - 1)

    def _row_icon(self, item):
        """A standard-style icon for the loud kinds; ``None`` for everything else."""
        pixmaps = {"exception": QStyle.StandardPixmap.SP_MessageBoxCritical,
                   "escalate_fit": QStyle.StandardPixmap.SP_MessageBoxWarning}
        pixmap = pixmaps.get(item.kind)
        return self.style().standardIcon(pixmap) if pixmap is not None else None

    def _toggle_expanded(self, item, _column=0) -> None:
        """Click a rendered line to expand it in place onto its raw JSON payload."""
        if item is None or item.parent() is not None:
            return
        item.setExpanded(not item.isExpanded())

    def _copy_feed_selection(self) -> None:
        lines = [item.text(0) for item in self._feed.selectedItems() if item.parent() is None]
        if lines:
            QApplication.clipboard().setText("\n".join(lines))

    def _copy_feed_all(self) -> None:
        QApplication.clipboard().setText("\n".join(item.text for item in self._feed_items))

    def _save_feed(self) -> None:
        path, _kind = QFileDialog.getSaveFileName(self, "Save campaign log", "campaign-log.txt",
                                                   "Text (*.txt);;All files (*)", "",
                                                   QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("\n".join(item.text for item in self._feed_items))
                stream.write("\n" if self._feed_items else "")
        except OSError as exc:
            log.warning("campaign feed save failed for %s: %s", path, exc)
            self._status_line.setText(f"could not save campaign log — {exc}")

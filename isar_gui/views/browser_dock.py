"""BrowserDock — the Results browser: card summary + fit-result group/member tree (Task 9, C1).

Titled "Results" in the UI: it browses fit-result collation buckets and feeds the C2 inspector — a
different axis from the Phase-E Scans dock, which shows physical scans grouped by metadata/manual
membership. Same read-only viewmodel and structure as before; only the window title changed.

Purely presentational: all derivation (labels, tree data, verdict text) lives in
:class:`~isar_gui.viewmodels.browser.BrowserViewModel`; this widget only lays out and renders what
the viewmodel already computed, and forwards a member-fit selection to the viewmodel's ``select()``
(the ``selection_changed`` hook later views subscribe to).

Rendering-rules discipline applies to text: a member whose ``interval_reliable`` is ``False`` gets an
``(unreliable)`` suffix on its verdict cell — never a numeric interval rendered anywhere in the tree.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_SUMMARY_FIELDS = (
    "facility", "instrument", "plugin", "counting_mode", "collation_keys",
    "schema_version", "lattice_epochs", "workspace_path",
)


def _fmt(v) -> str:
    return "" if v is None else str(v)


def _member_verdict_text(fv) -> str:
    """Verdict cell text — a suffix, never a numeric interval (rendering-rules discipline)."""
    verdict = fv.verdict or ""
    if fv.interval_reliable is False:
        return f"{verdict} (unreliable)".strip()
    return verdict


def _group_label_text(row) -> str:
    marker = " [pooled]" if row.pooled else ""
    return f"{row.label} ({len(row.members)} members){marker}"


class BrowserDock(QDockWidget):
    """Card summary header + group/member tree over a :class:`BrowserViewModel`."""

    def __init__(self, vm, parent=None, *, unit_key_for_fit=None):
        # "Results": this dock browses fit-result collation buckets and feeds the C2 inspector — a
        # different axis from the Phase-E Scans dock (physical scans by group). Retitled to keep the
        # two straight; objectName stays "browser_dock" so saved layouts still resolve it.
        super().__init__("Results", parent)
        self.setObjectName("browser_dock")  # stable name for saveState()/restoreState()
        self._vm = vm
        self._active_key: str | None = None
        # Legacy results omit experiment/unit identity.  The window supplies the only authoritative
        # resolver: it checks the current ledger and returns a key only when the match is unique.
        # The Results tree itself must never infer identity from its own child count.
        self._unit_key_for_fit = unit_key_for_fit

        container = QWidget(self)
        layout = QVBoxLayout(container)

        self._form = QFormLayout()
        self._summary_labels: dict[str, QLabel] = {}
        layout.addLayout(self._form)

        self._tree = QTreeWidget(container)
        self._tree.setHeaderLabels(["Group / Scan", "Detector", "Verdict", "q", "T"])
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree)

        # The Results tree is intentionally fit-only: parsed scans live in the Scans / Scan Table
        # views.  An empty QTreeWidget says nothing at all, but an empty *display* tree does not
        # prove analysis produced no result records (some records cannot form a display group).
        self._no_results = QLabel(container)
        self._no_results.setObjectName("browser_no_results")
        self._no_results.setWordWrap(True)
        self._no_results.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._no_results)

        self._warnings_button = QPushButton(container)
        self._warnings_button.setCheckable(True)
        self._warnings_button.toggled.connect(self._on_warnings_toggled)
        layout.addWidget(self._warnings_button)

        self._warnings_view = QPlainTextEdit(container)
        self._warnings_view.setReadOnly(True)
        self._warnings_view.setVisible(False)
        # A dropped file must reach MainWindow.dropEvent, not be pasted as text into this pane.
        self._warnings_view.setAcceptDrops(False)
        layout.addWidget(self._warnings_view)

        self.setWidget(container)

        vm.refreshed.connect(self.refresh)
        self.refresh()

    # ---- rendering ------------------------------------------------------------
    def refresh(self) -> None:
        self._render_summary()
        self._render_tree()
        self._render_warnings()

    def _render_summary(self) -> None:
        summary = self._vm.card_summary
        if not self._summary_labels:
            for key in _SUMMARY_FIELDS:
                label = QLabel()
                self._form.addRow(key, label)
                self._summary_labels[key] = label
        for key, label in self._summary_labels.items():
            label.setText(_fmt(summary.get(key)))

    def _render_tree(self) -> None:
        # Block signals while rebuilding so clearing/repopulating the tree does not spuriously fire
        # itemSelectionChanged (and re-emit a stale selection through the viewmodel).
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            rows = self._vm.group_rows
            for row in rows:
                group_item = QTreeWidgetItem([_group_label_text(row), "", "", "", ""])
                group_item.setData(0, Qt.ItemDataRole.UserRole, None)
                self._tree.addTopLevelItem(group_item)
                for fv in row.members:
                    child = QTreeWidgetItem([
                        _fmt(fv.scan_number), _fmt(fv.detector_id), _member_verdict_text(fv),
                        _fmt(fv.q), _fmt(fv.T),
                    ])
                    child.setData(0, Qt.ItemDataRole.UserRole, fv)
                    group_item.addChild(child)
                for fv in row.derived or []:
                    child = QTreeWidgetItem([
                        "Derived pooled result", _fmt(fv.detector_id), _member_verdict_text(fv),
                        _fmt(fv.q), _fmt(fv.T),
                    ])
                    child.setToolTip(0, "Combined fit derived from this group's physical scans")
                    child.setData(0, Qt.ItemDataRole.UserRole, fv)
                    group_item.addChild(child)
            self._tree.expandAll()
            self._restore_active_item()
        finally:
            self._tree.blockSignals(False)
        self._tree.setVisible(bool(rows))
        self._no_results.setVisible(not rows)
        if not rows:
            self._no_results.setText(self._no_results_text())

    def _no_results_text(self) -> str:
        """Explain an empty display tree without overstating the underlying result state."""
        workspace = self._vm.workspace
        if workspace.results:
            return (
                "No displayable Results yet.\n\n"
                "Analysis records exist, but none can be shown in this Results tree. "
                "Check Warnings and scan provenance for the reason."
            )
        scans = workspace.scans
        if scans:
            return (
                "No displayable Results yet.\n\n"
                "Parsed scans are listed in Scans or Scan Table. "
                "Use Run \u25b8 Fast-pass all ready when you are ready to analyse them."
            )
        return (
            "No displayable Results yet.\n\n"
            "Parsed scans will appear in Scans or Scan Table after ingestion. "
            "Use Run \u25b8 Fast-pass all ready when scans are ready."
        )

    def set_active_key(self, key: str | None) -> None:
        """Show the inspected detector in the results tree without re-emitting a selection."""
        self._active_key = str(key) if key else None
        self._tree.blockSignals(True)
        try:
            self._restore_active_item()
        finally:
            self._tree.blockSignals(False)

    def _restore_active_item(self) -> None:
        """Restore the child matching the canonical unit id after a tree rebuild.

        A pooled aggregate has no atomic unit identity; in that case the tree deliberately has no
        current child rather than pretending one of its members is being inspected.
        """
        target = self._active_key
        found = None
        if target is not None:
            for i in range(self._tree.topLevelItemCount()):
                group = self._tree.topLevelItem(i)
                for j in range(group.childCount()):
                    child = group.child(j)
                    fv = child.data(0, Qt.ItemDataRole.UserRole)
                    if getattr(fv, "unit_id", None) == target:
                        found = child
                        break
                    if (getattr(fv, "unit_id", None) is None
                            and self._unit_key_for_fit is not None
                            and self._unit_key_for_fit(fv) == target):
                        found = child
                        break
                if found is not None:
                    break
        self._tree.setCurrentItem(found)

    def _render_warnings(self) -> None:
        warnings = self._vm.warnings
        self._warnings_button.setText(f"Warnings ({len(warnings)})")
        self._warnings_view.setPlainText("\n".join(warnings))

    # ---- interaction ------------------------------------------------------------
    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        fv = items[0].data(0, Qt.ItemDataRole.UserRole)
        if fv is not None:  # a group header carries no FitView; only a member selection counts
            self._vm.select(fv)

    def _on_warnings_toggled(self, checked: bool) -> None:
        self._warnings_view.setVisible(checked)

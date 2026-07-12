"""BrowserViewModel — view-ready state for the workspace browser (Task 9, C1).

Turns the read-only :class:`~isar.workspace_api.Workspace` facade into what
:class:`~isar_gui.views.browser_dock.BrowserDock` renders: a card summary header and a
group/member tree. Holds no cache of its own beyond what the facade already caches — every property
re-reads ``self.workspace`` fresh, so calling :meth:`refresh` (or the base class's watch-driven
``on_workspace_changed``) after ``WatchBridge`` has re-read the facade is enough to pick up changes.

``selection_changed`` is the hook later views (inspector C2, waterfall C4) subscribe to: the dock
calls :meth:`select` with the clicked member's :class:`~isar.workspace_api.FitView`, and this
re-emits it unchanged — the payload is always a ``FitView``, never a group.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal

from isar_gui.viewmodels.base import ViewModel


@dataclass
class GroupRow:
    """One group-tree top-level row: a card collation-key bucket and its member fits."""

    label: str
    key_fields: dict
    members: list  # list[FitView]
    pooled: bool  # True when a pooled master downstream covers this group's members


def _group_label(key_fields: dict) -> str:
    if not key_fields:
        return "(ungrouped)"
    return ", ".join(f"{name}={val}" for name, val in key_fields.items())


def _member_scan_numbers(members) -> set:
    """Scan numbers referenced by a pooled ``FitView.members`` provenance list.

    Each entry is ``[experiment_id, scan_number, detector_id]`` (the JSON round-trip of the
    reducer's member tuples, ``isar.reduce.collate.MergedSpectrum.members``); scan_number is the
    middle element.
    """
    out = set()
    for m in members or ():
        if isinstance(m, (list, tuple)) and len(m) >= 2:
            try:
                out.add(int(m[1]))
            except (TypeError, ValueError):
                continue
    return out


class BrowserViewModel(ViewModel):
    """Card summary + group/member tree, and the selection hook later views subscribe to."""

    #: Emitted with the selected FitView when the dock's tree selection changes.
    selection_changed = Signal(object)

    # ---- card summary -------------------------------------------------------
    @property
    def card_summary(self) -> dict:
        card = self.workspace.card
        pol = card.policy
        return {
            "facility": getattr(pol, "facility", None),
            "instrument": getattr(pol, "instrument", None),
            "plugin": getattr(pol, "plugin", None),
            "counting_mode": getattr(pol, "counting_mode", None),
            "collation_keys": list((card.collation or {}).get("key", []) or []),
            "schema_version": self.workspace.schema_version,
            "lattice_epochs": len(card.lattice_epochs),
            # None (empty-start) stays None so the view's _fmt blanks it — never the string "None".
            "workspace_path": None if self.workspace.root is None else str(self.workspace.root),
        }

    # ---- group tree -----------------------------------------------------------
    @property
    def group_rows(self) -> list:
        """``ws.groups`` reshaped into display-ready :class:`GroupRow`\\ s.

        A group is marked ``pooled`` when some pooled master elsewhere in ``ws.results`` lists one
        of this group's member scan numbers among its own members — i.e. a pooled fit downstream
        already covers (some of) this group.
        """
        pooled_member_sets = [
            _member_scan_numbers(fv.members)
            for fv in self.workspace.results
            if fv.pooled and fv.members
        ]
        rows = []
        for g in self.workspace.groups:
            scan_numbers = {m.scan_number for m in g.members if m.scan_number is not None}
            pooled = bool(scan_numbers) and any(scan_numbers & s for s in pooled_member_sets)
            rows.append(GroupRow(
                label=_group_label(g.key_fields),
                key_fields=dict(g.key_fields),
                members=list(g.members),
                pooled=pooled,
            ))
        return rows

    # ---- warnings passthrough ---------------------------------------------
    @property
    def warnings(self) -> list:
        return list(self.workspace.warnings)

    # ---- selection ----------------------------------------------------------
    def select(self, fit_view) -> None:
        """Slot the dock calls when the user picks a member fit; the payload stays a ``FitView``."""
        self.selection_changed.emit(fit_view)

    # ---- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        """Explicit re-pull slot: re-emits ``refreshed`` so the dock re-reads the properties above."""
        self.refreshed.emit()

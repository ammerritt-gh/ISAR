"""GroupDropMixin — a plot dock accepts a scan-group drop and restricts its view (GUI Pass 2, 2A).

Mixed into a :class:`~PySide6.QtWidgets.QDockWidget` whose ``self._vm`` implements the Part-2A
restriction seam (``can_accept`` / ``set_scan_restriction`` / ``restriction_active``). A drop of a
manual group (or a raw multi-selection) restricts the view to the group's covered scans — a pure,
reversible VIEW filter (G10), never a re-fit or a write. A refused drop reports the destination's
specific reason on the window status bar (there is no toast facility); a "Show all" banner clears it.

Only unit ids ever cross into the engine — the dock hands the VM a set of scan ids, never a "group".
"""
from __future__ import annotations

from html import escape
import logging

from PySide6.QtWidgets import QGraphicsView, QLabel

from isar_gui.mime import MIME_GROUP_REF, MIME_SCAN_IDS, decode_group_ref, decode_scan_ids

log = logging.getLogger(__name__)

_BANNER_STYLE = ("QLabel#groupBanner { background: #dbe9ff; color: #17324f;"
                 " border: 1px solid #9cc0f0; border-radius: 3px; padding: 3px 6px; }")


class GroupDropMixin:
    """Drop-target behaviour for a plot dock. Host: set ``self._vm`` first, then call
    :meth:`_init_group_drop` with the dock body's outer layout (the banner inserts at the top)."""

    def _init_group_drop(self, outer_layout) -> None:
        self.setAcceptDrops(True)
        self._grp_label = "selection"
        self._grp_count = 0
        # The dock body, id-scoped so a drag-reveal edge glow can be applied without cascading into
        # the pyqtgraph plot children (R12).
        self._grp_body = outer_layout.parentWidget()
        if self._grp_body is not None:
            self._grp_body.setObjectName("groupDropBody")
        self._grp_banner = QLabel(self)
        self._grp_banner.setObjectName("groupBanner")
        self._grp_banner.setWordWrap(True)
        self._grp_banner.setStyleSheet(_BANNER_STYLE)
        self._grp_banner.setVisible(False)
        self._grp_banner.linkActivated.connect(lambda _l: self._clear_group_restriction())
        outer_layout.insertWidget(0, self._grp_banner)
        # Keep the banner honest when the VM rebuilds (a refresh may clear the restriction).
        self._vm.spec_changed.connect(self._sync_group_banner)
        # The dock body owns drops; the inner pyqtgraph QGraphicsView must NOT accept them (view AND
        # viewport — pyqtgraph turns drops on for both), or it logs "dragLeaveEvent received before
        # drag enter" and fights the dock's drag handling (R17).
        from isar_gui.plotspec_pg import disable_view_drops
        for view in self.findChildren(QGraphicsView):
            disable_view_drops(view)

    # ---- drop target ------------------------------------------------------
    @staticmethod
    def _keys(md):
        return decode_scan_ids(md.data(MIME_SCAN_IDS).data()) if md.hasFormat(MIME_SCAN_IDS) else []

    def dragEnterEvent(self, event):  # noqa: N802
        keys = self._keys(event.mimeData())
        if not keys:
            return   # not a scan drag (e.g. a data-file url) -> let it propagate to the window
        ok, reason = self._vm.can_accept(keys)
        if ok:
            event.acceptProposedAction()
        else:
            if reason:
                self._status(reason)
            # Claim the event (no-drop) so Qt does NOT propagate the drag into the child
            # QGraphicsView, which would log "drag leave received before drag enter".
            event.accept()

    def dragMoveEvent(self, event):  # noqa: N802
        keys = self._keys(event.mimeData())
        if not keys:
            return
        if self._vm.can_accept(keys)[0]:
            event.acceptProposedAction()
        else:
            event.accept()   # keep claiming; forbidden-cursor, no propagation into the plot view

    def dropEvent(self, event):  # noqa: N802
        md = event.mimeData()
        keys = self._keys(md)
        if not keys:
            return
        ok, reason = self._vm.can_accept(keys)
        if not ok:
            self._status(reason)
            return
        label = "selection"
        if md.hasFormat(MIME_GROUP_REF):
            ref = decode_group_ref(md.data(MIME_GROUP_REF).data())
            if ref and ref.get("label"):
                label = ref["label"]
        self.apply_group(keys, label)
        event.acceptProposedAction()

    # ---- drag-reveal: edge-glow while a compatible scan drag is in flight (R12) ----
    def arm_group_drop(self, active: bool, keys) -> None:
        """Glow this dock's edge as a valid target while a scan drag is in flight — but ONLY when it
        can actually accept the scans. No glow = not a valid target (the honest 'not available' cue,
        e.g. unfitted scans on a plot). The window calls this only for the VISIBLE dock."""
        ok = bool(active) and self._vm.can_accept(list(keys or []))[0]
        if self._grp_body is None:
            return
        self._grp_body.setStyleSheet(
            "#groupDropBody { border: 2px solid #5b9bd5; border-radius: 4px; }" if ok else "")

    # ---- restriction lifecycle -------------------------------------------
    def apply_group(self, keys, label: str = "selection") -> None:
        """Restrict the view to ``keys`` (a group's covered ids) and show the banner. Also the entry
        point the window uses for the menu ('Send to') path."""
        self._grp_label = label
        self._grp_count = len(keys)
        self._vm.set_scan_restriction(keys)
        self._sync_group_banner()
        note = getattr(self._vm, "other_q_count", None)
        if callable(note) and note() > 0:
            self._status(f"showing one q; {note()} other q bin(s) hidden")

    def _clear_group_restriction(self) -> None:
        self._vm.set_scan_restriction(None)
        self._grp_banner.setVisible(False)

    def _sync_group_banner(self) -> None:
        # A refresh may have auto-cleared a now-stale restriction (all ids gone) — say so, once.
        take_stale = getattr(self._vm, "take_restriction_stale", None)
        if callable(take_stale) and take_stale():
            self._status(f"group '{self._grp_label}' scans are no longer available — showing all")
        if not getattr(self._vm, "restriction_active", False):
            self._grp_banner.setVisible(False)
            return
        self._grp_banner.setText(
            f"<b>Showing group</b> '{escape(str(self._grp_label))}' "
            f"({self._grp_count} scan(s)) "
            '&nbsp; <a href="#all">Show all</a>')
        self._grp_banner.setVisible(True)

    def _status(self, msg: str) -> None:
        try:
            self.window().statusBar().showMessage(msg)
        except (AttributeError, RuntimeError) as exc:   # no MainWindow parent (e.g. a bare test dock)
            log.debug("group-drop status not shown (%s): %s", exc, msg)

"""ScanListViewModel -- the scan list's snapshot, current spectrum and selection rule.

Holds the :class:`~isar.workspace_api.scan_list.ScanListSnapshot` of the open experiment (re-read on a
bind and on a workspace change) and the stable key of the current spectrum. Identity is always a
spectrum's unit key, never a position or a display label, so regrouping or a background update keeps
the selection.

No feedback loops: ``set_workspace``, ``on_workspace_changed`` and ``set_active_key`` never emit
``display_requested`` or ``inspect_requested``. Only :meth:`select` asks for the display, so a
background refresh never moves the central page under the user.
"""
from __future__ import annotations

from PySide6.QtCore import Signal

from isar.workspace_api.scan_list import ScanListSnapshot, scan_list
from isar_gui.viewmodels.base import ViewModel


class ScanListViewModel(ViewModel):
    snapshot_changed = Signal()         # after every rebuild: bind, background update
    display_requested = Signal(object)  # FitView | str unit key | None: MainWindow._route_inspect's payload
    current_changed = Signal(object)    # str key | None: the list's current spectrum or pooled row
    inspect_requested = Signal(str)     # open the inspector column on this key

    def __init__(self, ws, parent=None) -> None:
        super().__init__(ws, parent)
        self._snapshot: ScanListSnapshot = scan_list(ws)
        self._current_key: str | None = None

    # ---- reads ---------------------------------------------------------------------------
    @property
    def snapshot(self) -> ScanListSnapshot:
        return self._snapshot

    @property
    def current_key(self) -> str | None:
        return self._current_key

    # ---- the workspace changed -----------------------------------------------------------
    def set_workspace(self, ws) -> None:
        """Rebind: re-read, keep ``current_key`` only if it still exists, emit ``snapshot_changed``."""
        self._ws = ws
        self._reread()

    def on_workspace_changed(self, changed=None) -> None:
        """Re-read after the facade refresh; emits ``snapshot_changed``; never ``display_requested``."""
        self._reread()

    def _reread(self) -> None:
        self._snapshot = scan_list(self._ws)
        cleared = self._current_key is not None and self._snapshot.spectrum(self._current_key) is None
        if cleared:
            self._current_key = None
        self.snapshot_changed.emit()
        if cleared:
            self.current_changed.emit(None)

    # ---- selection -----------------------------------------------------------------------
    def select(self, key: str | None) -> None:
        """User selection: set current, emit ``current_changed`` and ``display_requested``.

        A fitted spectrum hands its ``FitView`` to the display, any other key its unit key, and
        ``None`` clears the display: today's rule of ``ScansViewModel.request_inspect``."""
        self._current_key = key
        self.current_changed.emit(key)
        row = None if key is None else self._snapshot.spectrum(key)
        fit = None if row is None else row.fit
        self.display_requested.emit(key if fit is None else fit)

    def inspect(self, key: str) -> None:
        """Select ``key`` if it is not current, then ask for the inspector column."""
        if key != self._current_key:
            self.select(key)
        self.inspect_requested.emit(key)

    def set_active_key(self, key: str | None) -> None:
        """The active unit changed elsewhere (``ActiveUnitController``): follow it, ``current_changed``
        only, and only when it differs."""
        if key == self._current_key:
            return
        self._current_key = key
        self.current_changed.emit(key)

    # ---- view state (filled with the list itself) ----------------------------------------
    def load_view_state(self, session) -> None:
        """Foundation: no-op."""

    def save_view_state(self, session) -> None:
        """Foundation: no-op."""

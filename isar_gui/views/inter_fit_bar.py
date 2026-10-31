"""The Scan(s) flat fit dropdown, actions and info line.

It shows a ``BarState`` and reports the user's clicks as signals. It holds no writer and reads nothing:
the page's view model answers the signals and calls the writers (AGENTS.md hard rule 7).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QToolButton, QWidget

from isar.workspace_api.inter_spectra import FitEntry
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.viewmodels.inter_fits import BarState, entry_tip

MENU_TIP = ("Choose the fit of this spectrum to show. All fits are listed directly, oldest first. "
            "With this button focused, Up and Down show the previous or next fit. "
            "In the open list, Up and Down move, Enter selects and Esc closes. "
            "Viewing a fit does not make it authoritative.")
AUTHORITATIVE_TIP = "Make the fit shown the one ISAR uses for this spectrum."
LOCK_TIP = "Keep this fit as the one ISAR uses: no batch will refit this spectrum."
UNLOCK_TIP = "Let batches refit this spectrum again."
WATERFALL_TIP = "Draw this spectrum's fits one above another, oldest at the bottom."


def _apply(button, off_reason: str | None, tip: str) -> None:
    button.setEnabled(off_reason is None)
    button.setToolTip(tip if off_reason is None else off_reason)


class _FitMenuButton(QToolButton):
    step_requested = Signal(int)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.modifiers() == Qt.KeyboardModifier.NoModifier and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.step_requested.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class InterFitBar(QWidget):
    fit_chosen = Signal(str)                # a fit id from the dropdown
    make_authoritative_requested = Signal()
    lock_requested = Signal()               # locks the fit shown, or unlocks the spectrum when locked
    waterfall_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("interFitBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.menu_button = _FitMenuButton(self)
        self.menu_button.setObjectName("interFitMenu")
        self.menu_button.setText("Inter-spectra fitting")
        self.menu_button.setAccessibleName("Inter-spectra fitting: choose the fit to show")
        self.menu_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)
        self.menu.setToolTipsVisible(True)
        self.menu_button.setMenu(self.menu)
        self._entries = None
        self._viewed = "fast"
        self.menu_button.step_requested.connect(self._step)
        self.menu.aboutToShow.connect(self._mark_viewed)
        self.authoritative_button = QPushButton("Make authoritative", self)
        self.authoritative_button.setObjectName("interMakeAuthoritative")
        self.lock_button = QPushButton("Lock this fit", self)
        self.lock_button.setObjectName("interLock")
        self.waterfall_button = QToolButton(self)
        self.waterfall_button.setObjectName("interWaterfall")
        self.waterfall_button.setText("Waterfall of fits")
        self.waterfall_button.setAccessibleName("Waterfall of this spectrum's fits")
        self.waterfall_button.setCheckable(True)
        self.info = ElidedLabel("", self, Qt.TextElideMode.ElideRight)
        self.info.setObjectName("interFitInfo")
        for widget in (self.menu_button, self.authoritative_button, self.lock_button, self.waterfall_button):
            row.addWidget(widget)
        row.addWidget(self.info, 1)
        self.authoritative_button.clicked.connect(lambda _checked=False: self.make_authoritative_requested.emit())
        self.lock_button.clicked.connect(lambda _checked=False: self.lock_requested.emit())
        self.waterfall_button.toggled.connect(self.waterfall_toggled)

    def show_state(self, state: BarState) -> None:
        loading = state.entries is None
        self.menu_button.setText("Inter-spectra fitting (reading…)" if loading else "Inter-spectra fitting")
        self.menu_button.setEnabled(state.menu_off is None)
        self.menu_button.setToolTip(state.menu_off or MENU_TIP)
        self._viewed = state.viewed
        self._fill_menu(state.entries or (), state.viewed)
        _apply(self.authoritative_button, state.authoritative_off, AUTHORITATIVE_TIP)
        self.lock_button.setText(state.lock_text)
        _apply(self.lock_button, state.lock_off, UNLOCK_TIP if state.locked else LOCK_TIP)
        with QSignalBlocker(self.waterfall_button):
            self.waterfall_button.setChecked(state.waterfall_on)
        _apply(self.waterfall_button, state.waterfall_off, WATERFALL_TIP)
        self.info.set_full_text(state.info)
        self.info.setToolTip(state.info_tip)

    def _fill_menu(self, entries: tuple[FitEntry, ...], viewed: str) -> None:
        # Keep keyboard position when the plot refreshes while the menu is open.
        active = self.menu.activeAction()
        active_id = active.data() if active is not None and self.menu.isVisible() else viewed
        if entries != self._entries:
            self.menu.clear()
            for entry in entries:
                marks = [m for m, on in (("authoritative", entry.authoritative), ("stale", entry.stale),
                                         ("unchanged", entry.batch_id is not None and not entry.changed)) if on]
                action = self.menu.addAction(" · ".join([entry.label, *marks]))
                action.setCheckable(True)
                action.setData(entry.fit_id)
                action.setToolTip(entry_tip(entry))
                action.triggered.connect(lambda _checked=False, fit=entry.fit_id: self.fit_chosen.emit(fit))
            self._entries = entries
        for action in self.menu.actions():
            action.setChecked(action.data() == viewed)
        active = next((a for a in self.menu.actions() if a.data() == active_id), None)
        self.menu.setActiveAction(active)

    def _mark_viewed(self) -> None:
        self.menu.setActiveAction(next((a for a in self.menu.actions() if a.data() == self._viewed), None))

    def _step(self, direction: int) -> None:
        ids = [entry.fit_id for entry in self._entries or ()]
        if self._viewed not in ids:
            return
        index = ids.index(self._viewed)
        target = max(0, min(len(ids) - 1, index + direction))
        if target != index:
            # Advance before emitting: another key can arrive before the queued redraw.
            self._viewed = ids[target]
            self.fit_chosen.emit(self._viewed)

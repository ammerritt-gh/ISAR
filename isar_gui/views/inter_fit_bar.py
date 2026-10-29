"""The Scan(s) inter-spectra bar: the fit menu, Make authoritative, Lock, Waterfall and one info line.

It shows a ``BarState`` and reports the user's clicks as signals. It holds no writer and reads nothing:
the page's view model answers the signals and calls the writers (AGENTS.md hard rule 7).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QToolButton, QWidget

from isar.workspace_api.inter_spectra import FitEntry
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.viewmodels.inter_fits import BarState, entry_tip, group_fits

MENU_TIP = "Choose the fit of this spectrum to show: Fast solo, or an inter-spectra refit."
AUTHORITATIVE_TIP = "Make the fit shown the one ISAR uses for this spectrum."
LOCK_TIP = "Keep this fit as the one ISAR uses: no batch will refit this spectrum."
UNLOCK_TIP = "Let batches refit this spectrum again."
WATERFALL_TIP = "Draw this spectrum's fits one above another, oldest at the bottom."


def _apply(button, off_reason: str | None, tip: str) -> None:
    button.setEnabled(off_reason is None)
    button.setToolTip(tip if off_reason is None else off_reason)


class InterFitBar(QWidget):
    fit_chosen = Signal(str)                # a fit id from the menu
    make_authoritative_requested = Signal()
    lock_requested = Signal()               # locks the fit shown, or unlocks the spectrum when locked
    waterfall_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("interFitBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.menu_button = QToolButton(self)
        self.menu_button.setObjectName("interFitMenu")
        self.menu_button.setText("Inter-spectra fitting")
        self.menu_button.setAccessibleName("Inter-spectra fitting: choose the fit to show")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)
        self.menu.setToolTipsVisible(True)
        self.menu_button.setMenu(self.menu)
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
        self.menu.clear()
        solo, groups = group_fits(entries)
        for entry in solo:
            self._choice(self.menu, entry, viewed)
        for group in groups:
            batch = self._submenu(self.menu, group.label, f"The refits of {group.label}, oldest first.")
            for entry in group.changed:
                self._choice(batch, entry, viewed)
            if group.unchanged:
                count = len(group.unchanged)
                collapsed = self._submenu(batch, f"{count} refit{'s' if count != 1 else ''} changed nothing",
                                          "Refits that left the fit unchanged, kept here so none is lost.")
                for entry in group.unchanged:
                    self._choice(collapsed, entry, viewed)

    @staticmethod
    def _submenu(parent: QMenu, title: str, tip: str) -> QMenu:
        menu = parent.addMenu(title)
        menu.setToolTipsVisible(True)
        menu.menuAction().setToolTip(tip)
        return menu

    def _choice(self, menu: QMenu, entry: FitEntry, viewed: str) -> None:
        text = entry.label if entry.batch_id is None else (entry.stage_label or entry.label)
        marks = [m for m, on in (("authoritative", entry.authoritative), ("stale", entry.stale)) if on]
        action = menu.addAction(" · ".join([text, *marks]))
        action.setCheckable(True)
        action.setChecked(entry.fit_id == viewed)
        action.setToolTip(entry_tip(entry))
        fit_id = entry.fit_id
        action.triggered.connect(lambda _checked=False, fit=fit_id: self.fit_chosen.emit(fit))

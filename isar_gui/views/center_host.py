"""CenterHost — the central view switcher (GUI Pass 3, M1-2).

Hosts the five display surfaces (the C2 fit inspector plus the four plot panes) as switchable pages
of one permanent central widget, replacing the four temporary ``_PaneDock`` shims. A slim top bar of
checkable, auto-exclusive :class:`~PySide6.QtWidgets.QToolButton`\\s selects the visible page; a
right-aligned "Auto" toggle records whether the view should follow selection/campaign events (the
follow rules themselves live in the window — this widget only holds the flag).

Design notes:
- The switcher buttons use ``clicked`` (a genuine user press), never ``toggled``: the programmatic
  ``setChecked`` that keeps the bar in sync with :meth:`show_view` must not be mistaken for a user
  view change (which clears auto-follow), the same ``triggered``-vs-``toggled`` discipline the Docks
  menu uses.
- Hidden pyqtgraph pages accumulate stale / zero-size render state, so :meth:`show_view` calls the
  freshly-shown page's ``refresh()`` (when it has one) after switching.
- The host does not own the pages' viewmodels; :meth:`dispose` only detaches and deletes the page
  widgets it was given, so a rebind leaves no dangling receivers.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class CenterHost(QWidget):
    """A page switcher over the central display surfaces.

    Pages are registered with :meth:`add_page` in display order; :meth:`show_view` switches the
    visible page (refreshing it) and, on a user-driven switch, clears auto-follow.
    """

    view_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys: list[str] = []
        self._pages: dict[str, QWidget] = {}
        self._buttons: dict[str, QToolButton] = {}
        self._current: str | None = None
        self._auto = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- switcher bar --------------------------------------------------
        self._bar = QWidget(self)
        bar_l = QHBoxLayout(self._bar)
        bar_l.setContentsMargins(4, 2, 4, 2)
        bar_l.setSpacing(2)
        self._bar_layout = bar_l
        # Auto-exclusive group so exactly one view button is checked at a time.
        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        # The stretch and the Auto button anchor the right edge; view buttons insert before them.
        bar_l.addStretch(1)
        self._auto_btn = QToolButton(self._bar)
        self._auto_btn.setText("Auto")
        self._auto_btn.setCheckable(True)
        self._auto_btn.setChecked(True)
        self._auto_btn.setAutoRaise(True)
        self._auto_btn.setToolTip("View follows selection and campaign events")
        self._auto_btn.toggled.connect(self._on_auto_toggled)
        bar_l.addWidget(self._auto_btn)
        outer.addWidget(self._bar)

        # --- pages ---------------------------------------------------------
        self._stack = QStackedLayout()
        outer.addLayout(self._stack, 1)

    # ---- registration -----------------------------------------------------
    def add_page(self, key: str, title: str, widget: QWidget) -> None:
        """Register ``widget`` under ``key`` (label ``title``), appended after existing pages."""
        if key in self._pages:
            raise KeyError(f"duplicate center-host page key {key!r}")
        btn = QToolButton(self._bar)
        btn.setText(title)
        btn.setCheckable(True)
        btn.setAutoRaise(True)
        # `clicked` is user-only; the setChecked sync in show_view must not read as a view change.
        btn.clicked.connect(lambda _checked=False, k=key: self.show_view(k, user=True))
        self._btn_group.addButton(btn)
        # Keep the switcher buttons left of the stretch + Auto (inserted at the tail-2 position).
        self._bar_layout.insertWidget(self._bar_layout.count() - 2, btn)
        self._stack.addWidget(widget)
        self._keys.append(key)
        self._pages[key] = widget
        self._buttons[key] = btn
        if self._current is None:
            self._current = key
            btn.setChecked(True)

    # ---- switching --------------------------------------------------------
    def show_view(self, key: str, *, user: bool = False) -> None:
        """Switch to page ``key`` and refresh it. ``user=True`` clears auto-follow."""
        if key not in self._pages:
            raise KeyError(f"unknown center-host page key {key!r}")
        if user:
            self.auto_follow = False
        changed = key != self._current
        self._current = key
        btn = self._buttons[key]
        if not btn.isChecked():
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)
        self._stack.setCurrentWidget(self._pages[key])
        # A hidden pyqtgraph page carries stale/zero-size render state; refresh now that it is shown.
        refresh = getattr(self._pages[key], "refresh", None)
        if callable(refresh):
            refresh()
        if changed:
            self.view_changed.emit(key)

    # ---- auto-follow ------------------------------------------------------
    def _on_auto_toggled(self, checked: bool) -> None:
        self._auto = bool(checked)

    @property
    def auto_follow(self) -> bool:
        return self._auto

    @auto_follow.setter
    def auto_follow(self, value: bool) -> None:
        value = bool(value)
        self._auto = value
        if self._auto_btn.isChecked() != value:
            self._auto_btn.blockSignals(True)
            self._auto_btn.setChecked(value)
            self._auto_btn.blockSignals(False)

    # ---- accessors --------------------------------------------------------
    @property
    def current_key(self) -> str | None:
        return self._current

    def pane(self, key: str) -> QWidget:
        """The page widget registered under ``key`` (KeyError on an unknown key)."""
        if key not in self._pages:
            raise KeyError(f"unknown center-host page key {key!r}")
        return self._pages[key]

    def glw_for(self, key: str):
        """The page's ``_glw`` graphics widget, or ``None`` if it has none (KeyError on unknown key)."""
        if key not in self._pages:
            raise KeyError(f"unknown center-host page key {key!r}")
        return getattr(self._pages[key], "_glw", None)

    # ---- teardown ---------------------------------------------------------
    def dispose(self) -> None:
        """Detach and delete every page + switcher button so a rebind leaves no dangling receivers."""
        self._auto_btn.toggled.disconnect(self._on_auto_toggled)
        for key in list(self._keys):
            btn = self._buttons.pop(key)
            self._btn_group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
            page = self._pages.pop(key)
            self._stack.removeWidget(page)
            page.setParent(None)
            page.deleteLater()
        self._keys.clear()
        self._current = None

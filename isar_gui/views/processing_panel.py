"""The right panel's Processing tab and the slim rail that opens and closes it.

:class:`ProcessingPanel` holds the readiness line, the *Fast-pass all ready (N)* button (the window's one
analysis entry point), the line for an ingest, export or campaign running in the window, and below them the
analysis queue (:class:`~isar_gui.views.queue_panel.QueuePanel`), shown once the window hands the panel its
controller. It only displays what it is handed and emits its one request signal; the window decides what it does.

:class:`ProcessingRail` is the strip at the display's right edge. While the right panel is collapsed it reads
"Processing ‹" and, when the queue has work, what is happening and what needs a look ("Processing ‹ · 5 running
· 14 waiting · 2 need a look"); while expanded it is the same strip reading "›" and collapses the panel.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedButton
from isar_gui.views.queue_panel import QueuePanel, rail_status
from isar_gui.views.setup_section import RUN_TEXT, RUN_TIP

RAIL_WIDTH = 24


class ProcessingPanel(QWidget):
    run_requested = Signal()            # "Fast-pass all ready (N)" -> MainWindow.start_analysis

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("processing_panel")
        self._run = ElidedButton(RUN_TEXT, self)
        self._run.setObjectName("processing_run")
        self._run.setToolTip(RUN_TIP)
        self._run.setDefault(True)
        self._run.clicked.connect(self.run_requested)
        self._reason = QLabel("", self)
        self._reason.setObjectName("processing_reason")
        self._reason.setWordWrap(True)
        self._activity = QLabel("", self)
        self._activity.setObjectName("processing_activity")
        self._activity.setToolTip("What ISAR is doing in this experiment besides the analysis queue below.")
        self._activity.setWordWrap(True)
        self._queue = QueuePanel(self)
        self._queue.hide()                  # shown once the window hands the panel a controller

        self._layout = layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(self._run)
        layout.addWidget(self._reason)
        layout.addWidget(self._activity)
        layout.addWidget(self._queue)
        layout.addStretch(1)
        self.set_run_state(False, RUN_TEXT, "Open or create an experiment first.")
        self.set_activity("")

    def set_readiness_widget(self, widget: QWidget) -> None:
        """Place the readiness line directly above the *Fast-pass all ready* button."""
        self._layout.insertWidget(self._layout.indexOf(self._run), widget)

    def set_controller(self, controller) -> None:
        """The queue controller the panel shows and drives (``None``: none). The queue takes the room below the
        run controls while it is shown; without one the spacer does."""
        self._queue.set_controller(controller)
        self._queue.setVisible(controller is not None)
        self._layout.setStretch(self._layout.indexOf(self._queue), 1 if controller is not None else 0)
        self._layout.setStretch(self._layout.count() - 1, 0 if controller is not None else 1)

    def set_activity(self, text: str) -> None:
        """The line for an ingest, an export or a campaign running in the window (``""`` hides it)."""
        self._activity.setText(text)
        self._activity.setVisible(bool(text))

    def set_run_state(self, enabled: bool, label: str, reason: str) -> None:
        """The *Fast-pass all ready (N)* button: ``label`` carries the count; a greyed button says why, beside it
        and on hover."""
        self._run.set_full_text(label)
        self._run.setEnabled(enabled)
        self._run.setToolTip(RUN_TIP if enabled else (reason or RUN_TIP))
        self._reason.setText("" if enabled else reason)
        self._reason.setVisible(not enabled and bool(reason))


class ProcessingRail(QWidget):          # the slim right-edge strip; paints vertical text
    toggled = Signal()                  # clicked: expand when collapsed, collapse when expanded

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("processing_rail")
        self.setFixedWidth(RAIL_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapsed = True
        self._controller = None
        self._plain = ""                    # what is happening / what needs a look, from the controller
        self._look = ""
        self._soon = QTimer(self)           # coalesces a burst of `changed` into one redraw
        self._soon.setSingleShot(True)
        self._soon.setInterval(0)
        self._soon.timeout.connect(self.refresh)
        self._apply_texts()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """Reads "Processing ‹" when collapsed, "›" when expanded."""
        if collapsed == self._collapsed:
            return
        self._collapsed = bool(collapsed)
        self._apply_texts()
        self.update()

    def _parts(self) -> tuple[str, str]:
        """(the text, the part of it that needs a look); only the collapsed rail carries the status."""
        if not self._collapsed:
            return "›", ""
        plain = " · ".join(p for p in ("Processing ‹", self._plain) if p)
        return plain, (" · " + self._look if self._look else "")

    def text(self) -> str:
        plain, look = self._parts()
        return plain + look

    def set_controller(self, controller) -> None:
        """The queue controller whose state the rail summarises (``None``: none)."""
        if self._controller is not None:
            self._controller.changed.disconnect(self._schedule)
        self._controller = controller
        if controller is not None:
            controller.changed.connect(self._schedule)
        self.refresh()

    def _schedule(self) -> None:
        if not self._soon.isActive():
            self._soon.start()

    def refresh(self) -> None:
        """Read the controller now."""
        self._soon.stop()
        c = self._controller
        self._plain, self._look = rail_status(c.view()) if c is not None else ("", "")
        self._apply_texts()
        self.update()

    def _apply_texts(self) -> None:
        if not self._collapsed:
            name = tip = "Collapse the processing panel"
        else:
            name = "Expand the processing panel"
            now = " · ".join(p for p in (self._plain, self._look) if p)
            tip = name
            if now:
                name = f"{name}: {now}"
                tip = (f"{name}.\n\"Need a look\" counts analyzers that were held, failed or finished with a "
                       "warning; open the panel to see which and why." if self._look else f"{name}.")
        self.setAccessibleName(name)
        self.setToolTip(tip)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(RAIL_WIDTH, 120)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(RAIL_WIDTH, 40)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggled.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.AlternateBase))
        painter.setPen(palette.color(QPalette.ColorRole.Mid))
        painter.drawLine(0, 0, 0, self.height())
        plain, look = self._parts()
        metrics = QFontMetrics(self.font())
        # Vertical text reading top to bottom: rotate the painter, draw in the rotated frame. The part that needs
        # a look keeps its room (and its warning colour); the rest is elided to what is left.
        painter.translate(self.width(), 0)
        painter.rotate(90)
        rotated = QRect(0, 0, self.height(), self.width())
        room = self.height() - 8
        look_w = metrics.horizontalAdvance(look)
        plain = metrics.elidedText(plain, Qt.TextElideMode.ElideRight, max(room - look_w, 0))
        x = (self.height() - metrics.horizontalAdvance(plain) - look_w) // 2
        y = (self.width() + metrics.ascent() - metrics.descent()) // 2
        painter.setPen(QColor(palette.color(QPalette.ColorRole.WindowText)))
        painter.drawText(x, y, plain)
        if look:
            painter.setPen(QColor(TONE_COLOR[Tone.WARN]))
            painter.drawText(x + metrics.horizontalAdvance(plain), y, look)
        if self.hasFocus():
            painter.setPen(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRect(rotated.adjusted(1, 1, -2, -2))
        painter.end()

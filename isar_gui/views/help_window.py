"""HelpWindow -- Help > *Quick guide* and *What the status words mean*, in one reused, non-modal window.

Both pages are static text shipped with ISAR: the guide is ``isar_gui/resources/quick_guide.html``, read when it is
opened, and the status page is generated from :mod:`isar_gui.status_words`, the table the scan list displays from.
The window is shown with ``show()``/``raise_()`` and never ``exec()``: a page to read while the window stays usable.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from isar_gui import status_words

log = logging.getLogger(__name__)

GUIDE_FILE = Path(__file__).resolve().parent.parent / "resources" / "quick_guide.html"
GUIDE, WORDS = "guide", "words"
TITLES = {GUIDE: "Quick guide", WORDS: "What the status words mean"}


def guide_html() -> str:
    """The shipped quick guide; a missing or unreadable file is said on the page and in the log."""
    try:
        return GUIDE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("quick guide unreadable: %s: %s", GUIDE_FILE, exc)
        return f"<p>The quick guide could not be read: {html.escape(str(GUIDE_FILE))} ({html.escape(str(exc))}).</p>"


class HelpWindow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("help_window")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(560, 640)
        self.page = ""
        self._browser = QTextBrowser(self)
        self._browser.setObjectName("help_text")
        self._browser.setOpenExternalLinks(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._browser)

    def show_page(self, page: str) -> None:
        """Show ``GUIDE`` or ``WORDS`` in this window (the same window both times), in front."""
        self.page = page
        self.setWindowTitle(f"ISAR — {TITLES[page]}")
        self._browser.setHtml(guide_html() if page == GUIDE else status_words.page_html())
        self._browser.verticalScrollBar().setValue(0)
        self.show()
        self.raise_()
        self.activateWindow()

    def text(self) -> str:
        return self._browser.toPlainText()

"""Amendment 7: Help > Quick guide and What the status words mean. Offscreen Qt, the real MainWindow."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

import isar_gui.status_words as sw
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views import help_window


def _win(qtbot):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    return w


def _help_actions(w):
    menu = next(m for m in w.menuBar().findChildren(type(w._view_menu)) if m.title() == "&Help")
    return {a.text(): a for a in menu.actions() if a.text()}


def test_the_help_menu_holds_the_guide_the_status_words_and_the_log_folder(qtbot):
    w = _win(qtbot)
    assert set(_help_actions(w)) == {"Quick guide", "What the status words mean", "Open GUI Log Folder"}


def test_both_pages_open_in_one_reused_non_modal_window(qtbot):
    w = _win(qtbot)
    actions = _help_actions(w)
    assert w._help_window is None
    actions["Quick guide"].trigger()
    win = w._help_window
    assert win.isVisible() and not win.isModal() and win.page == help_window.GUIDE
    guide = win.text()
    for key in ("Drop data files or a folder here", "Choose curves folder…", "Run analysis…", "Needs attention",
                "Fast pass", "Export results…"):
        assert key in guide, key
    actions["What the status words mean"].trigger()
    assert w._help_window is win and win.isVisible() and win.page == help_window.WORDS     # the same window
    words = win.text()
    for column, entries in sw.TABLE.items():
        for word, entry in entries.items():
            assert word in words and (entry.plain in words or column == "fast" and word == "held"), (column, word)
    assert "held: needs" in words and "writes the fitting code's words" in words
    w.statusBar().showMessage("still usable")                    # a page to read while the window stays usable
    QApplication.processEvents()
    assert w.statusBar().currentMessage() == "still usable"


def test_the_guide_names_only_labels_the_window_shows(qtbot):
    """The guide quotes buttons by their text: each must be a label on screen now (so a rename fails here)."""
    w = _win(qtbot)
    shown = {b.text() for b in w.findChildren(QPushButton)} | {b.full_text() for b in w.findChildren(QPushButton)
                                                                if hasattr(b, "full_text")}
    for label in ("Add data files…", "Add data folder…", "Choose curves folder…", "Run analysis…", "Open inspector"):
        assert label in shown, label


def test_a_missing_guide_file_is_said_on_the_page_not_swallowed(qtbot, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(help_window, "GUIDE_FILE", tmp_path / "gone.html")
    with caplog.at_level("WARNING"):
        page = help_window.guide_html()
    assert "could not be read" in page and "quick guide unreadable" in caplog.text

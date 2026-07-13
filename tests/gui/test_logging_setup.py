"""Focused tests for the durable GUI log setup (no QApplication required)."""
from __future__ import annotations

import logging

from isar_gui import logging_setup


def test_configure_gui_logging_is_utf8_rotating_and_idempotent(monkeypatch, tmp_path):
    root = logging.getLogger()
    prior = [h for h in root.handlers if getattr(h, logging_setup._HANDLER_MARKER, False)]
    for handler in prior:
        root.removeHandler(handler)
        handler.close()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    try:
        path = logging_setup.configure_gui_logging()
        assert path == tmp_path / "appdata" / "isar_gui" / "isar-gui.log"
        handler = next(h for h in root.handlers if getattr(h, logging_setup._HANDLER_MARKER, False))
        assert handler.encoding.lower().replace("-", "") == "utf8"
        assert handler.maxBytes == 2 * 1024 * 1024
        assert handler.backupCount == 3
        assert logging_setup.configure_gui_logging() == path
    finally:
        for handler in list(root.handlers):
            if getattr(handler, logging_setup._HANDLER_MARKER, False):
                root.removeHandler(handler)
                handler.close()
        for handler in prior:
            root.addHandler(handler)


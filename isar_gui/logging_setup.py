"""Durable, process-wide logging for the desktop GUI.

The on-screen Log dock is useful while a window is open, but beamtime diagnosis also needs a file
that survives a crash and can be found without knowing a workspace path.  This module deliberately
does not import Qt, so it remains simple to test and can be configured before ``QApplication``.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_HANDLER_MARKER = "_isar_gui_rotating_log"
_LOG_FILENAME = "isar-gui.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3


def gui_log_dir() -> Path:
    """Preferred per-user log directory; `%APPDATA%` on Windows, otherwise the home directory."""
    appdata = os.environ.get("APPDATA")
    return (Path(appdata) / "isar_gui") if appdata else (Path.home() / "isar_gui")


def configure_gui_logging() -> Path | None:
    """Attach one UTF-8 2 MiB × 3 rotating file handler and return its path.

    The function is idempotent for a process (important for GUI smoke tests).  A directory failure
    falls back to the home directory; if both fail, stderr receives an explicit diagnostic and the
    application still starts with its normal on-screen failure surfaces.
    """
    preferred = gui_log_dir()
    fallback = Path.home() / "isar_gui"
    candidates = [preferred] if preferred == fallback else [preferred, fallback]
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return Path(handler.baseFilename)

    last_error = None
    failed_directories = []
    for directory in candidates:
        path = directory / _LOG_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
        except OSError as exc:
            last_error = exc
            failed_directories.append((directory, exc))
            continue
        setattr(handler, _HANDLER_MARKER, True)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        # Ensure the GUI's own status/failure trail reaches the durable file. A host may already be
        # more verbose (DEBUG), which we preserve.
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        for failed_directory, exc in failed_directories:
            logging.getLogger(__name__).warning(
                "could not create GUI log directory %s (%s); using %s",
                failed_directory, exc, path.parent)
        logging.getLogger(__name__).info("GUI log started: %s", path)
        return path

    print(f"ISAR GUI could not create its log file ({last_error})", file=sys.stderr)
    return None

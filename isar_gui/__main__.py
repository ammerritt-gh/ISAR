"""``python -m isar_gui <workspace>`` — the module entry point, equivalent to ``isar view``.

Under ``pythonw`` (the launcher's no-console start) a start-up failure would vanish, so it is written
to the GUI log and shown in a message box instead.
"""
from __future__ import annotations

import io
import logging
import sys
from typing import Callable


def _show_error_box(text: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "ISAR could not start", 0x10)


def run(argv: list[str], *, windowless: bool, show_error: Callable[[str], None]) -> int:
    if not windowless:
        from isar_gui.app import main

        return main(argv)

    from isar_gui.logging_setup import configure_gui_logging

    log = logging.getLogger(__name__)
    log_path = configure_gui_logging()
    details = f"Details: {log_path}" if log_path else "No GUI log file could be created."
    # pythonw has no stdout or stderr, so the app's own messages are captured for the dialog.
    captured = io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = captured
    try:
        from isar_gui.app import main

        rc = main(argv)
        if rc == 0:
            return 0
        reason = captured.getvalue().strip() or f"ISAR exited with code {rc}."
        log.error("GUI did not start (exit %s): %s", rc, reason)
    except Exception as exc:  # noqa: BLE001 — a windowless failure must reach the log and a dialog
        rc = 1
        reason = f"{type(exc).__name__}: {exc}"
        log.critical("GUI failed to start", exc_info=True)
    finally:
        sys.stdout, sys.stderr = saved
    show_error(f"{reason}\n\n{details}")
    return rc


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:], windowless=sys.stderr is None, show_error=_show_error_box))

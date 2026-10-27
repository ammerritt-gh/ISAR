"""app.main — the QApplication bootstrap for the ISAR GUI.

``main(argv)`` resolves a workspace, builds the :class:`~isar_gui.main_window.MainWindow`, and runs
the Qt event loop, returning a process exit code.

Workspace resolution:
  * an **explicit** path argument is opened read-only; a nonexistent/invalid one is a hard CLI error
    (stderr + exit 2 — no dialog yet; GUI error dialogs come later);
  * with **no argument**, leftover temporary work is offered for recovery before a generic recent
    workspace is opened. If neither is available, the window opens in EMPTY-START — no console
    error. A double-click launch must never fail on a stale recent, so an unopenable recent falls
    back silently.

Smoke hook: set ``ISAR_GUI_SMOKE_MS=<ms>`` to auto-quit the app after that many milliseconds with
exit 0 (a ``QTimer.singleShot`` posted after ``exec`` starts). Used by the offscreen live smoke
check so the shell can be launched end-to-end in CI without a human closing the window.
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar_gui.logging_setup import configure_gui_logging


def _install_exception_hook(log_path) -> None:
    """Log uncaught GUI exceptions and give the operator a durable-path prompt on screen."""
    previous = sys.excepthook

    def _hook(exc_type, exc, traceback):
        logging.getLogger(__name__).critical(
            "uncaught GUI exception", exc_info=(exc_type, exc, traceback))
        try:
            from PySide6.QtWidgets import QMessageBox
            location = str(log_path) if log_path is not None else "the Log dock and stderr"
            QMessageBox.critical(
                None, "ISAR encountered an unexpected error",
                "ISAR encountered an unexpected error. The details were recorded in " + location
                + ". You can also open the GUI log folder from Help.")
        except Exception as dialog_exc:  # noqa: BLE001 — preserve the original exception path
            logging.getLogger(__name__).error(
                "could not show uncaught-error dialog: %s", dialog_exc)
        previous(exc_type, exc, traceback)

    sys.excepthook = _hook


def _resolve_recent_workspace() -> "Workspace | None":
    """The most-recent still-openable workspace from the GUI session, or ``None``.

    Iterates the session recents (``isar_gui.session`` is Qt-free — safe to import here) newest
    first, skipping any that no longer exist on disk or no longer open as a workspace. A stale/
    removed/not-a-workspace recent must never error a no-arg launch, so it is skipped, not raised.

    A recent under the system temp directory is a transient scratch/test workspace (e.g. a pytest
    ``tmp_path`` bind that leaked into the real session file); auto-opening it on launch buries the
    empty-start "drop data here" surface behind a dead workspace, so such paths are skipped here as a
    defense in depth even though :meth:`Session.note_workspace` also refuses to record them.
    """
    import tempfile

    from isar_gui.session import load_session

    tmp_root = Path(tempfile.gettempdir()).resolve()
    for path in load_session().recent_workspaces:
        if not Path(path).is_dir():
            continue
        try:
            if Path(path).resolve().is_relative_to(tmp_root):
                logging.getLogger(__name__).info(
                    "skipping transient recent workspace under temp dir: %s", path)
                continue
        except OSError:
            # A path that cannot be resolved (permissions, gone mid-iteration) is treated like any
            # other unopenable recent: skip it rather than error the launch.
            continue
        try:
            return Workspace.open(path)
        except (WorkspaceNotFound, OSError):
            continue
    return None


def _maybe_recover_workspace() -> "Workspace | None":
    """Offer any leftover temporary workspaces for recovery (Phase C). Returns the recovered
    :class:`Workspace` if the operator opened one, else ``None`` (nothing to recover, or they chose
    Discard/Later). A modal recovery prompt is suppressed under the offscreen smoke hook so CI never
    blocks. Built lazily so the common no-candidates path adds no Qt latency.
    """
    if os.environ.get("ISAR_GUI_SMOKE_MS"):
        return None
    from isar_gui.workspace_manager import recovery_candidates

    candidates = recovery_candidates()
    if not candidates:
        return None
    from isar_gui.recovery_dialog import RecoveryDialog

    dlg = RecoveryDialog(candidates)
    dlg.exec()
    if dlg.chosen is None:
        return None
    try:
        return Workspace.open(dlg.chosen)
    except (WorkspaceNotFound, OSError):
        return None


def _resolve_default_workspace() -> "Workspace | None":
    """Resolve a no-argument launch without hiding unfinished temporary work.

    A saved recent workspace is merely a convenience. A recovery candidate may contain the data
    from the operator's immediately preceding session, so it must be offered first. Choosing Later
    (or having no candidate) falls through to the most-recent openable saved workspace.
    """
    recovered = _maybe_recover_workspace()
    return recovered if recovered is not None else _resolve_recent_workspace()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    explicit = bool(argv)

    ws = None
    if explicit:
        ws_path = argv[0]
        try:
            ws = Workspace.open(ws_path)
        except WorkspaceNotFound as exc:
            print(f"isar view: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"isar view: cannot open workspace {ws_path!r}: {exc}", file=sys.stderr)
            return 2

    # Configure durable logging before opening any Qt surface. A fatal construction error therefore
    # still has an operator-readable trace rather than only a vanished GUI process.
    log_path = configure_gui_logging()

    # Import Qt only now — never at module import time, so `import isar_gui` stays QApplication-free.
    from PySide6.QtWidgets import QApplication

    from isar_gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(argv)
    _install_exception_hook(log_path)

    if not explicit:
        # No path given: unfinished temporary work takes precedence over the generic recents list.
        # An explicit CLI arg suppresses both recovery and recent-workspace resolution.
        ws = _resolve_default_workspace()

    window = MainWindow(ws)  # ws may be None -> empty-start
    if log_path is not None:
        window.log(f"GUI log: {log_path} (Help ▸ Open GUI Log Folder)")
        window.statusBar().showMessage("GUI log ready — Help ▸ Open GUI Log Folder")
    window.show()
    logging.getLogger(__name__).info("main window shown")  # the launcher smoke test waits for this line

    smoke_ms = os.environ.get("ISAR_GUI_SMOKE_MS")
    if smoke_ms:
        from PySide6.QtCore import QTimer
        try:
            delay = max(0, int(smoke_ms))
        except ValueError:
            delay = 0
        QTimer.singleShot(delay, app, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

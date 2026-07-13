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
from pathlib import Path

from isar.workspace_api import Workspace, WorkspaceNotFound


def _resolve_recent_workspace() -> "Workspace | None":
    """The most-recent still-openable workspace from the GUI session, or ``None``.

    Iterates the session recents (``isar_gui.session`` is Qt-free — safe to import here) newest
    first, skipping any that no longer exist on disk or no longer open as a workspace. A stale/
    removed/not-a-workspace recent must never error a no-arg launch, so it is skipped, not raised.
    """
    from isar_gui.session import load_session

    for path in load_session().recent_workspaces:
        if not Path(path).is_dir():
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

    # Import Qt only now — never at module import time, so `import isar_gui` stays QApplication-free.
    from PySide6.QtWidgets import QApplication

    from isar_gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(argv)

    if not explicit:
        # No path given: unfinished temporary work takes precedence over the generic recents list.
        # An explicit CLI arg suppresses both recovery and recent-workspace resolution.
        ws = _resolve_default_workspace()

    window = MainWindow(ws)  # ws may be None -> empty-start
    window.show()

    smoke_ms = os.environ.get("ISAR_GUI_SMOKE_MS")
    if smoke_ms:
        from PySide6.QtCore import QTimer
        try:
            delay = max(0, int(smoke_ms))
        except ValueError:
            delay = 0
        QTimer.singleShot(delay, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

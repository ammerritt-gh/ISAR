"""app.main — the QApplication bootstrap for the ISAR GUI.

``main(argv)`` opens the workspace read-only, builds the :class:`~isar_gui.main_window.MainWindow`,
and runs the Qt event loop, returning a process exit code. A nonexistent/invalid workspace path
fails cleanly on stderr with exit 2 (no dialog yet — Phase-A shell; GUI error dialogs come later).

Smoke hook: set ``ISAR_GUI_SMOKE_MS=<ms>`` to auto-quit the app after that many milliseconds with
exit 0 (a ``QTimer.singleShot`` posted after ``exec`` starts). Used by the offscreen live smoke
check so the shell can be launched end-to-end in CI without a human closing the window.
"""
from __future__ import annotations

import os
import sys

from isar.workspace_api import Workspace, WorkspaceNotFound


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("isar view: a workspace path is required", file=sys.stderr)
        return 2
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

    window = MainWindow(ws)
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

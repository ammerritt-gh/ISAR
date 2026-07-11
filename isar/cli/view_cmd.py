"""`isar view [workspace]` — launch the desktop GUI on a workspace (Phase-A shell).

The GUI lives in the top-level ``isar_gui`` package and pulls in the ``[gui]`` extra (PySide6,
pyqtgraph, watchdog). Those are imported *inside* :func:`run` (never at CLI module import time) so
the core ``isar`` CLI stays Qt-free and importable without the extra — the Qt-free-core boundary
test (tests/test_qt_free_core.py) is the proof. A missing extra fails with a helpful hint, not a
traceback.

The workspace argument is optional: with none given, :func:`_most_recent_workspace` falls back to
the most-recent still-existing workspace recorded in the GUI session (``isar_gui/session.py``,
itself Qt-free — safe to import here without pulling in PySide6). No recent/valid workspace is the
same class of user error as a bad path, so it exits 2 with a message rather than launching empty.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MISSING_MSG = "GUI extra not installed - pip install -e .[gui]"
_NO_RECENT_MSG = "isar view: no workspace given and no recent workspace found in the GUI session"


def _most_recent_workspace() -> str | None:
    """The most-recent still-existing workspace from the GUI session (Qt-free: ``isar_gui.session``
    is a plain-data module), or ``None`` when there is no session or every recent path is gone."""
    from isar_gui.session import load_session

    for path in load_session().recent_workspaces:
        if Path(path).is_dir():
            return path
    return None


def run(args) -> int:
    # isar_gui ships with the core install and its app module is Qt-free at import scope, so the
    # ImportError guard alone would never fire — the missing dep is PySide6, which app.py imports
    # lazily *inside* main(). Probe for it up front so a missing [gui] extra prints the hint instead
    # of a raw ModuleNotFoundError traceback. The ImportError guard stays as belt-and-braces (e.g. a
    # source checkout without isar_gui on the path).
    if importlib.util.find_spec("PySide6") is None:
        print(_MISSING_MSG, file=sys.stderr)
        return 2
    try:
        from isar_gui.app import main as gui_main
    except ImportError:
        print(_MISSING_MSG, file=sys.stderr)
        return 2

    workspace = args.workspace
    if workspace is None:
        workspace = _most_recent_workspace()
        if workspace is None:
            print(_NO_RECENT_MSG, file=sys.stderr)
            return 2
    return gui_main([workspace])

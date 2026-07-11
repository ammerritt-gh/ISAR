"""`isar view [workspace]` — launch the desktop GUI on a workspace (Phase-A shell).

The GUI lives in the top-level ``isar_gui`` package and pulls in the ``[gui]`` extra (PySide6,
pyqtgraph, watchdog). Those are imported *inside* :func:`run` (never at CLI module import time) so
the core ``isar`` CLI stays Qt-free and importable without the extra — the Qt-free-core boundary
test (tests/test_qt_free_core.py) is the proof. A missing extra fails with a helpful hint, not a
traceback.

The workspace argument is optional. With none given, the GUI (:func:`isar_gui.app.main`) resolves
the most-recent still-openable workspace from the GUI session and, failing that, opens in
empty-start (a window with no workspace loaded) — never a console error, since this launcher is
double-clicked. An *explicit* bad path is still a hard error (stderr + exit 2), handled in the GUI
bootstrap. This command only gates the [gui] extra and forwards the (optional) path.
"""
from __future__ import annotations

import importlib.util
import sys

_MISSING_MSG = "GUI extra not installed - pip install -e .[gui]"


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

    # Forward the (optional) path; a None workspace becomes an empty argv, which gui_main resolves
    # to the most-recent workspace or empty-start.
    workspace = args.workspace
    return gui_main([] if workspace is None else [workspace])

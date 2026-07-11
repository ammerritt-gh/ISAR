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


_GUI_EXTRA_DEPS = ("PySide6", "pyqtgraph", "watchdog")


def run(args) -> int:
    # isar_gui ships with the core install and its app module is Qt-free at import scope, so an
    # ImportError from `import isar_gui.app` alone would never fire on a missing extra — the actual
    # missing deps (PySide6, pyqtgraph, watchdog) are imported lazily *inside* isar_gui.app / its
    # viewmodels. Probe all three up front so a missing [gui] extra prints the hint instead of a raw
    # ModuleNotFoundError traceback. Once all three are present, import isar_gui.app with NO
    # try/except: a failure at that point is a genuine bug inside isar_gui, not a missing extra, and
    # must traceback rather than being swallowed as "GUI extra not installed".
    if any(importlib.util.find_spec(dep) is None for dep in _GUI_EXTRA_DEPS):
        print(_MISSING_MSG, file=sys.stderr)
        return 2
    from isar_gui.app import main as gui_main

    # Forward the (optional) path; a None workspace becomes an empty argv, which gui_main resolves
    # to the most-recent workspace or empty-start.
    workspace = args.workspace
    return gui_main([] if workspace is None else [workspace])

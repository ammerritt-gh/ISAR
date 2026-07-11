"""`isar view <workspace>` — launch the desktop GUI on a workspace (Phase-A shell).

The GUI lives in the top-level ``isar_gui`` package and pulls in the ``[gui]`` extra (PySide6,
pyqtgraph, watchdog). Those are imported *inside* :func:`run` (never at CLI module import time) so
the core ``isar`` CLI stays Qt-free and importable without the extra — the Qt-free-core boundary
test (tests/test_qt_free_core.py) is the proof. A missing extra fails with a helpful hint, not a
traceback.
"""
from __future__ import annotations

import sys


def run(args) -> int:
    try:
        from isar_gui.app import main as gui_main
    except ImportError:
        print("GUI extra not installed - pip install -e .[gui]", file=sys.stderr)
        return 2
    return gui_main([args.workspace])

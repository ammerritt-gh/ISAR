"""isar_gui — the ISAR desktop GUI (PySide6 + pyqtgraph), launched as ``isar view <workspace>``.

Thin-MVVM over :mod:`isar.workspace_api`: the GUI imports from ``isar`` ONLY through
``isar.workspace_api`` / ``isar.geometry`` / ``isar.plotspec`` and never re-fits, re-collates, or
writes to the workspace. No matplotlib here — plotting is pyqtgraph.

Importing this package is intentionally cheap and Qt-free at module scope: it must NOT construct a
``QApplication`` (that happens only in :func:`isar_gui.app.main`). Qt is imported lazily inside the
submodules that need it, so ``import isar_gui`` from a headless/notebook context stays inert.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]

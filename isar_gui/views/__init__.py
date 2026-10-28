"""isar_gui.views — the component views (browser, inspector, heatmap, ...).

Placeholder in the Phase-A shell; each view lands in a later task as a dock/widget that binds to a
viewmodel from :mod:`isar_gui.viewmodels`. Views are Qt-only presentation: they read view-ready
state off a viewmodel and never touch the workspace facade directly.
"""
from __future__ import annotations

from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.inspector_widget import FitTables
from isar_gui.views.scan_table_dock import ScanTableDock
from isar_gui.views.scans_dock import ScansDock

__all__ = ["BrowserDock", "FitTables", "ScansDock", "ScanTableDock"]

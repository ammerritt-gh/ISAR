"""isar_gui.views — the component views (browser, inspector, heatmap, ...).

Placeholder in the Phase-A shell; each view lands in a later task as a dock/widget that binds to a
viewmodel from :mod:`isar_gui.viewmodels`. Views are Qt-only presentation: they read view-ready
state off a viewmodel and never touch the workspace facade directly.
"""
from __future__ import annotations

from isar_gui.views.browser_dock import BrowserDock
from isar_gui.views.inspector_widget import InspectorWidget

__all__ = ["BrowserDock", "InspectorWidget"]

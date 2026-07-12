"""isar_gui.viewmodels — thin viewmodels mediating between the workspace facade and the views.

A viewmodel holds the read-only :class:`~isar.workspace_api.Workspace` and turns it into
view-ready state, emitting Qt signals when that state changes. Views are dumb; computation lives
here. Component viewmodels land in later tasks on top of :class:`~isar_gui.viewmodels.base.ViewModel`.
"""
from __future__ import annotations

from isar_gui.viewmodels.base import ViewModel
from isar_gui.viewmodels.browser import BrowserViewModel, GroupRow
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.viewmodels.files import FilesViewModel
from isar_gui.viewmodels.heatmap import HeatmapViewModel
from isar_gui.viewmodels.inspector import InspectorViewModel
from isar_gui.viewmodels.waterfall import WaterfallViewModel

__all__ = ["ViewModel", "BrowserViewModel", "GroupRow", "InspectorViewModel",
           "HeatmapViewModel", "DispersionViewModel", "WaterfallViewModel", "FilesViewModel"]

"""isar.workspace_api — a deterministic, read-only facade over a workspace directory.

The GUI's entire data layer and the notebook interface. It aggregates the existing readers and
exposes a small surface of read-only views (``FitView``, ``ScanView``, ``GroupView``, ``CardView``,
``BranchModelView``, ``CampaignView``). It never re-fits, re-collates from raw, or writes.

    from isar.workspace_api import Workspace
    ws = Workspace.open(path)      # raises WorkspaceNotFound on a non-workspace dir
    ws.results                     # list[FitView]
    ws.scans                       # list[ScanView]
    ws.groups                      # list[GroupView] (card collation key)
    ws.evaluate_model(scan_id, det_id)   # params-only model curve

Qt-free by construction (no PySide6/pyqtgraph/matplotlib); scipy-free except the ``evaluate_model``
fallback, which pulls scipy lazily.
"""
from __future__ import annotations

from .views import (BranchModelView, CampaignView, CardView, FitView, GroupView, ScanView)
from .workspace import SCHEMA_VERSION, Workspace, WorkspaceNotFound
from .scan_keys import scan_prefix, split_key, unit_key

__all__ = [
    "Workspace",
    "WorkspaceNotFound",
    "SCHEMA_VERSION",
    "CardView",
    "ScanView",
    "FitView",
    "GroupView",
    "BranchModelView",
    "CampaignView",
    "unit_key",
    "split_key",
    "scan_prefix",
]

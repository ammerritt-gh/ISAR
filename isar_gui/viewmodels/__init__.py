"""isar_gui.viewmodels — thin viewmodels mediating between the workspace facade and the views.

A viewmodel holds the read-only :class:`~isar.workspace_api.Workspace` and turns it into
view-ready state, emitting Qt signals when that state changes. Views are dumb; computation lives
here. Component viewmodels land in later tasks on top of :class:`~isar_gui.viewmodels.base.ViewModel`.
"""
from __future__ import annotations

from isar_gui.viewmodels.base import ViewModel

__all__ = ["ViewModel"]

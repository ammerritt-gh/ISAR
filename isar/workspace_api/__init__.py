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

from .views import (BranchModelView, CampaignView, CardView, CertificationStatusView, FitView,
                    GroupView, MasterAssessmentView, MasterGroupView, ScanView)
from .workspace import SCHEMA_VERSION, Workspace, WorkspaceNotFound
from .scan_keys import scan_prefix, split_key, unit_key
from .config_gates import (DEFAULT_CLOUD_FALLBACK_MODEL, PolicyValidation, validate_endpoint,
                           validate_policy_text)
from .display import (AcquisitionSelection, DisplayInterval, FitDisplay, FitDisplayState,
                      fit_certifying_interval, fit_display, latest_completed_acquisition,
                      matching_assessment, resolve_interval)
from .scene import sample_knowledge, scene_observations
from .scene_view import (Curve, GapEntry, HypothesisEntry, HypothesisMap, ModeMarker,
                         SceneRevisionView, hypothesis_map, scene_revision)
from .agenda_view import (AgendaRevisionView, FreshnessReport, agenda_revision, read_transitions)
from .decision_view import latest_decision

__all__ = [
    "Workspace",
    "WorkspaceNotFound",
    "SCHEMA_VERSION",
    "CardView",
    "ScanView",
    "FitView",
    "GroupView",
    "MasterGroupView",
    "MasterAssessmentView",
    "CertificationStatusView",
    "BranchModelView",
    "CampaignView",
    "unit_key",
    "split_key",
    "scan_prefix",
    "validate_endpoint",
    "DEFAULT_CLOUD_FALLBACK_MODEL",
    "validate_policy_text",
    "PolicyValidation",
    "DisplayInterval",
    "FitDisplay",
    "FitDisplayState",
    "fit_display",
    "matching_assessment",
    "resolve_interval",
    "AcquisitionSelection",
    "latest_completed_acquisition",
    "fit_certifying_interval",
    # The scene layer's two doors: observations INTO a build, and a stored revision back out.
    "scene_observations",
    "sample_knowledge",
    "scene_revision",
    "SceneRevisionView",
    "hypothesis_map",
    "HypothesisMap",
    "HypothesisEntry",
    "ModeMarker",
    "GapEntry",
    "Curve",
    # The agenda layer's read door (PR 4.2b C8): a stored revision back out, plus its
    # tri-state freshness against the current scene/program/registry state.
    "agenda_revision",
    "AgendaRevisionView",
    "FreshnessReport",
    "read_transitions",
    # The discovery decision ledger's read door (SCENE-5 5.2): the latest persisted decision, for
    # the operator alert on a scoring fallback (D7) — never a second write path.
    "latest_decision",
]

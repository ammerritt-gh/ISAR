"""The operator-alert banner (SCENE-5 5.2, D7): CampaignViewModel.scoring_fallback +
CampaignMonitor's banner, driven by real ``report/decisions.jsonl`` lines.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from isar.workspace_api import Workspace
from isar_gui.viewmodels.campaign import CampaignViewModel
from isar_gui.views.campaign_monitor import CampaignMonitor


class _HeatmapVM(QObject):
    """The minimal surface CampaignMonitor reads from a heatmap viewmodel."""

    spec_changed = Signal()
    spec = None


class _Ops:
    """The minimal surface CampaignMonitor reads from the operation coordinator."""

    def is_running_campaign(self):
        return False

    def is_stop_pending(self):
        return False

    def request_drive_stop(self):
        return True


def _write_decisions(root, records):
    path = root / "report"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "decisions.jsonl", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


_SCORED = {"schema": "isar.discovery-decision/1", "selection": {"n": 1}}
_FALLBACK = {"schema": "isar.discovery-decision/1", "selection": {"n": 2},
            "decision_basis": {"fallback": {"reason": "scoring_exception", "pool": "canonical"}}}


def test_viewmodel_surfaces_fallback_block(qtbot, make_ws):
    root = make_ws(rows=[])
    _write_decisions(root, [_FALLBACK])
    vm = CampaignViewModel(Workspace.open(root))
    assert vm.scoring_fallback == {"reason": "scoring_exception", "pool": "canonical"}


def test_viewmodel_scoring_fallback_none_on_scored_decision(qtbot, make_ws):
    root = make_ws(rows=[])
    _write_decisions(root, [_SCORED])
    vm = CampaignViewModel(Workspace.open(root))
    assert vm.scoring_fallback is None


def test_banner_visible_with_operator_wording_on_fallback(qtbot, make_ws):
    root = make_ws(rows=[])
    _write_decisions(root, [_FALLBACK])
    vm = CampaignViewModel(Workspace.open(root))
    monitor = CampaignMonitor(vm, _HeatmapVM(), _Ops())
    qtbot.addWidget(monitor)
    monitor.show()

    assert monitor._fallback_banner.isVisible()
    text = monitor._fallback_banner.text()
    assert "Scene scoring failed" in text
    assert "campaign continuing on basic placement" in text
    assert "scoring_exception" in text


def test_banner_hides_on_next_fallback_free_decision(qtbot, make_ws):
    root = make_ws(rows=[])
    _write_decisions(root, [_FALLBACK])
    vm = CampaignViewModel(Workspace.open(root))
    monitor = CampaignMonitor(vm, _HeatmapVM(), _Ops())
    qtbot.addWidget(monitor)
    monitor.show()
    assert monitor._fallback_banner.isVisible()

    _write_decisions(root, [_FALLBACK, _SCORED])
    vm.on_workspace_changed({"events"})

    assert not monitor._fallback_banner.isVisible()
    assert monitor._fallback_banner.text() == ""


def test_banner_hidden_by_default_with_no_decisions_yet(qtbot, make_ws):
    root = make_ws(rows=[])
    vm = CampaignViewModel(Workspace.open(root))
    monitor = CampaignMonitor(vm, _HeatmapVM(), _Ops())
    qtbot.addWidget(monitor)
    monitor.show()
    assert not monitor._fallback_banner.isVisible()

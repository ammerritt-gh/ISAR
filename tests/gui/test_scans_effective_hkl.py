"""The Scans dock's q-groups and the ``q:`` filter read the effective hkl the scan list shows (S1U3).

The BL43 cases run on a generated, imported workspace (``build_bl43_gpi_workspace``) through a real IntakeState, so
the hkl comes from the real gpi check and the real fact rows (hard rule 10: every number here is generated).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

import bl43_fixture
from isar.workspace_api import Workspace
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.views.scans_dock import ScansDock

WAIT_MS = 15000


class _Holder(QObject):
    """What the Scans view model reads of the intake: ``fact_rows`` and ``changed``."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.fact_rows = ()


def _bound_intake(qtbot, root):
    intake = IntakeState()
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(root)
    return intake


def _q_groups(vm):
    """The analyzers of each auto q-group, as detector ids, in group order."""
    by_key = {u.key: u.det for u in vm.rows()}
    return [sorted(by_key[k] for k in g.scan_keys) for g in vm.auto_groups("q")]


def test_gpi_hkl_groups_the_analyzers_and_the_q_filter_finds_them(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="accepted")
    vm = ScansViewModel(Workspace.open(root), store=GuiStore.load(tmp_path))
    vm.set_intake(_bound_intake(qtbot, root))
    assert sorted((u.det, list(u.q)) for u in vm.rows()) == [
        ("a03", [0.5, 0.5, 3.0]), ("a04", [0.5, 0.5, 3.5]), ("a05", [0.5, 0.5, 4.0])]
    assert _q_groups(vm) == [["a03"], ["a04"], ["a05"]]
    assert vm.apply_query("q:0.5,0.5,3") is None
    assert [u.det for u in vm.rows()] == ["a03"]


def test_rejected_gpi_file_leaves_no_q_group_and_no_q_match(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="rejected")
    vm = ScansViewModel(Workspace.open(root), store=GuiStore.load(tmp_path))
    vm.set_intake(_bound_intake(qtbot, root))
    assert all(u.q is None for u in vm.rows())
    assert vm.auto_groups("q") == []
    vm.apply_query("q:0.5,0.5,3")
    assert vm.rows() == []


def test_intake_change_regroups_the_dock_without_a_workspace_refresh(qtbot, tmp_path):
    root = bl43_fixture.build_bl43_gpi_workspace(tmp_path, gpi="accepted")
    fact_rows = _bound_intake(qtbot, root).fact_rows
    holder = _Holder()
    vm = ScansViewModel(Workspace.open(root), store=GuiStore.load(tmp_path))
    vm.set_intake(holder)                          # no fact rows yet: BL43 files carry no hkl of their own
    assert vm.auto_groups("q") == []
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    holder.fact_rows = fact_rows
    with qtbot.waitSignal(vm.refreshed, timeout=WAIT_MS):
        holder.changed.emit()                      # the holder's own change; the workspace is not re-read
    assert _q_groups(vm) == [["a03"], ["a04"], ["a05"]]
    q_bar = dock._filter_bar._q                # the filter bar's q dropdown, resynced on the same change
    assert (0.5, 0.5, 3.0) in [q_bar.itemData(i) for i in range(q_bar.count())]
    dock._build_page("q")
    boxes = [b.box_id for b in dock._boxes if b.box_id.startswith("auto:q:")]
    assert len(boxes) == 3 and boxes == [g.id for g in vm.auto_groups("q")]

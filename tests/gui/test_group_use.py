"""Part 2A (GUI Pass 2): a group is a usable object. Covers the pure restriction filter + pooled
policy, the per-destination `accepts` refusals, the VM `set_scan_restriction` seam, the plot-dock
drop (apply / loud refuse / Show-all banner), and the "Send to" routing signal.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QMimeData

from isar.workspace_api import FitView, Workspace
from isar_gui import group_targets
from isar_gui.gui_store import GuiStore
from isar_gui.mime import (
    MIME_GROUP_REF,
    MIME_SCAN_IDS,
    encode_group_ref,
    encode_scan_ids,
)
from isar_gui.viewmodels import projection
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.viewmodels.heatmap import HeatmapViewModel
from isar_gui.viewmodels.scans import ScansViewModel
from isar_gui.viewmodels.waterfall import WaterfallViewModel
from isar_gui.views.dispersion_pane import DispersionPane
from isar_gui.views.scans_dock import ScansDock

_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


# --------------------------------------------------------------------------- pure fakes
def _fv(uid, T=300.0, pooled=False, members=()):
    return SimpleNamespace(unit_id=uid, T=T, q=[1.0, 0.0, 0.0], omega0=5.0,
                           pooled=pooled, members=list(members))


# --------------------------------------------------------------------------- projection.restrict
def test_restrict_by_unit_id_and_none_passthrough():
    cands = [_fv("a"), _fv("b"), _fv("c")]
    assert {fv.unit_id for fv in projection.restrict(cands, {"a", "c"})} == {"a", "c"}
    assert projection.restrict(cands, None) is cands   # None -> unchanged


def _pooled_fit(members):
    """A production-shaped pooled master: FitView.members is a list of persisted strings."""
    return FitView.from_outcome(
        {"detector_id": "pooled", "pooled": True, "members": list(members)},
        scan_number=9_000_001,
        unit_id="master:9000001:pooled",
    )


def test_restrict_keeps_pooled_master_for_exact_qualified_member():
    master = _pooled_fit(["facility:run:473:ana1", "facility:run:494:ana1"])
    loose = _fv("other:473:ana1")
    kept = projection.restrict([master, loose], {"facility:run:473:ana1"})
    assert master in kept and loose not in kept


def test_restrict_qualified_member_never_cross_matches_same_scan_number():
    master = _pooled_fit(["facility:run:473:ana1"])
    assert projection.restrict([master], {"other:run:473:ana1"}) == []
    assert projection.restrict([master], {"facility:run:473:ana2"}) == []


def test_restrict_bare_member_matches_selected_scan_number():
    master = _pooled_fit(["473"])
    assert projection.restrict([master], {"facility:run:473:ana9"}) == [master]


# --------------------------------------------------------------------------- group_targets.accepts
def test_accepts_refuses_when_nothing_fitted():
    ok, reason = group_targets.accepts("waterfall", [_fv("a")], ["zzz"])
    assert not ok and "fitted" in reason


def test_accepts_single_t_ok_multi_t_refused_on_planes():
    results = [_fv("a", T=200.0), _fv("b", T=400.0)]
    ok_w, _ = group_targets.accepts("waterfall", results, ["a", "b"])
    assert ok_w                                   # waterfall accepts multi-T
    for target in ("dispersion", "heatmap"):
        ok, reason = group_targets.accepts(target, results, ["a", "b"])
        assert not ok and "span" in reason and "200" in reason and "400" in reason
    ok1, _ = group_targets.accepts("dispersion", results, ["a"])
    assert ok1                                    # a single-T subset is fine


# --------------------------------------------------------------------------- real 2-temperature ws
def _ws_2t(make_ws) -> Workspace:
    scans, rows, data = {}, [], []
    # scans 1,2 at T=200; scans 3,4 at T=400; all at H≈2.0/2.1, K=L=0 (in-plane).
    for scan, h, T, om in [(1, 2.0, 200.0, 4.0), (2, 2.1, 200.0, 5.0),
                           (3, 2.0, 400.0, 4.2), (4, 2.1, 400.0, 5.1)]:
        oc = {"detector_id": "det", "verdict": "resolved", "disposition": "resolved",
              "confidence": 0.9, "omega0": om, "gamma": 1.0, "q": [h, 0.0, 0.0], "T": T,
              "sample": "tavi", "assignment_entropy": 0.0,
              "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
              "interval": [om - 0.4, om + 0.4], "interval_reliable": True}
        sid = f"tavi:{scan}:det"
        scans[sid] = {"scan_id": sid, "scan_number": scan, "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": scan, "detector": "det", "sample": "tavi", "T": T,
                     "h": h, "k": 0.0, "l": 0.0, "status": "fitted"})
        data.append({"scan_number": scan, "energy": list(_X), "counts": _counts(om)})
    return Workspace.open(make_ws(scans=scans, rows=rows, data_files=data, state={}))


_COLD = {"tavi:1:det", "tavi:2:det"}   # the two T=200 units
_MIXED = ["tavi:1:det", "tavi:3:det"]  # one T=200 + one T=400


def test_dispersion_vm_restriction_narrows_temperatures(make_ws):
    vm = DispersionViewModel(_ws_2t(make_ws))
    assert vm.temperatures == [200.0, 400.0]
    vm.set_scan_restriction(_COLD)
    assert vm.restriction_active and vm.temperatures == [200.0]
    vm.set_scan_restriction(None)
    assert not vm.restriction_active and vm.temperatures == [200.0, 400.0]


def test_dispersion_vm_can_accept(make_ws):
    vm = DispersionViewModel(_ws_2t(make_ws))
    assert vm.can_accept(list(_COLD))[0]
    ok, reason = vm.can_accept(_MIXED)
    assert not ok and "span" in reason


def test_heatmap_vm_restriction_narrows_temperatures(make_ws):
    vm = HeatmapViewModel(_ws_2t(make_ws))
    assert vm.temperatures == [200.0, 400.0]
    vm.set_scan_restriction(_COLD)
    assert vm.temperatures == [200.0]


def test_waterfall_vm_accepts_multi_t_and_restricts(make_ws):
    vm = WaterfallViewModel(_ws_2t(make_ws))
    assert vm.can_accept(_MIXED)[0]           # waterfall accepts multi-T
    vm.set_scan_restriction(_COLD)
    assert vm.restriction_active


# --------------------------------------------------------------------------- dock drop (mixin)
class _Drop:
    def __init__(self, keys, *, gref=None):
        md = QMimeData()
        md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        if gref is not None:
            gid, label = gref
            md.setData(MIME_GROUP_REF, encode_group_ref("manual", gid, label))
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


def test_dispersion_dock_applies_single_t_group_and_shows_banner(qtbot, make_ws):
    vm = DispersionViewModel(_ws_2t(make_ws))
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    drop = _Drop(sorted(_COLD), gref=("g1", "Cold"))
    dock.dropEvent(drop)
    assert drop.accepted and vm.restriction_active
    # isHidden() is the explicit hide-state (offscreen widgets report isVisible()==False regardless).
    assert not dock._grp_banner.isHidden() and "Cold" in dock._grp_banner.text()
    # Show-all clears it.
    dock._clear_group_restriction()
    assert not vm.restriction_active and dock._grp_banner.isHidden()


def test_group_banner_escapes_group_label(qtbot, make_ws):
    vm = DispersionViewModel(_ws_2t(make_ws))
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    dock.apply_group(sorted(_COLD), '<i title="unsafe">Cold & Co</i>')
    text = dock._grp_banner.text()
    assert '&lt;i title=&quot;unsafe&quot;&gt;Cold &amp; Co&lt;/i&gt;' in text
    assert '<i title="unsafe">' not in text


def test_dispersion_dock_refuses_multi_t_group(qtbot, make_ws):
    vm = DispersionViewModel(_ws_2t(make_ws))
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    drop = _Drop(_MIXED, gref=("g2", "Both"))
    dock.dropEvent(drop)
    assert not drop.accepted and not vm.restriction_active     # refused, nothing applied
    assert dock._grp_banner.isHidden()


# --------------------------------------------------------------------------- Send-to routing
def test_send_to_signal_carries_full_members_and_target(qtbot, make_ws, tmp_path):
    ws = _ws_2t(make_ws)
    store = GuiStore.load(tmp_path)
    vm = ScansViewModel(ws, store=store)
    gid = vm.create_group_from("Cold", sorted(_COLD))
    dock = ScansDock(vm)
    qtbot.addWidget(dock)
    dock._tabs.setCurrentIndex(list(dock._pages).index("Manual"))
    box = next(b for b in dock._boxes if b.box_id == gid)
    got = []
    dock.send_group_requested.connect(lambda keys, target, label: got.append((set(keys), target, label)))
    box._apply_header_action("send:waterfall")
    assert got == [(set(_COLD), "waterfall", "Cold")]


def test_restriction_self_heals_when_all_ids_go_stale(make_ws):
    # A restriction to ids that vanish on a later refresh must auto-clear (not linger over an empty
    # plot) and flag it once for the status note (plan R-lifecycle self-healing).
    vm = DispersionViewModel(_ws_2t(make_ws))
    vm.set_scan_restriction({"tavi:999:det"})   # an id not present -> covered() empty, ids stale
    assert not vm.restriction_active            # auto-cleared on the rebuild inside set_scan_restriction
    assert vm.take_restriction_stale() is True  # flagged once...
    assert vm.take_restriction_stale() is False  # ...and consumed
    assert vm.temperatures == [200.0, 400.0]    # full population restored


def test_arm_group_drop_glows_only_when_can_accept(qtbot, make_ws):
    # R12: the plot edge-glow arms only when the dock can actually accept the scans; unfitted / stale
    # ids or an inactive drag leave it un-glowed (absence of glow = 'not a valid target').
    vm = DispersionViewModel(_ws_2t(make_ws))
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    dock.arm_group_drop(True, list(_COLD))            # fitted single-T group -> glow
    assert "border" in dock._grp_body.styleSheet()
    dock.arm_group_drop(True, _MIXED)                 # multi-T -> can't accept -> no glow
    assert dock._grp_body.styleSheet() == ""
    dock.arm_group_drop(True, ["nope:1:det"])         # unfitted/unknown -> no glow
    assert dock._grp_body.styleSheet() == ""
    dock.arm_group_drop(False, list(_COLD))           # drag ended -> cleared
    assert dock._grp_body.styleSheet() == ""


def test_plot_view_does_not_accept_drops(qtbot, make_ws):
    # R17: the inner pyqtgraph QGraphicsView must not accept drops (the dock body owns them), else it
    # logs "dragLeaveEvent received before drag enter" and fights the dock's drag handling.
    from PySide6.QtWidgets import QGraphicsView
    vm = DispersionViewModel(_ws_2t(make_ws))
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    views = dock.findChildren(QGraphicsView)
    assert views and all(not v.acceptDrops() for v in views)


def test_scans_vm_can_target_greys_multi_t(make_ws, tmp_path):
    vm = ScansViewModel(_ws_2t(make_ws), store=GuiStore.load(tmp_path))
    assert vm.can_target("waterfall", _MIXED)[0]           # waterfall ok
    assert not vm.can_target("dispersion", _MIXED)[0]      # multi-T plane refused
    assert vm.can_target("dispersion", list(_COLD))[0]     # single-T plane ok

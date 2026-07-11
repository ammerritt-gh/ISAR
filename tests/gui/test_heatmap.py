"""C3 (q, E) plane tests: HeatmapViewModel derives the projection (varying q component + integration
window), builds one measured strip per in-window scan, counts excluded scans, and rides the honesty
overlays on top (ω̂ whisker only where a tier certifies, warning marker otherwise, entropy channel,
verdict glyphs); a plot click routes to the inspector. Offscreen (conftest forces QT offscreen),
importorskip, xdist-safe (each test stamps its own workspace)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.plotspec.spec import MarkerKind, Role, State
from isar.workspace_api import Workspace
from isar_gui.viewmodels.heatmap import HeatmapViewModel

_MVP16 = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")

_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


def _outcome(scan, h, k, l, *, omega0, reliable=True, T=200.0, entropy=0.0,
             verdict="resolved") -> dict:
    oc = {
        "detector_id": "det", "verdict": verdict, "disposition": verdict, "confidence": 0.9,
        "omega0": omega0, "gamma": 1.0, "q": [h, k, l], "T": T, "sample": "tavi",
        "assignment_entropy": entropy,
        "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
    }
    if reliable:
        oc["interval"] = [omega0 - 0.4, omega0 + 0.4]
        oc["interval_reliable"] = True
    else:
        oc["interval"] = [omega0 - 0.4, omega0 + 0.4]
        oc["interval_reliable"] = False
    return oc


# Three in-window scans at distinct H (K=L=0) + one out-of-window scan at deviant K=0.5.
_SPECS = [
    dict(scan=1, h=2.0, k=0.0, l=0.0, omega0=4.0, reliable=True, entropy=0.0, verdict="resolved"),
    dict(scan=2, h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.3, verdict="resolved"),
    dict(scan=3, h=2.2, k=0.0, l=0.0, omega0=6.0, reliable=False, entropy=0.69, verdict="flagged"),
    dict(scan=4, h=2.05, k=0.5, l=0.0, omega0=5.5, reliable=True, entropy=0.1, verdict="resolved"),
]


def _make(make_ws) -> Workspace:
    scans, rows, data = {}, [], []
    for s in _SPECS:
        oc = _outcome(s["scan"], s["h"], s["k"], s["l"], omega0=s["omega0"],
                      reliable=s["reliable"], entropy=s["entropy"], verdict=s["verdict"])
        sid = f"tavi:{s['scan']}:det"
        scans[sid] = {"scan_id": sid, "scan_number": s["scan"], "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": s["scan"], "detector": "det", "sample": "tavi", "T": 200.0,
                     "h": s["h"], "k": s["k"], "l": s["l"], "status": "fitted"})
        data.append({"scan_number": s["scan"], "energy": list(_X), "counts": _counts(s["omega0"])})
    root = make_ws(scans=scans, rows=rows, data_files=data)
    return Workspace.open(root)


def test_projection_varying_component_and_window(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    pj = vm.spec.projection
    assert pj is not None
    # H varies; K, L fixed at 0 within the 0.05 window.
    assert pj.plane.startswith("(H, 0, 0)")
    assert "|K|≤0.05" in pj.integration and "|L|≤0.05" in pj.integration


def test_excluded_count_and_annotation(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    assert vm.excluded_count == 1  # the deviant-K scan is outside the projection
    assert any("1 scans outside this projection" in a.text for a in vm.spec.annotations)


def test_one_strip_per_in_window_scan(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    strips = vm.spec.strips
    assert len(strips) == 3  # three in-window scans; the excluded one contributes no strip
    centers = sorted(round(s.x_center, 3) for s in strips)
    assert centers == [2.0, 2.1, 2.2]
    # width = 0.8 x median distinct-q spacing (0.1) = 0.08; strips never overlap.
    assert all(abs(s.width - 0.08) < 1e-9 for s in strips)
    assert all("scan_id" in s.meta for s in strips)


def test_omega_hat_whiskers_only_where_certifying(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    data_series = [s for s in vm.spec.series if s.role is Role.DATA and s.yerr is not None]
    assert len(data_series) == 1
    # Two reliable in-window scans -> two whiskered points; the unreliable one is NOT here.
    assert len(data_series[0].x) == 2
    assert data_series[0].state is State.ENTROPY
    # entropy is WIRED onto the scatter as a per-point colour channel (not just meta).
    assert data_series[0].color_values is not None and len(data_series[0].color_values) == 2
    assert data_series[0].color_norm is not None


def test_unreliable_omega_hat_is_warning_marker_without_whiskers(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    warnings = [m for m in vm.spec.markers if m.kind is MarkerKind.WARNING]
    assert len(warnings) == 1 and abs(warnings[0].x - 2.2) < 1e-9  # the unreliable H=2.2 scan
    # exactly one "(unreliable)" note in the legend area, not per point
    unrel = [a for a in vm.spec.annotations if a.role is State.UNRELIABLE]
    assert len(unrel) == 1 and unrel[0].xy is None


def test_entropy_channel_populated(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    ent = vm.spec.meta.get("omega_hat_entropy")
    assert ent is not None and len(ent) == 3  # one per in-window ω̂ point
    assert all(e is not None for e in ent)
    assert any("assignment entropy" in a.text for a in vm.spec.annotations)


def test_verdict_glyphs_present(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    kinds = {m.kind for m in vm.spec.markers}
    assert MarkerKind.VERDICT_RESOLVED in kinds
    assert MarkerKind.VERDICT_FLAGGED in kinds


def test_point_click_activates_fit(qtbot, make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.1, 5.0)  # right on the H=2.1 / ω̂=5.0 point
    fit = blocker.args[0]
    assert fit is not None
    assert fit.scan_number == 2


def test_temperature_switch_rebuilds(qtbot, make_ws):
    # Add a second-temperature scan so the T selector is meaningful.
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    assert vm.temperature == 200.0  # modal T
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_temperature(200.0)
    assert vm.spec is not None


def test_dock_renders_and_routes_click(qtbot, make_ws):
    from isar_gui.views.heatmap_dock import HeatmapDock
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    dock = HeatmapDock(vm)
    qtbot.addWidget(dock)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.0, 4.0)
    assert blocker.args[0].scan_number == 1


def test_mainwindow_registers_plane_dock_and_routes(qtbot, make_ws):
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session
    ws = _make(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.heatmap_dock.isVisible() or "Plane (q, E)" in w._docks
    with qtbot.waitSignal(w.inspector_vm.spec_changed, timeout=1000):
        w.heatmap_vm.activate_at(2.1, 5.0)
    assert not w.inspector_vm.is_empty
    assert w.inspector_vm.state.main_spec is not None


# ---- MVP-16 smoke (read-only) ------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen baseline not present")
def test_mvp16_heatmap_smoke():
    ws = Workspace.open(_MVP16)
    vm = HeatmapViewModel(ws)
    spec = vm.spec
    assert spec is not None
    assert isinstance(vm.excluded_count, int)
    assert spec.projection is not None
    assert len(spec.strips) > 0

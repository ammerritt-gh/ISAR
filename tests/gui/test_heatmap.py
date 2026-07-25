"""C3 (q, E) plane tests: HeatmapViewModel derives the projection (varying q component + integration
window), builds one measured strip per in-window scan, counts excluded scans, and rides the honesty
overlays on top (ω̂ whisker only where a tier certifies, warning marker otherwise, entropy channel,
verdict glyphs); a plot click routes to the inspector. Offscreen (conftest forces QT offscreen),
importorskip, xdist-safe (each test stamps its own workspace)."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from baselines import mvp16_path
from isar.plotspec.spec import MarkerKind, Role, State
from isar.workspace_api import Workspace
from isar_gui.session import Session
from isar_gui.viewmodels.heatmap import HeatmapViewModel

_MVP16 = mvp16_path()

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


def _make(make_ws, *, with_master: bool = False, specs=_SPECS) -> Workspace:
    scans, rows, data = {}, [], []
    for s in specs:
        T = s.get("T", 200.0)
        seed_omega = s["omega0"] if s["omega0"] is not None else s.get("candidate_seed", 1.0)
        oc = _outcome(s["scan"], s["h"], s["k"], s["l"], omega0=seed_omega,
                      reliable=s["reliable"], entropy=s["entropy"], verdict=s["verdict"], T=T)
        if s["omega0"] is None:
            oc.update({"omega0": None, "interval": None, "interval_reliable": False,
                       "selected_candidate_index": None,
                       "branch_candidates": list(s.get("branch_candidates") or [])})
        sid = f"tavi:{s['scan']}:det"
        scans[sid] = {"scan_id": sid, "scan_number": s["scan"], "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": s["scan"], "detector": "det", "sample": "tavi", "T": T,
                     "h": s["h"], "k": s["k"], "l": s["l"], "status": "fitted"})
        data.append({"scan_number": s["scan"], "energy": list(_X), "counts": _counts(seed_omega)})
    if with_master:
        # A pooled master at H=2.1 (in-window) with its OWN interval and 3 members; NO data file, so
        # it contributes no strip (masters have no persisted spectrum) but IS on the ω̂ overlay.
        moc = _outcome(10, 2.1, 0.0, 0.0, omega0=5.0, reliable=True, entropy=0.0, verdict="resolved")
        moc["pooled"] = True
        moc["n_members"] = 3
        scans["tavi:10:det"] = {"scan_id": "tavi:10:det", "scan_number": 10,
                                "scan_type": "measurement", "status": "fitted",
                                "detectors": {"det": moc}, "provenance": {"M": 600}}
        rows.append({"scan": 10, "detector": "det", "sample": "tavi", "T": 200.0,
                     "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted",
                     "pooled": True, "n_members": 3})
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
    ent = vm.spec.meta.get("omega_hat_entropy_all")
    assert ent is not None and len(ent) == 3  # one per in-window ω̂ point
    assert all(e is not None for e in ent)
    assert any("assignment entropy" in a.text for a in vm.spec.annotations)


def test_pooled_master_on_overlay_but_not_strips(make_ws):
    ws = _make(make_ws, with_master=True)
    vm = HeatmapViewModel(ws)
    # The master is a marked POOLED scatter with its own whisker + "pooled (3)" label...
    pooled = [s for s in vm.spec.series if s.state is State.POOLED]
    assert len(pooled) == 1 and pooled[0].yerr is not None
    assert pooled[0].label == "pooled (3)"
    # ...the per-scan member scatter still shows (masters not mixed in)...
    members = [s for s in vm.spec.series
               if s.role is Role.DATA and s.yerr is not None and s.state is not State.POOLED]
    assert len(members) == 1 and len(members[0].x) == 2
    # ...and the master contributes NO strip (no persisted spectrum): still 3 strips.
    assert len(vm.spec.strips) == 3
    at_master = [m for m in vm.spec.markers if m.kind is MarkerKind.VERDICT_RESOLVED
                 and abs(m.x - 2.1) < 1e-9 and abs(m.y - 5.0) < 1e-9]
    assert len(at_master) == 1  # member only; pooled verdict is composed into the square


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
    # Two scans at T=300 alongside the (modal) T=200 set, so the switch actually rebuilds against a
    # different scan population rather than "switching" to the temperature already in effect.
    specs = _SPECS + [
        dict(scan=7, h=2.0, k=0.0, l=0.0, omega0=4.0, reliable=True, entropy=0.0,
             verdict="resolved", T=300.0),
        dict(scan=8, h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.0,
             verdict="resolved", T=300.0),
    ]
    ws = _make(make_ws, specs=specs)
    vm = HeatmapViewModel(ws)
    assert vm.temperature == 200.0  # modal T (4 scans at 200 vs 2 at 300)
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_temperature(300.0)
    assert vm.temperature == 300.0
    assert vm.spec is not None
    assert len(vm.spec.strips) == 2  # the two T=300 scans only


def test_ambiguous_candidates_remain_visible_on_plane_without_whiskers(make_ws):
    specs = _SPECS + [{
        "scan": 9, "h": 2.3, "k": 0.0, "l": 0.0, "omega0": None,
        "candidate_seed": 2.7, "reliable": False, "entropy": 0.69,
        "verdict": "branch_ambiguous", "branch_candidates": [
            {"energy_meV": 2.7, "omega0_err": 0.12},
            {"energy_meV": 6.85, "omega0_err": 0.18},
        ],
    }]
    vm = HeatmapViewModel(_make(make_ws, specs=specs))
    ambiguous = [marker for marker in vm.spec.markers
                 if marker.kind is MarkerKind.AMBIGUOUS_CANDIDATE]
    assert [(marker.x, marker.y) for marker in ambiguous] == [(2.3, 2.7), (2.3, 6.85)]


def test_dock_renders_and_routes_click(qtbot, make_ws):
    from isar_gui.views.heatmap_pane import HeatmapPane
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    dock = HeatmapPane(vm)
    qtbot.addWidget(dock)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.0, 4.0)
    assert blocker.args[0].scan_number == 1


def test_colorbar_detaches_when_spec_has_no_strips(qtbot, make_ws):
    """When a refresh yields a strip-less spec, the colorbar must stop pointing at the previous
    (now-removed) strip ImageItem — it gets a fresh placeholder instead of a dangling reference."""
    import pyqtgraph as pg

    from isar.plotspec.spec import PlotSpec
    from isar_gui.views.heatmap_pane import HeatmapPane
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    dock = HeatmapPane(vm)
    qtbot.addWidget(dock)
    old_img = dock._colorbar.imageItem()
    assert isinstance(old_img, pg.ImageItem)
    # Refresh with a strip-less (but non-None) spec.
    vm._spec = PlotSpec(title="empty", xlabel="q", ylabel="E")
    dock.refresh()
    assert dock._colorbar.imageItem() is not old_img


def test_view_state_round_trips_through_session(make_ws):
    ws = _make(make_ws)
    vm1 = HeatmapViewModel(ws)
    vm1.set_temperature(200.0)
    session = Session()
    vm1.save_view_state(session)
    assert session.view_state["heatmap"] == {"temperature": 200.0}

    ws2 = _make(make_ws)
    vm2 = HeatmapViewModel(ws2)
    vm2.load_view_state(session)
    assert vm2.temperature == pytest.approx(200.0)


def test_mainwindow_registers_plane_dock_and_routes(qtbot, make_ws):
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session
    ws = _make(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._center_host.pane("plane") is not None
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

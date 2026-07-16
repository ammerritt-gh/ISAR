"""C5 dispersion view tests: DispersionViewModel derives the same projection as the heatmap (shared
helper), rides the ω̂ honesty overlay (certifying whisker only, warning otherwise, entropy channel
aligned to the whiskered subset, verdict glyphs), draws the dispersion prior + branch-belief curves,
flags points that sit >3σ off the prior, and routes a plot click to the inspector. Offscreen,
importorskip, xdist-safe."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from baselines import mvp16_path
from isar.plotspec.spec import MarkerKind, Role, State
from isar.workspace_api import Workspace
from isar_gui.viewmodels.dispersion import DispersionViewModel

_MVP16 = mvp16_path()

_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


def _outcome(*, h, k, l, omega0, reliable=True, T=200.0, entropy=0.0, verdict="resolved") -> dict:
    oc = {
        "detector_id": "det", "verdict": verdict, "disposition": verdict, "confidence": 0.9,
        "omega0": omega0, "gamma": 1.0, "q": [h, k, l], "T": T, "sample": "tavi",
        "assignment_entropy": entropy,
        "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
        "interval": [omega0 - 0.4, omega0 + 0.4], "interval_reliable": bool(reliable),
    }
    return oc


# Three in-window scans at distinct H (K=L=0) + one out-of-window scan at deviant K=0.5.
_SPECS = [
    dict(scan=1, h=2.0, k=0.0, l=0.0, omega0=4.0, reliable=True, entropy=0.0, verdict="resolved"),
    dict(scan=2, h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.3, verdict="resolved"),
    dict(scan=3, h=2.2, k=0.0, l=0.0, omega0=6.0, reliable=False, entropy=0.69, verdict="flagged"),
    dict(scan=4, h=2.05, k=0.5, l=0.0, omega0=5.5, reliable=True, entropy=0.1, verdict="resolved"),
]

# A 2-point dispersion prior: a flat line ω≈4.0 across H, so the H=2.1 (ω=5.0) point sits ~2.5σ off
# (half-width 0.4) — NOT flagged — while a bumped prior makes an outlier fire (see the off-curve test).
_PRIOR = [
    {"sample": "tavi", "t_bin": 200.0, "q": [2.0, 0.0, 0.0], "omega0": 4.0, "sigma": 0.2},
    {"sample": "tavi", "t_bin": 200.0, "q": [2.2, 0.0, 0.0], "omega0": 4.0, "sigma": 0.2},
]


def _make(make_ws, *, prior=None, branch=None, specs=_SPECS, with_master=False) -> Workspace:
    scans, rows, data = {}, [], []
    for s in specs:
        oc = _outcome(h=s["h"], k=s["k"], l=s["l"], omega0=s["omega0"],
                      reliable=s["reliable"], entropy=s["entropy"], verdict=s["verdict"],
                      T=s.get("T", 200.0))
        sid = f"tavi:{s['scan']}:det"
        scans[sid] = {"scan_id": sid, "scan_number": s["scan"], "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": s["scan"], "detector": "det", "sample": "tavi", "T": s.get("T", 200.0),
                     "h": s["h"], "k": s["k"], "l": s["l"], "status": "fitted"})
        data.append({"scan_number": s["scan"], "energy": list(_X), "counts": _counts(s["omega0"])})
    if with_master:
        # A pooled master (delivered per-q answer) at H=2.1 with its own interval + 3 members; no
        # data file (masters have no persisted spectrum), but it belongs on the ω̂ overlay.
        moc = _outcome(h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.0, verdict="resolved")
        moc["pooled"] = True
        moc["n_members"] = 3
        scans["tavi:10:det"] = {"scan_id": "tavi:10:det", "scan_number": 10,
                                "scan_type": "measurement", "status": "fitted",
                                "detectors": {"det": moc}, "provenance": {"M": 600}}
        rows.append({"scan": 10, "detector": "det", "sample": "tavi", "T": 200.0,
                     "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted",
                     "pooled": True, "n_members": 3})
    state = {}
    if prior is not None:
        state["dispersion_prior"] = prior
    if branch is not None:
        state["branch_belief"] = branch
    root = make_ws(scans=scans, rows=rows, data_files=data, state=state)
    return Workspace.open(root)


def test_projection_matches_heatmap(make_ws):
    ws = _make(make_ws)
    vm = DispersionViewModel(ws)
    pj = vm.spec.projection
    assert pj is not None
    assert pj.plane.startswith("(H, 0, 0)")
    assert "|K|≤0.05" in pj.integration and "|L|≤0.05" in pj.integration
    assert vm.excluded_count == 1  # the deviant-K scan is outside the projection


def test_whiskers_only_where_certifying_entropy_aligned(make_ws):
    ws = _make(make_ws)
    vm = DispersionViewModel(ws)
    data = [s for s in vm.spec.series if s.role is Role.DATA and s.yerr is not None]
    assert len(data) == 1
    assert len(data[0].x) == 2                       # two reliable in-window scans
    assert data[0].state is State.ENTROPY
    assert data[0].color_values is not None and len(data[0].color_values) == 2  # aligned to subset
    assert data[0].color_norm is not None
    # the unreliable scan is a single warning marker + one note
    warn = [m for m in vm.spec.markers if m.kind is MarkerKind.WARNING]
    assert len(warn) == 1 and abs(warn[0].x - 2.2) < 1e-9
    unrel = [a for a in vm.spec.annotations if a.role is State.UNRELIABLE]
    assert len(unrel) == 1


def test_pooled_master_marked_and_members_still_shown(make_ws):
    ws = _make(make_ws, with_master=True)
    vm = DispersionViewModel(ws)
    pooled = [s for s in vm.spec.series if s.state is State.POOLED]
    assert len(pooled) == 1 and pooled[0].yerr is not None  # its own certifying whisker
    assert pooled[0].label == "pooled (3)"
    members = [s for s in vm.spec.series
               if s.role is Role.DATA and s.yerr is not None and s.state is not State.POOLED]
    assert len(members) == 1 and len(members[0].x) == 2      # per-scan members still visible
    # the master is clickable -> routes to its own FitView
    with_master_scan = [fv for _, _, fv in vm._points if fv.scan_number == 10]
    assert with_master_scan and with_master_scan[0].pooled


def test_prior_and_branch_curves(make_ws):
    branch = [
        {"sample": "tavi", "t_bin": 200.0, "q": [2.0, 0.0, 0.0], "omega0": 4.2, "sigma": 0.3},
        {"sample": "tavi", "t_bin": 200.0, "q": [2.2, 0.0, 0.0], "omega0": 6.2, "sigma": 0.3},
    ]
    ws = _make(make_ws, prior=_PRIOR, branch=branch)
    vm = DispersionViewModel(ws)
    roles = [s.role for s in vm.spec.series]
    assert Role.PRIOR in roles and Role.BRANCH in roles


def test_prior_degrades_when_fewer_than_two_points(make_ws):
    ws = _make(make_ws, prior=[_PRIOR[0]])  # a single anchor is not a curve
    vm = DispersionViewModel(ws)
    assert not any(s.role is Role.PRIOR for s in vm.spec.series)


def test_off_curve_flag_fires_on_outlier(make_ws):
    # Prior is flat ω≈4.0 across H in [2.0, 2.2]; an added IN-DOMAIN outlier at H=2.15 with ω=9.0
    # (half-width 0.4) sits ~12σ off the prior and must be flagged.
    specs = _SPECS + [dict(scan=5, h=2.15, k=0.0, l=0.0, omega0=9.0, reliable=True,
                           entropy=0.0, verdict="resolved")]
    ws = _make(make_ws, prior=_PRIOR, specs=specs)
    vm = DispersionViewModel(ws)
    off = [a for a in vm.spec.annotations if a.xy is not None and "off prior" in a.text]
    assert any(abs(a.xy[0] - 2.15) < 1e-9 and abs(a.xy[1] - 9.0) < 1e-9 for a in off)


def test_off_curve_skips_points_outside_prior_domain(make_ws):
    # Prior spans H in [2.0, 2.2]; a point far OUTSIDE that q-domain (H=5.0, ω=20) must NOT be
    # flagged — np.interp would clamp to the end anchor and manufacture a bogus off-curve flag.
    specs = _SPECS + [dict(scan=6, h=5.0, k=0.0, l=0.0, omega0=20.0, reliable=True,
                           entropy=0.0, verdict="resolved")]
    ws = _make(make_ws, prior=_PRIOR, specs=specs)
    vm = DispersionViewModel(ws)
    off = [a for a in vm.spec.annotations if a.xy is not None and "off prior" in a.text]
    assert not any(abs(a.xy[0] - 5.0) < 1e-9 for a in off)  # out-of-domain point not flagged


def test_no_off_curve_without_prior(make_ws):
    ws = _make(make_ws)  # no prior
    vm = DispersionViewModel(ws)
    assert not any(a.xy is not None and "off prior" in a.text for a in vm.spec.annotations)


def test_temperature_switch_rebuilds(qtbot, make_ws):
    # Two scans at T=300 alongside the (modal) T=200 set, so the switch actually changes the
    # rebuilt projection rather than "switching" to the temperature already in effect.
    specs = _SPECS + [
        dict(scan=7, h=2.0, k=0.0, l=0.0, omega0=4.0, reliable=True, entropy=0.0,
             verdict="resolved", T=300.0),
        dict(scan=8, h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.0,
             verdict="resolved", T=300.0),
    ]
    ws = _make(make_ws, specs=specs)
    vm = DispersionViewModel(ws)
    assert vm.temperature == 200.0  # modal temperature (4 scans at 200 vs 2 at 300)
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_temperature(300.0)
    assert vm.temperature == 300.0
    assert vm.spec is not None
    assert vm.spec.meta["temperature"] == 300.0


def test_point_click_activates_fit(qtbot, make_ws):
    ws = _make(make_ws)
    vm = DispersionViewModel(ws)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.1, 5.0)
    assert blocker.args[0].scan_number == 2


def test_dock_renders_and_routes_click(qtbot, make_ws):
    from isar_gui.views.dispersion_pane import DispersionPane
    ws = _make(make_ws)
    vm = DispersionViewModel(ws)
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.0, 4.0)
    assert blocker.args[0].scan_number == 1


def test_dock_clears_render_when_spec_becomes_none(qtbot, make_ws):
    """A None spec must clear the previous render rather than retaining stale data on the plot."""
    from isar_gui.views.dispersion_pane import DispersionPane
    ws = _make(make_ws)
    vm = DispersionViewModel(ws)
    dock = DispersionPane(vm)
    qtbot.addWidget(dock)
    assert getattr(dock._plot, "_isar_rendered", None) is not None
    vm._spec = None
    dock.refresh()
    assert getattr(dock._plot, "_isar_rendered", None) is None


def test_view_state_round_trips_through_session(make_ws):
    from isar_gui.session import Session

    ws = _make(make_ws)
    vm1 = DispersionViewModel(ws)
    vm1.set_temperature(200.0)
    session = Session()
    vm1.save_view_state(session)
    assert session.view_state["dispersion"] == {"temperature": 200.0}

    ws2 = _make(make_ws)
    vm2 = DispersionViewModel(ws2)
    vm2.load_view_state(session)
    assert vm2.temperature == pytest.approx(200.0)


def test_mainwindow_registers_dispersion_dock_and_routes(qtbot, make_ws):
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session
    ws = _make(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._center_host.pane("dispersion") is not None
    with qtbot.waitSignal(w.inspector_vm.spec_changed, timeout=1000):
        w.dispersion_vm.activate_at(2.1, 5.0)
    assert not w.inspector_vm.is_empty
    assert w.inspector_vm.state.main_spec is not None


# ---- MVP-16 smoke (read-only) ------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen baseline not present")
def test_mvp16_dispersion_smoke():
    ws = Workspace.open(_MVP16)
    vm = DispersionViewModel(ws)
    spec = vm.spec
    assert spec is not None
    assert spec.projection is not None
    pts = [s for s in spec.series if s.role is Role.DATA]
    assert sum(len(np.atleast_1d(s.x)) for s in pts) > 0

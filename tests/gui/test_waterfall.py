"""C4 waterfall/comparison view tests: WaterfallViewModel derives the same plane as the heatmap/
dispersion (shared helper), stacks per-T traces with a view-state-only offset (G10: never written to
the workspace/card), follows the pooled curve-only rule, reuses dispersion_spec verbatim in
comparison mode, and round-trips mode/q/offset through the GUI session file.

Uses a LOCAL fixture builder (parallel to tests/gui/test_dispersion.py's ``_make``, not the shared
tests/synthetic_ws.py) so this file's two-T-bin population doesn't perturb other suites' fixture
counts. Offscreen, importorskip, xdist-safe."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.plotspec.spec import Role
from isar.workspace_api import Workspace
from isar_gui.session import Session
from isar_gui.viewmodels.waterfall import WaterfallViewModel

_MVP16 = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")

_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


def _outcome(*, h, k, l, omega0, T, reliable=True, entropy=0.0, verdict="resolved",
            pooled=False, n_members=None) -> dict:
    oc = {
        "detector_id": "det", "verdict": verdict, "disposition": verdict, "confidence": 0.9,
        "omega0": omega0, "gamma": 1.0, "q": [h, k, l], "T": T, "sample": "tavi",
        "assignment_entropy": entropy,
        "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
        "interval": [omega0 - 0.4, omega0 + 0.4], "interval_reliable": bool(reliable),
    }
    if pooled:
        oc["pooled"] = True
        oc["n_members"] = n_members
        # A pooled master has no persisted spectrum, so its curve can only come from the "pure"
        # (amplitudes-only) evaluate_model path — mirrors how a real pooled fit stores its combined
        # linear amplitudes rather than raw member counts.
        oc["amplitudes"] = {"phonon.A": 500.0, "bg.BG": 100.0}
    return oc


# Two q's (H=2.0, H=2.1), each measured at two T bins (10 K, 20 K) -> a 2x2 grid, so both the q
# selector and the T-bin waterfall have something to switch between.
_SPECS = [
    dict(scan=1, h=2.0, k=0.0, l=0.0, omega0=4.0, T=10.0),
    dict(scan=2, h=2.0, k=0.0, l=0.0, omega0=4.2, T=20.0),
    dict(scan=3, h=2.1, k=0.0, l=0.0, omega0=5.0, T=10.0),
    dict(scan=4, h=2.1, k=0.0, l=0.0, omega0=5.1, T=20.0),
]


def _make(make_ws, *, specs=_SPECS, with_master=False) -> Workspace:
    scans, rows, data = {}, [], []
    for s in specs:
        oc = _outcome(h=s["h"], k=s["k"], l=s["l"], omega0=s["omega0"], T=s["T"])
        sid = f"tavi:{s['scan']}:det"
        scans[sid] = {"scan_id": sid, "scan_number": s["scan"], "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": s["scan"], "detector": "det", "sample": "tavi", "T": s["T"],
                     "h": s["h"], "k": s["k"], "l": s["l"], "status": "fitted"})
        data.append({"scan_number": s["scan"], "energy": list(_X), "counts": _counts(s["omega0"])})
    if with_master:
        # A pooled master at H=2.0, T=30 K (its own T bin) — no data file, curve-only per the rule.
        moc = _outcome(h=2.0, k=0.0, l=0.0, omega0=4.1, T=30.0, pooled=True, n_members=3)
        scans["tavi:10:det"] = {"scan_id": "tavi:10:det", "scan_number": 10,
                                "scan_type": "measurement", "status": "fitted",
                                "detectors": {"det": moc}, "provenance": {"M": 600}}
        rows.append({"scan": 10, "detector": "det", "sample": "tavi", "T": 30.0,
                     "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted",
                     "pooled": True, "n_members": 3})
    root = make_ws(scans=scans, rows=rows, data_files=data)
    return Workspace.open(root)


def test_two_t_bins_at_selected_q_produce_two_traces(make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    spec = vm.spec
    data = [s for s in spec.series if s.role is Role.DATA]
    assert len(data) == 2
    fit = [s for s in spec.series if s.role is Role.FIT]
    assert len(fit) == 2
    labels = {s.label for s in data}
    assert labels == {"T=10 K", "T=20 K"}


def test_q_selector_lists_distinct_q(make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    assert set(round(q, 3) for q in vm.q_values) == {2.0, 2.1}


def test_pooled_master_at_a_q_is_curve_only_trace(make_ws):
    ws = _make(make_ws, with_master=True)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    spec = vm.spec
    data = [s for s in spec.series if s.role is Role.DATA]
    fit = [s for s in spec.series if s.role is Role.FIT]
    assert len(data) == 2      # the two real scans only — the pooled T bin draws no data
    assert len(fit) == 3       # but all three T bins get a fit curve
    pooled_fit = next(s for s in fit if "pooled" in s.label)
    assert "member data not shown" in pooled_fit.label
    assert "3" in pooled_fit.label


def test_mode_switch_rebuilds_to_comparison(qtbot, make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_mode("comparison")
    assert vm.mode == "comparison"
    data = [s for s in vm.spec.series if s.role is Role.DATA]
    # per-T series, distinctly labeled (comparison overlays ω̂(q) per T)
    assert any("T=10 K" in (s.label or "") for s in data)
    assert any("T=20 K" in (s.label or "") for s in data)
    assert vm.spec.projection is not None


def test_offset_change_re_emits_and_shifts_traces(qtbot, make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_offset_step(1000.0)
    data = sorted((s for s in vm.spec.series if s.role is Role.DATA), key=lambda s: s.label)
    # T=10 K is trace 0 (no shift baseline visible via yerr-unshifted check instead); T=20 K is
    # trace 1, shifted by +1000 relative to its raw counts.
    t20 = next(s for s in data if s.label == "T=20 K")
    assert float(np.max(t20.y)) > 1000.0
    notes = [a.text for a in vm.spec.annotations]
    assert any("1000" in t for t in notes)


def test_offset_annotation_on_screen(make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    assert any("offset" in a.text for a in vm.spec.annotations)


def test_trace_click_activates_fit(qtbot, make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    # scan 1 (T=10K, H=2.0) has omega0=4.0; click near its curve peak.
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(4.0, vm.spec.series[0].y.max())
    assert blocker.args[0].scan_number in (1, 2)


def test_comparison_point_click_activates_fit(qtbot, make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_mode("comparison")
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.0, 4.0)
    assert blocker.args[0].scan_number == 1


def test_dock_clears_render_when_spec_becomes_none(qtbot, make_ws):
    """A None spec must clear the previous render rather than retaining stale data on the plot."""
    from isar_gui.views.waterfall_pane import WaterfallPane
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    dock = WaterfallPane(vm)
    qtbot.addWidget(dock)
    assert getattr(dock._plot, "_isar_rendered", None) is not None
    vm._spec = None
    dock.refresh()
    assert getattr(dock._plot, "_isar_rendered", None) is None


def test_view_state_round_trips_through_session(make_ws):
    ws = _make(make_ws)
    vm1 = WaterfallViewModel(ws)
    vm1.set_mode("comparison")
    vm1.set_selected_q(2.1)
    vm1.set_offset_step(42.0)
    session = Session()
    vm1.save_view_state(session)
    assert "waterfall" in session.view_state

    ws2 = _make(make_ws)
    vm2 = WaterfallViewModel(ws2)
    vm2.load_view_state(session)
    assert vm2.mode == "comparison"
    assert vm2.selected_q == pytest.approx(2.1)
    assert vm2.offset_step == pytest.approx(42.0)


def test_default_q_from_browser_selection(make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    fv = next(fv for fv in ws.results if fv.scan_number == 3)  # H=2.1
    vm.default_q_from_selection(fv)
    assert vm.selected_q == pytest.approx(2.1)


def test_refresh_idempotent(make_ws):
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    spec1 = vm.spec
    vm.refresh()
    spec2 = vm.spec
    assert spec1.title == spec2.title
    assert len(spec1.series) == len(spec2.series)


def test_dock_renders_and_routes_click(qtbot, make_ws):
    from isar_gui.views.waterfall_pane import WaterfallPane
    ws = _make(make_ws)
    vm = WaterfallViewModel(ws)
    vm.set_selected_q(2.0)
    dock = WaterfallPane(vm)
    qtbot.addWidget(dock)
    with qtbot.waitSignal(vm.point_activated, timeout=1000):
        vm.activate_at(4.0, vm.spec.series[0].y.max())


def test_mainwindow_registers_waterfall_dock_and_routes(qtbot, make_ws):
    from isar_gui.main_window import MainWindow
    ws = _make(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert "Waterfall / Comparison" in w._docks
    w.waterfall_vm.set_mode("comparison")
    with qtbot.waitSignal(w.inspector_vm.spec_changed, timeout=1000):
        w.waterfall_vm.activate_at(2.0, 4.0)
    assert not w.inspector_vm.is_empty


def test_mainwindow_browser_selection_sets_default_q(qtbot, make_ws):
    from isar_gui.main_window import MainWindow
    ws = _make(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    fv = next(fv for fv in ws.results if fv.scan_number == 3)  # H=2.1
    w.browser_vm.select(fv)
    assert w.waterfall_vm.selected_q == pytest.approx(2.1)


# ---- MVP-16 smoke (read-only) ------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen baseline not present")
def test_mvp16_waterfall_smoke():
    ws = Workspace.open(_MVP16)
    vm = WaterfallViewModel(ws)
    spec = vm.spec
    assert spec is not None
    assert vm.selected_q is not None
    assert len(spec.series) > 0

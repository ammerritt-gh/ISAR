"""C3 (q, E) plane tests: measured strips, state-first fit glyphs, directional uncertainty, optional
entropy colouring, latest-acquisition regions, and inspector routing."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from baselines import mvp16_path
from isar.plotspec.spec import Glyph, RegionKind, Role, State
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


def _make(make_ws, *, with_master: bool = False, master_spectrum: bool = False,
          specs=_SPECS) -> Workspace:
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
        if master_spectrum:
            # The collated group's persisted ΣN: pools scan 2 (the H=2.1 member); its union
            # grid is covered by ΣM=2 everywhere except the last four points (ΣM=4), so the
            # plane's normalisation to the common reference (median ΣM = 2) halves those.
            scans["tavi:10:det"]["provenance"].update({
                "pooled": True, "members": [["tavi", 2, "det"]],
                "spectrum_ref": "data/pooled/m.json"})
            rows[-1]["spectrum_ref"] = "data/pooled/m.json"
    root = make_ws(scans=scans, rows=rows, data_files=data)
    if with_master and master_spectrum:
        import json
        pooled = root / "data" / "pooled" / "m.json"
        pooled.parent.mkdir(parents=True, exist_ok=True)
        counts = _counts(5.0)
        pooled.write_text(json.dumps({
            "schema": "isar.pooled_spectrum/1", "energy": list(_X), "counts": counts,
            "variance": counts, "monitor": [2.0] * 17 + [4.0] * 4, "mask": [True] * 21,
            "members": [["tavi", 2, "det"]], "n_members": 1}), encoding="utf-8")
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


def test_latest_legacy_fallback_box_uses_visible_strip_bounds(make_ws):
    # With no detailed driver events, the last physical result is the documented legacy fallback.
    vm = HeatmapViewModel(_make(make_ws, specs=_SPECS[:3]))
    assert len(vm.spec.regions) == 1
    region = vm.spec.regions[0]
    assert region.kind is RegionKind.LATEST_ACQUISITION
    assert region.x_min == pytest.approx(2.16)
    assert region.x_max == pytest.approx(2.24)
    assert region.y_min == pytest.approx(-2.0)
    assert region.y_max == pytest.approx(8.0)


def test_global_latest_outside_current_projection_has_no_box(make_ws):
    # The default fixture's globally last physical result is H=2.05, K=0.5, outside the K=0 slice.
    vm = HeatmapViewModel(_make(make_ws))
    assert vm.spec.regions == []


def test_omega_hat_directional_whiskers_only_where_certifying(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    valid = next(s for s in vm.spec.series if s.glyph is Glyph.INDIVIDUAL_VALID)
    assert len(valid.x) == 2
    assert valid.yerr is None and valid.errors is not None
    np.testing.assert_allclose(valid.errors.bottom, [0.4, 0.4])
    np.testing.assert_allclose(valid.errors.top, [0.4, 0.4])
    assert valid.color_values is None  # state-first colouring is the default


def test_unreliable_omega_hat_is_red_diamond_without_whiskers(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    unreliable = next(
        s for s in vm.spec.series if s.glyph is Glyph.INDIVIDUAL_UNRELIABLE)
    assert unreliable.x.tolist() == [2.2]
    assert unreliable.errors is None
    assert vm.spec.markers == []


def test_entropy_channel_populated(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    ent = vm.spec.meta.get("omega_hat_entropy_all")
    assert ent is not None and len(ent) == 3  # one per in-window ω̂ point
    assert all(e is not None for e in ent)
    assert not any("assignment entropy" in a.text for a in vm.spec.annotations)
    vm.set_color_by_entropy(True)
    assert any("assignment entropy" in a.text for a in vm.spec.annotations)
    valid = next(s for s in vm.spec.series if s.glyph is Glyph.INDIVIDUAL_VALID)
    assert valid.color_values == [0.0, 0.3]
    unreliable = next(
        s for s in vm.spec.series if s.glyph is Glyph.INDIVIDUAL_UNRELIABLE)
    assert unreliable.color_values == [0.69]


def test_pooled_master_on_overlay_but_not_strips(make_ws):
    ws = _make(make_ws, with_master=True)
    vm = HeatmapViewModel(ws)
    # Legacy workspace: the master is explicitly unassessed, but still carries its fit interval.
    pooled = [s for s in vm.spec.series if s.state is State.POOLED]
    assert len(pooled) == 1 and pooled[0].errors is not None
    assert pooled[0].glyph is Glyph.MASTER_UNASSESSED
    assert pooled[0].label == "pooled master (legacy unassessed)"
    # ...the per-scan member scatter still shows (masters not mixed in)...
    members = [s for s in vm.spec.series
               if s.glyph is Glyph.INDIVIDUAL_VALID]
    assert len(members) == 1 and len(members[0].x) == 2
    # ...and a master WITHOUT a persisted spectrum (pre-artifact workspace) contributes no
    # strip; its member's own column stays: still 3 strips.
    assert len(vm.spec.strips) == 3
    assert vm.spec.markers == []


def test_master_with_persisted_spectrum_is_the_column_at_its_q(make_ws):
    """The plane's column at a q is the collated MASTER, normalised to one plane-wide
    reference monitor, and it replaces its members' columns (which would otherwise stack at
    the same x with the latest scan on top). The master is found through its members, not
    its fit row."""
    ws = _make(make_ws, with_master=True, master_spectrum=True)
    vm = HeatmapViewModel(ws)
    strips = vm.spec.strips
    assert sorted(round(s.x_center, 3) for s in strips) == [2.0, 2.1, 2.2]  # one per q
    master = next(s for s in strips if abs(s.x_center - 2.1) < 1e-9)
    assert master.meta["pooled"] is True
    assert master.meta["unit_id"] == "tavi:10:det"
    assert master.meta["member_unit_ids"] == ["tavi:2:det"]
    counts = np.asarray(_counts(5.0))
    # median ΣM over the union grid is 2 -> reference 2: ΣM=2 points are raw ΣN, ΣM=4 halve.
    np.testing.assert_allclose(master.values[:17], counts[:17])
    np.testing.assert_allclose(master.values[17:], counts[17:] / 2.0)
    assert vm.spec.value_label == "counts per 2 monitor"
    # The member's own column is gone (the master stands for it); other q's stay per-scan.
    assert all(not s.meta.get("pooled") for s in strips if abs(s.x_center - 2.1) > 1e-9)


def test_latest_acquisition_box_resolves_through_the_master_column(make_ws):
    """With no detailed driver events the legacy fallback is the last physical result; when
    that scan is pooled into the master column, the box lands on the master's column."""
    specs = [_SPECS[0], _SPECS[2], _SPECS[1]]  # scan 2 (H=2.1, the pooled member) is last
    vm = HeatmapViewModel(_make(make_ws, with_master=True, master_spectrum=True, specs=specs))
    assert len(vm.spec.regions) == 1
    assert vm.spec.regions[0].x_min == pytest.approx(2.06)
    assert vm.spec.regions[0].x_max == pytest.approx(2.14)


def test_fit_display_glyphs_replace_verdict_overlays(make_ws):
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    glyphs = {series.glyph for series in vm.spec.series}
    assert Glyph.INDIVIDUAL_VALID in glyphs
    assert Glyph.INDIVIDUAL_UNRELIABLE in glyphs
    assert vm.spec.markers == []


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


def test_ambiguous_candidates_remain_visible_as_composed_glyph_without_whiskers(make_ws):
    specs = _SPECS + [{
        "scan": 9, "h": 2.3, "k": 0.0, "l": 0.0, "omega0": None,
        "candidate_seed": 2.7, "reliable": False, "entropy": 0.69,
        "verdict": "branch_ambiguous", "branch_candidates": [
            {"energy_meV": 2.7, "omega0_err": 0.12},
            {"energy_meV": 6.85, "omega0_err": 0.18},
        ],
    }]
    vm = HeatmapViewModel(_make(make_ws, specs=specs))
    ambiguous = next(
        series for series in vm.spec.series
        if series.glyph is Glyph.UNSELECTED_CANDIDATE)
    assert list(zip(ambiguous.x, ambiguous.y)) == [(2.3, 2.7), (2.3, 6.85)]
    assert ambiguous.errors is None


def test_dock_renders_and_routes_click(qtbot, make_ws):
    from isar_gui.views.heatmap_pane import HeatmapPane
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    dock = HeatmapPane(vm)
    qtbot.addWidget(dock)
    with qtbot.waitSignal(vm.point_activated, timeout=1000) as blocker:
        vm.activate_at(2.0, 4.0)
    assert blocker.args[0].scan_number == 1


def test_colorbar_proxy_controls_levels_and_lut_for_every_strip(qtbot, make_ws):
    import pyqtgraph as pg

    from isar_gui.views.heatmap_pane import HeatmapPane
    vm = HeatmapViewModel(_make(make_ws))
    dock = HeatmapPane(vm)
    qtbot.addWidget(dock)
    rendered = dock._plot._isar_rendered
    assert rendered.strip_colormap == "viridis"
    assert dock._strip_images == rendered.strip_images
    assert dock._colorbar.imageItem() is dock._colorbar_proxy
    proxy_lut = np.asarray(dock._colorbar_proxy.lut)
    assert all(np.array_equal(np.asarray(image.lut), proxy_lut)
               for image in dock._strip_images)

    dock._colorbar.setLevels(150.0, 450.0)
    assert all(tuple(image.getLevels()) == pytest.approx((150.0, 450.0))
               for image in dock._strip_images)
    plasma = pg.colormap.get("plasma")
    dock._colorbar.gradient.setColorMap(plasma)
    proxy_lut = np.asarray(dock._colorbar_proxy.lut)
    assert all(np.array_equal(np.asarray(image.lut), proxy_lut)
               for image in dock._strip_images)


def test_colorbar_keeps_private_proxy_and_drops_strip_references(qtbot, make_ws):
    """The histogram owns only its private proxy; a strip-less refresh drops all real images."""
    import pyqtgraph as pg

    from isar.plotspec.spec import PlotSpec
    from isar_gui.views.heatmap_pane import HeatmapPane
    ws = _make(make_ws)
    vm = HeatmapViewModel(ws)
    dock = HeatmapPane(vm)
    qtbot.addWidget(dock)
    old_img = dock._colorbar.imageItem()
    assert isinstance(old_img, pg.ImageItem)
    assert old_img is dock._colorbar_proxy
    assert dock._strip_images
    assert all(image is not old_img for image in dock._strip_images)
    # Refresh with a strip-less (but non-None) spec.
    vm._spec = PlotSpec(title="empty", xlabel="q", ylabel="E")
    dock.refresh()
    assert dock._colorbar.imageItem() is old_img
    assert dock._strip_images == []


def test_view_state_round_trips_through_session(make_ws):
    ws = _make(make_ws)
    vm1 = HeatmapViewModel(ws)
    vm1.set_temperature(200.0)
    vm1.set_color_by_entropy(True)
    session = Session()
    vm1.save_view_state(session)
    assert session.view_state["heatmap"] == {"temperature": 200.0}
    assert session.view_state["fit_display"] == {"color_by_entropy": True}

    ws2 = _make(make_ws)
    vm2 = HeatmapViewModel(ws2)
    vm2.load_view_state(session)
    assert vm2.temperature == pytest.approx(200.0)
    assert vm2.color_by_entropy is True


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


def test_colour_scale_is_set_by_the_inelastic_signal_and_the_elastic_line_saturates(make_ws):
    """A Bragg-elastic cell thousands of counts tall must not paint every phonon black: the plane's
    levels come from |E| beyond 1.5 resolution FWHM (1.2 fixture: gauss_fwhm 1.2 -> 1.8 meV),
    the elastic line saturates, and the spec says so. Without an elastic cell there is no clip."""
    import json
    ws = _make(make_ws, with_master=True, master_spectrum=True)
    vm = HeatmapViewModel(ws)
    assert vm.spec.strip_levels is None                      # nothing reaches the elastic band
    pooled = ws.root / "data" / "pooled" / "m.json"
    payload = json.loads(pooled.read_text(encoding="utf-8"))
    at_zero = int(np.argmin(np.abs(_X)))                     # E = 0 on the fixture grid
    payload["counts"][at_zero] = 50000.0
    payload["variance"][at_zero] = 50000.0
    pooled.write_text(json.dumps(payload), encoding="utf-8")
    ws.refresh()
    vm.refresh()
    spec = vm.spec
    inelastic_max = max(float(np.nanmax(np.asarray(s.values)[np.abs(np.asarray(s.y)) > 1.8]))
                        for s in spec.strips)
    assert spec.strip_levels is not None
    assert spec.strip_levels[1] == pytest.approx(inelastic_max)
    assert spec.strip_levels[1] < 1000.0                     # the elastic cell is above the scale
    assert any("elastic line saturates" in a.text for a in spec.annotations)

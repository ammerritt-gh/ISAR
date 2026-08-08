"""C2 fit inspector tests: InspectorViewModel derives the interval ladder (with the correct
certifying tier), model-candidate rows (posterior-weighted, ΔAIC diagnostic), verdict/provenance;
InspectorWidget renders each case offscreen; a browser selection drives the inspector. Offscreen
(conftest forces QT offscreen), importorskip, xdist-safe (each test stamps its own workspace)."""
from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.inspector import InspectorViewModel
from isar_gui.views.inspector_widget import InspectorWidget

_MVP16 = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")

_X = np.linspace(-2.0, 8.0, 25)
_COUNTS = (100.0 + 500.0 * np.exp(-((_X - 5.0) ** 2) / 2.0)).round()

_DEFAULT_CANDS = [
    {"label": "phonon", "delta_aic": 0.0, "akaike_weight": 0.82,
     "posterior_probability": 0.9, "n_modes": 1},
    {"label": "phonon+qe", "delta_aic": 3.4, "akaike_weight": 0.18,
     "posterior_probability": 0.1, "n_modes": 1},
]


def _outcome(**over) -> dict:
    base = {
        "detector_id": "det", "verdict": "resolved", "disposition": "resolved",
        "confidence": 0.9, "omega0": 5.0, "gamma": 1.0, "q": [2.0, 0.0, 0.0], "T": 200.0,
        "sample": "tavi", "best_label": "phonon", "modality": "single", "fit_kind": "unconstrained",
        "n_modes": 1, "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
        "amplitudes": {"phonon.A": 500.0, "bg.BG": 100.0},
        "candidates": [dict(c) for c in _DEFAULT_CANDS],
        "interval": [4.6, 5.4], "interval_reliable": True,
        "bayes_interval": [4.55, 5.0, 5.45], "bayes_ess": 420.0,
    }
    base.update(over)
    return base


def _ws_with(make_ws, outcome) -> Workspace:
    scans = {"tavi:1:det": {"scan_id": "tavi:1:det", "scan_number": 1,
                            "scan_type": "measurement", "status": "fitted",
                            "detectors": {"det": outcome}, "provenance": {"M": 200}}}
    root = make_ws(scans=scans,
                   data_files=[{"scan_number": 1, "energy": list(_X), "counts": list(_COUNTS)}])
    return Workspace.open(root)


def _fit(ws):
    return ws.scans[0].detectors["det"]


# ---- VM: interval ladder + certifying tier ----------------------------------
def test_ladder_reliable_bootstrap_certifies(make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert st.certifying_tier == "bootstrap"
    boot = next(r for r in st.ladder_rows if r.tier == "bootstrap")
    assert boot.certifying and "[4.6" in boot.interval_text
    bayes = next(r for r in st.ladder_rows if r.tier == "bayes")
    assert not bayes.certifying and bayes.diagnostics_only


def test_ladder_unreliable_bootstrap_falls_back_to_profile(make_ws):
    ws = _ws_with(make_ws, _outcome(interval_reliable=False, omega0_profile=[4.7, 5.3]))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert st.certifying_tier == "profile"
    boot = next(r for r in st.ladder_rows if r.tier == "bootstrap")
    assert not boot.certifying and boot.interval_text == "(unreliable)"
    prof = next(r for r in st.ladder_rows if r.tier == "profile")
    assert prof.certifying


def test_ladder_hesse_trusted_only_certifies(make_ws):
    ws = _ws_with(make_ws, _outcome(interval=None, omega0_profile=None,
                                    hesse_interval=[4.8, 5.2], hesse_trusted=True))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert st.certifying_tier == "hesse"
    assert sum(1 for r in st.ladder_rows if r.certifying) == 1


def test_ladder_bayes_never_certifies_even_when_only_tier(make_ws):
    ws = _ws_with(make_ws, _outcome(interval=None, omega0_profile=None,
                                    hesse_interval=None, hesse_trusted=False))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert st.certifying_tier is None
    assert all(not r.certifying for r in st.ladder_rows)


def test_candidates_none_flags_not_recorded(make_ws):
    ws = _ws_with(make_ws, _outcome(candidates=None))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    assert vm.state.candidates_recorded is False
    assert vm.state.candidate_rows == []


def test_candidates_winner_is_best_label(make_ws):
    """The winner is the row matching best_label — delta_aic==0 no longer decides (the
    posterior does; ΔAIC is a persisted diagnostic)."""
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    winners = [r for r in vm.state.candidate_rows if r.winner]
    assert len(winners) == 1 and winners[0].label == "phonon"
    assert winners[0].posterior == pytest.approx(0.9)


def test_candidates_without_posterior_render_blank_not_invented(make_ws):
    """A pre-/2 ledger (no posterior_probability) still rows up; posterior stays None."""
    cands = [{"label": "phonon", "delta_aic": 0.0, "akaike_weight": 0.82, "n_modes": 1}]
    ws = _ws_with(make_ws, _outcome(candidates=cands))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    rows = vm.state.candidate_rows
    assert len(rows) == 1 and rows[0].posterior is None and rows[0].winner


def test_spec_changed_fires_and_empty_reset(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_fit(_fit(ws))
    assert not vm.is_empty and vm.state.main_spec is not None
    with qtbot.waitSignal(vm.spec_changed, timeout=1000):
        vm.set_fit(None)
    assert vm.is_empty


def test_provenance_pooled_enumerates_members(make_ws):
    ws = _ws_with(make_ws, _outcome(pooled=True, n_members=2, members=["101", "102"]))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    prov = vm.state.provenance
    assert "pooled" in prov and prov["members"] == ["101", "102"]


def test_provenance_rejected_master_is_uncertifiable(make_ws):
    ws = _ws_with(make_ws, _outcome(pooled=True, disposition="rejected", n_members=2))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    assert vm.state.provenance["pooled"] == "pooled master — uncertifiable"


def test_pooled_master_never_overlays_member_data(make_ws):
    # scan_number 1 resolves to a real member data/*.json in this workspace; the honesty law says
    # the pooled master must NOT draw those member counts under a pooled title. Curve-only + note.
    from isar.plotspec.spec import Role, State
    ws = _ws_with(make_ws, _outcome(pooled=True, n_members=2, members=["1", "2"]))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert st.main_spec is not None
    assert all(s.role is not Role.DATA for s in st.main_spec.series)
    assert any(a.role is State.POOLED and "member data not shown" in a.text
               for a in st.main_spec.annotations)
    assert st.residuals_spec is None  # no residuals without the (unpersisted) pooled spectrum


def test_pooled_master_does_not_read_scan_data_and_uses_omega_window(make_ws, monkeypatch):
    # A pooled master whose scan_number (1) collides with a real member data file on disk must NOT
    # read that file: it never draws member counts, and the colliding grid must not seed the fine
    # grid. The curve falls back to the ω₀±10 window instead of the file's [-2, 8] grid.
    from isar.plotspec.spec import Role
    ws = _ws_with(make_ws, _outcome(pooled=True, n_members=2, members=["1", "2"], omega0=5.0))
    calls = []
    orig = ws.scan_data
    monkeypatch.setattr(ws, "scan_data", lambda n: (calls.append(n), orig(n))[1])
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state
    assert calls == []  # the pooled path never reads the (colliding) member data file
    assert st.main_spec is not None
    fit_total = [s for s in st.main_spec.series
                 if s.role is Role.FIT and np.asarray(s.x).size > 1]
    assert len(fit_total) == 1
    xs = np.asarray(fit_total[0].x, dtype=float)
    assert xs.min() == pytest.approx(-5.0)  # ω₀±10 window, not the file grid ([-2, 8])
    assert xs.max() == pytest.approx(15.0)


def test_op3_shaped_two_mode_master_shows_pooled_data_components_and_residuals(make_ws):
    from isar.plotspec.spec import Role

    manifest = {
        "schema": "isar.model_manifest/1",
        "components": [
            {"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
            {"type": "phonon_dho", "prefix": "phonon2", "peak": "stokes+astokes"},
            {"type": "constant_background", "prefix": "bg"},
        ],
        "parameters": {
            "phonon.Omega_d": 3.9, "phonon.Gamma": 0.3, "phonon.A": 350.0,
            "phonon2.Omega_d": 6.2, "phonon2.Gamma": 0.4, "phonon2.A": 220.0,
            "bg.BG": 20.0,
        },
    }
    ws = _ws_with(make_ws, _outcome(
        pooled=True, n_members=5, members=["10", "11", "12", "13", "14"],
        spectrum_ref="data/pooled/op3.json", best_label="phonon+phonon2+bg",
        model_manifest=manifest, n_modes=2,
        modes=[{"omega0": 3.91, "gamma": 0.3}, {"omega0": 6.21, "gamma": 0.4}]))
    pooled_path = ws.root / "data" / "pooled" / "op3.json"
    pooled_path.parent.mkdir(parents=True, exist_ok=True)
    pooled_path.write_text(json.dumps({
            "schema": "isar.pooled_spectrum/1", "energy": list(_X),
            "counts": list(_COUNTS), "variance": list(_COUNTS),
            "monitor": [1.0] * len(_X),
            "mask": [True] * len(_X),
        "members": [["tavi", number, "det"] for number in range(10, 15)],
    }), encoding="utf-8")

    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    state = vm.state
    assert state.curve_error is None
    assert state.residuals_spec is not None
    assert any(series.role is Role.DATA for series in state.main_spec.series)
    assert any(series.role is Role.FIT for series in state.main_spec.series)
    assert sum(series.role is Role.COMPONENT for series in state.main_spec.series) >= 2


# ---- Widget rendering -------------------------------------------------------
def test_widget_renders_reliable_case(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    assert w._ladder.rowCount() == 4
    # certifying bootstrap row is bold (visually primary)
    boot_row = next(i for i in range(4) if w._ladder.item(i, 0).text().startswith("bootstrap"))
    assert w._ladder.item(boot_row, 0).font().bold()


def test_widget_unreliable_bootstrap_shows_text_no_numbers(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome(interval_reliable=False, omega0_profile=[4.7, 5.3]))
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    boot_row = next(i for i in range(w._ladder.rowCount())
                    if w._ladder.item(i, 0).text().startswith("bootstrap"))
    text = w._ladder.item(boot_row, 1).text()
    assert text == "(unreliable)"
    assert not any(ch.isdigit() for ch in text)


def test_widget_candidates_winner_bold(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    headers = [w._candidates.horizontalHeaderItem(i).text()
               for i in range(w._candidates.columnCount())]
    assert headers == ["Model", "P(model)", "ΔAIC", "n_modes"]
    win_row = next(i for i in range(w._candidates.rowCount())
                   if w._candidates.item(i, 0).text() == "phonon")
    assert w._candidates.item(win_row, 0).font().bold()
    assert w._candidates.item(win_row, 1).text() == "0.900"
    assert w._candidates.item(win_row, 2).text() == "0.00"


def test_widget_candidates_not_recorded_placeholder(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome(candidates=None))
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    assert w._candidates.item(0, 0).text() == "not recorded"


def test_verdict_escapes_markup_in_best_label(qtbot, make_ws):
    """A workspace-derived value containing markup must be HTML-escaped in the RichText verdict label
    (no injected bold segment / display corruption)."""
    ws = _ws_with(make_ws, _outcome(best_label="<b>evil</b>"))
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    text = w._verdict_label.text()
    assert "&lt;b&gt;evil&lt;/b&gt;" in text   # escaped, shown literally
    assert "<b>evil</b>" not in text           # NOT interpreted as markup


def test_widget_empty_state_shows_hint(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    assert vm.is_empty
    assert w._empty_label.isVisibleTo(w)


# ---- Integration: browser selection drives inspector ------------------------
def test_browser_selection_updates_inspector(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome())
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w.inspector_vm.is_empty
    fit = ws.scans[0].detectors["det"]
    with qtbot.waitSignal(w.inspector_vm.spec_changed, timeout=1000):
        w.browser_vm.select(fit)
    assert not w.inspector_vm.is_empty
    assert w.inspector_vm.state.main_spec is not None


# ---- Phase 4: raw-unit metadata + counts view (set_unit) --------------------
_UNIT_X = [-2.0, 0.0, 2.0, 4.0]
_UNIT_COUNTS = [0.0, 10.0, 40.0, 5.0]   # counts[0]=0 exercises the σ=0 whisker (D3)


def _unit_ws(make_ws, *, status="ingested", axis=None, with_file=True, prov_over=None) -> Workspace:
    """A workspace carrying one not-yet-fitted S30 unit (``s30:12:ana3``) with rich provenance and,
    unless ``with_file`` is False, its per-unit ``isar.unit_data/1`` file."""
    prov = {"source_file": r"C:\data\FeSeS_1.spec", "T": 300.0, "q": [1.5, 0.0, 0.0],
            "q_target": [1.5, 0.0, 0.0], "command": "scan deltaE -2 4 200",
            "counting_mode": "monitor", "preset": 200.0, "n_points": 4,
            "energy_status": (axis or {}).get("calibration_status", "absolute")}
    if prov_over:
        prov.update(prov_over)
    scans = {"s30:12:ana3": {"scan_id": "s30:12:ana3", "scan_number": 12,
                             "scan_type": "measurement", "status": status,
                             "detectors": {}, "provenance": prov}}
    parts = {"scans": scans}
    if with_file:
        parts["unit_files"] = [{"exp": "s30", "scan_number": 12, "det": "ana3",
                                "x": _UNIT_X, "counts": _UNIT_COUNTS, "axis": axis}]
    return Workspace.open(make_ws(**parts))


def test_set_unit_is_data_only_and_sigma_zero_stays_zero(make_ws):
    from isar.plotspec.spec import Role
    ws = _unit_ws(make_ws)
    vm = InspectorViewModel(ws)
    vm.set_unit("s30:12:ana3")
    st = vm.state
    assert not st.empty and st.kind == "unit"
    data = [s for s in st.main_spec.series if s.role is Role.DATA]
    assert len(data) == 1                                   # exactly one measured series
    assert all(s.role is not Role.FIT for s in st.main_spec.series)   # no fit curve
    assert not any(s.role is Role.COMPONENT for s in st.main_spec.series)
    assert st.residuals_spec is None                        # no residuals without a fit
    assert float(np.asarray(data[0].y)[0]) == 0.0
    assert float(np.asarray(data[0].yerr)[0]) == 0.0        # σ=0 for 0 counts (D3), not floored


@pytest.mark.parametrize("status,has_energy_transfer,needle", [
    ("absolute", True, "energy transfer"),
    ("needs_zero", False, "uncalibrated"),
    ("needs_conversion_and_zero", False, "uncalibrated"),
    (None, False, "uncalibrated"),
])
def test_unit_axis_label_is_three_state_honest(make_ws, status, has_energy_transfer, needle):
    """Hard rule 4: uncalibrated x is NEVER labeled 'energy transfer' — a non-absolute status says so
    out loud (the x values are the raw abscissa, never shifted to imply E0=0)."""
    axis = {"name": "deltaE", "kind": "energy", "units": "meV",
            "raw_abscissa_kind": "energy", "calibration_status": status}
    ws = _unit_ws(make_ws, axis=axis)
    vm = InspectorViewModel(ws)
    vm.set_unit("s30:12:ana3")
    xlabel = vm.state.main_spec.xlabel
    assert ("energy transfer" in xlabel) is has_energy_transfer
    assert needle in xlabel


def test_fitted_inspector_withholds_energy_fit_for_raw_unit_axis(make_ws, monkeypatch):
    """A fitted S30 unit must not fall back to a scan-number-only TAVI data file."""
    axis = {"name": "HerixE", "kind": "angle", "units": "deg",
            "raw_abscissa_kind": "angle", "calibration_status": "needs_conversion_and_zero"}
    outcome = _outcome(detector_id="ana3")
    scans = {"s30:12:ana3": {"scan_id": "s30:12:ana3", "scan_number": 12,
                              "scan_type": "measurement", "status": "fitted",
                              "detectors": {"ana3": outcome}, "provenance": {"M": 200}}}
    ws = Workspace.open(make_ws(
        scans=scans,
        unit_files=[{"exp": "s30", "scan_number": 12, "det": "ana3", "x": _UNIT_X,
                     "counts": _UNIT_COUNTS, "mask": [True, False, True, True], "axis": axis}],
    ))
    calls = []
    original_evaluate = ws.evaluate_model
    monkeypatch.setattr(ws, "evaluate_model",
                        lambda *args, **kwargs: (calls.append((args, kwargs)),
                                                 original_evaluate(*args, **kwargs))[1])
    vm = InspectorViewModel(ws)
    vm.set_fit(ws.scans[0].detectors["ana3"])
    st = vm.state
    assert st.main_spec.xlabel == "raw angle (deg) — uncalibrated"
    data = next(s for s in st.main_spec.series if s.role.value == "data")
    assert data.x.size == 3
    assert len(st.main_spec.markers) >= 1
    assert any(m.kind.value == "masked" for m in st.main_spec.markers)
    assert calls == []
    assert not any(s.role.value == "fit" for s in st.main_spec.series)
    assert st.residuals_spec is None
    assert "withheld" in st.curve_error
    assert any("calibration pending" in a.text for a in st.main_spec.annotations)


def test_fitted_inspector_keeps_raw_counts_when_model_evaluation_fails(make_ws, monkeypatch):
    """A broken/stale fit must not make usable measured counts disappear from the inspector."""
    from isar.plotspec.spec import Role

    ws = _ws_with(make_ws, _outcome())
    monkeypatch.setattr(ws, "evaluate_fit",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            ValueError("no stored amplitudes and no re-parseable context")))
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    st = vm.state

    assert st.main_spec is not None
    data = [s for s in st.main_spec.series if s.role is Role.DATA]
    assert len(data) == 1 and data[0].y.size == len(_COUNTS)
    assert not any(s.role is Role.FIT for s in st.main_spec.series)
    assert st.residuals_spec is None
    assert "model curve unavailable" in st.curve_error
    assert any("raw counts shown" in a.text for a in st.main_spec.annotations)


def test_unit_metadata_carries_command_qtarget_and_T(make_ws):
    ws = _unit_ws(make_ws)
    vm = InspectorViewModel(ws)
    vm.set_unit("s30:12:ana3")
    md = vm.state.metadata
    assert md["command"] == "scan deltaE -2 4 200"
    assert md["target #Q"] == "[1.5, 0, 0]"
    assert md["T"].startswith("300")
    assert md["analyzer"] == "ana3"


def test_unit_metadata_tolerant_of_pre_phase2_provenance(make_ws):
    # A pre-Phase-2 workspace has only the old ingest keys — no command/q_target/n_points. The panel
    # degrades to what's present (never a placeholder, never a crash), and the unit still plots.
    ws = _unit_ws(make_ws, prov_over={"command": None, "q_target": None, "counting_mode": None,
                                      "preset": None, "n_points": None, "energy_status": None})
    vm = InspectorViewModel(ws)
    vm.set_unit("s30:12:ana3")
    md = vm.state.metadata
    assert "command" not in md and "target #Q" not in md
    assert md["T"].startswith("300")                # the surviving old keys still show
    assert vm.state.main_spec is not None           # the counts file still drives the plot


def test_missing_unit_file_sets_curve_error_without_crashing(make_ws):
    ws = _unit_ws(make_ws, with_file=False)
    vm = InspectorViewModel(ws)
    vm.set_unit("s30:12:ana3")
    st = vm.state
    assert st.kind == "unit" and not st.empty
    assert st.main_spec is None and st.curve_error
    assert st.metadata                              # metadata still renders (short view)


def test_request_inspect_routes_fitview_or_unit_key():
    from types import SimpleNamespace
    from isar_gui.viewmodels.scans import ScansViewModel

    fit_obj = SimpleNamespace(detector_id="det")
    fitted = SimpleNamespace(scan_id="s30:1:det", experiment_id="s30", scan_number=1,
                             scan_type="measurement", status="fitted", source_file=None, T=300.0,
                             hkl=[1, 0, 0], provenance={}, detectors={"det": fit_obj})
    raw = SimpleNamespace(scan_id="s30:2:det", experiment_id="s30", scan_number=2,
                          scan_type="measurement", status="ingested", source_file=None, T=300.0,
                          hkl=[1, 0, 0], provenance={}, detectors={"det": SimpleNamespace()})
    vm = ScansViewModel(SimpleNamespace(scans=[fitted, raw]), store=None)
    got = []
    vm.inspect_requested.connect(got.append)
    vm.request_inspect("s30:1:det")
    vm.request_inspect("s30:2:det")
    vm.request_inspect("s30:9:det")                 # unknown -> None (empty state)
    assert got[0] is fit_obj                         # fitted -> FitView
    assert got[1] == "s30:2:det"                     # not fitted -> unit-key str
    assert got[2] is None


def test_main_window_routes_unit_key_to_set_unit(qtbot, make_ws):
    ws = _unit_ws(make_ws)
    w = MainWindow(ws, session=Session(), watch=False)
    qtbot.addWidget(w)
    w._route_inspect("s30:12:ana3")
    assert not w.inspector_vm.is_empty and w.inspector_vm.state.kind == "unit"
    w._route_inspect(None)                           # a non-str routes to set_fit (empty here)
    assert w.inspector_vm.is_empty


def test_widget_unit_mode_hides_fit_sections_shows_metadata(qtbot, make_ws):
    ws = _unit_ws(make_ws)
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_unit("s30:12:ana3")
    assert w._metadata_label.isVisibleTo(w)
    assert "command" in w._metadata_label.text()
    for hidden in (w._ladder, w._ladder_header, w._candidates, w._verdict_header,
                   w._provenance_header):
        assert not hidden.isVisibleTo(w)


def test_widget_unit_then_fit_restores_fit_sections(qtbot, make_ws):
    # Switching from a unit view back to a fit view re-shows the ladder/verdict (no orphan hiding).
    ws = _unit_ws(make_ws)
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_unit("s30:12:ana3")
    fit_ws = _ws_with(make_ws, _outcome())
    vm.set_fit(_fit(fit_ws))
    assert w._ladder.isVisibleTo(w) and w._verdict_header.isVisibleTo(w)
    assert not w._metadata_label.isVisibleTo(w)


# ---- MVP-16 smoke (read-only) ------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen baseline not present")
def test_mvp16_inspector_smoke():
    ws = Workspace.open(_MVP16)
    fit = next((f for f in ws.results if f.status == "fitted" and f.omega0 is not None), None)
    if fit is None:
        pytest.skip("no fitted result in baseline")
    vm = InspectorViewModel(ws)
    vm.set_fit(fit)  # exercises evaluate_model fallback (no stored amplitudes)
    st = vm.state
    assert not st.empty
    assert len(st.ladder_rows) == 4

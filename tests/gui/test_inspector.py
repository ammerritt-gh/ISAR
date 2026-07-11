"""C2 fit inspector tests: InspectorViewModel derives the interval ladder (with the correct
certifying tier), AIC candidate rows, verdict/provenance; InspectorWidget renders each case
offscreen; a browser selection drives the inspector. Offscreen (conftest forces QT offscreen),
importorskip, xdist-safe (each test stamps its own workspace)."""
from __future__ import annotations

from pathlib import Path

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
    {"label": "phonon", "delta_aic": 0.0, "akaike_weight": 0.82, "n_modes": 1},
    {"label": "phonon+qe", "delta_aic": 3.4, "akaike_weight": 0.18, "n_modes": 1},
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


def test_ladder_unreliable_bootstrap_defers_to_profile(make_ws):
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


def test_candidates_winner_is_delta_aic_zero(make_ws):
    ws = _ws_with(make_ws, _outcome())
    vm = InspectorViewModel(ws)
    vm.set_fit(_fit(ws))
    winners = [r for r in vm.state.candidate_rows if r.winner]
    assert len(winners) == 1 and winners[0].label == "phonon"


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
    assert vm.state.provenance["pooled"] == "pooled — uncertifiable"


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
    win_row = next(i for i in range(w._candidates.rowCount())
                   if w._candidates.item(i, 0).text() == "phonon")
    assert w._candidates.item(win_row, 0).font().bold()


def test_widget_candidates_not_recorded_placeholder(qtbot, make_ws):
    ws = _ws_with(make_ws, _outcome(candidates=None))
    vm = InspectorViewModel(ws)
    w = InspectorWidget(vm)
    qtbot.addWidget(w)
    vm.set_fit(_fit(ws))
    assert w._candidates.item(0, 0).text() == "not recorded"


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

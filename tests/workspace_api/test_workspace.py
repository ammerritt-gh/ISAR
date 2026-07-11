"""Behavioural tests for the isar.workspace_api read facade (synthetic workspaces)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar.workspace_api.evaluate import evaluate_model
from isar.workspace_api.views import FitView

from .conftest import OLD_COLUMNS, detector_outcome, scan_entry


# ---------------------------------------------------------------------------
# open + surfaces present
# ---------------------------------------------------------------------------
def test_open_missing_policy_raises(tmp_path):
    with pytest.raises(WorkspaceNotFound):
        Workspace.open(tmp_path)


def test_open_and_surfaces_present(make_ws):
    root = make_ws(
        rows=[{"scan": 7, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0,
               "l": 0.0, "status": "fitted", "verdict": "resolved", "omega0": 5.1, "gamma": 0.3}],
        scans={"tavi:7:det": scan_entry(7, detectors={"det": detector_outcome(omega0=5.1, gamma=0.3)})},
        state={"dispersion_prior": [{"q_value": 0.5, "omega0": 5.1}], "branch_models": [{"id": "b0"}],
               "branch_belief": [{"entropy": 0.1}], "lattice_epochs": []},
    )
    ws = Workspace.open(root)
    assert ws.schema_version == 1
    assert len(ws.results) == 1
    assert len(ws.scans) == 1
    assert ws.card.collation["key"] == ["sample", "q", "T"]
    assert ws.branch_model.dispersion_prior and ws.branch_model.branch_models
    assert ws.snapshot is not None
    assert ws.warnings == []


# ---------------------------------------------------------------------------
# results: field exposure, ladder tuples, None-degradation
# ---------------------------------------------------------------------------
def test_results_full_field_exposure(make_ws):
    root = make_ws(rows=[{
        "scan": 7, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0,
        "status": "fitted", "verdict": "resolved", "disposition": "resolved", "confidence": 0.82,
        "omega0": 5.1, "omega0_err": 0.12, "interval_lo": 4.9, "interval_hi": 5.3,
        "interval_reliable": "True", "omega0_profile_lo": 4.85, "omega0_profile_hi": 5.35,
        "hesse_lo": 4.95, "hesse_hi": 5.25, "hesse_trusted": "True",
        "bayes_lo": 4.9, "bayes_med": 5.1, "bayes_hi": 5.3, "bayes_ess": 900.0, "log_evidence": -12.3,
        "pooled": "False", "gamma": 0.3, "best_model": "phonon+elastic", "modality": None,
        "amplitudes": json.dumps({"phonon.A": 100.0}), "candidates": json.dumps([{"label": "x"}]),
    }])
    fv = Workspace.open(root).results[0]
    assert fv.scan_number == 7 and fv.detector_id == "det" and fv.sample == "tavi"
    assert fv.omega0 == pytest.approx(5.1) and fv.omega0_err == pytest.approx(0.12)
    assert fv.interval == (4.9, 5.3)
    assert fv.omega0_profile == (4.85, 5.35)
    assert fv.hesse_interval == (4.95, 5.25) and fv.hesse_trusted is True
    assert fv.bayes_interval == (4.9, 5.1, 5.3)
    assert fv.bayes_ess == pytest.approx(900.0) and fv.log_evidence == pytest.approx(-12.3)
    assert fv.interval_reliable is True
    assert fv.best_label == "phonon+elastic"
    assert fv.amplitudes == {"phonon.A": 100.0}
    assert fv.candidates == [{"label": "x"}]
    assert fv.q == [2.0, 0.0, 0.0]


def test_results_none_degradation_on_old_format(make_ws):
    # Old header: no bayes / members / amplitudes / candidates columns at all.
    root = make_ws(
        columns=OLD_COLUMNS,
        rows=[{"scan": 7, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0,
               "l": 0.0, "status": "fitted", "verdict": "resolved", "omega0": 5.1,
               "interval_lo": 4.9, "interval_hi": 5.3, "interval_reliable": "False", "gamma": 0.3}],
    )
    fv = Workspace.open(root).results[0]
    assert fv.interval == (4.9, 5.3)
    assert fv.interval_reliable is False
    # Columns absent entirely -> None, never KeyError.
    assert fv.bayes_interval is None
    assert fv.hesse_interval is None
    assert fv.bayes_ess is None
    assert fv.log_evidence is None
    assert fv.amplitudes is None
    assert fv.candidates is None
    assert fv.members is None
    assert fv.modality is None


def test_results_enriched_from_session_detector(make_ws):
    # calib lives only in the ledger; the results row has no calib column -> enrichment supplies it.
    root = make_ws(
        rows=[{"scan": 7, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0,
               "l": 0.0, "status": "fitted", "verdict": "resolved", "omega0": 5.1, "gamma": 0.3}],
        scans={"tavi:7:det": scan_entry(7, detectors={"det": detector_outcome(
            omega0=5.1, gamma=0.3, calib={"center": 0.0, "gauss_fwhm": 1.4, "lorentz_fwhm": 0.1},
            modality="single")})},
    )
    fv = Workspace.open(root).results[0]
    assert fv.calib == {"center": 0.0, "gauss_fwhm": 1.4, "lorentz_fwhm": 0.1}
    assert fv.modality == "single"          # ledger-only field surfaced via enrichment
    rc = fv.resolution_context()
    assert rc["center"] == 0.0 and rc["gauss_fwhm"] == pytest.approx(1.4)


# ---------------------------------------------------------------------------
# groups: same membership as the collation key
# ---------------------------------------------------------------------------
def test_groups_two_group_membership(make_ws):
    rows = [
        {"scan": 1, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 5.0, "gamma": 0.3},
        {"scan": 2, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.02, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 5.1, "gamma": 0.3},   # same q-bin as scan 1 (step 0.05)
        {"scan": 3, "detector": "det", "sample": "tavi", "T": 200.0, "h": 3.0, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 6.0, "gamma": 0.3},   # different q-bin -> 2nd group
        {"scan": 9000001, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0,
         "l": 0.0, "status": "fitted", "omega0": 5.05, "gamma": 0.3, "pooled": "True"},  # master: excluded
    ]
    ws = Workspace.open(make_ws(rows=rows))
    groups = ws.groups
    assert len(groups) == 2
    sizes = sorted(len(g.members) for g in groups)
    assert sizes == [1, 2]                              # scans {1,2} together, {3} alone
    # membership matches the canonical q-key (H≈2.0 vs H=3.0 on step 0.05)
    big = max(groups, key=lambda g: len(g.members))
    assert {m.scan_number for m in big.members} == {1, 2}


def test_groups_reuse_canonical_key_function():
    # Guard: the facade builds groups from isar.reduce.collate's key helpers (no re-definition).
    import inspect
    from isar.workspace_api import workspace as ws_mod
    src = inspect.getsource(ws_mod._build_groups if hasattr(ws_mod, "_build_groups")
                            else ws_mod.Workspace._build_groups)
    assert "isar.reduce.collate" in src and "_group_component" in src


def test_groups_membership_equals_real_collate(make_ws):
    """Behavioural upgrade of the source-string guard above: the facade's group partition must equal
    what the REAL ``isar.reduce.collate.collate()`` produces on records with the same (sample, q, T),
    under the same card policy — not merely reuse the key helpers, but agree on the outcome."""
    from isar.reduce.collate import collate

    rows = [
        {"scan": 1, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 5.0, "gamma": 0.3},
        {"scan": 2, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.02, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 5.1, "gamma": 0.3},   # same q-bin as scan 1 (step 0.05)
        {"scan": 3, "detector": "det", "sample": "tavi", "T": 200.0, "h": 3.0, "k": 0.0, "l": 0.0,
         "status": "fitted", "omega0": 6.0, "gamma": 0.3},   # different q-bin -> 2nd group
        {"scan": 9000001, "detector": "det", "sample": "tavi", "T": 200.0, "h": 2.0, "k": 0.0,
         "l": 0.0, "status": "fitted", "omega0": 5.05, "gamma": 0.3, "pooled": "True"},  # master
    ]
    ws = Workspace.open(make_ws(rows=rows))
    facade_partition = frozenset(frozenset(m.scan_number for m in g.members) for g in ws.groups)

    # Build the reducer's record shape for the same MEASUREMENT scans (the pooled master is a fit OF a
    # group, not a member — collate groups measurement spectra, exactly what the facade excludes).
    def _rec(scan, h):
        sp = SimpleNamespace(q=[h, 0.0, 0.0], point_flags=[], spectrum_flags=[],
                             miniscan_reject=None, miniscan_counts=None)
        return SimpleNamespace(experiment_id="tavi", scan_number=scan, scan_type="measurement",
                               scan_axes=[], spectra={"det": sp},
                               temperature=SimpleNamespace(setpoint=[200.0]),
                               normalization=None, scan_flags=[])

    records = [_rec(1, 2.0), _rec(2, 2.02), _rec(3, 3.0)]
    collation = collate(records, SimpleNamespace(policy=ws.card.policy))
    collate_partition = frozenset(
        frozenset(m.scan_number for m in g.members) for g in collation.groups)

    assert facade_partition == collate_partition == frozenset({frozenset({1, 2}), frozenset({3})})


# ---------------------------------------------------------------------------
# session_events: kind / since filter, truncation tolerance
# ---------------------------------------------------------------------------
def test_session_events_filters_and_truncation(make_ws):
    good = [
        json.dumps({"ts": 1.0, "kind": "propose", "payload": {"a": 1}}),
        json.dumps({"ts": 2.0, "kind": "run", "payload": {"b": 2}}),
        json.dumps({"ts": 3.0, "kind": "propose", "payload": {"c": 3}}),
    ]
    truncated = '{"ts": 4.0, "kind": "run", "payl'   # cut off mid-write
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted"}],
                   log_lines=good + [truncated])
    ws = Workspace.open(root)
    proposes = list(ws.session_events(kind="propose"))
    assert [e["ts"] for e in proposes] == [1.0, 3.0]
    since = list(ws.session_events(since=2.0))
    assert [e["ts"] for e in since] == [2.0, 3.0]
    # the truncated final line is dropped, the rest parse, and a warning is recorded
    assert len(list(ws.session_events())) == 3
    assert any("truncated" in w for w in ws.warnings)


# ---------------------------------------------------------------------------
# partial-write tolerance
# ---------------------------------------------------------------------------
def test_corrupt_card_state_degrades_with_warning(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted"}])
    (root / "card.state.json").write_text("{ this is not valid json ", encoding="utf-8")
    ws = Workspace.open(root)
    # card.state failing must not crash; branch model degrades to empty + a warning is recorded.
    assert ws.branch_model.dispersion_prior == []
    assert ws.card.state is not None
    assert any("state" in w for w in ws.warnings)
    # other surfaces still work
    assert len(ws.results) == 1


# ---------------------------------------------------------------------------
# evaluate_model: pure vs fallback equivalence
# ---------------------------------------------------------------------------
def _truth_curve(energy, *, omega0, gamma, amps, ctx):
    from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
    from isar.report.audit import assemble_curve
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    return assemble_curve(comp, energy, omega0=omega0, gamma=gamma, amp=amps, ctx=ctx)


def test_evaluate_pure_vs_fallback_equivalence(make_ws):
    energy = np.linspace(-2.0, 7.0, 60)
    ctx = SimpleNamespace(center=0.0, gauss_fwhm=1.2, lorentz_fwhm=0.0, T=200.0)
    amps = {"phonon.A": 5000.0, "elastic.E": 8000.0, "bg.BG": 40.0}
    truth = _truth_curve(energy, omega0=4.0, gamma=0.4, amps=amps, ctx=ctx)
    counts = np.asarray(truth.total, dtype=float)   # noise-free counts from the same model

    root = make_ws(data_files=[{"scan_number": 11, "energy": energy, "counts": counts}])
    data_dir = root / "data"

    res_row = {"scan": 11, "detector": "det", "omega0": 4.0, "gamma": 0.4,
               "zero": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0, "T": 200.0}
    fit_pure = FitView.from_row({**res_row, "amplitudes": amps})
    fit_fallback = FitView.from_row(res_row)   # no amplitudes -> forces the counts/NNLS path

    out_pure = evaluate_model(fit_pure, data_dir)
    out_fb = evaluate_model(fit_fallback, data_dir)

    assert out_pure.pure is True
    assert out_fb.pure is False
    # Counts equal the model's own output, so NNLS (fallback) recovers the stored amplitudes to
    # floating-point precision; 1e-6 relative is comfortably above solver round-off.
    assert np.allclose(out_pure.total, out_fb.total, rtol=1e-6, atol=1e-6)
    assert np.all(np.isfinite(out_pure.total)) and np.all(np.isfinite(out_fb.total))


def test_evaluate_pure_quasielastic_branch(make_ws):
    # QE composition path: amplitudes include a "qe.*" prefix and quasielastic_fwhm is supplied,
    # so _compose builds phonon+elastic+qe+bg and assemble_curve must include the QE component.
    energy = np.linspace(-2.0, 7.0, 40)
    root = make_ws(data_files=[{"scan_number": 21, "energy": energy, "counts": np.zeros_like(energy)}])
    data_dir = root / "data"

    amps = {"phonon.A": 5000.0, "elastic.E": 8000.0, "qe.QE": 300.0, "bg.BG": 40.0}
    res_row = {"scan": 21, "detector": "det", "omega0": 4.0, "gamma": 0.4,
               "zero": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0, "T": 200.0}
    fit_pure = FitView.from_row({**res_row, "amplitudes": amps})

    out = evaluate_model(fit_pure, data_dir, quasielastic_fwhm=2.5)

    assert out.pure is True
    names = [name for name, _ in out.components]
    assert "qe" in names
    assert np.all(np.isfinite(out.total))
    qe_values = dict(out.components)["qe"]
    assert np.any(qe_values != 0.0)


def test_evaluate_via_workspace_fallback(make_ws):
    energy = np.linspace(-2.0, 7.0, 50)
    ctx = SimpleNamespace(center=0.0, gauss_fwhm=1.2, lorentz_fwhm=0.0, T=200.0)
    amps = {"phonon.A": 4000.0, "elastic.E": 6000.0, "bg.BG": 30.0}
    counts = np.asarray(_truth_curve(energy, omega0=3.5, gamma=0.3, amps=amps, ctx=ctx).total)
    root = make_ws(
        rows=[{"scan": 5, "detector": "det", "sample": "tavi", "status": "fitted",
               "verdict": "resolved", "omega0": 3.5, "gamma": 0.3, "zero": 0.0,
               "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0, "T": 200.0}],
        data_files=[{"scan_number": 5, "energy": energy, "counts": counts}],
    )
    ws = Workspace.open(root)
    out = ws.evaluate_model("tavi:5:det", "det")
    assert out.pure is False
    assert np.all(np.isfinite(out.total))
    assert len(out.components) == 3


# ---------------------------------------------------------------------------
# hkl resolution
# ---------------------------------------------------------------------------
def test_hkl_passthrough_without_epochs(make_ws):
    root = make_ws(
        rows=[{"scan": 7, "detector": "det", "status": "fitted", "omega0": 5.0, "gamma": 0.3}],
        scans={"tavi:7:det": scan_entry(7, detectors={"det": detector_outcome(
            omega0=5.0, gamma=0.3, q=[2.0, 0.0, 0.0])})},
    )
    sv = Workspace.open(root).scans[0]
    assert sv.hkl == [2.0, 0.0, 0.0]        # acquisition-time HKL passthrough


def test_hkl_epoch_resolved(make_ws):
    epoch = {"version": 1, "valid_from": {"scan_ref": None, "ts": None},
             "lattice": {"a": 4.0, "b": 4.0, "c": 4.0, "alpha": 90.0, "beta": 90.0, "gamma": 90.0}}
    # instrument Q along a* for a cubic 4 A lattice: |q| = 2*pi/a * H  -> H = q*a/(2*pi)
    a = 4.0
    H = 2.0
    qx = 2.0 * np.pi / a * H
    root = make_ws(
        state={"lattice_epochs": [epoch]},
        rows=[{"scan": 7, "detector": "det", "status": "fitted", "omega0": 5.0, "gamma": 0.3}],
        scans={"tavi:7:det": scan_entry(7, provenance={"q_instrument": [qx, 0.0, 0.0]},
                                        detectors={"det": detector_outcome(
                                            omega0=5.0, gamma=0.3, q=[99.0, 99.0, 99.0])})},
    )
    sv = Workspace.open(root).scans[0]
    # epoch-resolved HKL comes from q_to_hkl (NOT the bogus stored q=[99,99,99])
    assert sv.hkl is not None
    assert sv.hkl[0] == pytest.approx(H, abs=1e-6)
    assert sv.hkl[1] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# campaign + refresh
# ---------------------------------------------------------------------------
def test_campaign_view_and_absence(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted"}],
                   campaign={"objective": {"name": "obj", "tolerance_meV": 0.5},
                             "budget": {"kind": "counting_seconds", "amount": 3600},
                             "stop": {"reason": "budget"}})
    ws = Workspace.open(root)
    assert ws.campaign.objective["name"] == "obj"
    assert ws.campaign.budget["amount"] == 3600
    assert ws.campaign.stop["reason"] == "budget"

    root2 = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted"}])
    assert Workspace.open(root2).campaign is None


def test_refresh_repicks_up_changes(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    ws = Workspace.open(root)
    assert len(ws.results) == 1
    # append a row on disk; cached view unchanged until refresh()
    from .conftest import write_results, FULL_COLUMNS
    write_results(root, [{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0},
                         {"scan": 2, "detector": "det", "status": "fitted", "omega0": 6.0}], FULL_COLUMNS)
    assert len(ws.results) == 1
    ws.refresh()
    assert len(ws.results) == 2

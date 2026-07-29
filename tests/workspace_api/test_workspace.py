"""Behavioural tests for the isar.workspace_api read facade (synthetic workspaces)."""
from __future__ import annotations

import json
import csv
import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.workspace_api import Workspace, WorkspaceNotFound
from isar.workspace_api.evaluate import (UnsupportedFitView, _compose_manifest,
                                         _load_scan_arrays, evaluate_model)
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
               "branch_belief": [{"entropy": 0.1}],
               "branch_tracks": {"revision": 2, "tracks": []}, "lattice_epochs": []},
    )
    ws = Workspace.open(root)
    assert ws.schema_version == 1
    assert len(ws.results) == 1
    assert len(ws.scans) == 1
    assert ws.card.collation["key"] == ["sample", "q", "T"]
    assert ws.branch_model.dispersion_prior and ws.branch_model.branch_models
    assert ws.branch_model.branch_tracks["revision"] == 2
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


def test_master_groups_and_pooled_spectrum_round_trip(make_ws):
    physical = detector_outcome(omega0=5.0, gamma=0.3, q=[2.0, 0.0, 0.0])
    root = make_ws(
        rows=[
            {"scan_id": "tavi:7:det", "scan": 7, "detector": "det", "status": "fitted",
             "h": 2.0, "k": 0.0, "l": 0.0, "omega0": 5.0},
            {"scan_id": "master:q2", "scan": 9000001, "detector": "det", "status": "fitted",
             "h": 2.0, "k": 0.0, "l": 0.0, "T": 200.0, "omega0": 5.1,
             "pooled": "True", "n_members": 1, "members": "7",
             "spectrum_ref": "data/pooled/abc.json"},
        ],
        scans={"tavi:7:det": scan_entry(7, detectors={"det": physical})})
    pooled = root / "data" / "pooled" / "abc.json"
    pooled.parent.mkdir(parents=True)
    pooled.write_text(json.dumps({
        "schema": "isar.pooled_spectrum/1", "energy": [-1.0, 0.0, 1.0],
        "counts": [0.0, 4.0, 9.0], "variance": [0.0, 4.0, 9.0],
        "monitor": [10.0, 10.0, 10.0], "mask": [True, False, True],
        "members": [["tavi", 7, "det"]], "n_members": 1,
    }), encoding="utf-8")

    ws = Workspace.open(root)
    assert len(ws.master_groups) == 1
    assert ws.master_groups[0].master.unit_id == "master:q2"
    assert [member.unit_id for member in ws.master_groups[0].members] == ["tavi:7:det"]
    spectrum = ws.pooled_spectrum(ws.master_groups[0].master)
    assert spectrum.counts.tolist() == [0.0, 4.0, 9.0]
    assert spectrum.yerr.tolist() == [0.0, 2.0, 3.0]  # no zero-count floor
    assert spectrum.mask.tolist() == [True, False, True]


def test_master_group_marks_unpublished_acquisition_stale(make_ws):
    lines = [
        json.dumps({"ts": 1.0, "kind": "physical_submission", "payload": {
            "job_id": "pending", "hkl": [2.000000001, 0.0, 0.0]}}),
        json.dumps({"ts": 2.0, "kind": "physical_submission", "payload": {
            "job_id": "published", "hkl": [2.0, 0.0, 0.0]}}),
        json.dumps({"ts": 3.0, "kind": "analyze", "payload": {"job_id": "published"}}),
        json.dumps({"ts": 4.0, "kind": "physical_submission", "payload": {
            "job_id": "failed", "hkl": [2.0, 0.0, 0.0]}}),
        json.dumps({"ts": 5.0, "kind": "exception", "payload": {
            "action": "scan_not_done", "job_id": "failed"}}),
    ]
    root = make_ws(rows=[{
        "scan_id": "master:q2", "scan": 9000001, "detector": "det", "status": "fitted",
        "h": 2.0, "k": 0.0, "l": 0.0, "pooled": "True", "n_members": 0,
    }], log_lines=lines)

    group = Workspace.open(root).master_groups[0]

    assert group.pending_acquisitions == ("pending",)
    assert group.stale_count == 1


def test_pooled_spectrum_rejects_workspace_escape(make_ws):
    ws = Workspace.open(make_ws())
    with pytest.raises(ValueError, match="escapes"):
        ws.pooled_spectrum("../outside.json")


def test_pooled_spectrum_rejects_misaligned_artifact(make_ws):
    root = make_ws()
    pooled = root / "data" / "pooled" / "bad.json"
    pooled.parent.mkdir(parents=True)
    pooled.write_text(json.dumps({
        "schema": "isar.pooled_spectrum/1",
        "energy": [0.0, 1.0],
        "counts": [1.0],
        "variance": [1.0],
        "monitor": [1.0],
        "mask": [True],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="not aligned"):
        Workspace.open(root).pooled_spectrum("data/pooled/bad.json")


def test_master_predictive_accepts_no_phonon_model(make_ws):
    root = make_ws()
    artifact = root / "data" / "master_predictive" / "null-model.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "schema": "isar.master_predictive/2",
        "models": [{"weight": 1.0, "parameter_names": ["a"],
                    "parameter_draws": [[1.0]], "omega0_draws": [],
                    "expected_counts": [[1.0]]}],
    }), encoding="utf-8")
    payload = Workspace.open(root).master_predictive(
        "data/master_predictive/null-model.json")
    assert payload["models"][0]["omega0_draws"] == []


def test_master_predictive_refuses_the_pre_v2_artifact(make_ws):
    # Clean break (ADR-006/013): a leftover /1 artifact is not upconverted, and refusing it must not
    # keep the workspace's scans and fits from opening.
    root = make_ws()
    artifact = root / "data" / "master_predictive" / "legacy.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "schema": "isar.master_predictive/1",
        "models": [{"weight": 1.0, "parameter_names": ["a"],
                    "parameter_draws": [[1.0]], "omega0_draws": [],
                    "expected_counts": [[1.0]]}],
    }), encoding="utf-8")
    workspace = Workspace.open(root)
    with pytest.raises(ValueError, match="unsupported master predictive schema"):
        workspace.master_predictive("data/master_predictive/legacy.json")
    assert workspace.scans is not None


def test_master_predictive_rejects_malformed_model(make_ws):
    root = make_ws()
    artifact = root / "data" / "master_predictive" / "bad.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({
        "schema": "isar.master_predictive/2",
        "models": [{"weight": 1.0, "parameter_names": ["a"],
                    "parameter_draws": [[1.0]], "omega0_draws": [],
                    "expected_counts": [[1.0], [2.0]]}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid master predictive model"):
        Workspace.open(root).master_predictive("data/master_predictive/bad.json")


def test_master_groups_records_invalid_spectrum_metadata_warning(make_ws):
    root = make_ws(rows=[{
        "scan_id": "master:q2", "scan": 9000001, "detector": "det", "status": "fitted",
        "h": 2.0, "k": 0.0, "l": 0.0, "pooled": "True", "n_members": 1,
        "spectrum_ref": "../outside.json",
    }])
    ws = Workspace.open(root)
    assert len(ws.master_groups) == 1
    assert any("pooled spectrum metadata unavailable" in warning for warning in ws.warnings)


def test_additive_branch_tracking_fields_decode_from_flat_row():
    fv = FitView.from_row({
        "scan": 7, "detector": "det",
        "branch_candidates": json.dumps([{"energy_meV": 5.0}, {"energy_meV": 7.8}]),
        "selected_candidate_index": "1", "track_state": "confirmed", "track_revision": "4",
    })
    assert fv.branch_candidates == [{"energy_meV": 5.0}, {"energy_meV": 7.8}]
    assert fv.selected_candidate_index == 1
    assert fv.track_state == "confirmed"
    assert fv.track_revision == 4


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


def test_results_scan_id_keeps_same_scan_detector_from_different_experiments_distinct(make_ws):
    """The additive CSV identity prevents a wrong cross-experiment outcome enrichment."""
    first = detector_outcome(omega0=3.0, gamma=0.3, sample="first")
    second = detector_outcome(omega0=7.0, gamma=0.4, sample="second")
    scans = {
        "first:9:det": {**scan_entry(9, detectors={"det": first}), "scan_id": "first:9:det"},
        "second:9:det": {**scan_entry(9, detectors={"det": second}), "scan_id": "second:9:det"},
    }
    root = make_ws(
        rows=[
            {"scan_id": "first:9:det", "scan": 9, "detector": "det", "status": "fitted"},
            {"scan_id": "second:9:det", "scan": 9, "detector": "det", "status": "fitted"},
        ], scans=scans)
    results = Workspace.open(root).results
    assert [(f.unit_id, f.omega0, f.sample) for f in results] == [
        ("first:9:det", 3.0, "first"), ("second:9:det", 7.0, "second")]


def test_stale_atomic_result_id_never_enriches_from_same_scan_detector(make_ws):
    """A full but stale identity is unresolved; the legacy pair fallback is not permitted."""
    actual = detector_outcome(omega0=7.0, gamma=0.4, sample="actual")
    scans = {"actual:9:det": {**scan_entry(9, detectors={"det": actual}),
                                "scan_id": "actual:9:det"}}
    root = make_ws(rows=[{"scan_id": "stale:9:det", "scan": 9, "detector": "det",
                          "status": "fitted"}], scans=scans)
    ws = Workspace.open(root)
    fit = ws.results[0]
    assert fit.unit_id == "stale:9:det"
    assert fit.omega0 is None and fit.sample is None
    assert any("stale:9:det" in warning and "unresolved" in warning for warning in ws.warnings)


def test_atomic_evaluation_lookup_never_falls_back_to_another_experiment(make_ws):
    actual = detector_outcome(omega0=7.0, gamma=0.4, sample="actual",
                              amplitudes={"phonon.A": 1.0, "bg.BG": 1.0})
    scans = {"actual:9:det": {**scan_entry(9, detectors={"det": actual}),
                                "scan_id": "actual:9:det"}}
    ws = Workspace.open(make_ws(scans=scans))
    with pytest.raises(KeyError, match="stale:9:det"):
        ws.evaluate_model("stale:9:det", "det")
    assert any("atomic scan_id 'stale:9:det'" in warning for warning in ws.warnings)


def test_results_writer_keeps_identity_for_no_detector_rows(tmp_path):
    """Held/non-result rows retain their additive identity even when no detector fit exists."""
    from isar.session.driver import _write_results_csv
    from isar.session.state import ScanEntry, SessionState
    from isar.session.workspace import Workspace as SessionWorkspace

    session = SessionState(scans={
        "first:9:det": ScanEntry(scan_id="first:9:det", scan_number=9,
                                   scan_type="measurement", status="held_for_calibration"),
        "second:9:det": ScanEntry(scan_id="second:9:det", scan_number=9,
                                    scan_type="measurement", status="non_result"),
    })
    path = _write_results_csv(SessionWorkspace.open(tmp_path), session)
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["scan_id"] for row in rows} == {"first:9:det", "second:9:det"}


def test_last_ingest_view_is_immutable_and_tolerates_old_workspaces(make_ws):
    root = make_ws()
    session_path = root / "session.json"
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    payload["last_ingest"] = {
        "completed_at": "2026-07-13T12:00:00+00:00", "logical_scan_count": "2",
        "analyzer_unit_count": 18, "failed_file_count": 1,
        "gaps": [["SYN", 4, 7], ["bad"], ["SYN", "not-a-number", 8]],
    }
    session_path.write_text(json.dumps(payload), encoding="utf-8")

    view = Workspace.open(root).last_ingest
    assert view.logical_scan_count == 2
    assert view.analyzer_unit_count == 18
    assert view.gaps == (("SYN", 4, 7),)
    with pytest.raises(Exception):
        view.failed_file_count = 0

    old_root = make_ws()
    assert Workspace.open(old_root).last_ingest is None


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


def test_master_assessments_use_latest_durable_model_snapshot(make_ws):
    lines = [
        json.dumps({"ts": 1.0, "kind": "decide", "payload": {
            "event": "model_update", "per_q": [{"hkl": [2.1, 0, 0],
            "assessment_state": "unmeasured", "converged": False}]}}),
        json.dumps({"ts": 2.0, "kind": "decide", "payload": {
            "event": "model_update", "per_q": [{"hkl": [2.1, 0, 0],
            "q": 0.0, "tolerance": 0.2, "assessment_state": "certified",
            "converged": True, "omega0": 5.0, "calibrated_halfwidth": 0.1,
            "master_revision": "rev-2", "master_n_members": 7,
            "diagnostics": ["linewidth_resolution_limited"]}]}}),
    ]
    assessment = Workspace.open(make_ws(log_lines=lines)).master_assessments[0]
    assert assessment.state == "certified" and assessment.converged
    assert assessment.master_revision == "rev-2" and assessment.n_members == 7
    assert assessment.halfwidth == pytest.approx(0.1)


def test_certification_status_validates_publication_hashes(make_ws):
    root = make_ws(rows=[])
    revision = root / "report" / "certification" / ("a" * 64)
    revision.mkdir(parents=True)
    progress = {
        "schema": "isar.certification.progress/1", "fingerprint": "a" * 64,
        "completed": 21, "total": 21, "phase": "publishing", "published": False,
        "updated_ts": 12.0,
    }
    (revision / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
    paths = {
        "session.json": root / "session.json",
        "card.state.json": root / "card.state.json",
        "results.csv": root / "report" / "results.csv",
        "card.policy.toml": root / "card.policy.toml",
    }
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
              for name, path in paths.items()}
    current = root / "report" / "certification" / "current.json"
    current.write_text(json.dumps({
        "schema": "isar.certification.current/1", "fingerprint": "a" * 64,
        "published_hashes": hashes,
    }), encoding="utf-8")
    assert Workspace.open(root).certification_status.published is True

    (root / "session.json").write_text("{}", encoding="utf-8")
    status = Workspace.open(root).certification_status
    assert status.published is False and status.phase == "publishing"


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


@pytest.mark.parametrize("manifest, message", [
    ({"schema": "isar.model_manifest/1", "components": ["phonon"], "parameters": {}},
     "must be an object"),
    ({"schema": "isar.model_manifest/1",
      "components": [{"type": "phonon_dho"}], "parameters": {}},
     "valid prefix"),
    ({"schema": "isar.model_manifest/1",
      "components": [{"type": "constant_background", "prefix": "bg"}],
      "parameters": {"bg.BG": "not-a-number"}},
     "must be numeric"),
])
def test_manifest_reconstruction_reports_malformed_artifacts(manifest, message):
    with pytest.raises(UnsupportedFitView, match=message):
        _compose_manifest(manifest, None)


# ---------------------------------------------------------------------------
# _load_scan_arrays: mask padding + malformed-data tolerance
# ---------------------------------------------------------------------------
def _write_raw_data(data_dir: Path, scan_number: int, doc) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"j-{int(scan_number):04d}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_load_scan_arrays_short_mask_excludes_uncovered_tail(make_ws):
    # A valid_mask covering only the first half of the energy points must EXCLUDE the uncovered
    # tail, not treat it as valid (the omitted positions are points the fit did not keep).
    energy = list(np.linspace(-2.0, 7.0, 10))
    counts = [100.0] * 10
    root = make_ws()
    _write_raw_data(root / "data", 30, {"job_id": "j-0030", "state": "done",
                    "result": {"scan_values_1": energy, "counts": counts,
                               "valid_mask_1": [True] * 5}})   # mask only covers first 5 points
    e, c = _load_scan_arrays(root / "data", 30)
    assert e is not None and e.size == 5                     # uncovered tail dropped
    assert np.allclose(e, np.asarray(energy)[:5])


def test_load_scan_arrays_rejects_list_root(make_ws):
    root = make_ws()
    _write_raw_data(root / "data", 31, [1, 2, 3])            # non-object JSON root
    assert _load_scan_arrays(root / "data", 31) == (None, None)


def test_load_scan_arrays_rejects_non_numeric_counts(make_ws):
    root = make_ws()
    _write_raw_data(root / "data", 32, {"result": {"scan_values_1": [0.0, 1.0, 2.0],
                                                    "counts": ["a", "b", "c"]}})
    assert _load_scan_arrays(root / "data", 32) == (None, None)


def test_load_scan_arrays_rejects_ragged_counts(make_ws):
    root = make_ws()
    _write_raw_data(root / "data", 33, {"result": {"scan_values_1": [0.0, 1.0],
                                                    "counts": [[1, 2], [3]]}})   # ragged/nested
    assert _load_scan_arrays(root / "data", 33) == (None, None)


def test_load_scan_arrays_rejects_2d_arrays(make_ws):
    root = make_ws()
    _write_raw_data(root / "data", 34, {"result": {"scan_values_1": [[0.0, 1.0], [2.0, 3.0]],
                                                    "counts": [[1.0, 2.0], [3.0, 4.0]]}})
    assert _load_scan_arrays(root / "data", 34) == (None, None)


# ---------------------------------------------------------------------------
# evaluate_model: a stored quasielastic_fwhm in the resolution context is honoured
# ---------------------------------------------------------------------------
class _StubFit:
    """A FitView-shaped stub whose resolution_context() carries a quasielastic_fwhm (which the real
    FitView does not surface today) — exercises the pure-path fallback to the ctx-carried qe width."""

    scan_number = 99
    detector_id = "det"
    omega0 = 4.0
    gamma = 0.4
    amplitudes = {"phonon.A": 5000.0, "elastic.E": 8000.0, "qe.QE": 300.0, "bg.BG": 40.0}

    def __init__(self, res):
        self._res = res

    def resolution_context(self):
        return dict(self._res)


def test_evaluate_pure_uses_ctx_quasielastic_fwhm_without_override(make_ws):
    # No explicit override, but the resolution context carries a qe width -> the pure path must use
    # it (previously the key was stripped and ignored, forcing the fallback/raise).
    energy = np.linspace(-2.0, 7.0, 40)
    res = {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0, "T": 200.0,
           "quasielastic_fwhm": 2.5}
    fit = _StubFit(res)
    out = evaluate_model(fit, make_ws() / "data", x_eval=energy)   # no explicit override
    assert out.pure is True
    assert "qe" in [name for name, _ in out.components]


def test_evaluate_pure_explicit_override_wins_over_ctx(make_ws):
    # An explicit override must win over the ctx-carried width: a different override changes the QE
    # component curve.
    energy = np.linspace(-2.0, 7.0, 40)
    res = {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0, "T": 200.0,
           "quasielastic_fwhm": 2.5}
    from_ctx = evaluate_model(_StubFit(res), make_ws() / "data", x_eval=energy)
    from_override = evaluate_model(_StubFit(res), make_ws() / "data",
                                   quasielastic_fwhm=0.5, x_eval=energy)
    assert from_ctx.pure is from_override.pure is True
    qe_ctx = dict(from_ctx.components)["qe"]
    qe_override = dict(from_override.components)["qe"]
    assert not np.allclose(qe_ctx, qe_override)    # the override actually changed the width


# ---------------------------------------------------------------------------
# interval completeness: a partial CSV row must not shadow a complete outcome interval
# ---------------------------------------------------------------------------
def test_partial_row_interval_falls_through_to_outcome():
    # interval_lo present, interval_hi blank -> the row form is incomplete, so the complete outcome
    # interval must win (not a half-populated (4.6, None)).
    row = {"scan": 7, "detector": "det", "interval_lo": 4.6, "interval_hi": ""}
    fv = FitView.from_row(row, {"interval": [4.6, 5.4]})
    assert fv.interval == (4.6, 5.4)


def test_complete_row_interval_wins_over_outcome():
    row = {"scan": 7, "detector": "det", "interval_lo": 1.0, "interval_hi": 2.0}
    fv = FitView.from_row(row, {"interval": [4.6, 5.4]})
    assert fv.interval == (1.0, 2.0)


# ---------------------------------------------------------------------------
# session_events: parse once, warn once
# ---------------------------------------------------------------------------
def test_session_events_parses_file_once(make_ws, monkeypatch):
    good = [json.dumps({"ts": 1.0, "kind": "propose"}),
            json.dumps({"ts": 2.0, "kind": "run"})]
    truncated = '{"ts": 3.0, "kind": "run", "payl'
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted"}],
                   log_lines=good + [truncated])

    orig_read = Path.read_text
    reads = {"n": 0}

    def _counting(self, *a, **k):
        if self.name == "session_log.jsonl":
            reads["n"] += 1
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _counting)

    ws = Workspace.open(root)
    assert len(list(ws.session_events())) == 2
    assert len(list(ws.session_events(kind="propose"))) == 1
    assert reads["n"] == 1                                  # parsed once, filtered in memory
    # the truncated line produced exactly one warning, not one per call
    assert sum("truncated" in w for w in ws.warnings) == 1
    ws.refresh()
    assert len(list(ws.session_events())) == 2
    assert reads["n"] == 2                                  # refresh() re-reads


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
    # This pre-atomic results.csv has no scan_id, so use the legacy scan-level form explicitly.
    out = ws.evaluate_model("tavi:5", "det")
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

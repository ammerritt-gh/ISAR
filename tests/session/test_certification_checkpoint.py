from __future__ import annotations

import hashlib
import json
import time

import pytest

from isar.session.certification import (CheckpointStore, create_analysis_manifest,
                                        master_fit_fingerprint, tier_satisfies)
from isar.session import driver, master_delivery, scan_delivery
from isar.session.state import ScanEntry


def _manifest(tmp_path, **extra):
    payload = {
        "schema": "isar.certification/1", "tier": "full",
        "inputs": {"policy_sha256": "a" * 64},
        "seeds": {"scan_seed_base": 6000, "bayes_seed_offset": 1_000_003},
        "worklist": [{"hkl": [2.0, 0, 0]}, {"hkl": [2.1, 0, 0]}],
        **extra,
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload["fingerprint"] = fingerprint
    revision = tmp_path / fingerprint
    revision.mkdir(parents=True)
    path = revision / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(scan_id="tavi:7:det", content_hash="content"):
    return ScanEntry(scan_id=scan_id, scan_number=7, scan_type="measurement",
                     status="fitted", content_hash=content_hash,
                     guidelines_hash="guide", detectors={"det": {"omega0": 5.0}},
                     provenance={"seed": 6007})


def test_checkpoint_round_trip_skips_valid_completed_unit(tmp_path):
    store = CheckpointStore(_manifest(tmp_path))
    entry = _entry()
    path = store.save("scan:tavi:7:det", entry)
    loaded = store.load("scan:tavi:7:det", content_hash="content", guidelines_hash="guide")
    assert loaded == entry
    assert store.save("scan:tavi:7:det", entry) == path
    store.mark_complete(unit_keys=["scan:tavi:7:det"])
    assert (store.root / "complete.json").exists()


def test_checkpoint_revision_never_overwrites_changed_unit(tmp_path):
    store = CheckpointStore(_manifest(tmp_path))
    store.save("scan:tavi:7:det", _entry())
    assert store.load("scan:tavi:7:det", content_hash="changed",
                      guidelines_hash="guide") is None
    with pytest.raises(ValueError, match="new manifest fingerprint"):
        store.save("scan:tavi:7:det", _entry(content_hash="changed"))


def test_corrupt_checkpoint_is_rerun_and_atomically_repaired(tmp_path):
    store = CheckpointStore(_manifest(tmp_path))
    path = store._unit_path("scan:tavi:7:det")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert store.load("scan:tavi:7:det", content_hash="content",
                      guidelines_hash="guide") is None
    store.save("scan:tavi:7:det", _entry())
    assert store.load("scan:tavi:7:det", content_hash="content",
                      guidelines_hash="guide") == _entry()


def test_structurally_wrapped_but_invalid_checkpoint_payload_is_repaired(tmp_path):
    store = CheckpointStore(_manifest(tmp_path))
    path = store._unit_path("scan:tavi:7:det")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "isar.certification.unit/2",
        "unit_key": "scan:tavi:7:det",
        "manifest_fingerprint": store.manifest["fingerprint"],
        "entry": {"scan_number": "not-an-integer"},
    }), encoding="utf-8")

    assert store.load("scan:tavi:7:det", content_hash="content",
                      guidelines_hash="guide") is None
    store.save("scan:tavi:7:det", _entry())

    assert store.load("scan:tavi:7:det", content_hash="content",
                      guidelines_hash="guide") == _entry()


def test_manifest_tamper_and_wrong_revision_are_rejected(tmp_path):
    path = _manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tier"] = "bayes"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        CheckpointStore(path)

    other = _manifest(tmp_path / "other")
    moved = other.parent.parent / "wrong" / "manifest.json"
    moved.parent.mkdir()
    moved.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprinted revision"):
        CheckpointStore(moved)


def test_progress_reporting_appends_session_owned_advisory_event(tmp_path):
    store = CheckpointStore(_manifest(tmp_path))
    log_path = tmp_path / "report" / "session_log.jsonl"
    (store.root / "runtime.json").write_text(json.dumps({
        "activity_id": "cert-1", "log_path": str(log_path), "total": 2,
    }), encoding="utf-8")

    store.report_progress(7)

    event = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert event["kind"] == "activity_progress"
    assert event["payload"]["activity_id"] == "cert-1"
    assert event["payload"]["completed"] == 2
    assert event["payload"]["total"] == 2
    assert event["payload"]["progress_unit"] == "points"
    assert event["payload"]["phase"] == "fitting"
    progress = json.loads((store.root / "progress.json").read_text(encoding="utf-8"))
    assert progress["schema"] == "isar.certification.progress/1"
    assert progress["completed"] == 2 and progress["published"] is False


def test_ordinary_analysis_manifest_is_content_addressed_and_reusable(tmp_path):
    payload = {"tier": "full", "inputs": {"policy_sha256": "a" * 64},
               "worklist": {"scans": [{"unit_key": "scan:1"}], "masters": []}}
    first = create_analysis_manifest(tmp_path, payload)
    second = create_analysis_manifest(tmp_path, payload)
    assert first == second
    assert first.parent.parent.name == "analysis"
    assert CheckpointStore(first).manifest["schema"] == "isar.analysis_work/1"


def test_soft_deadline_finishes_active_wave_then_stops_dispatch(monkeypatch):
    completed = []
    shadow_policies = []

    def fake_process(*_args, **kwargs):
        shadow_policies.append(kwargs["shadow_enabled"])
        return object()

    monkeypatch.setattr(scan_delivery, "_process_one", fake_process)
    tasks = [
        (None, "det", [2.0, 0, 0], 300.0, "sample", f"scan:{index}",
         f"content:{index}", "guide", 6000 + index)
        for index in range(2)
    ]
    gate = driver._AnalysisDeadline(time.monotonic() - 1.0, force_initial_wave=True)
    with pytest.raises(scan_delivery.AnalysisWorkIncomplete, match="1/2 scan fit units"):
        scan_delivery._run_tasks(
            tasks, object(), object(), object(), set(), M=10, errors_tier="full",
            boot_workers=1, use_pool=False, scan_workers=1,
            on_complete=completed.append, deadline_gate=gate)
    assert len(completed) == 1
    assert shadow_policies == [False]


def test_master_task_threads_deadline_shadow_policy(monkeypatch):
    captured = {}

    def fake_master_group(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(master_delivery, "_fit_master_group", fake_master_group)
    master_delivery._fit_master_group_task((
        None, None, None, "det", None, None, None, "master:q", 9000001, "hash",
        20, 1, "full", False, None, None, "certification", False,
    ))

    assert captured["fit_role"] == "certification"
    assert captured["shadow_enabled"] is False


def test_master_fingerprint_covers_numerical_dependencies_but_not_execution_order():
    base = {
        "spectrum_digest": "a" * 64, "policy": {"model": "phonon"},
        "resolved_guidelines": {"min_fit_points": 8, "monitor_frac": 0.5},
        "resolved_calibration": {"center": 0.0, "gauss_fwhm": 1.2},
        "frozen_state": {"dispersion_prior": [{"omega0": 5.0}]},
        "errors_tier": "full", "M": 200, "seed": 9006000,
        "boot_adaptive": True,
        "bayes_opts": {"n_live": 250, "dlogz": 0.5, "max_iter": 30000},
    }
    expected = master_fit_fingerprint(**base)
    for key, replacement in (
            ("spectrum_digest", "b" * 64), ("policy", {"model": "phonon2"}),
            ("resolved_guidelines", {"min_fit_points": 9, "monitor_frac": 0.5}),
            ("resolved_calibration", {"center": 0.1, "gauss_fwhm": 1.2}),
            ("frozen_state", {"dispersion_prior": [{"omega0": 5.1}]}),
            ("errors_tier", "bayes"), ("M", 201), ("seed", 9006001),
            ("boot_adaptive", False),
            ("bayes_opts", {"n_live": 251, "dlogz": 0.5, "max_iter": 30000})):
        changed = {**base, key: replacement}
        assert master_fit_fingerprint(**changed) != expected
    # Worker count, completion order, and allowance are deliberately absent from the API.
    assert tier_satisfies("bayes", "full")
    assert not tier_satisfies("full", "bayes")


# --------------------------------------------------------------- structurally unresolvable q (AP5)
# Regression: 7348cc6 introduced the inferred acoustic zone-centre endpoint — a q that is
# unresolvable by construction and therefore carries no pooled master. stopping.py and acquire.py
# learned to exclude it from precision coverage; certification did not, so it still demanded exactly
# one pooled master per objective-path point. A campaign that certified 20 of 21 points then aborted
# with "requested q [2.0, 0.0, 0.0] maps to 0 pooled masters".
from isar.session.certification import _is_structurally_unresolvable


def test_predicate_prefers_the_emitted_eligibility_verdict():
    """evaluate_stop's precision_coverage_eligible is the authority when present."""
    assert _is_structurally_unresolvable({"precision_coverage_eligible": False})
    assert not _is_structurally_unresolvable({"precision_coverage_eligible": True})
    # A record that carries no verdict at all is an ordinary certifiable target.
    assert not _is_structurally_unresolvable({})


def test_ordinary_missing_master_still_raises():
    """The exclusion must be narrow: a q that simply has no master is still an error."""
    assert not _is_structurally_unresolvable({"precision_coverage_eligible": True, "hkl": [2.1, 0, 0]})


def _cert_workspace(tmp_path):
    """Minimal real workspace: one pooled master at 2.05, nothing at the 2.00 origin."""
    from pathlib import Path
    from isar.parsers import tavi as tavi_parser
    from isar.session.state import SessionState

    root = tmp_path / "ws"
    (root / "report").mkdir(parents=True)
    (root / "card.policy.toml").write_text(
        Path(tavi_parser._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    (root / "card.state.json").write_text("{}", encoding="utf-8")

    spectrum_ref = "data/spectrum-2050.json"
    (root / "data").mkdir()
    (root / spectrum_ref).write_text(json.dumps({"x": [0.0], "counts": [1]}), encoding="utf-8")

    st = SessionState()
    st.put(ScanEntry(
        scan_id="master:tavi_(205, 0, 0)_300.0", scan_number=9_000_001,
        scan_type="measurement", status="fitted",
        detectors={"det": {"pooled": True, "q": [2.05, 0.0, 0.0], "T": 300.0, "calib": {}}},
        provenance={"members": [["tavi:1:det"]], "spectrum_ref": spectrum_ref}))
    st.save(root / "session.json")
    return root


def _cert_request(root, **extra):
    body = {
        "schema": "isar.certification.request/1", "tier": "full",
        "fit_options": {"M": 8},
        "objective_path": [[2.0, 0.0, 0.0], [2.05, 0.0, 0.0]],
        "seeds": {}, "calibration": {},
        **extra,
    }
    body["fingerprint"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    path = root / "request.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_manifest_excludes_an_inferred_origin_and_says_so(tmp_path):
    from isar.session.certification import create_master_certification_manifest

    root = _cert_workspace(tmp_path)
    request = _cert_request(root, per_q=[
        {"hkl": [2.0, 0.0, 0.0], "precision_coverage_eligible": False,
         "structural_endpoint_status": "inferred_unresolved", "is_reduced_q_origin": True,
         "reason": "acoustic endpoint inferred at physical reduced-q origin"},
        {"hkl": [2.05, 0.0, 0.0], "precision_coverage_eligible": True},
    ])
    manifest = json.loads(create_master_certification_manifest(
        root, request).read_text(encoding="utf-8"))

    assert [u["hkl"] for u in manifest["worklist"]] == [[2.05, 0.0, 0.0]]
    excluded = manifest["excluded_points"]
    assert len(excluded) == 1
    assert excluded[0]["hkl"] == [2.0, 0.0, 0.0]
    assert excluded[0]["structural_endpoint_status"] == "inferred_unresolved"
    assert excluded[0]["is_reduced_q_origin"] is True
    assert "reduced-q origin" in excluded[0]["reason"]


def test_manifest_still_raises_when_an_eligible_q_has_no_master(tmp_path):
    """The exclusion must not become a blanket tolerance for missing masters."""
    from isar.session.certification import create_master_certification_manifest

    root = _cert_workspace(tmp_path)
    request = _cert_request(root, per_q=[
        {"hkl": [2.0, 0.0, 0.0], "precision_coverage_eligible": True},
        {"hkl": [2.05, 0.0, 0.0], "precision_coverage_eligible": True},
    ])
    with pytest.raises(ValueError, match="maps to 0 pooled masters"):
        create_master_certification_manifest(root, request)


def test_excluded_points_present_and_empty_when_nothing_skipped(tmp_path):
    from isar.session.certification import create_master_certification_manifest

    root = _cert_workspace(tmp_path)
    request = _cert_request(
        root,
        objective_path=[[2.05, 0.0, 0.0]],
        per_q=[{"hkl": [2.05, 0.0, 0.0], "precision_coverage_eligible": True}])
    manifest = json.loads(create_master_certification_manifest(
        root, request).read_text(encoding="utf-8"))
    assert manifest["excluded_points"] == []


def test_worklist_index_gate_accepts_an_excluded_zone_origin():
    """Consumer side of the AP7 fix: a worklist that starts at canonical index 1 because the
    reduced-q origin was excluded must be accepted — the union of worklist + excluded covers the
    path. Previously ``_run_master_certification`` demanded the worklist alone equal range(len),
    which raised on every zone-origin acoustic campaign and hard-failed final certification."""
    worklist = [{"canonical_index": i} for i in range(1, 21)]
    excluded = [{"canonical_index": 0}]
    master_delivery._assert_canonical_indices_cover_path(worklist, excluded)  # no raise
    # Dense worklist with no exclusions still fine.
    master_delivery._assert_canonical_indices_cover_path(
        [{"canonical_index": 0}, {"canonical_index": 1}], [])


def test_worklist_index_gate_still_rejects_gaps_and_duplicates():
    """The exclusion tolerance must not weaken the real guarantee: every path point accounted for
    exactly once. A hole in the combined index set (a silently dropped point) or a duplicate is
    still a hard error."""
    with pytest.raises(ValueError, match="contiguous, unique"):
        # combined {0, 2} leaves index 1 unaccounted for — a silently dropped point.
        master_delivery._assert_canonical_indices_cover_path(
            [{"canonical_index": 2}], [{"canonical_index": 0}])
    with pytest.raises(ValueError, match="contiguous, unique"):
        master_delivery._assert_canonical_indices_cover_path(
            [{"canonical_index": 1}, {"canonical_index": 1}], [{"canonical_index": 0}])

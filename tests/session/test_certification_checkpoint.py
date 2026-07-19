from __future__ import annotations

import hashlib
import json

import pytest

from isar.session.certification import CheckpointStore
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
    assert event["payload"] == {
        "activity_id": "cert-1", "completed": 2, "total": 2, "progress_unit": "points",
    }

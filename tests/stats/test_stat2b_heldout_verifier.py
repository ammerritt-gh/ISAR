"""The STAT-2b.1 held-out evidence trust boundary (S10).

The frozen gate and aggregate authenticate at their frozen identities, the replay
reproduces the preserved S8 decision, and development evidence cannot be substituted
for held-out evidence. The retired source-closure cycle has no tests here — S10.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = ROOT / "tools" / "stats" / "stat2b_heldout_verifier.py"
SPEC = importlib.util.spec_from_file_location("stat2b_heldout_verifier_test", ENGINE_PATH)
assert SPEC is not None and SPEC.loader is not None
ENGINE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ENGINE
SPEC.loader.exec_module(ENGINE)


def _heldout_repo(tmp_path: Path) -> Path:
    """Copy exactly the three files the held-out trust boundary reads."""
    for relative in (
        ENGINE.HISTORICAL_GATE_PATH,
        ENGINE.HELDOUT_SUMMARY_PATH,
        ENGINE.HISTORICAL_EVALUATOR_PATH,
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return tmp_path


def _rewrite_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_historical_gate_and_aggregate_replay_are_exact():
    gate = ENGINE.authenticate_historical_gate(ROOT)
    replay = ENGINE.replay_heldout_aggregate(ROOT, gate)

    assert ENGINE.sha256_file(ROOT / ENGINE.HISTORICAL_GATE_PATH) == (
        ENGINE.HISTORICAL_GATE_SHA256
    )
    assert replay["status"] == "authority_withheld"
    assert replay["aggregate_path"] == ENGINE.HELDOUT_SUMMARY_PATH
    assert replay["replay_fingerprint"] == ENGINE.EXPECTED_REPLAY_FINGERPRINT
    assert {record["rule_id"] for record in replay["failed_rules"]} == {
        "comparative_false_certification_noninferiority",
        "minimum_complete_comparison_rate",
        "missed_mode_noninferiority",
        "omega_rmse_noninferiority",
        "predictive_loss_superiority",
    }
    assert all(
        set(record)
        == {"rule_id", "metric", "scope", "observed_bound", "required_margin"}
        for record in replay["failed_rules"]
    )


def test_historical_replay_rejects_development_summary_substitution(tmp_path):
    repo = _heldout_repo(tmp_path)
    development = (
        ROOT / "docs/assets/stat2b/2026-07-30-stat2b1/decision_summary.json"
    )
    heldout = repo / ENGINE.HELDOUT_SUMMARY_PATH
    heldout.write_bytes(development.read_bytes())

    gate = ENGINE.authenticate_historical_gate(repo)
    with pytest.raises(ENGINE.ClosureError, match="held-out aggregate bytes changed"):
        ENGINE.replay_heldout_aggregate(repo, gate)


def test_historical_replay_recomputes_the_complete_summary_fingerprint(
    tmp_path, monkeypatch
):
    repo = _heldout_repo(tmp_path)
    heldout = repo / ENGINE.HELDOUT_SUMMARY_PATH
    summary = json.loads(heldout.read_text(encoding="utf-8"))
    summary["cell_summaries"][0]["n"] = 102
    _rewrite_json(heldout, summary)
    monkeypatch.setattr(ENGINE, "HELDOUT_SUMMARY_SHA256", ENGINE.sha256_file(heldout))

    gate = ENGINE.authenticate_historical_gate(repo)
    with pytest.raises(ENGINE.ClosureError, match="semantic fingerprint does not reproduce"):
        ENGINE.replay_heldout_aggregate(repo, gate)


def test_source_content_hash_is_portable_across_line_endings(tmp_path):
    # The replay hashes the historical evaluator over canonical LF content, so a CRLF
    # checkout must not change its identity.
    source = tmp_path / "source.py"
    source.write_bytes(b"value = 1\r\nprint(value)\r\n")
    windows_hash = ENGINE.source_content_sha256(source)
    source.write_bytes(b"value = 1\nprint(value)\n")
    assert ENGINE.source_content_sha256(source) == windows_hash

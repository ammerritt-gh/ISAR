"""Regression checks for the compact STAT-2b.1 held-out evidence record."""

import json
from pathlib import Path


REPO = Path(__file__).parents[2]
BUNDLE = REPO / "docs" / "campaigns" / "2026-07-31_STAT2B1_HELDOUT"


def _read_json(name: str) -> dict[str, object]:
    with (BUNDLE / name).open(encoding="utf-8") as stream:
        return json.load(stream)


def test_heldout_bundle_is_complete_aggregate_evidence_without_raw_campaign_artifacts():
    assert {path.name for path in BUNDLE.iterdir()} == {
        "campaign-spec.toml",
        "cell_summaries.csv",
        "decision_summary.json",
        "generated_findings.txt",
        "heldout_owner_gate.json",
        "run_manifest.json",
    }

    summary = _read_json("decision_summary.json")
    manifest = _read_json("run_manifest.json")
    gate = _read_json("heldout_owner_gate.json")

    assert summary["heldout_summary_fingerprint"] == (
        "e85c05edb6114f0f014e66a30074147a5f086622af9db5d6df296b7666ef438c"
    )
    assert summary["heldout_gate_evaluation"]["status"] == "authority_withheld"
    assert summary["evidence_completeness"]["complete"] is True
    assert manifest["dataset_count"] == 2163
    assert manifest["scenario_count"] == 21
    assert manifest["runtime_seconds"] <= gate["runtime_ceiling_seconds"]

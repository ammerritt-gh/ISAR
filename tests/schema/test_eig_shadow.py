from __future__ import annotations

import subprocess
import sys

import pytest

from isar.schema.eig_shadow import (
    ScanDesignShadowScoring,
    ScanDesignShadowVariant,
    ScoreRefusalEvidence,
)


def test_score_refusal_is_per_variant_fingerprinted_nonnumeric_and_omitted_when_absent():
    old = ScanDesignShadowVariant(
        policy_id="policy", policy_fingerprint="a" * 64,
        variant_id="variant", variant_fingerprint="b" * 64,
        status="unavailable", reason_codes=("shadow_scoring_failed",))
    assert "score_refusal_evidence" not in old.to_dict()
    assert old.variant_evidence_fingerprint == (
        "8f209101f7ab704fc2de9f2cce32369dd8f176edf124b55435aaa0df84be13e0")
    assert ScanDesignShadowVariant.from_dict(old.to_dict()).to_dict() == old.to_dict()

    refusal = ScoreRefusalEvidence(
        score_set_status="partial", score_set_reason_codes=("score_set_incomplete",),
        candidate_refusals=(
            {"candidate_id": "legacy", "candidate_family": "legacy_uniform/1",
             "status": "available", "reason_codes": [], "source_reason_code": None},
            {"candidate_id": "target", "candidate_family": "target_core/1",
             "status": "unavailable", "reason_codes": ["missing_exposure_basis"],
             "source_reason_code": None},),
    )
    variant = ScanDesignShadowVariant(
        policy_id="policy", policy_fingerprint="a" * 64,
        variant_id="variant", variant_fingerprint="b" * 64,
        status="unavailable",
        reason_codes=("score_set_incomplete", "missing_exposure_basis"),
        score_refusal_evidence=refusal)
    payload = variant.to_dict()
    assert payload["score_refusal_evidence"]["score_refusal_fingerprint"]
    assert ScanDesignShadowVariant.from_dict(payload).to_dict() == payload

    payload["score_refusal_evidence"]["candidate_refusals"][0]["source_reason_code"] = 1
    with pytest.raises(ValueError, match="nonnumeric"):
        ScanDesignShadowVariant.from_dict(payload)

    aggregate = ScanDesignShadowScoring.unavailable_internal("shadow_scoring_failed").to_dict()
    aggregate["score_refusal_evidence"] = refusal.to_dict()
    with pytest.raises(ValueError, match="fields are invalid"):
        ScanDesignShadowScoring.from_dict(aggregate)


def test_scan_design_and_empty_card_validation_import_without_eig_or_scipy():
    code = (
        "import sys;"
        "from isar.drive.acquisition.scan_design import ScanDesignContext,design_scan_windows;"
        "from isar.schema.card import Card,CardState,load_policy;"
        "p=load_policy(r'isar/parsers/cards/tavi.policy.toml');"
        "assert p.scan_window_eig_policy_set=={};"
        "assert Card(p,CardState()).validate()==[];"
        "bad=[name for name in sys.modules if name.startswith(('isar.eig','scipy'))];"
        "assert not bad,bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("mutation", [
    "partial_all_unavailable", "unavailable_with_available", "available_with_reason",
    "unavailable_without_reason", "source_without_predictive_unavailable",
])
def test_score_refusal_semantics_reject_consistently_reminted_claims(mutation):
    status = "partial"
    candidates = [
        {"candidate_id": "legacy", "candidate_family": "legacy_uniform/1",
         "status": "available", "reason_codes": [], "source_reason_code": None},
        {"candidate_id": "target", "candidate_family": "target_core/1",
         "status": "unavailable", "reason_codes": ["expected_counts_invalid"],
         "source_reason_code": None},
    ]
    if mutation == "partial_all_unavailable":
        candidates[0].update(status="unavailable", reason_codes=["expected_counts_invalid"])
    elif mutation == "unavailable_with_available":
        status = "unavailable"
    elif mutation == "available_with_reason":
        candidates[0]["reason_codes"] = ["expected_counts_invalid"]
    elif mutation == "unavailable_without_reason":
        candidates[1]["reason_codes"] = []
    else:
        candidates[1]["source_reason_code"] = "missing_exposure_basis"

    with pytest.raises(ValueError):
        ScoreRefusalEvidence(
            score_set_status=status, score_set_reason_codes=("candidate_score_unavailable",),
            candidate_refusals=tuple(candidates))


def test_score_refusal_reason_codes_reject_strings_and_off_shape_is_exact():
    payload = ScoreRefusalEvidence(
        score_set_status="partial", score_set_reason_codes=("candidate_score_unavailable",),
        candidate_refusals=(
            {"candidate_id": "legacy", "candidate_family": "legacy_uniform/1",
             "status": "available", "reason_codes": [], "source_reason_code": None},
            {"candidate_id": "target", "candidate_family": "target_core/1",
             "status": "unavailable", "reason_codes": ["expected_counts_invalid"],
             "source_reason_code": None},)).to_dict()
    payload["score_set_reason_codes"] = "candidate_score_unavailable"
    with pytest.raises(ValueError, match="sequences"):
        ScoreRefusalEvidence.from_dict(payload)
    candidate_payload = ScoreRefusalEvidence(
        score_set_status="partial", score_set_reason_codes=("candidate_score_unavailable",),
        candidate_refusals=(
            {"candidate_id": "legacy", "candidate_family": "legacy_uniform/1",
             "status": "available", "reason_codes": [], "source_reason_code": None},
            {"candidate_id": "target", "candidate_family": "target_core/1",
             "status": "unavailable", "reason_codes": ["expected_counts_invalid"],
             "source_reason_code": None},)).to_dict()
    candidate_payload["candidate_refusals"][1]["reason_codes"] = "expected_counts_invalid"
    with pytest.raises(ValueError, match="sequences"):
        ScoreRefusalEvidence.from_dict(candidate_payload)

    with pytest.raises(ValueError, match="non-empty strings"):
        ScoreRefusalEvidence(
            score_set_status="unavailable", score_set_reason_codes=(1,),
            candidate_refusals=(
                {"candidate_id": "target", "candidate_family": "target_core/1",
                 "status": "unavailable", "reason_codes": ["expected_counts_invalid"],
                 "source_reason_code": None},))

    with pytest.raises(ValueError, match="not-requested"):
        ScanDesignShadowScoring(
            status="not_requested", reason_codes=("scan_window_eig_off",),
            scoring_config_fingerprint="a" * 64)

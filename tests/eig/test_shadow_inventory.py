"""Focused Slice 6 tests for pure shadow inventory scoring."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import sys
from dataclasses import replace

import pytest

from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.scan_design import (
    SCAN_DESIGN_INVENTORY_SCHEMA_V2,
    SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED,
    ScanDesignInventory,
    design_scan_windows,
    load_scan_design_inventory,
    validate_scan_design_inventory,
)
from isar.eig.shadow_inventory import (
    ScanDesignShadowScoring,
    _candidate_pair,
    derive_shadow_seed,
    score_shadow_inventory,
)
from isar.eig.scorer import score_scan_window_candidates
from isar.predictive import evaluate_master_predictive
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowShadowScoringConfig,
)
from isar.schema.statistics import semantics_fingerprint


def _fp(name: str) -> str:
    return semantics_fingerprint("tests.eig.shadow_inventory", {"name": name})


def _producer_bundle(monkeypatch):
    eig_helpers = runpy.run_path(str(Path(__file__).with_name("test_design_measure.py")))
    predictive, _projection, _qoi, eligibility = eig_helpers["_bundle"](monkeypatch)
    scan_helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "drive" / "test_scan_design.py"))
    inventory = design_scan_windows(
        scan_helpers["_context"](scan_window_eig_mode="shadow"))
    return predictive, eligibility, inventory


def _policies(scope: str, *, unsupported: bool = False) -> QoIDistinctionPolicySet:
    variants = (
        QoIDistinctionVariant(
            "coarse", {"origin": 0.0, "bin_width": 1.0, "offsets": [0.0]}),
        QoIDistinctionVariant(
            "fine", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}),
    )
    policy = QoIDistinctionPolicy(
        "policy",
        "isar.future_partitions/1" if unsupported
        else "isar.scalar_absolute_partitions/o36.5",
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": scope,
         "annotation": {"campaign_name": "fixture", "experiment_description": "Slice 6",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "exercise the live shadow scorer"},
        variants,
    )
    return QoIDistinctionPolicySet((policy,))


def test_shadow_seed_recipe_is_golden_and_signed_63_bit():
    evidence = derive_shadow_seed(
        scoring_config_fingerprint="a" * 64,
        design_measure_fingerprint="b" * 64,
        candidate_pair_fingerprint="c" * 64,
    )
    assert evidence.seed == 2531981767377547511
    assert 0 <= evidence.seed < 2**63
    assert type(evidence).from_dict(evidence.to_dict()) == evidence


def test_real_producer_chain_evaluates_pair_once_and_reuses_it(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    calls = {"evaluator": 0, "scorer": 0}

    def counted_evaluator(*args, **kwargs):
        calls["evaluator"] += 1
        return evaluate_master_predictive(*args, **kwargs)

    def counted_scorer(*args, **kwargs):
        calls["scorer"] += 1
        return score_scan_window_candidates(*args, **kwargs)

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=16, block_count=4, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=counted_evaluator, scorer=counted_scorer,
    )

    assert calls == {"evaluator": 2, "scorer": 2}, (
        result.status, result.reason_codes, result.design_measure)
    assert result.status == "complete"
    assert len(result.variants) == 2
    assert {item.score_set["seed"] for item in result.variants} == {
        result.seed_evidence.seed}
    attached = inventory.with_shadow_scoring(result)
    assert attached.selected_candidate_id == inventory.selected_candidate_id
    assert attached.selection_basis == SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED
    assert validate_scan_design_inventory(attached).content_hash() == attached.content_hash()


def test_policy_refusals_precede_predictive_evaluation(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("predictive evaluation must not run")

    config = ScanWindowShadowScoringConfig(simulation_count=8, block_count=2)
    missing = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=QoIDistinctionPolicySet(()), scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=forbidden,
    )
    assert missing.status == "unavailable"
    assert missing.reason_codes == ("qoi_policy_missing",)

    unsupported = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope, unsupported=True), scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        predictive_evaluator=forbidden,
    )
    assert unsupported.status == "unavailable"
    assert all(item.reason_codes == ("qoi_policy_unsupported",)
               for item in unsupported.variants)
    assert all(item.compiled_partition is None and item.score_set is None
               and item.target_core_minus_legacy is None
               for item in unsupported.variants)


@pytest.mark.parametrize(("field", "changed"), [
    ("kind", "identity_probe"),
    ("reason", "tampered target-core rationale"),
])
def test_candidate_pair_rejects_kind_and_reason_mismatch_before_evaluation(
        monkeypatch, field, changed):
    _predictive, _eligibility, inventory = _producer_bundle(monkeypatch)
    target_index = next(index for index, item in enumerate(inventory.candidates)
                        if item.candidate_family == "target_core/1")
    candidates = list(inventory.candidates)
    candidates[target_index] = replace(candidates[target_index], **{field: changed})
    unchecked = type("UncheckedInventory", (), {
        "context": inventory.context, "generator_config": inventory.generator_config,
        "candidates": tuple(candidates)})()

    with pytest.raises(ValueError, match="scan_design_candidate_pair_invalid"):
        _candidate_pair(unchecked)


def test_variant_failure_is_bounded_and_retains_prior_complete_evidence(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second variant failed")
        return score_scan_window_candidates(*args, **kwargs)

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=8, block_count=2, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        scorer=fail_second,
    )

    assert result.status == "partial"
    assert calls == 2
    by_id = {item.variant_id: item for item in result.variants}
    assert by_id["coarse"].status == "complete"
    assert by_id["coarse"].score_set is not None
    assert by_id["fine"].status == "unavailable"
    assert by_id["fine"].reason_codes == ("shadow_scoring_failed",)
    assert by_id["fine"].score_set is None
    assert by_id["fine"].target_core_minus_legacy is None


def test_variant_deadline_retains_complete_evidence_and_marks_remaining(monkeypatch):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    checks = 0

    class AnalysisLimitExceeded(Exception):
        pass

    def deadline():
        nonlocal checks
        checks += 1
        # Two pre-evaluation checks and the first variant check succeed.  Refuse the second variant.
        if checks == 4:
            raise AnalysisLimitExceeded("soft allowance")

    result = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=8, block_count=2, max_runtime_variants=8),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
        deadline_check=deadline,
    )

    assert result.status == "partial"
    by_id = {item.variant_id: item for item in result.variants}
    assert by_id["coarse"].status == "complete"
    assert by_id["fine"].reason_codes == ("shadow_scoring_deadline",)
    assert by_id["fine"].score_set is None


def test_strict_inventory_loader_checks_content_address_and_nested_evidence(
        monkeypatch, tmp_path):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scored = inventory.with_shadow_scoring(score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
    ))
    digest = scored.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_text(canonical_json(scored.to_full_dict()), encoding="utf-8")
    reference = {"kind": "scan_design_inventory", "sha256": digest,
                 "path": f"report/scan-design/{digest}.json"}
    assert load_scan_design_inventory(reference, tmp_path).content_hash() == digest

    tampered = scored.to_full_dict()
    tampered["shadow_scoring"]["variants"][0]["score_set"]["candidate_scores"][0][
        "conditional_target_qoi_information_nats"] += 1.0
    path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="address"):
        load_scan_design_inventory(reference, tmp_path)


@pytest.mark.parametrize(("section", "field", "value"), [
    ("scoring_config", "simulation_count", 4),
    ("policy_set", "contract", "isar.qoi_distinction_policy_set/tampered"),
])
def test_persisted_shadow_inputs_reauthenticate_frozen_content(
        monkeypatch, section, field, value):
    predictive, eligibility, inventory = _producer_bundle(monkeypatch)
    scope = _fp("campaign")
    scored = inventory.with_shadow_scoring(score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=_policies(scope),
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2),
        scope_kind="campaign_configuration", scope_fingerprint=scope,
    ))
    payload = scored.to_full_dict()
    payload["shadow_scoring"][section][field] = value

    with pytest.raises(ValueError):
        ScanDesignInventory.from_dict(payload)


def test_importing_eig_does_not_import_campaign_or_acquisition_modules():
    code = (
        "import sys; import isar.eig; "
        "bad=[name for name in sys.modules if name.startswith('isar.drive')]; "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_internal_unavailable_evidence_claims_no_source_identity():
    result = ScanDesignShadowScoring.unavailable_internal(
        diagnostics={"message": "synthetic failure"})
    second = ScanDesignShadowScoring.unavailable_internal(
        diagnostics={"message": "different prose"})
    payload = result.to_dict()
    assert result.status == "unavailable"
    assert payload["eligibility"] is None
    assert payload["candidate_pair"] is None
    assert second.shadow_scoring_fingerprint == result.shadow_scoring_fingerprint
    assert "synthetic failure" not in canonical_json({
        key: value for key, value in payload.items() if key != "diagnostics"})


@pytest.mark.parametrize("field", ["winner", "recommendation", "threshold"])
def test_shadow_evidence_recursively_rejects_authoritative_decision_fields(field):
    with pytest.raises(ValueError, match="authoritative decision field"):
        ScanDesignShadowScoring.unavailable_internal(
            diagnostics={"nested": {field: "target_core/1"}})


def test_historical_v2_inventory_remains_loadable_from_canonical_bytes(monkeypatch, tmp_path):
    _predictive, _eligibility, current = _producer_bundle(monkeypatch)
    historical = replace(
        current, inventory_version=SCAN_DESIGN_INVENTORY_SCHEMA_V2,
        shadow_scoring=None)
    digest = historical.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_text(canonical_json(historical.to_full_dict()), encoding="utf-8")
    loaded = load_scan_design_inventory(
        {"kind": "scan_design_inventory", "sha256": digest,
         "path": f"report/scan-design/{digest}.json"}, tmp_path)
    assert loaded.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA_V2
    assert loaded.shadow_scoring is None

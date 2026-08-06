from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from isar.drive.objective import ObjectiveSpec, current_objective_comparison_scope
from isar.drive.supervisor.goal_to_spec import _build_spec, spec_from_dict
from isar.eig.offline_compare import (
    QoIDistinctionPolicy as OfflinePolicy,
    QoIDistinctionPolicySet as OfflinePolicySet,
    QoIDistinctionVariant as OfflineVariant,
)
from isar.eig.policy import compile_card_eig_policy_set, compile_qoi_distinction_policy
from isar.schema.eig_policy import (
    QOI_DISTINCTION_POLICY_CONTRACT,
    QOI_DISTINCTION_POLICY_SET_CONTRACT,
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowEIGConfig,
    ScanWindowShadowScoringConfig,
    matching_qoi_policies,
)
from isar.schema.card import validate_policy_text
from isar.schema.statistics import semantics_fingerprint


def _scope() -> dict[str, str]:
    return {
        "producer_revision": "isar.current_objective_comparison_scope/1",
        "fingerprint": "a" * 64,
    }


def _authoring(*, kind: str = "isar.scalar_absolute_partitions/o36.5", variants=1):
    return {
        "policies": [{
            "policy_id": "omega0",
            "policy_kind": kind,
            "source_qoi": {"id": "omega0", "unit": "meV"},
            "scope": {
                "bind_to_current_campaign_configuration": True,
                "annotation": {
                    "campaign_name": "fixture",
                    "experiment_description": "runtime shadow policy",
                    "date": "2026-08-03",
                },
            },
            "provenance": {
                "kind": "explicit_user_analysis/1",
                "source_id": "operator",
                "rationale": "explicit analysis scale",
            },
            "variants": [{
                "variant_id": f"v{index:02d}",
                "parameters": {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]},
            } for index in range(variants)],
        }],
    }


def _goal_data() -> dict:
    return {
        "q_start": [1.0, 0.0, 0.0],
        "q_end": [1.1, 0.0, 0.0],
        "step_rlu": 0.05,
        "tolerance_meV": 0.2,
        "steering": {"scan_window_eig_mode": "shadow"},
    }


def test_extracted_o365_records_keep_exact_payload_and_fingerprint_recipe():
    variant = QoIDistinctionVariant(
        "base", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]})
    assert variant.variant_fingerprint == semantics_fingerprint(
        "isar.offline_eig/o36.5/variant",
        {"variant_id": "base", "parameters": {
            "origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}},
    )
    policy = QoIDistinctionPolicy(
        "policy", "isar.scalar_absolute_partitions/o36.5",
        {"id": "omega0", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": "a" * 64,
         "annotation": {"campaign_name": "fixture", "experiment_description": "desc",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "operator",
         "rationale": "test"}, (variant,))
    policy_set = QoIDistinctionPolicySet((policy,))

    assert policy.to_dict()["contract"] == QOI_DISTINCTION_POLICY_CONTRACT
    assert policy_set.to_dict()["contract"] == QOI_DISTINCTION_POLICY_SET_CONTRACT
    assert OfflineVariant is QoIDistinctionVariant
    assert OfflinePolicy is QoIDistinctionPolicy
    assert OfflinePolicySet is QoIDistinctionPolicySet
    assert QoIDistinctionPolicySet.from_dict(policy_set.to_dict()) == policy_set


def test_card_compiler_materializes_exact_scope_and_strips_authoring_marker():
    policy_set = compile_card_eig_policy_set(_authoring(), _scope())
    policy = policy_set.policies[0]
    assert dict(policy.scope)["kind"] == "campaign_configuration"
    assert dict(policy.scope)["fingerprint"] == _scope()["fingerprint"]
    assert "bind_to_current_campaign_configuration" not in str(policy_set.to_dict())
    assert matching_qoi_policies(
        policy_set, qoi_id="omega0", qoi_unit="meV",
        scope_kind="campaign_configuration", scope_fingerprint=_scope()["fingerprint"],
    ) == (policy,)
    assert compile_qoi_distinction_policy(policy, policy.variants[0]).qoi_id == "omega0"


def test_goal_compilation_binds_policy_without_changing_objective_scope_or_hypothesis_hash():
    plain = _build_spec(_goal_data(), {"tier": "deterministic", "step_h": 0.05})
    frozen = _build_spec(
        _goal_data(), {"tier": "deterministic", "step_h": 0.05},
        card_eig_policy_set=_authoring())

    assert frozen.scan_window_eig.policy_set is not None
    assert frozen.hypothesis_fingerprint == plain.hypothesis_fingerprint
    assert current_objective_comparison_scope(frozen) == current_objective_comparison_scope(plain)
    policy = frozen.scan_window_eig.policy_set.policies[0]
    assert policy.scope["fingerprint"] == current_objective_comparison_scope(frozen)["fingerprint"]


def test_explicit_frozen_spec_accepts_bounded_nondefault_scoring_and_rejects_authoring_marker():
    base = _build_spec(_goal_data(), {"tier": "deterministic", "step_h": 0.05})
    config = ScanWindowShadowScoringConfig(
        simulation_count=12, block_count=3, max_runtime_variants=2)
    explicit = replace(base, scan_window_eig=ScanWindowEIGConfig(scoring_config=config))
    restored = ObjectiveSpec.from_dict(explicit.to_dict())
    assert restored.scan_window_eig.scoring_config.simulation_count == 12

    malformed = explicit.to_dict()
    malformed["scan_window_eig"] = _authoring()
    with pytest.raises(ValueError, match="objective configuration fields"):
        ObjectiveSpec.from_dict(malformed)


def test_legacy_objective_two_omission_materializes_no_policy_defaults_without_card_supplement():
    objective = _build_spec(_goal_data(), {"tier": "deterministic", "step_h": 0.05})
    missing = objective.to_dict()
    del missing["scan_window_eig"]
    restored = ObjectiveSpec.from_dict(missing)
    assert restored.steering["scan_window_eig_mode"] == "shadow"
    assert restored.scan_window_eig.policy_set is None
    assert restored.scan_window_eig.scoring_config == ScanWindowShadowScoringConfig()
    assert restored.to_dict()["scan_window_eig"] == ScanWindowEIGConfig().to_dict()

    explicit_null = objective.to_dict()
    explicit_null["scan_window_eig"] = None
    with pytest.raises(TypeError, match="scan_window_eig"):
        ObjectiveSpec.from_dict(explicit_null)

    authoring_marker = objective.to_dict()
    authoring_marker["scan_window_eig_policy_set"] = _authoring()
    with pytest.raises(ValueError, match="fields are invalid"):
        ObjectiveSpec.from_dict(authoring_marker)

    restored = spec_from_dict(
        missing,
        card_eig_policy_set={"this would fail if consulted": True},
    )
    assert restored.scan_window_eig.policy_set is None


def test_compact_spec_inherits_inline_card_policy_before_freeze():
    objective = spec_from_dict(
        _goal_data(),
        defaults={"tier": "deterministic", "step_h": 0.05},
        card_eig_policy_set=_authoring(),
    )
    assert objective.scan_window_eig.policy_set is not None
    assert objective.scan_window_eig.policy_set.policies[0].scope["fingerprint"] == (
        current_objective_comparison_scope(objective)["fingerprint"])


def test_full_frozen_objective_precompiles_supported_policy_but_retains_unknown_kind():
    base = _build_spec(_goal_data(), {"tier": "deterministic", "step_h": 0.05})
    annotation = {"campaign_name": "fixture", "experiment_description": "explicit",
                  "date": "2026-08-03"}
    provenance = {"kind": "explicit_user_analysis/1", "source_id": "operator",
                  "rationale": "explicit scale"}
    malformed_variant = QoIDistinctionVariant(
        "base", {"origin": 0.0, "bin_width": 0.0, "offsets": [0.0]})
    malformed_policy = QoIDistinctionPolicy(
        "policy", "isar.scalar_absolute_partitions/o36.5",
        {"id": "omega0", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": "a" * 64,
         "annotation": annotation}, provenance, (malformed_variant,))
    malformed = base.to_dict()
    malformed["scan_window_eig"] = ScanWindowEIGConfig(
        policy_set=QoIDistinctionPolicySet((malformed_policy,))).to_dict()
    with pytest.raises(ValueError, match="bin_width"):
        ObjectiveSpec.from_dict(malformed)

    unknown_policy = QoIDistinctionPolicy(
        "future", "isar.future_partitions/1",
        {"id": "omega0", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": "a" * 64,
         "annotation": annotation}, provenance, (malformed_variant,))
    unknown = base.to_dict()
    unknown["scan_window_eig"] = ScanWindowEIGConfig(
        policy_set=QoIDistinctionPolicySet((unknown_policy,))).to_dict()
    restored = ObjectiveSpec.from_dict(unknown)
    assert restored.scan_window_eig.policy_set.policies[0].policy_kind == "isar.future_partitions/1"


@pytest.mark.parametrize("kwargs", [
    {"simulation_count": 1},
    {"simulation_count": 129},
    {"simulation_count": 10, "block_count": 3},
    {"max_runtime_variants": 0},
    {"max_runtime_variants": 9},
])
def test_shadow_scoring_config_enforces_frozen_resource_bounds(kwargs):
    with pytest.raises(ValueError, match="configuration|between"):
        ScanWindowShadowScoringConfig(**kwargs)


def test_card_compiler_enforces_policy_and_variant_limits_before_campaign_start():
    with pytest.raises(ValueError, match="max_runtime_variants"):
        compile_card_eig_policy_set(_authoring(variants=9), _scope())

    too_many = _authoring()
    too_many["policies"] = []
    for index in range(33):
        policy = _authoring()["policies"][0]
        policy["policy_id"] = f"policy-{index:02d}"
        too_many["policies"].append(policy)
    with pytest.raises(ValueError, match="at most 32"):
        compile_card_eig_policy_set(too_many, _scope())


def test_malformed_supported_policy_is_authoring_error_but_unknown_versioned_kind_freezes():
    malformed = _authoring()
    malformed["policies"][0]["variants"][0]["parameters"]["bin_width"] = 0.0
    with pytest.raises(ValueError):
        compile_card_eig_policy_set(malformed, _scope())

    unsupported = compile_card_eig_policy_set(
        _authoring(kind="isar.future_partitions/1"), _scope())
    assert unsupported.policies[0].policy_kind == "isar.future_partitions/1"
    with pytest.raises(ValueError, match="qoi_policy_unsupported"):
        compile_qoi_distinction_policy(
            unsupported.policies[0], unsupported.policies[0].variants[0])


def test_frozen_policy_and_scoring_changes_do_not_enter_current_objective_scope():
    objective = _build_spec(
        _goal_data(), {"tier": "deterministic", "step_h": 0.05},
        card_eig_policy_set=_authoring())
    changed = replace(
        objective,
        scan_window_eig=ScanWindowEIGConfig(
            policy_set=objective.scan_window_eig.policy_set,
            scoring_config=ScanWindowShadowScoringConfig(
                simulation_count=64, block_count=8, max_runtime_variants=8),
        ),
    )
    assert changed.hypothesis_fingerprint == objective.hypothesis_fingerprint
    assert current_objective_comparison_scope(changed) == current_objective_comparison_scope(objective)
    assert changed.scan_window_eig.scan_window_eig_fingerprint != objective.scan_window_eig.scan_window_eig_fingerprint


def test_inline_card_toml_policy_is_recognized_and_validated():
    base = Path("isar/parsers/cards/tavi.policy.toml").read_text(encoding="utf-8")
    authoring = """

[[scan_window_eig_policy_set.policies]]
policy_id = "omega0"
policy_kind = "isar.scalar_absolute_partitions/o36.5"

[scan_window_eig_policy_set.policies.source_qoi]
id = "omega0"
unit = "meV"

[scan_window_eig_policy_set.policies.scope]
bind_to_current_campaign_configuration = true

[scan_window_eig_policy_set.policies.scope.annotation]
campaign_name = "fixture"
experiment_description = "runtime policy"
date = "2026-08-03"

[scan_window_eig_policy_set.policies.provenance]
kind = "explicit_user_analysis/1"
source_id = "operator"
rationale = "explicit analysis scale"

[[scan_window_eig_policy_set.policies.variants]]
variant_id = "base"

[scan_window_eig_policy_set.policies.variants.parameters]
origin = 0.0
bin_width = 0.5
offsets = [0.0]
"""
    result = validate_policy_text(base + authoring)
    assert result.errors == ()
    assert result.warnings == ()
    assert len(result.policy.scan_window_eig_policy_set["policies"]) == 1

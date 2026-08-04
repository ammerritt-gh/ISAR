"""Freeze and evaluate the bounded EIG-4 Slice 8 shadow gate.

This is retained-evidence tooling, not an acquisition selector.  It deliberately imports the
existing strict EIG readers so the Slice 8 gate cannot become a second implementation of Slice 7
authentication.  No runtime module imports this file.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from isar.drive.acquisition.realized_comparison import (
    load_scan_design_realized_comparison,
)
from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
from isar.drive.acquisition.scan_design import load_scan_design_inventory
from isar.eig.eligibility import (
    EligibilityInput,
    eligibility_summary,
    evaluate_eig_eligibility,
    validate_eligibility_summary,
)
from isar.eig.qoi import QoIProjectionEvidence, _QoIModel, validate_qoi_projection_evidence
from isar.schema.action_identity import canonical_json
from isar.schema.eig_policy import ScanWindowShadowScoringConfig
from isar.schema.eig_realized import RealizedScanRequest
from isar.schema.eig_shadow import thaw
from isar.schema.statistics import semantics_fingerprint


log = logging.getLogger(__name__)

GATE_CONTRACT = "isar.eig4_slice8_shadow_gate/o36.8"
CASE_MANIFEST_CONTRACT = "isar.eig4_slice8_case_manifest/o36.8"
FIXTURE_DEFINITIONS_CONTRACT = "isar.eig4_slice8_fixture_definitions/o36.8"
CASE_RESULTS_CONTRACT = "isar.eig4_slice8_case_results/o36.8"
GATE_RESULT_CONTRACT = "isar.eig4_slice8_gate_result/o36.8"
ELIGIBILITY_SOURCE_CONTRACT = "isar.eig4_slice8_eligibility_source/o36.8"
CASE_EVIDENCE_CONTRACT = "isar.eig4_slice8_case_evidence/o36.8"
FAULT_EVIDENCE_CONTRACT = "isar.eig4_slice8_fault_evidence/o36.8"

OUTCOME_TARGET = "target_core_would_win"
OUTCOME_LEGACY = "legacy_control_would_be_retained"
OUTCOME_NON_EIG = "non_eig_path_would_be_used"
OUTCOME_UNAVAILABLE = "selection_evidence_unavailable"
OUTCOMES = {OUTCOME_TARGET, OUTCOME_LEGACY, OUTCOME_NON_EIG, OUTCOME_UNAVAILABLE}

PRIMARY_POLICY_ID = "eig4_slice8_excitation_energy"
PRIMARY_VARIANT_ID = "primary_0p5_mev"
PRIMARY_POLICY_KIND = "isar.scalar_absolute_partitions/o36.5"
PRIMARY_PARAMETERS = {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]}
MARGIN_MULTIPLIER = 2.0

CASE_DEFINITIONS = (
    {
        "case_id": "s8-c01-strong-confirmed",
        "case_generation_seed": 8040801,
        "classification": "strong_isolated_confirmed_target",
        "expected_eligibility": "eligible",
        "required_eligibility_code": None,
        "expected_outcome": OUTCOME_TARGET,
    },
    {
        "case_id": "s8-c02-weak-unresolved-confirmed",
        "case_generation_seed": 8040802,
        "classification": "weak_unresolved_confirmed_target",
        "expected_eligibility": "eligible",
        "required_eligibility_code": None,
        "expected_outcome": OUTCOME_LEGACY,
    },
    {
        "case_id": "s8-c03-near-elastic-selection-time",
        "case_generation_seed": 8040803,
        "classification": "near_elastic_selection_time_snapshot",
        "expected_eligibility": "eligible",
        "required_eligibility_code": None,
        "expected_outcome": OUTCOME_LEGACY,
    },
    {
        "case_id": "s8-c04-forked-target",
        "case_generation_seed": 8040804,
        "classification": "partially_resolved_or_forked_target",
        "expected_eligibility": "ineligible",
        "required_eligibility_code": "modality_fork",
        "expected_outcome": OUTCOME_NON_EIG,
    },
    {
        "case_id": "s8-c05-projection-join-mismatch",
        "case_generation_seed": 8040805,
        "classification": "authenticated_projection_join_mismatch",
        "expected_eligibility": "ineligible",
        "required_eligibility_code": "projection_join_mismatch",
        "expected_outcome": OUTCOME_NON_EIG,
    },
    {
        "case_id": "s8-c06-constrained-selection",
        "case_generation_seed": 8040806,
        "classification": "effective_fit_constraint",
        "expected_eligibility": "ineligible",
        "required_eligibility_code": "selection_constrained",
        "expected_outcome": OUTCOME_NON_EIG,
    },
)

FAULT_DEFINITIONS = (
    {
        "fault_id": "s8-f01-grid-order-mismatch",
        "source_case_id": "s8-c01-strong-confirmed",
        "classification": "returned_grid_order_changed",
        "expected_outcome": OUTCOME_UNAVAILABLE,
        "required_reason_code": "executed_grid_mismatch",
    },
)

REQUIRED_RUNTIME_INVARIANTS = (
    "off_behavior_unchanged",
    "shadow_executes_legacy",
    "authoritative_setup_refuses",
    "inventories_remain_legacy_selected",
    "gate_output_absent_from_submission",
    "recovery_remains_outside_eig",
    "scoring_failure_cannot_suppress_action",
)


class GateEvidenceError(ValueError):
    """The retained evidence cannot support a Slice 8 classification."""


def _exact(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise GateEvidenceError(f"{label} fields are invalid")
    return value


def _valid_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64 and value == value.lower()
            and all(character in "0123456789abcdef" for character in value))


def _valid_commit(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 40 and value == value.lower()
            and all(character in "0123456789abcdef" for character in value))


def _fingerprint(domain: str, value: Mapping[str, Any], fingerprint_field: str) -> str:
    payload = dict(value)
    payload.pop(fingerprint_field, None)
    return semantics_fingerprint(domain, payload)


def fixture_definition_fingerprint(value: Mapping[str, Any]) -> str:
    return semantics_fingerprint(
        "isar.eig4_slice8_fixture_definition_fingerprint/o36.8", value)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        log.error("cannot read Slice 8 evidence %s: %s", path, error)
        raise GateEvidenceError(f"{path.name} is unreadable") from error
    if not isinstance(value, Mapping):
        raise GateEvidenceError(f"{path.name} must contain an object")
    return value


def build_gate_manifest(implementation_anchor: str) -> dict[str, Any]:
    if not _valid_commit(implementation_anchor):
        raise GateEvidenceError("implementation anchor must be a full lowercase Git commit")
    scoring = ScanWindowShadowScoringConfig()
    value: dict[str, Any] = {
        "contract": GATE_CONTRACT,
        "gate_revision": "o36.8",
        "implementation_anchor": implementation_anchor,
        "primary_policy": {
            "policy_id": PRIMARY_POLICY_ID,
            "policy_kind": PRIMARY_POLICY_KIND,
            "qoi_id": "excitation.energy",
            "qoi_unit": "meV",
            "variant_id": PRIMARY_VARIANT_ID,
            "parameters": PRIMARY_PARAMETERS,
        },
        "runtime_scoring_configuration": scoring.to_dict(),
        "selection_margin_multiplier": MARGIN_MULTIPLIER,
        "selection_margin_status": "pragmatic_engineering_margin_not_coverage_claim",
        "required_invariants": list(REQUIRED_RUNTIME_INVARIANTS),
        "cases": [dict(item) for item in CASE_DEFINITIONS],
        "faults": [dict(item) for item in FAULT_DEFINITIONS],
        "gate_manifest_fingerprint": "",
    }
    value["gate_manifest_fingerprint"] = _fingerprint(
        "isar.eig4_slice8_gate_manifest_fingerprint/o36.8",
        value,
        "gate_manifest_fingerprint",
    )
    return value


def validate_gate_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact(value, {
        "contract", "gate_revision", "implementation_anchor", "primary_policy",
        "runtime_scoring_configuration", "selection_margin_multiplier",
        "selection_margin_status", "required_invariants", "cases", "faults",
        "gate_manifest_fingerprint",
    }, "gate manifest")
    if (value["contract"] != GATE_CONTRACT or value["gate_revision"] != "o36.8"
            or not _valid_commit(value["implementation_anchor"])
            or value["selection_margin_multiplier"] != MARGIN_MULTIPLIER
            or value["selection_margin_status"]
            != "pragmatic_engineering_margin_not_coverage_claim"
            or value["required_invariants"] != list(REQUIRED_RUNTIME_INVARIANTS)
            or value["cases"] != [dict(item) for item in CASE_DEFINITIONS]
            or value["faults"] != [dict(item) for item in FAULT_DEFINITIONS]):
        raise GateEvidenceError("gate manifest does not match the frozen Slice 8 gate")
    policy = _exact(value["primary_policy"], {
        "policy_id", "policy_kind", "qoi_id", "qoi_unit", "variant_id", "parameters",
    }, "primary policy")
    if policy != {
            "policy_id": PRIMARY_POLICY_ID,
            "policy_kind": PRIMARY_POLICY_KIND,
            "qoi_id": "excitation.energy",
            "qoi_unit": "meV",
            "variant_id": PRIMARY_VARIANT_ID,
            "parameters": PRIMARY_PARAMETERS}:
        raise GateEvidenceError("primary policy is not the frozen two-offset variant")
    config = ScanWindowShadowScoringConfig.from_dict(value["runtime_scoring_configuration"])
    if config != ScanWindowShadowScoringConfig():
        raise GateEvidenceError("gate must use the runtime 128/8 scoring configuration")
    expected = _fingerprint(
        "isar.eig4_slice8_gate_manifest_fingerprint/o36.8",
        value,
        "gate_manifest_fingerprint",
    )
    if value["gate_manifest_fingerprint"] != expected:
        raise GateEvidenceError("gate manifest fingerprint is invalid")
    return value


def validate_fixture_definitions(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact(value, {
        "contract", "cases", "fixture_definitions_fingerprint",
    }, "fixture definitions")
    if value["contract"] != FIXTURE_DEFINITIONS_CONTRACT:
        raise GateEvidenceError("fixture definitions contract is unsupported")
    cases = value["cases"]
    if not isinstance(cases, list):
        raise GateEvidenceError("fixture definitions cases must be a list")
    expected_ids = [item["case_id"] for item in CASE_DEFINITIONS]
    if [item.get("case_id") for item in cases if isinstance(item, Mapping)] != expected_ids:
        raise GateEvidenceError("fixture definitions must name the six frozen cases in order")
    for item in cases:
        if not isinstance(item, Mapping):
            raise GateEvidenceError("fixture definition must be an object")
        _exact(item, {
            "case_id", "source_kind", "generation_recipe", "physical_parameters",
            "ordinary_analysis_acceptance", "fixture_rationale", "truth_use",
        }, f"fixture definition {item.get('case_id')}")
        if item["source_kind"] not in {
                "deterministic_tavi", "deterministic_synthetic",
                "deterministic_tavi_with_synthetic_predictive_fixture"}:
            raise GateEvidenceError("fixture source kind is unsupported")
        for name in ("generation_recipe", "physical_parameters",
                     "ordinary_analysis_acceptance"):
            if not isinstance(item[name], Mapping) or not item[name]:
                raise GateEvidenceError(f"fixture {name} must be a non-empty object")
        if not isinstance(item["fixture_rationale"], str) or not item["fixture_rationale"].strip():
            raise GateEvidenceError("fixture rationale must be non-empty")
        if item["truth_use"] != "post_run_evaluation_only":
            raise GateEvidenceError("fixture truth may be used only after ordinary analysis")
    expected_fingerprint = _fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8",
        value,
        "fixture_definitions_fingerprint",
    )
    if value["fixture_definitions_fingerprint"] != expected_fingerprint:
        raise GateEvidenceError("fixture definitions fingerprint mismatch")
    return value


def build_case_manifest(gate_manifest: Mapping[str, Any],
                        fixture_definitions: Mapping[str, Any]) -> dict[str, Any]:
    validate_gate_manifest(gate_manifest)
    validate_fixture_definitions(fixture_definitions)
    fixtures = {item["case_id"]: item for item in fixture_definitions["cases"]}
    cases = []
    for item in CASE_DEFINITIONS:
        cases.append({
            **item,
            "fixture_definition": fixtures[item["case_id"]],
            "fixture_freeze_rule": (
                "choose and document planted physical/ordinary-analysis criteria before final EIG "
                "scoring; never retune after the frozen disposition is observed"),
            "truth_authority": "post_run_evaluation_only",
        })
    value: dict[str, Any] = {
        "contract": CASE_MANIFEST_CONTRACT,
        "gate_manifest_fingerprint": gate_manifest["gate_manifest_fingerprint"],
        "fixture_definitions_fingerprint": (
            fixture_definitions["fixture_definitions_fingerprint"]),
        "cases": cases,
        "faults": [dict(item) for item in FAULT_DEFINITIONS],
        "case_manifest_fingerprint": "",
    }
    value["case_manifest_fingerprint"] = _fingerprint(
        "isar.eig4_slice8_case_manifest_fingerprint/o36.8",
        value,
        "case_manifest_fingerprint",
    )
    return value


def validate_case_manifest(value: Mapping[str, Any], gate: Mapping[str, Any]) -> Mapping[str, Any]:
    validate_gate_manifest(gate)
    _exact(value, {
        "contract", "gate_manifest_fingerprint", "fixture_definitions_fingerprint",
        "cases", "faults", "case_manifest_fingerprint",
    }, "case manifest")
    if value["contract"] != CASE_MANIFEST_CONTRACT:
        raise GateEvidenceError("case manifest contract is unsupported")
    if not isinstance(value["cases"], list):
        raise GateEvidenceError("case manifest cases must be a list")
    fixtures = {
        "contract": FIXTURE_DEFINITIONS_CONTRACT,
        "cases": [item.get("fixture_definition") for item in value["cases"]
                  if isinstance(item, Mapping)],
        "fixture_definitions_fingerprint": value["fixture_definitions_fingerprint"],
    }
    expected = build_case_manifest(gate, fixtures)
    if canonical_json(value) != canonical_json(expected):
        raise GateEvidenceError("case manifest does not match the frozen Slice 8 cases")
    return value


def serialize_eligibility_input(value: EligibilityInput) -> dict[str, Any]:
    if not isinstance(value, EligibilityInput):
        raise TypeError("value must be an EligibilityInput")
    result = evaluate_eig_eligibility(value)
    return {
        "contract": ELIGIBILITY_SOURCE_CONTRACT,
        "action_intent": thaw(value.action_intent),
        "action_provenance": thaw(value.action_provenance),
        "target_outcome": thaw(value.target_outcome),
        "selection_record": thaw(value.selection_record),
        "qoi_evidence": None if value.qoi_evidence is None else value.qoi_evidence.to_dict(),
        "shadow_record": thaw(value.shadow_record),
        "scan_design_provenance": thaw(value.scan_design_provenance),
        "scan_design_events": list(value.scan_design_events),
        "eligibility_summary": eligibility_summary(result),
    }


def _qoi_from_dict(value: Mapping[str, Any]) -> QoIProjectionEvidence:
    fields = {
        "contract", "adapter_revision", "qoi_id", "qoi_unit", "source_qoi", "status",
        "scientific_codes", "observability_codes", "predictive_basis_fingerprint",
        "predictive_retained_set_fingerprint", "predictive_statistical_semantics_fingerprint",
        "predictive_source_model_selection_fingerprint", "target_projection_set_fingerprint",
        "projection_statistical_semantics_fingerprint", "projection_authority",
        "projection_authority_fingerprint", "qoi_evidence_fingerprint",
        "source_authority_scopes", "authority_scopes", "diagnostics", "models",
    }
    _exact(value, fields, "QoI evidence")
    models = value["models"]
    if not isinstance(models, list):
        raise GateEvidenceError("QoI evidence models are invalid")
    normalized = []
    for model in models:
        _exact(model, {
            "label", "identity_label", "weight", "source_weight", "draw_basis",
            "draw_weighting", "draw_mass_retained", "draw_weights",
            "target_present_by_draw", "target_qoi_draws",
        }, "QoI model")
        normalized.append(_QoIModel(
            label=model["label"], identity_label=model["identity_label"],
            weight=model["weight"], source_weight=model["source_weight"],
            draw_basis=model["draw_basis"], draw_weighting=model["draw_weighting"],
            draw_mass_retained=model["draw_mass_retained"],
            draw_weights=tuple(model["draw_weights"]),
            target_present=tuple(model["target_present_by_draw"]),
            qoi_draws=tuple(model["target_qoi_draws"]),
        ))
    evidence = QoIProjectionEvidence(
        contract=value["contract"], adapter_revision=value["adapter_revision"],
        qoi_id=value["qoi_id"], qoi_unit=value["qoi_unit"], source_qoi=value["source_qoi"],
        status=value["status"], scientific_codes=tuple(value["scientific_codes"]),
        observability_codes=tuple(value["observability_codes"]),
        predictive_basis_fingerprint=value["predictive_basis_fingerprint"],
        predictive_retained_set_fingerprint=value["predictive_retained_set_fingerprint"],
        predictive_statistical_semantics_fingerprint=(
            value["predictive_statistical_semantics_fingerprint"]),
        predictive_source_model_selection_fingerprint=(
            value["predictive_source_model_selection_fingerprint"]),
        target_projection_set_fingerprint=value["target_projection_set_fingerprint"],
        projection_statistical_semantics_fingerprint=(
            value["projection_statistical_semantics_fingerprint"]),
        projection_authority=tuple(sorted(value["projection_authority"].items())),
        projection_authority_fingerprint=value["projection_authority_fingerprint"],
        qoi_evidence_fingerprint=value["qoi_evidence_fingerprint"],
        source_authority_scopes=tuple(sorted(value["source_authority_scopes"].items())),
        authority_scopes=tuple(sorted(value["authority_scopes"].items())),
        diagnostics=tuple(sorted(value["diagnostics"].items())),
        _models=tuple(normalized),
    )
    return validate_qoi_projection_evidence(evidence)


def deserialize_eligibility_input(value: Mapping[str, Any]) -> tuple[EligibilityInput, Mapping[str, Any]]:
    _exact(value, {
        "contract", "action_intent", "action_provenance", "target_outcome",
        "selection_record", "qoi_evidence", "shadow_record", "scan_design_provenance",
        "scan_design_events", "eligibility_summary",
    }, "eligibility source")
    if value["contract"] != ELIGIBILITY_SOURCE_CONTRACT:
        raise GateEvidenceError("eligibility source contract is unsupported")
    qoi = None if value["qoi_evidence"] is None else _qoi_from_dict(value["qoi_evidence"])
    source = EligibilityInput.from_current_contract(
        action_intent=value["action_intent"], action_provenance=value["action_provenance"],
        target_outcome=value["target_outcome"], selection_record=value["selection_record"],
        qoi_evidence=qoi, shadow_record=value["shadow_record"],
        scan_design_provenance=value["scan_design_provenance"],
        scan_design_events=value["scan_design_events"],
    )
    result = evaluate_eig_eligibility(source)
    summary = validate_eligibility_summary(value["eligibility_summary"])
    if canonical_json(eligibility_summary(result)) != canonical_json(summary):
        raise GateEvidenceError("eligibility source does not reproduce its summary")
    return source, summary


def _safe_artifact_root(bundle: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise GateEvidenceError("case artifact root is invalid")
    root = bundle.resolve()
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise GateEvidenceError("case artifact root escapes the retained bundle") from error
    return candidate


def _score_set_payload(comparison: Any, inventory: Any, variant: Any) -> tuple[
        Mapping[str, Any], str, str]:
    if variant.score_set is not None:
        return (thaw(variant.score_set),
                comparison.realized_pair.legacy.realized_request_fingerprint,
                comparison.realized_pair.target_core.realized_request_fingerprint)
    source = next((item for item in inventory.shadow_scoring.variants
                   if item.policy_id == variant.policy_id
                   and item.variant_id == variant.variant_id), None)
    if source is None or source.score_set is None:
        raise GateEvidenceError("complete primary variant has no authenticated raw score set")
    pair = inventory.shadow_scoring.candidate_pair
    return (thaw(source.score_set), pair["legacy"]["candidate_id"],
            pair["target_core"]["candidate_id"])


def _paired_blocks(score_set: Mapping[str, Any], legacy_id: str, target_id: str) -> tuple[float, ...]:
    comparisons = score_set.get("pairwise_comparisons")
    if not isinstance(comparisons, list):
        raise GateEvidenceError("score set has no pairwise comparisons")
    record = next((item for item in comparisons if isinstance(item, Mapping)
                   and {item.get("candidate_a_id"), item.get("candidate_b_id")}
                   == {legacy_id, target_id}), None)
    if record is None:
        raise GateEvidenceError("score set has no legacy/target paired comparison")
    raw = record.get("paired_block_deltas_per_executed_point_nats")
    if not isinstance(raw, list) or len(raw) < 2:
        raise GateEvidenceError("paired per-point block evidence is missing")
    sign = 1.0 if record.get("candidate_b_id") == target_id else -1.0
    blocks = tuple(sign * float(item) for item in raw)
    if any(not math.isfinite(item) for item in blocks):
        raise GateEvidenceError("paired per-point block evidence is nonfinite")
    return blocks


def _paired_summary(blocks: Sequence[float]) -> tuple[float, float, float]:
    count = len(blocks)
    delta = math.fsum(blocks) / count
    variance = math.fsum((item - delta) ** 2 for item in blocks) / (count - 1)
    standard_error = math.sqrt(variance) / math.sqrt(count)
    return delta, standard_error, delta - MARGIN_MULTIPLIER * standard_error


def _classify_eligible(case: Mapping[str, Any], bundle: Path,
                       gate: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    required = {
        "artifact_root", "realized_comparison_ref", "applicability", "executed_grid_status",
        "submission_provenance", "paired_block_deltas_per_executed_point_nats",
        "analysis_outcome",
    }
    if any(name not in case for name in required):
        raise GateEvidenceError("eligible case lacks branch-required retained evidence")
    if case["applicability"] == "invalidated":
        return OUTCOME_NON_EIG, {"reason": "eig_realization_invalidated_by_recovery"}
    if case["applicability"] != "applicable":
        raise GateEvidenceError("eligible case applicability is unavailable")
    if case["executed_grid_status"] != "matched":
        raise GateEvidenceError("eligible case executed grid is unavailable")

    artifact_root = _safe_artifact_root(bundle, case["artifact_root"])
    comparison = load_scan_design_realized_comparison(
        case["realized_comparison_ref"], artifact_root)
    inventory = load_scan_design_inventory(comparison.source_inventory_ref, artifact_root)
    expected_config = gate["runtime_scoring_configuration"]
    if (inventory.shadow_scoring is None
            or canonical_json(thaw(inventory.shadow_scoring.scoring_config))
            != canonical_json(expected_config)
            or comparison.source_scoring_config_fingerprint
            != expected_config["scoring_config_fingerprint"]):
        raise GateEvidenceError("realized comparison does not use the frozen runtime scoring config")
    target = comparison.realized_pair.target_core
    if not isinstance(target, RealizedScanRequest):
        return OUTCOME_LEGACY, {"reason": "target_core_realization_refused"}
    primary = next((item for item in comparison.variants
                    if item.policy_id == PRIMARY_POLICY_ID
                    and item.variant_id == PRIMARY_VARIANT_ID), None)
    if primary is None or primary.status != "complete":
        raise GateEvidenceError("primary realized variant is unavailable")
    partition = thaw(primary.compiled_partition)
    if (not isinstance(partition, Mapping)
            or partition.get("qoi_id") != "excitation.energy"
            or partition.get("qoi_unit") != "meV"
            or partition.get("origin") != 0.0
            or partition.get("bin_width") != 0.5
            or partition.get("offsets") != [0.0, 0.25]):
        raise GateEvidenceError("primary realized partition is not the frozen two-offset variant")

    score_set, legacy_id, target_id = _score_set_payload(comparison, inventory, primary)
    blocks = _paired_blocks(score_set, legacy_id, target_id)
    retained_blocks = case["paired_block_deltas_per_executed_point_nats"]
    if (not isinstance(retained_blocks, list)
            or canonical_json(retained_blocks) != canonical_json(list(blocks))):
        raise GateEvidenceError("retained paired blocks do not reproduce from strict score evidence")
    delta, standard_error, lower = _paired_summary(blocks)
    retained_delta = primary.target_core_minus_legacy
    if (not isinstance(retained_delta, Mapping)
            or not math.isclose(delta, float(retained_delta[
                "delta_information_per_executed_point_nats"]), rel_tol=1e-12, abs_tol=1e-12)
            or not math.isclose(standard_error, float(retained_delta[
                "delta_information_per_executed_point_standard_error_nats"]),
                rel_tol=1e-12, abs_tol=1e-12)):
        raise GateEvidenceError("paired block calculation does not reproduce realized summary")

    legacy = comparison.realized_pair.legacy
    submission = case["submission_provenance"]
    if (not isinstance(submission, Mapping)
            or submission.get("scan_design_realized_comparison_fingerprint")
            != comparison.realized_comparison_fingerprint
            or submission.get("scan_design_realized_selected_request_fingerprint")
            != comparison.selected_request_fingerprint
            or submission.get("scan_design_realized_selected_grid_fingerprint")
            != comparison.selected_grid_fingerprint):
        raise GateEvidenceError("submission provenance does not authenticate the realized comparison")

    target_wire = TAVI_GRID_ADAPTER.wire_window(
        target.e_lo, target.e_hi, target.e_step)
    legacy_wire = TAVI_GRID_ADAPTER.wire_window(
        legacy.e_lo, legacy.e_hi, legacy.e_step)
    target_wire_bounds = sorted(target_wire[:2])
    legacy_wire_bounds = sorted(legacy_wire[:2])
    request_contained = (
        target_wire_bounds[0] >= legacy_wire_bounds[0]
        and target_wire_bounds[1] <= legacy_wire_bounds[1])
    executed_contained = (
        min(target.executed_energies) >= min(legacy.executed_energies)
        and max(target.executed_energies) <= max(legacy.executed_energies))
    target_span = max(target.executed_energies) - min(target.executed_energies)
    legacy_span = max(legacy.executed_energies) - min(legacy.executed_energies)
    structural = {
        "wire_request_contained": request_contained,
        "executed_support_contained": executed_contained,
        "executed_span_strictly_smaller": target_span < legacy_span,
        "executed_points_strictly_fewer": (
            target.executed_point_count < legacy.executed_point_count),
        "per_point_ncount_equal": target.ncount == legacy.ncount,
    }
    diagnostics = {
        "delta_information_per_executed_point_nats": delta,
        "paired_standard_error_nats": standard_error,
        "lower_margin_nats": lower,
        "selection_margin_multiplier": gate["selection_margin_multiplier"],
        "legacy_total_information_nats": _candidate_information(score_set, legacy_id),
        "target_core_total_information_nats": _candidate_information(score_set, target_id),
        "legacy_executed_point_count": legacy.executed_point_count,
        "target_core_executed_point_count": target.executed_point_count,
        "structural_conditions": structural,
    }
    wins = all(structural.values()) and lower > 0.0
    return (OUTCOME_TARGET if wins else OUTCOME_LEGACY), diagnostics


def _candidate_information(score_set: Mapping[str, Any], candidate_id: str) -> float:
    scores = score_set.get("candidate_scores")
    if not isinstance(scores, list):
        raise GateEvidenceError("score set candidate scores are missing")
    score = next((item for item in scores if isinstance(item, Mapping)
                  and item.get("candidate_id") == candidate_id), None)
    if score is None:
        raise GateEvidenceError("score set candidate information is missing")
    value = float(score["conditional_target_qoi_information_nats"])
    if not math.isfinite(value):
        raise GateEvidenceError("candidate information is nonfinite")
    return value


def evaluate_case(case: Mapping[str, Any], definition: Mapping[str, Any], bundle: Path,
                  gate: Mapping[str, Any]) -> dict[str, Any]:
    common_fields = {
        "contract", "case_id", "case_generation_seed", "classification",
        "fixture_definition_fingerprint", "eligibility_source", "artifact_root",
        "realized_comparison_ref", "applicability",
        "executed_grid_status", "submission_provenance",
        "paired_block_deltas_per_executed_point_nats", "analysis_outcome",
        "analysis_evidence",
        "case_evidence_fingerprint",
    }
    if not isinstance(case, Mapping) or set(case) - common_fields:
        raise GateEvidenceError("case evidence fields are invalid")
    for required in ("contract", "case_id", "case_generation_seed", "classification",
                     "fixture_definition_fingerprint", "eligibility_source",
                     "case_evidence_fingerprint"):
        if required not in case:
            raise GateEvidenceError("case evidence is incomplete")
    if (case["contract"] != CASE_EVIDENCE_CONTRACT
            or case["case_id"] != definition["case_id"]
            or case["case_generation_seed"] != definition["case_generation_seed"]
            or case["classification"] != definition["classification"]
            or case["fixture_definition_fingerprint"] != fixture_definition_fingerprint(
                definition["fixture_definition"])):
        raise GateEvidenceError("case evidence identity does not match its frozen definition")
    expected_fp = _fingerprint(
        "isar.eig4_slice8_case_evidence_fingerprint/o36.8",
        case,
        "case_evidence_fingerprint",
    )
    if case["case_evidence_fingerprint"] != expected_fp:
        raise GateEvidenceError("case evidence fingerprint is invalid")
    _source, summary = deserialize_eligibility_input(case["eligibility_source"])
    status = summary["status"]
    if status != definition["expected_eligibility"]:
        raise GateEvidenceError("case eligibility does not match its frozen classification")
    code = definition["required_eligibility_code"]
    if code is not None and code not in summary["scientific_codes"]:
        raise GateEvidenceError("case eligibility lacks its frozen reason code")
    if status in {"ineligible", "unassessable"}:
        outcome, diagnostics = OUTCOME_NON_EIG, {
            "eligibility_status": status,
            "scientific_codes": list(summary["scientific_codes"]),
            "observability_codes": list(summary["observability_codes"]),
        }
    else:
        try:
            outcome, diagnostics = _classify_eligible(case, bundle, gate)
        except (GateEvidenceError, KeyError, TypeError, ValueError, OSError) as error:
            log.error("eligible Slice 8 case %s is unavailable: %s", case["case_id"], error)
            outcome, diagnostics = OUTCOME_UNAVAILABLE, {
                "unavailability_reason": type(error).__name__, "message": str(error)}
    return {
        "case_id": case["case_id"],
        "expected_outcome": definition["expected_outcome"],
        "outcome": outcome,
        "matches_expected": outcome == definition["expected_outcome"],
        "diagnostics": diagnostics,
    }


def evaluate_fault(fault: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, Any]:
    _exact(fault, {
        "contract", "fault_id", "source_case_id", "executed_grid_status", "reason_code",
        "ordinary_shadow_analysis_continued", "fault_evidence_fingerprint",
    }, "fault evidence")
    expected_fp = _fingerprint(
        "isar.eig4_slice8_fault_evidence_fingerprint/o36.8",
        fault,
        "fault_evidence_fingerprint",
    )
    if (fault["contract"] != FAULT_EVIDENCE_CONTRACT
            or fault["fault_id"] != definition["fault_id"]
            or fault["source_case_id"] != definition["source_case_id"]
            or fault["fault_evidence_fingerprint"] != expected_fp):
        raise GateEvidenceError("fault evidence identity is invalid")
    outcome = (OUTCOME_UNAVAILABLE if (
        fault["executed_grid_status"] == "mismatch"
        and fault["reason_code"] == definition["required_reason_code"]
        and fault["ordinary_shadow_analysis_continued"] is True)
        else OUTCOME_LEGACY)
    return {
        "fault_id": fault["fault_id"], "expected_outcome": definition["expected_outcome"],
        "outcome": outcome, "matches_expected": outcome == definition["expected_outcome"],
    }


def validate_case_results(value: Mapping[str, Any], gate: Mapping[str, Any],
                          cases: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact(value, {
        "contract", "gate_manifest_fingerprint", "case_manifest_fingerprint",
        "implementation_anchor", "evidence_generation_anchor", "cases", "faults",
        "runtime_invariants", "verification", "case_results_fingerprint",
    }, "case results")
    if (value["contract"] != CASE_RESULTS_CONTRACT
            or value["gate_manifest_fingerprint"] != gate["gate_manifest_fingerprint"]
            or value["case_manifest_fingerprint"] != cases["case_manifest_fingerprint"]
            or value["implementation_anchor"] != gate["implementation_anchor"]
            or not _valid_commit(value["evidence_generation_anchor"])
            or not isinstance(value["cases"], list) or not isinstance(value["faults"], list)
            or not isinstance(value["runtime_invariants"], Mapping)
            or not isinstance(value["verification"], Mapping)):
        raise GateEvidenceError("case results metadata is invalid")
    expected = _fingerprint(
        "isar.eig4_slice8_case_results_fingerprint/o36.8",
        value,
        "case_results_fingerprint",
    )
    if value["case_results_fingerprint"] != expected:
        raise GateEvidenceError("case results fingerprint is invalid")
    return value


def evaluate_bundle(bundle: str | Path) -> dict[str, Any]:
    root = Path(bundle)
    gate = validate_gate_manifest(_read_json(root / "gate-manifest.json"))
    cases = validate_case_manifest(_read_json(root / "case-manifest.json"), gate)
    raw = validate_case_results(_read_json(root / "case-results.json"), gate, cases)
    by_case = {item.get("case_id"): item for item in raw["cases"]
               if isinstance(item, Mapping)}
    if set(by_case) != {item["case_id"] for item in CASE_DEFINITIONS}:
        raise GateEvidenceError("case results do not cover the frozen case set exactly")
    by_fault = {item.get("fault_id"): item for item in raw["faults"]
                if isinstance(item, Mapping)}
    if set(by_fault) != {item["fault_id"] for item in FAULT_DEFINITIONS}:
        raise GateEvidenceError("case results do not cover the frozen fault set exactly")

    results = [evaluate_case(by_case[item["case_id"]], item, root, gate)
               for item in cases["cases"]]
    fault_results = [evaluate_fault(by_fault[item["fault_id"]], item)
                     for item in FAULT_DEFINITIONS]
    invariants = raw["runtime_invariants"]
    invariant_pass = (set(invariants) == set(REQUIRED_RUNTIME_INVARIANTS)
                      and all(invariants.get(name) is True
                              for name in REQUIRED_RUNTIME_INVARIANTS))
    verification = raw["verification"]
    verification_pass = (verification.get("focused_tests") == "passed"
                         and verification.get("normative_predictive_fixture") == "passed"
                         and verification.get("live_tavi_case_1") == "passed")
    strong = next(item for item in results if item["case_id"] == "s8-c01-strong-confirmed")
    weak = next(item for item in results
                if item["case_id"] == "s8-c02-weak-unresolved-confirmed")
    strong_case = by_case[strong["case_id"]]
    strong_analysis = strong_case.get("analysis_outcome")
    strong_evidence = strong_case.get("analysis_evidence")
    strong_rejoin = (isinstance(strong_analysis, Mapping)
                     and isinstance(strong_evidence, Mapping)
                     and strong_evidence.get("analysis_outcome") == strong_analysis
                     and strong_evidence.get("ordinary_analysis_command") == "isar run"
                     and strong_evidence.get("source_workspace_fingerprint_before")
                     == strong_evidence.get("source_workspace_fingerprint_after")
                     and isinstance(strong_evidence.get("result_rows"), list)
                     and bool(strong_evidence["result_rows"])
                     and all(strong_analysis.get(name) is True for name in (
                         "normal_ingestion", "normal_reduction", "normal_fit",
                         "pooled_master_updated", "usable_target_delivery",
                         "no_eig_specific_fit_route", "original_workspace_unchanged")))
    weak_diagnostics = weak["diagnostics"]
    weak_uncertainty = (
        isinstance(weak_diagnostics, Mapping)
        and weak_diagnostics.get("delta_information_per_executed_point_nats", -math.inf) > 0.0
        and weak_diagnostics.get("lower_margin_nats", math.inf) <= 0.0)
    passed = (all(item["matches_expected"] for item in results)
              and all(item["matches_expected"] for item in fault_results)
              and invariant_pass and verification_pass and strong_rejoin and weak_uncertainty)
    disposition = "passed" if passed else "failed"
    result: dict[str, Any] = {
        "contract": GATE_RESULT_CONTRACT,
        "gate_manifest_fingerprint": gate["gate_manifest_fingerprint"],
        "case_manifest_fingerprint": cases["case_manifest_fingerprint"],
        "case_results_fingerprint": raw["case_results_fingerprint"],
        "implementation_anchor": gate["implementation_anchor"],
        "evidence_generation_anchor": raw["evidence_generation_anchor"],
        "selection_margin_multiplier": MARGIN_MULTIPLIER,
        "selection_margin_status": "pragmatic_engineering_margin_not_coverage_claim",
        "cases": results,
        "faults": fault_results,
        "runtime_invariants_passed": invariant_pass,
        "verification_passed": verification_pass,
        "strong_case_fit_rejoin_passed": strong_rejoin,
        "weak_case_uncertainty_condition_passed": weak_uncertainty,
        "positive_envelope_case_count": sum(
            item["outcome"] == OUTCOME_TARGET for item in results),
        "overall_disposition": disposition,
        "gate_result_fingerprint": "",
    }
    result["gate_result_fingerprint"] = _fingerprint(
        "isar.eig4_slice8_gate_result_fingerprint/o36.8",
        result,
        "gate_result_fingerprint",
    )
    return result


def _git_text(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return completed.stdout.strip()


def freeze_manifests(output: Path, implementation_anchor: str,
                     fixture_definitions_path: Path) -> None:
    head = _git_text("rev-parse", "HEAD")
    status = _git_text("status", "--porcelain")
    if head != implementation_anchor:
        raise GateEvidenceError("HEAD does not equal the requested implementation anchor")
    if status:
        raise GateEvidenceError("working tree must be clean before Slice 8 manifests are frozen")
    gate = build_gate_manifest(implementation_anchor)
    fixture_definitions = json.loads(
        fixture_definitions_path.read_text(encoding="utf-8"))
    if not isinstance(fixture_definitions, Mapping):
        raise GateEvidenceError("fixture definitions artifact must contain an object")
    cases = build_case_manifest(gate, fixture_definitions)
    _write_json(output / "gate-manifest.json", gate)
    _write_json(output / "case-manifest.json", cases)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="write the frozen gate and case manifests")
    freeze.add_argument("--implementation-anchor", required=True)
    freeze.add_argument("--fixture-definitions", type=Path, required=True)
    freeze.add_argument("--output-dir", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="evaluate a self-contained retained bundle")
    evaluate.add_argument("--bundle", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="reproduce and compare retained gate-result bytes")
    verify.add_argument("--bundle", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "freeze":
            freeze_manifests(
                args.output_dir, args.implementation_anchor, args.fixture_definitions)
            return 0
        result = evaluate_bundle(args.bundle)
        if args.command == "evaluate":
            _write_json(args.output, result)
            return 0
        retained = (args.bundle / "gate-result.json").read_bytes()
        reproduced = _canonical_bytes(result)
        if retained != reproduced:
            raise GateEvidenceError("retained gate-result bytes do not reproduce")
        return 0
    except (GateEvidenceError, OSError, subprocess.CalledProcessError, ValueError) as error:
        log.error("Slice 8 gate %s failed: %s", args.command, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

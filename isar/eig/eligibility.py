"""Pure O36 eligibility policy for same-action shadow scan-window EIG."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

from isar.drive.acquisition.inventory import canonical_json, canonical_hkl
from isar.schema.statistics import (
    SemanticsConstructionError,
    validate_fit_selection_semantics,
    validate_shadow_model_selection,
)

from .qoi import QoIProjectionEvidence


ELIGIBILITY_CONTRACT = "isar.eig_eligibility/o36.1"

SCIENTIFIC_REASON_ORDER = (
    "target_delivery_ineligible", "target_not_finite", "target_not_confirmed",
    "action_not_canonical_target", "modality_fork", "modality_not_fold", "pool_inconsistent",
    "selection_ineligible", "selection_constrained", "selection_rescued",
    "selection_comparison_incomplete", "predictive_material_nonbootstrap_mass",
    "projection_incomplete", "projection_stale", "projection_join_mismatch",
    "qoi_materially_unavailable", "shadow_disagrees", "shadow_comparison_invalid",
    "recovery_or_missed_window_state",
)
OBSERVABILITY_REASON_ORDER = (
    "target_delivery_unknown", "target_identity_unknown", "action_context_unknown", "modality_unknown",
    "pool_consistency_unknown", "selection_record_missing", "selection_record_invalid",
    "predictive_record_missing", "predictive_record_invalid", "predictive_material_basis_unknown",
    "predictive_material_retention_unknown", "projection_record_missing", "projection_record_invalid",
    "projection_currentness_unknown", "projection_join_unknown", "shadow_record_missing",
    "shadow_record_invalid", "scan_design_provenance_missing", "scan_design_provenance_invalid",
)
_SCIENCE_RANK = {code: index for index, code in enumerate(SCIENTIFIC_REASON_ORDER)}
_GAP_RANK = {code: index for index, code in enumerate(OBSERVABILITY_REASON_ORDER)}
_ELIGIBLE_SCOPES = {
    "eig_shadow": "candidate",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_INELIGIBLE_SCOPES = {name: "ineligible" for name in _ELIGIBLE_SCOPES}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return None
    fields = getattr(value, "__dataclass_fields__", None)
    if fields:
        return {name: getattr(value, name) for name in fields}
    return None


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(_thaw(value)).encode("utf-8")).hexdigest()


def _ordered(codes: list[str], ranks: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(sorted(set(codes), key=lambda code: (ranks.get(code, len(ranks)), code)))


@dataclass(frozen=True)
class EligibilityInput:
    """Immutable current-contract values consumed by :func:`evaluate_eig_eligibility`."""

    action_intent: Mapping[str, Any] | None
    action_provenance: Mapping[str, Any] | None
    target_outcome: Mapping[str, Any] | None
    selection_record: Mapping[str, Any] | None
    qoi_evidence: QoIProjectionEvidence | None
    shadow_record: Mapping[str, Any] | None
    scan_design_events: tuple[str, ...] = ()

    @classmethod
    def from_current_contract(cls, *, action_intent: Any, action_provenance: Any,
                              target_outcome: Any, selection_record: Any,
                              qoi_evidence: QoIProjectionEvidence | None,
                              shadow_record: Any, scan_design_events: Any = ()) -> "EligibilityInput":
        return cls(
            action_intent=_freeze(_mapping(action_intent)) if _mapping(action_intent) is not None else None,
            action_provenance=_freeze(_mapping(action_provenance)) if _mapping(action_provenance) is not None else None,
            target_outcome=_freeze(_mapping(target_outcome)) if _mapping(target_outcome) is not None else None,
            selection_record=_freeze(_mapping(selection_record)) if _mapping(selection_record) is not None else None,
            qoi_evidence=qoi_evidence,
            shadow_record=_freeze(_mapping(shadow_record)) if _mapping(shadow_record) is not None else None,
            scan_design_events=tuple(str(item) for item in (scan_design_events or ())),
        )


@dataclass(frozen=True)
class EligibilityResult:
    contract: str
    status: str
    scientific_codes: tuple[str, ...]
    observability_codes: tuple[str, ...]
    primary_scientific_code: str | None
    primary_observability_code: str | None
    authority_scopes: tuple[tuple[str, str], ...]
    input_fingerprint: str
    result_fingerprint: str
    diagnostics: tuple[tuple[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "status": self.status,
            "scientific_codes": list(self.scientific_codes),
            "observability_codes": list(self.observability_codes),
            "primary_scientific_code": self.primary_scientific_code,
            "primary_observability_code": self.primary_observability_code,
            "authority_scopes": dict(self.authority_scopes),
            "input_fingerprint": self.input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "diagnostics": dict(self.diagnostics),
        }


def _target_checks(value: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> None:
    if not isinstance(value, Mapping):
        gaps.extend(("target_delivery_unknown", "target_identity_unknown", "pool_consistency_unknown"))
        return
    delivery = value.get("target_delivery_eligible")
    if delivery is not True and delivery is not False:
        gaps.append("target_delivery_unknown")
    elif delivery is False:
        science.append("target_delivery_ineligible")
    if value.get("pooled") is not True:
        science.append("target_delivery_ineligible")
    if not _finite(value.get("omega0")):
        science.append("target_not_finite")
    track = value.get("track_state")
    candidate = value.get("candidate_state")
    if track is None or candidate is None or value.get("candidate_id") is None or value.get("selected_candidate_index") is None:
        gaps.append("target_identity_unknown")
    elif track != "confirmed" or candidate != "confirmed":
        science.append("target_not_confirmed")
    modality = value.get("modality")
    if modality is None:
        gaps.append("modality_unknown")
    elif modality == "fork":
        science.append("modality_fork")
    elif modality != "fold":
        science.append("modality_not_fold")
    pool = value.get("pool_consistent")
    if pool is None:
        gaps.append("pool_consistency_unknown")
    elif pool is False:
        science.append("pool_inconsistent")


def _action_checks(intent: Mapping[str, Any] | None, provenance: Mapping[str, Any] | None,
                   target: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> None:
    if not isinstance(intent, Mapping) or not isinstance(provenance, Mapping):
        gaps.append("action_context_unknown")
        return
    action_class = intent.get("action_class")
    if provenance.get("action_class") not in (None, action_class):
        gaps.append("action_context_unknown")
        return
    intent_sink = intent.get("evidence_sink")
    provenance_sink = provenance.get("evidence_sink")
    if intent_sink is not None and provenance_sink is not None and intent_sink != provenance_sink:
        gaps.append("action_context_unknown")
        return
    required = (
        intent.get("scientific_action_fingerprint") or provenance.get("scientific_action_fingerprint"),
        action_class,
        intent.get("hkl"),
        provenance.get("acquisition_action_role"),
        intent_sink if intent_sink is not None else provenance_sink,
    )
    if any(item is None for item in required):
        gaps.append("action_context_unknown")
        return
    if not isinstance(provenance.get("driver_inserted"), bool):
        gaps.append("action_context_unknown")
        return
    try:
        same_q = canonical_hkl(intent["hkl"]) == canonical_hkl(target.get("hkl", target.get("target_hkl")))
    except (TypeError, ValueError):
        gaps.append("action_context_unknown")
        return
    if action_class not in {"production", "confirm"}:
        science.append("action_not_canonical_target")
    if provenance.get("acquisition_action_role") != "production":
        science.append("action_not_canonical_target")
    if (intent_sink if intent_sink is not None else provenance_sink) != "canonical_master":
        science.append("action_not_canonical_target")
    if provenance.get("driver_inserted") or provenance.get("insertion_purpose") is not None:
        science.append("action_not_canonical_target")
    if not same_q:
        science.append("action_not_canonical_target")
    expected_revision = target.get("master_revision") if isinstance(target, Mapping) else None
    action_revision = provenance.get("authoritative_evidence_revision")
    if expected_revision is None or action_revision is None or str(expected_revision) != str(action_revision):
        gaps.append("action_context_unknown")


def _selection_checks(record: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> None:
    if record is None:
        gaps.append("selection_record_missing")
        return
    try:
        # Schema readers intentionally work on ordinary JSON containers.  The
        # public input is frozen, so thaw only for validation; no caller-owned
        # object is exposed for mutation.
        validated = validate_fit_selection_semantics(_thaw(record))
    except (SemanticsConstructionError, TypeError, ValueError, KeyError, OverflowError):
        gaps.append("selection_record_invalid")
        return
    if validated.get("model_weight_kind") != "akaike_relative_weight" or validated.get("selection_eligibility") != "eligible":
        science.append("selection_ineligible")
    candidates = validated.get("candidate_records")
    if not isinstance(candidates, list) or not candidates:
        science.append("selection_comparison_incomplete")
        return
    for candidate in candidates:
        semantics = candidate.get("fit_semantics") if isinstance(candidate, Mapping) else None
        if not isinstance(semantics, Mapping):
            science.append("selection_comparison_incomplete")
            continue
        if semantics.get("estimator_kind") != "data_likelihood_mle":
            science.append("selection_ineligible")
        if semantics.get("selection_eligibility") != "eligible":
            science.append("selection_ineligible")
        if semantics.get("effective_constraints"):
            science.append("selection_constrained")
        if any(item.get("origin") is not None for item in semantics.get("effective_constraints") or ()
               if isinstance(item, Mapping)):
            science.append("selection_constrained")
        reasons = semantics.get("selection_ineligibility_reasons") or ()
        if any("rescue" in str(reason) or "post_selection" in str(reason) for reason in reasons):
            science.append("selection_rescued")
    route = validated.get("candidate_generation_route")
    if not isinstance(route, Mapping):
        science.append("selection_comparison_incomplete")
        return
    # The persisted producer uses ``route`` while the read-only census exposes
    # its normalized ``route_kind`` diagnostic.  They carry the same contract
    # value; accepting both keeps this pure seam grounded in either caller.
    route_kind = route.get("route_kind", route.get("route"))
    if route_kind not in {"standard_two_stage", "full_two_stage"}:
        science.append("selection_comparison_incomplete")
    offered = route.get("offered_labels")
    scored = route.get("scored_labels")
    if not isinstance(offered, list) or not isinstance(scored, list) or set(offered) != set(scored):
        science.append("selection_comparison_incomplete")
    before = route.get("labels_before_guideline")
    filtered = route.get("guideline_structure_filter")
    if isinstance(before, list) and isinstance(filtered, list) and set(before) != set(filtered):
        science.append("selection_comparison_incomplete")
    if route.get("structure_hint") is not None or route.get("guideline_changed") is True:
        science.append("selection_comparison_incomplete")


def _shadow_checks(record: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> None:
    if record is None:
        gaps.append("shadow_record_missing")
        return
    try:
        validated = validate_shadow_model_selection(_thaw(record))
    except (SemanticsConstructionError, TypeError, ValueError, KeyError, OverflowError):
        gaps.append("shadow_record_invalid")
        return
    if validated.get("schema") != "isar.shadow_model_selection/2":
        gaps.append("shadow_record_invalid")
        return
    status = validated.get("selection_status")
    if status == "complete":
        if validated.get("selection_changed") is True:
            science.append("shadow_disagrees")
        return
    if status == "degenerate_single_candidate":
        science.append("shadow_comparison_invalid")
        return
    neutral_skip = (
        status == "unavailable"
        and validated.get("shadow_execution_kind") == "skipped"
        and validated.get("unavailable_reason_code") == "shadow_skipped_soft_deadline"
        and validated.get("runtime_increment_seconds") == 0.0
        and not validated.get("attempted_model_identities")
        and not validated.get("reused_model_identities")
        and not validated.get("freshly_fitted_model_identities")
    )
    if not neutral_skip:
        science.append("shadow_comparison_invalid")


def evaluate_eig_eligibility(value: EligibilityInput) -> EligibilityResult:
    """Apply the frozen O36 eligibility policy to immutable current-contract evidence."""
    science: list[str] = []
    gaps: list[str] = []
    _target_checks(value.target_outcome, science, gaps)
    _action_checks(value.action_intent, value.action_provenance, value.target_outcome, science, gaps)
    _selection_checks(value.selection_record, science, gaps)
    if value.qoi_evidence is None:
        gaps.append("projection_record_missing")
    else:
        science.extend(value.qoi_evidence.scientific_codes)
        gaps.extend(value.qoi_evidence.observability_codes)
    _shadow_checks(value.shadow_record, science, gaps)
    if "scan_design_provenance_missing" in value.scan_design_events:
        gaps.append("scan_design_provenance_missing")
    if "scan_design_provenance_invalid" in value.scan_design_events:
        gaps.append("scan_design_provenance_invalid")
    if any("recovery" in event or "missed" in event for event in value.scan_design_events):
        science.append("recovery_or_missed_window_state")
    scientific_codes = _ordered(science, _SCIENCE_RANK)
    observability_codes = _ordered(gaps, _GAP_RANK)
    status = "ineligible" if scientific_codes else "unassessable" if observability_codes else "eligible"
    scopes = _ELIGIBLE_SCOPES if status == "eligible" else _INELIGIBLE_SCOPES
    input_payload = {
        "contract": ELIGIBILITY_CONTRACT,
        "action_intent": value.action_intent,
        "action_provenance": value.action_provenance,
        "target_outcome": value.target_outcome,
        "selection_record": value.selection_record,
        "qoi_evidence": value.qoi_evidence.to_dict() if value.qoi_evidence is not None else None,
        "shadow_record": value.shadow_record,
        "scan_design_events": value.scan_design_events,
    }
    input_fingerprint = _fingerprint(input_payload)
    result_payload = {
        "contract": ELIGIBILITY_CONTRACT,
        "status": status,
        "scientific_codes": scientific_codes,
        "observability_codes": observability_codes,
        "input_fingerprint": input_fingerprint,
        "authority_scopes": scopes,
    }
    result_fingerprint = _fingerprint(result_payload)
    diagnostics = tuple(sorted((
        ("qoi_status", value.qoi_evidence.status if value.qoi_evidence is not None else "missing"),
        ("scan_design_event_count", len(value.scan_design_events)),
    )))
    return EligibilityResult(
        contract=ELIGIBILITY_CONTRACT,
        status=status,
        scientific_codes=scientific_codes,
        observability_codes=observability_codes,
        primary_scientific_code=scientific_codes[0] if scientific_codes else None,
        primary_observability_code=observability_codes[0] if observability_codes else None,
        authority_scopes=tuple(sorted(scopes.items())),
        input_fingerprint=input_fingerprint,
        result_fingerprint=result_fingerprint,
        diagnostics=diagnostics,
    )

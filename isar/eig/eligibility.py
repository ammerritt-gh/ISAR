"""Pure O36 eligibility policy for same-action shadow scan-window EIG."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
from types import MappingProxyType
from typing import Any, Mapping

from isar.schema.action_identity import (
    canonical_json,
    canonical_hkl,
    scientific_fingerprint_from_mapping,
)
from isar.schema.statistics import (
    SemanticsConstructionError,
    validate_fit_selection_semantics,
    validate_shadow_model_selection,
)

from .qoi import QoIProjectionEvidence, validate_qoi_projection_evidence


ELIGIBILITY_CONTRACT = "isar.eig_eligibility/o36.2.1"
log = logging.getLogger(__name__)

SCIENTIFIC_REASON_ORDER = (
    "target_delivery_ineligible", "target_not_finite", "target_not_confirmed",
    "action_not_canonical_target", "modality_fork", "pool_inconsistent",
    "selection_ineligible", "selection_constrained", "selection_rescued",
    # RETIRED, never emitted: one shared string across seven emission sites made the blocker
    # histogram bottom out, because two thirds of a campaign's decisions named a cause covering
    # seven unrelated conditions that could not be told apart.  It stays in the vocabulary at its
    # original rank so eligibility summaries persisted before the split still authenticate.
    "selection_comparison_incomplete",
    # The split, most severe first.
    "selection_no_candidates", "selection_candidate_semantics_missing",
    "selection_route_missing", "selection_route_kind_unsupported",
    "selection_scored_not_offered", "selection_labels_shifted_before_guideline",
    "selection_guideline_forced", "predictive_insufficient_bootstrap_mass",
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


def _result_payload(*, status: str, scientific_codes: tuple[str, ...] | list[str],
                    observability_codes: tuple[str, ...] | list[str],
                    input_fingerprint: str) -> dict[str, Any]:
    scopes = _ELIGIBLE_SCOPES if status == "eligible" else _INELIGIBLE_SCOPES
    return {
        "contract": ELIGIBILITY_CONTRACT, "status": status,
        "scientific_codes": list(scientific_codes),
        "observability_codes": list(observability_codes),
        "input_fingerprint": input_fingerprint, "authority_scopes": scopes,
    }


def eligibility_summary(value: "EligibilityResult") -> dict[str, Any]:
    """Return the compact authenticated eligibility record embedded by downstream evidence.

    Deliberately six fields and no diagnostics.  The design measure re-checks this envelope key
    for key and folds it into ``design_measure_fingerprint``, which seeds the scorer, so a
    seventh explanatory field here would move a number rather than explain one.  The diagnostics
    a reader needs -- ``target_modality`` above all -- ride on the ``eig_decision`` session-log
    event instead, which is diagnostic-only and fingerprints nothing.
    """
    return {
        "contract": value.contract, "status": value.status,
        "scientific_codes": list(value.scientific_codes),
        "observability_codes": list(value.observability_codes),
        "input_fingerprint": value.input_fingerprint,
        "result_fingerprint": value.result_fingerprint,
    }


def validate_eligibility_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Authenticate a compact summary with the producer's own ordering and fingerprint recipe."""
    expected = {"contract", "status", "scientific_codes", "observability_codes",
                "input_fingerprint", "result_fingerprint"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("EIG eligibility summary fields are invalid")
    science = value["scientific_codes"]
    gaps = value["observability_codes"]
    valid_fp = lambda item: (isinstance(item, str) and len(item) == 64
                             and item == item.lower()
                             and all(character in "0123456789abcdef" for character in item))
    if (value["contract"] != ELIGIBILITY_CONTRACT
            or not isinstance(science, list) or not isinstance(gaps, list)
            or science != list(_ordered(science, _SCIENCE_RANK))
            or gaps != list(_ordered(gaps, _GAP_RANK))
            or any(code not in _SCIENCE_RANK for code in science)
            or any(code not in _GAP_RANK for code in gaps)
            or not valid_fp(value["input_fingerprint"])
            or not valid_fp(value["result_fingerprint"])):
        raise ValueError("EIG eligibility summary is invalid")
    status = "ineligible" if science else "unassessable" if gaps else "eligible"
    if value["status"] != status:
        raise ValueError("EIG eligibility status is invalid")
    expected_fingerprint = _fingerprint(_result_payload(
        status=status, scientific_codes=science, observability_codes=gaps,
        input_fingerprint=value["input_fingerprint"]))
    if value["result_fingerprint"] != expected_fingerprint:
        raise ValueError("EIG eligibility result fingerprint is invalid")
    return value


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
    scan_design_provenance: Mapping[str, Any] | None = None
    scan_design_events: tuple[str, ...] = ()

    @classmethod
    def from_current_contract(cls, *, action_intent: Any, action_provenance: Any,
                              target_outcome: Any, selection_record: Any,
                              qoi_evidence: QoIProjectionEvidence | None,
                              shadow_record: Any, scan_design_provenance: Any = None,
                              scan_design_events: Any = ()) -> "EligibilityInput":
        return cls(
            action_intent=_freeze(_mapping(action_intent)) if _mapping(action_intent) is not None else None,
            action_provenance=_freeze(_mapping(action_provenance)) if _mapping(action_provenance) is not None else None,
            target_outcome=_freeze(_mapping(target_outcome)) if _mapping(target_outcome) is not None else None,
            selection_record=_freeze(_mapping(selection_record)) if _mapping(selection_record) is not None else None,
            qoi_evidence=qoi_evidence,
            shadow_record=_freeze(_mapping(shadow_record)) if _mapping(shadow_record) is not None else None,
            scan_design_provenance=(_freeze(_mapping(scan_design_provenance))
                                    if _mapping(scan_design_provenance) is not None else None),
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


def _target_checks(value: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> str | None:
    """Apply the target gates and return the observed modality for diagnostics."""
    if not isinstance(value, Mapping):
        gaps.extend(("target_delivery_unknown", "target_identity_unknown", "pool_consistency_unknown"))
        return None
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
    # Only a fork is a scientific rejection: it is the genuinely multi-branch
    # answer the same-action seam cannot score.  Every other modality — notably
    # ``unpooled`` for a confirmed single-member pooled source — is a
    # representation and history state, not a statement about the science, so it
    # is recorded diagnostically and gated on pool consistency alone.
    modality = value.get("modality")
    if modality is None:
        gaps.append("modality_unknown")
    elif modality == "fork":
        science.append("modality_fork")
    pool = value.get("pool_consistent")
    if pool is None:
        gaps.append("pool_consistency_unknown")
    elif pool is False:
        science.append("pool_inconsistent")
    return None if modality is None else str(modality)


def _target_hkl(target: Mapping[str, Any]) -> Any:
    """Read the one producer-owned target-Q alias order used by the EIG seam."""
    for name in ("hkl", "target_hkl", "q"):
        value = target.get(name)
        if value is not None:
            return value
    return None


def _action_checks(intent: Mapping[str, Any] | None, provenance: Mapping[str, Any] | None,
                   target: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> str | None:
    if not isinstance(intent, Mapping) or not isinstance(provenance, Mapping):
        gaps.append("action_context_unknown")
        return None
    if not isinstance(target, Mapping):
        gaps.append("action_context_unknown")
        return None
    try:
        recomputed_fingerprint = scientific_fingerprint_from_mapping(intent)
    except (TypeError, ValueError):
        gaps.append("action_context_unknown")
        recomputed_fingerprint = None
    inventory_fingerprint = intent.get("scientific_action_fingerprint")
    proposal_fingerprint = provenance.get(
        "proposal_scientific_action_fingerprint", provenance.get("scientific_action_fingerprint"))
    provenance_fingerprint = provenance.get("scientific_action_fingerprint")
    authenticated = (isinstance(recomputed_fingerprint, str)
                     and all(isinstance(item, str) and item for item in (
        inventory_fingerprint, proposal_fingerprint, provenance_fingerprint))
                     and all(item == recomputed_fingerprint for item in (
                         inventory_fingerprint, proposal_fingerprint, provenance_fingerprint)))
    if not authenticated:
        gaps.append("action_context_unknown")
    action_class = intent.get("action_class")
    if provenance.get("action_class") != action_class:
        gaps.append("action_context_unknown")
        authenticated = False
    intent_sink = intent.get("evidence_sink")
    provenance_sink = provenance.get("evidence_sink")
    if intent_sink != provenance_sink:
        gaps.append("action_context_unknown")
        authenticated = False
    required = (action_class, intent.get("hkl"), provenance.get("acquisition_action_role"), intent_sink)
    if any(item is None for item in required):
        gaps.append("action_context_unknown")
        return None
    if not isinstance(provenance.get("driver_inserted"), bool):
        gaps.append("action_context_unknown")
        return None
    try:
        same_q = canonical_hkl(intent["hkl"]) == canonical_hkl(_target_hkl(target))
        provenance_q = provenance.get("hkl", provenance.get("target_hkl", provenance.get("q")))
        if provenance_q is None or canonical_hkl(provenance_q) != canonical_hkl(intent["hkl"]):
            gaps.append("action_context_unknown")
            authenticated = False
    except (TypeError, ValueError):
        gaps.append("action_context_unknown")
        return None
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
        authenticated = False
    return recomputed_fingerprint if authenticated else None


def _selection_checks(record: Mapping[str, Any] | None, science: list[str], gaps: list[str]) -> Mapping[str, Any] | None:
    if record is None:
        gaps.append("selection_record_missing")
        return None
    try:
        # Schema readers intentionally work on ordinary JSON containers.  The
        # public input is frozen, so thaw only for validation; no caller-owned
        # object is exposed for mutation.
        validated = validate_fit_selection_semantics(_thaw(record))
    except (SemanticsConstructionError, TypeError, ValueError, KeyError, OverflowError):
        gaps.append("selection_record_invalid")
        return None
    # Fail closed: only the evidence lane may steer acquisition. A recorded AIC-fallback
    # selection (evidence degraded or absent) is exactly the record that must not drive
    # beam time, so it reads as scientifically ineligible rather than being grandfathered.
    if validated.get("model_weight_kind") != "posterior_probability" or validated.get("selection_eligibility") != "eligible":
        science.append("selection_ineligible")
    candidates = validated.get("candidate_records")
    if not isinstance(candidates, list) or not candidates:
        science.append("selection_no_candidates")
        return validated
    # Winner = the max-posterior candidate, the same rule the evidence lane selects by. On the
    # fallback lane posteriors are absent and this stays None — harmless, because the record is
    # already selection_ineligible above.
    winner = None
    best_posterior = None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        posterior = candidate.get("posterior_probability")
        if isinstance(posterior, (int, float)) and (
                best_posterior is None or posterior > best_posterior):
            winner, best_posterior = candidate.get("label"), posterior
    for candidate in candidates:
        semantics = candidate.get("fit_semantics") if isinstance(candidate, Mapping) else None
        if not isinstance(semantics, Mapping):
            science.append("selection_candidate_semantics_missing")
            continue
        constraints = semantics.get("effective_constraints") or ()
        # Owner ruling 2026-08-09: a NON-winning offered catalogue variant (every constraint
        # origin == catalogue_variant) does not disqualify the scan — near-zone-centre acoustic
        # and crossing regimes deliberately offer close-together rival hypotheses, and "offered,
        # priced, rejected" conditions nothing about the winner. A variant that WINS still
        # fails closed below.
        if (candidate.get("label") != winner and bool(constraints) and all(
                isinstance(item, Mapping) and item.get("origin") == "catalogue_variant"
                for item in constraints)):
            continue
        if semantics.get("estimator_kind") != "data_likelihood_mle":
            science.append("selection_ineligible")
        if semantics.get("selection_eligibility") != "eligible":
            science.append("selection_ineligible")
        if constraints:
            science.append("selection_constrained")
        if any(item.get("origin") is not None for item in constraints
               if isinstance(item, Mapping)):
            science.append("selection_constrained")
        reasons = semantics.get("selection_ineligibility_reasons") or ()
        if any("rescue" in str(reason) or "post_selection" in str(reason) for reason in reasons):
            science.append("selection_rescued")
    route = validated.get("candidate_generation_route")
    if not isinstance(route, Mapping):
        science.append("selection_route_missing")
        return validated
    # The persisted producer uses ``route`` while the read-only census exposes
    # its normalized ``route_kind`` diagnostic.  They carry the same contract
    # value; accepting both keeps this pure seam grounded in either caller.
    route_kind = route.get("route_kind", route.get("route"))
    if route_kind not in {"standard_two_stage", "full_two_stage"}:
        science.append("selection_route_kind_unsupported")
    offered = route.get("offered_labels")
    scored = route.get("scored_labels")
    # ``scored_labels`` is the set that received an Akaike weight, and
    # :func:`isar.analyze.select.score_models` scores only the fits whose optimizer converged —
    # the weights then normalise over exactly that set, which is the only thing they can do.  So
    # a route that offers four structures and scores two has not truncated a comparison; it has
    # reported that two structures failed to converge, and the surviving comparison is complete
    # over what could be compared.  Demanding ``offered == scored`` demanded that every offered
    # structure converge on every master, which rejected two thirds of the real fits in the R3
    # cold-start campaign.  What actually matters is the reverse containment: a scored label that
    # was never offered means the weight denominator holds a model the route cannot account for.
    # Whether the surviving normalisation is sound is the producer's own judgement and is already
    # read above through ``selection_eligibility``.
    if not isinstance(offered, list) or not isinstance(scored, list) or not scored:
        science.append("selection_scored_not_offered")
    elif not set(scored) <= set(offered):
        science.append("selection_scored_not_offered")
    # ``labels_before_guideline`` carries model labels; ``guideline_structure_filter``
    # carries the card's structure directives (normally empty).  They are different
    # vocabularies and were never comparable — comparing them rejected every real
    # fit.  Completeness is instead grounded in producer-owned facts: the offered
    # set was the scored set, the pre-guideline model set survived into it, the
    # producer declares the guideline changed no model set, and no hint or
    # guideline forced the route.  The label comparison is unconditional so the
    # verdict never depends on whether the flag was written at all.
    before = route.get("labels_before_guideline")
    if isinstance(before, list) and isinstance(offered, list) and set(before) != set(offered):
        science.append("selection_labels_shifted_before_guideline")
    if (route.get("guideline_changed") is True
            or route.get("guideline_changed_model_set") is True
            or route.get("multimode_route") == "guideline_forced"
            or (route.get("supplied_hint") is not None
                and (route.get("hint_accepted") is True
                     or route.get("delivery_hint_accepted") is True))):
        science.append("selection_guideline_forced")
    return validated


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


def _selection_identity(validated: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(validated, Mapping):
        return None
    route = validated.get("candidate_generation_route")
    candidates = validated.get("candidate_records")
    candidate_state = []
    if isinstance(candidates, list):
        for candidate in candidates:
            semantics = candidate.get("fit_semantics") if isinstance(candidate, Mapping) else None
            if not isinstance(semantics, Mapping):
                candidate_state.append({"valid": False})
                continue
            candidate_state.append({
                "label": candidate.get("label"),
                "estimator_kind": semantics.get("estimator_kind"),
                "selection_eligibility": semantics.get("selection_eligibility"),
                "constraint_count": len(semantics.get("effective_constraints") or ()),
                "rescue_or_post_selection": any(
                    "rescue" in str(reason) or "post_selection" in str(reason)
                    for reason in (semantics.get("selection_ineligibility_reasons") or ())),
            })
    route_state = {
        "route_kind": route.get("route_kind", route.get("route")) if isinstance(route, Mapping) else None,
        "multimode_route": route.get("multimode_route") if isinstance(route, Mapping) else None,
        "offered_labels": sorted(str(item) for item in route.get("offered_labels") or ()) if isinstance(route, Mapping) else [],
        "scored_labels": sorted(str(item) for item in route.get("scored_labels") or ()) if isinstance(route, Mapping) else [],
        "labels_before_guideline": sorted(str(item) for item in route.get("labels_before_guideline") or ()) if isinstance(route, Mapping) else [],
        "guideline_structure_filter": sorted(str(item) for item in route.get("guideline_structure_filter") or ()) if isinstance(route, Mapping) else [],
        "supplied_hint": bool(isinstance(route, Mapping) and route.get("supplied_hint") is not None),
        "hint_accepted": bool(isinstance(route, Mapping) and (
            route.get("hint_accepted") is True or route.get("delivery_hint_accepted") is True)),
        "guideline_changed": bool(isinstance(route, Mapping) and (
            route.get("guideline_changed") is True or route.get("guideline_changed_model_set") is True)),
    }
    return {
        "model_selection_fingerprint": validated.get("model_selection_fingerprint"),
        "model_weight_kind": validated.get("model_weight_kind"),
        "selection_eligibility": validated.get("selection_eligibility"),
        "candidate_state": sorted(candidate_state, key=lambda item: str(item.get("label", ""))),
        "route_state": route_state,
    }


def _shadow_identity(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    try:
        validated = validate_shadow_model_selection(_thaw(record))
    except (SemanticsConstructionError, TypeError, ValueError, KeyError, OverflowError):
        return None
    status = validated.get("selection_status")
    if status == "complete":
        fingerprint = validated.get("standard_selection_fingerprint")
        return {"kind": "standard_selection", "fingerprint": fingerprint if isinstance(fingerprint, str) else None}
    neutral_skip = (
        status == "unavailable"
        and validated.get("shadow_execution_kind") == "skipped"
        and validated.get("unavailable_reason_code") == "shadow_skipped_soft_deadline"
        and validated.get("runtime_increment_seconds") == 0.0
        and not validated.get("attempted_model_identities")
        and not validated.get("reused_model_identities")
        and not validated.get("freshly_fitted_model_identities")
    )
    if neutral_skip:
        return {"kind": "neutral_skip", "identity": "isar.shadow_neutral_skip/1"}
    return {
        "kind": "nonstandard",
        "selection_status": status,
        "standard_selection_fingerprint": validated.get("standard_selection_fingerprint"),
    }


def _target_identity(target: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(target, Mapping):
        return None
    try:
        q = list(canonical_hkl(_target_hkl(target)))
    except (TypeError, ValueError):
        q = None
    return {
        "hkl": q,
        "master_revision": target.get("master_revision"),
        "track_revision": target.get("track_revision"),
        "track_state": target.get("track_state"),
        "candidate_id": target.get("candidate_id"),
        "candidate_state": target.get("candidate_state"),
        "selected_candidate_index": target.get("selected_candidate_index"),
        "target_delivery_eligible": target.get("target_delivery_eligible"),
        "pooled": target.get("pooled"),
        "omega0_is_finite": _finite(target.get("omega0")),
        "modality": target.get("modality"),
        "pool_consistent": target.get("pool_consistent"),
    }


def _intent_q(intent: Mapping[str, Any] | None) -> list[float] | None:
    if not isinstance(intent, Mapping):
        return None
    try:
        return list(canonical_hkl(intent.get("hkl")))
    except (TypeError, ValueError):
        return None


def _scan_design_identity(provenance: Mapping[str, Any] | None,
                          events: tuple[str, ...]) -> Mapping[str, Any]:
    source = provenance if isinstance(provenance, Mapping) else {}
    return {
        "inventory_fingerprint": source.get("inventory_fingerprint"),
        "context_fingerprint": source.get("context_fingerprint"),
        "selected_candidate_id": source.get("selected_candidate_id"),
        "status": source.get("status"),
        "recovery_state": source.get("recovery_state"),
        "events": sorted(str(event) for event in events),
    }


def evaluate_eig_eligibility(value: EligibilityInput) -> EligibilityResult:
    """Apply the frozen O36 eligibility policy to immutable current-contract evidence."""
    science: list[str] = []
    gaps: list[str] = []
    target_modality = _target_checks(value.target_outcome, science, gaps)
    action_fingerprint = _action_checks(
        value.action_intent, value.action_provenance, value.target_outcome, science, gaps)
    validated_selection = _selection_checks(value.selection_record, science, gaps)
    validated_qoi = None
    qoi_status = "missing"
    if value.qoi_evidence is None:
        gaps.append("projection_record_missing")
    else:
        try:
            validated_qoi = validate_qoi_projection_evidence(value.qoi_evidence)
        except (TypeError, ValueError) as error:
            log.warning("EIG eligibility refused unauthenticated QoI evidence: %s", error)
            gaps.append("projection_record_invalid")
            qoi_status = "invalid"
        else:
            qoi_status = validated_qoi.status
            science.extend(validated_qoi.scientific_codes)
            gaps.extend(validated_qoi.observability_codes)
            selection_fingerprint = (
                validated_selection.get("model_selection_fingerprint")
                if isinstance(validated_selection, Mapping) else None)
            predictive_fingerprint = validated_qoi.predictive_source_model_selection_fingerprint
            if not isinstance(selection_fingerprint, str) or not isinstance(predictive_fingerprint, str):
                gaps.append("projection_join_unknown")
            elif selection_fingerprint != predictive_fingerprint:
                science.append("projection_join_mismatch")
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
        "authenticated_action_fingerprint": action_fingerprint,
        "canonical_action_q": _intent_q(value.action_intent),
        "target": _target_identity(value.target_outcome),
        "selection": _selection_identity(validated_selection),
        "qoi_evidence_fingerprint": (
            validated_qoi.qoi_evidence_fingerprint if validated_qoi is not None else None),
        "shadow": _shadow_identity(value.shadow_record),
        "scan_design": _scan_design_identity(value.scan_design_provenance, value.scan_design_events),
    }
    input_fingerprint = _fingerprint(input_payload)
    result_payload = _result_payload(
        status=status, scientific_codes=scientific_codes,
        observability_codes=observability_codes, input_fingerprint=input_fingerprint)
    result_fingerprint = _fingerprint(result_payload)
    diagnostics = tuple(sorted((
        ("qoi_status", qoi_status),
        ("scan_design_event_count", len(value.scan_design_events)),
        # Modality no longer gates beyond ``fork``; it stays visible so a reader
        # can still tell an unpooled single-member master from a folded one.
        ("target_modality", target_modality),
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

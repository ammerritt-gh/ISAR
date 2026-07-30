"""Pure STAT-2 shadow comparison with no delivery or selection authority.

The legacy selector remains authoritative during STAT-2a.  This module accepts point-fit
results plus producer-owned route/identity records, validates the complete attempted family,
and emits a self-authenticating, JSON-clean diagnostic comparison.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from numbers import Integral, Real
from types import SimpleNamespace
from typing import Any

from isar.analyze.select import score_models
from isar.schema.statistics import (
    CONSTRAINT_ORIGINS,
    FIT_SELECTION_SCHEMA,
    MODEL_SELECTION_CONTRACT,
    SHADOW_MODEL_SELECTION_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    canonical_shadow_model_identities,
    semantics_fingerprint,
    shadow_model_set_fingerprint,
    shadow_selection_fingerprint,
    validate_fit_selection_semantics,
    validate_for_write,
)


SHADOW_REASON_MISSING_ATTEMPT = "shadow_missing_attempted_candidate"
SHADOW_REASON_IDENTITY_JOIN = "shadow_candidate_identity_join_failed"
SHADOW_REASON_MISSING_SEMANTICS = "shadow_missing_candidate_fit_semantics"
SHADOW_REASON_MALFORMED_SEMANTICS = "shadow_malformed_candidate_fit_semantics"
SHADOW_REASON_UNAPPROVED_ESTIMATOR = "shadow_unapproved_candidate_estimator"
SHADOW_REASON_INELIGIBLE = "shadow_candidate_selection_ineligible"
SHADOW_REASON_INCOMPARABLE = "shadow_incomparable_data_likelihood_context"
SHADOW_REASON_MALFORMED_CANDIDATE = "shadow_malformed_candidate_result"
SHADOW_REASON_NO_DENOMINATOR = "shadow_no_eligible_converged_candidates"
SHADOW_REASON_DUPLICATE_ATTEMPT = "shadow_duplicate_candidate_attempt"
SHADOW_REASON_CANDIDATE_FIT_FAILED = "shadow_candidate_fit_failed"
SHADOW_REASON_MISSING_LEGACY_FINGERPRINT = (
    "shadow_missing_legacy_model_selection_fingerprint"
)

_ISSUE_PRECEDENCE = {
    SHADOW_REASON_MISSING_LEGACY_FINGERPRINT: -1,
    SHADOW_REASON_IDENTITY_JOIN: 0,
    SHADOW_REASON_DUPLICATE_ATTEMPT: 1,
    SHADOW_REASON_MISSING_ATTEMPT: 2,
    SHADOW_REASON_MISSING_SEMANTICS: 3,
    SHADOW_REASON_MALFORMED_SEMANTICS: 4,
    SHADOW_REASON_UNAPPROVED_ESTIMATOR: 5,
    SHADOW_REASON_INELIGIBLE: 6,
    SHADOW_REASON_INCOMPARABLE: 7,
    SHADOW_REASON_MALFORMED_CANDIDATE: 8,
    SHADOW_REASON_CANDIDATE_FIT_FAILED: 9,
}

_ROUTE_SET_KEYS = frozenset(
    {
        "guideline_structure_filter",
        "labels_before_guideline",
        "offered_labels",
        "attempted_labels",
        "converged_labels",
        "denominator_labels",
        "failed_labels",
        "one_mode_labels",
        "multimode_labels",
        "scored_labels",
    }
)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def shadow_model_identity(
    label: str, model_parameterization_fingerprint: str
) -> dict[str, str]:
    """Build the label-plus-parameterization identity used by the shadow lane."""
    identity = {
        "label": str(label),
        "model_parameterization_fingerprint": str(
            model_parameterization_fingerprint
        ),
    }
    identity["model_identity"] = semantics_fingerprint(
        "isar.shadow_model_identity/1", identity
    )
    return canonical_shadow_model_identities([identity])[0]


def _canonical_route_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): _canonical_route_value(item, str(name))
            for name, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        items = [_canonical_route_value(item) for item in value]
        if key in _ROUTE_SET_KEYS:
            return sorted(items, key=lambda item: str(item))
        return items
    if value is None or isinstance(value, (str, bool, Integral, Real)):
        return value
    raise SemanticsConstructionError(
        "malformed_candidate_generation_route", type(value).__name__
    )


def canonical_candidate_generation_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """Return the order-invariant persisted form of a standard generation route."""
    if not isinstance(route, Mapping):
        raise SemanticsConstructionError("malformed_candidate_generation_route")
    return _canonical_route_value(copy.deepcopy(dict(route)))


def _canonical_legacy_weights(weights: Any) -> list[dict[str, Any]]:
    if isinstance(weights, Mapping):
        items = [{"label": label, "weight": weight} for label, weight in weights.items()]
    elif isinstance(weights, Sequence) and not isinstance(
        weights, (str, bytes, bytearray)
    ):
        items = []
        for value in weights:
            if isinstance(value, Mapping):
                items.append(
                    {"label": value.get("label"), "weight": value.get("weight")}
                )
            else:
                items.append(
                    {"label": _get(value, "label"), "weight": _get(value, "weight")}
                )
    else:
        raise SemanticsConstructionError("malformed_legacy_model_weights")
    canonical = []
    for item in items:
        label = item["label"]
        weight = item["weight"]
        if (
            not isinstance(label, str)
            or not label
            or isinstance(weight, bool)
            or not isinstance(weight, Real)
            or not math.isfinite(float(weight))
            or float(weight) < 0
        ):
            raise SemanticsConstructionError("malformed_legacy_model_weights")
        canonical.append({"label": label, "weight": float(weight)})
    canonical.sort(key=lambda item: item["label"])
    if len({item["label"] for item in canonical}) != len(canonical):
        raise SemanticsConstructionError("malformed_legacy_model_weights")
    return canonical


def _identity_sets(
    offered: list[dict[str, str]],
    attempted: list[dict[str, str]],
    converged: list[dict[str, str]],
    denominator: list[dict[str, str]],
    failed: list[dict[str, str]],
) -> dict[str, Any]:
    records = {
        "offered": canonical_shadow_model_identities(offered),
        "attempted": canonical_shadow_model_identities(attempted),
        "converged": canonical_shadow_model_identities(converged),
        "denominator": canonical_shadow_model_identities(denominator),
        "failed": canonical_shadow_model_identities(failed),
    }
    result: dict[str, Any] = {}
    for kind, identities in records.items():
        result[f"{kind}_model_identities"] = identities
        result[f"{kind}_model_set_fingerprint"] = shadow_model_set_fingerprint(
            kind, identities
        )
    return result


def _assemble_record(
    *,
    selection_context_fingerprint: str,
    candidate_generation_route: Mapping[str, Any],
    legacy_model_selection_fingerprint: str | None,
    legacy_selected_label: str | None,
    legacy_model_weights: Any,
    delivery_effective_constraint_origins: Sequence[str],
    runtime_increment_seconds: float,
    identity_sets: Mapping[str, Any],
    candidate_records: list[dict[str, Any]],
    selection_complete: bool,
    selection_status: str,
    standard_selected_label: str | None,
    standard_model_weights: list[dict[str, Any]],
    unavailable_stage: str | None,
    unavailable_reason_code: str | None,
    affected_candidate_identities: Sequence[str],
) -> dict[str, Any]:
    origins = sorted(set(str(origin) for origin in delivery_effective_constraint_origins))
    if any(origin not in CONSTRAINT_ORIGINS for origin in origins):
        raise SemanticsConstructionError(
            "malformed_delivery_effective_constraint_origins"
        )
    record = {
        "schema": SHADOW_MODEL_SELECTION_SCHEMA,
        "selection_contract_revision": MODEL_SELECTION_CONTRACT,
        "legacy_model_selection_fingerprint": legacy_model_selection_fingerprint,
        "selection_context_fingerprint": selection_context_fingerprint,
        "candidate_generation_route": canonical_candidate_generation_route(
            candidate_generation_route
        ),
        "legacy_selected_label": legacy_selected_label,
        "standard_selected_label": standard_selected_label,
        "selection_changed": (
            None
            if selection_status == "unavailable"
            else bool(legacy_selected_label != standard_selected_label)
        ),
        "legacy_model_weights": _canonical_legacy_weights(legacy_model_weights),
        "standard_model_weights": standard_model_weights,
        "standard_candidate_records": sorted(
            candidate_records,
            key=lambda item: (item["model_identity"], item["label"]),
        ),
        "delivery_effective_constraint_origins": origins,
        **identity_sets,
        "selection_complete": bool(selection_complete),
        "selection_status": selection_status,
        "runtime_increment_seconds": float(runtime_increment_seconds),
        "unavailable_stage": unavailable_stage,
        "unavailable_reason_code": unavailable_reason_code,
        "affected_candidate_identities": sorted(
            set(str(identity) for identity in affected_candidate_identities)
        ),
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }
    record["standard_selection_fingerprint"] = shadow_selection_fingerprint(record)
    return validate_for_write(record, SHADOW_MODEL_SELECTION_SCHEMA)


def unavailable_shadow_selection(
    reason_code: str,
    *,
    stage: str,
    selection_context_fingerprint: str,
    candidate_generation_route: Mapping[str, Any],
    legacy_model_selection_fingerprint: str | None,
    legacy_selected_label: str | None,
    legacy_model_weights: Any,
    offered_model_identities: Sequence[Mapping[str, Any]] = (),
    attempted_model_identities: Sequence[Mapping[str, Any]] = (),
    converged_model_identities: Sequence[Mapping[str, Any]] = (),
    failed_model_identities: Sequence[Mapping[str, Any]] = (),
    standard_candidate_records: Sequence[Mapping[str, Any]] = (),
    affected_candidate_identities: Sequence[str] = (),
    delivery_effective_constraint_origins: Sequence[str] = (),
    runtime_increment_seconds: float = 0.0,
    selection_complete: bool = False,
) -> dict[str, Any]:
    """Construct an authenticated unavailable record without hand-rolled schema.

    Producer boundaries use this for deadline skips and caught context/fit/validation/
    comparison/persistence failures.  No legacy scores are ever substituted.
    """
    if not isinstance(reason_code, str) or not reason_code:
        raise SemanticsConstructionError("missing_shadow_unavailable_reason_code")
    if not isinstance(stage, str) or not stage:
        raise SemanticsConstructionError("missing_shadow_unavailable_stage")
    offered = canonical_shadow_model_identities(offered_model_identities)
    attempted = canonical_shadow_model_identities(attempted_model_identities)
    converged = canonical_shadow_model_identities(converged_model_identities)
    failed = canonical_shadow_model_identities(failed_model_identities)
    identities = _identity_sets(offered, attempted, converged, [], failed)
    affected = list(affected_candidate_identities)
    if not affected and offered:
        affected = [item["model_identity"] for item in offered]
    return _assemble_record(
        selection_context_fingerprint=selection_context_fingerprint,
        candidate_generation_route=candidate_generation_route,
        legacy_model_selection_fingerprint=legacy_model_selection_fingerprint,
        legacy_selected_label=legacy_selected_label,
        legacy_model_weights=legacy_model_weights,
        delivery_effective_constraint_origins=delivery_effective_constraint_origins,
        runtime_increment_seconds=runtime_increment_seconds,
        identity_sets=identities,
        candidate_records=[dict(item) for item in standard_candidate_records],
        selection_complete=selection_complete,
        selection_status="unavailable",
        standard_selected_label=None,
        standard_model_weights=[],
        unavailable_stage=stage,
        unavailable_reason_code=reason_code,
        affected_candidate_identities=affected,
    )


def _safe_number(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _safe_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        return None
    return int(value)


def _candidate_record(
    candidate: Any,
    identity: Mapping[str, str],
) -> tuple[dict[str, Any], str | None]:
    label = identity["label"]
    converged_raw = _get(candidate, "optimizer_converged")
    converged = converged_raw if isinstance(converged_raw, bool) else False
    cost = _safe_number(_get(candidate, "cost"))
    k = _safe_nonnegative_int(_get(candidate, "k"))
    ndof = _safe_nonnegative_int(_get(candidate, "ndof"))
    n = None if k is None or ndof is None else k + ndof
    semantics_raw = _get(candidate, "fit_semantics")
    semantics = None
    issue = None
    if semantics_raw is None:
        issue = SHADOW_REASON_MISSING_SEMANTICS
    elif not isinstance(semantics_raw, Mapping):
        issue = SHADOW_REASON_MALFORMED_SEMANTICS
    else:
        try:
            semantics = validate_fit_selection_semantics(semantics_raw)
        except (SemanticsConstructionError, KeyError, TypeError, ValueError, OverflowError):
            issue = SHADOW_REASON_MALFORMED_SEMANTICS
    if semantics is not None and (
        semantics.get("model_parameterization_fingerprint")
        != identity["model_parameterization_fingerprint"]
    ):
        issue = SHADOW_REASON_IDENTITY_JOIN
    elif semantics is not None and semantics.get("estimator_kind") != "data_likelihood_mle":
        issue = SHADOW_REASON_UNAPPROVED_ESTIMATOR
    elif semantics is not None and semantics.get("selection_eligibility") != "eligible":
        issue = SHADOW_REASON_INELIGIBLE
    if cost is None or k is None or n is None or n <= 0 or k > n:
        issue = issue or SHADOW_REASON_MALFORMED_CANDIDATE
    record = {
        "label": label,
        "model_identity": identity["model_identity"],
        "cost": cost,
        "k": k,
        "n": n,
        "optimizer_converged": bool(converged),
        "candidate_validation_status": "invalid" if issue else "valid",
        "candidate_validation_reason_code": issue,
        "aic": None,
        "aicc": None,
        "bic": None,
        "delta_aic": None,
        "weight": None,
        "fit_semantics": semantics,
    }
    return record, issue


def compare_shadow_selection(
    candidates: Sequence[Any],
    *,
    offered_model_identities: Sequence[Mapping[str, Any]],
    candidate_generation_route: Mapping[str, Any],
    selection_context_fingerprint: str,
    legacy_model_selection_fingerprint: str | None,
    legacy_selected_label: str | None,
    legacy_model_weights: Any,
    delivery_effective_constraint_origins: Sequence[str] = (),
    runtime_increment_seconds: float = 0.0,
) -> dict[str, Any]:
    """Compare a complete standard candidate family using eligible converged MLE fits only.

    Required-candidate defects make the comparison structurally unavailable.  They never
    normalize a surviving subset and never copy legacy weights into the standard fields.
    """
    offered = canonical_shadow_model_identities(offered_model_identities)
    offered_by_id = {item["model_identity"]: item for item in offered}
    offered_by_label: dict[str, list[dict[str, str]]] = {}
    for item in offered:
        offered_by_label.setdefault(item["label"], []).append(item)

    attempted_by_id: dict[str, dict[str, str]] = {}
    candidate_by_id: dict[str, Any] = {}
    issues: list[tuple[str, str]] = []
    if legacy_model_selection_fingerprint is None:
        issues.append((SHADOW_REASON_MISSING_LEGACY_FINGERPRINT, ""))
    unresolved_join = False
    for candidate in candidates:
        label = _get(candidate, "label")
        semantics = _get(candidate, "fit_semantics")
        parameterization = (
            semantics.get("model_parameterization_fingerprint")
            if isinstance(semantics, Mapping)
            else None
        )
        identity = None
        if isinstance(label, str) and label and isinstance(parameterization, str):
            try:
                proposed = shadow_model_identity(label, parameterization)
            except SemanticsConstructionError:
                proposed = None
            if proposed is not None:
                identity = offered_by_id.get(proposed["model_identity"])
        if identity is None and isinstance(label, str):
            matches = offered_by_label.get(label, [])
            if len(matches) == 1:
                identity = matches[0]
        if identity is None:
            unresolved_join = True
            continue
        model_id = identity["model_identity"]
        if model_id in attempted_by_id:
            issues.append((SHADOW_REASON_DUPLICATE_ATTEMPT, model_id))
            continue
        attempted_by_id[model_id] = identity
        candidate_by_id[model_id] = candidate

    missing_ids = sorted(set(offered_by_id) - set(attempted_by_id))
    if unresolved_join:
        affected = sorted(offered_by_id)
        issues.append((SHADOW_REASON_IDENTITY_JOIN, affected[0] if affected else ""))
    issues.extend((SHADOW_REASON_MISSING_ATTEMPT, model_id) for model_id in missing_ids)

    candidate_records = []
    data_likelihoods: dict[str, str] = {}
    for model_id in sorted(attempted_by_id):
        record, issue = _candidate_record(
            candidate_by_id[model_id], attempted_by_id[model_id]
        )
        candidate_records.append(record)
        if issue is not None:
            issues.append((issue, model_id))
        elif not record["optimizer_converged"]:
            issues.append((SHADOW_REASON_CANDIDATE_FIT_FAILED, model_id))
        elif record["fit_semantics"] is not None:
            data_likelihoods[model_id] = record["fit_semantics"][
                "data_likelihood_fingerprint"
            ]

    if len(set(data_likelihoods.values())) > 1:
        for record in candidate_records:
            if record["model_identity"] in data_likelihoods:
                record["candidate_validation_status"] = "invalid"
                record["candidate_validation_reason_code"] = SHADOW_REASON_INCOMPARABLE
                record["aic"] = None
                record["aicc"] = None
                record["bic"] = None
                record["delta_aic"] = None
                record["weight"] = None
                issues.append((SHADOW_REASON_INCOMPARABLE, record["model_identity"]))

    attempted = [attempted_by_id[model_id] for model_id in sorted(attempted_by_id)]
    converged = [
        attempted_by_id[record["model_identity"]]
        for record in candidate_records
        if record["optimizer_converged"]
    ]
    failed = [
        attempted_by_id[record["model_identity"]]
        for record in candidate_records
        if not record["optimizer_converged"]
    ]
    selection_complete = not missing_ids and not unresolved_join and not issues

    if issues:
        reason, _ = min(
            issues,
            key=lambda item: (_ISSUE_PRECEDENCE.get(item[0], 999), item[1]),
        )
        affected = sorted({model_id for _, model_id in issues if model_id in offered_by_id})
        identities = _identity_sets(offered, attempted, converged, [], failed)
        return _assemble_record(
            selection_context_fingerprint=selection_context_fingerprint,
            candidate_generation_route=candidate_generation_route,
            legacy_model_selection_fingerprint=legacy_model_selection_fingerprint,
            legacy_selected_label=legacy_selected_label,
            legacy_model_weights=legacy_model_weights,
            delivery_effective_constraint_origins=delivery_effective_constraint_origins,
            runtime_increment_seconds=runtime_increment_seconds,
            identity_sets=identities,
            candidate_records=candidate_records,
            selection_complete=False,
            selection_status="unavailable",
            standard_selected_label=None,
            standard_model_weights=[],
            unavailable_stage=(
                "legacy_projection"
                if reason == SHADOW_REASON_MISSING_LEGACY_FINGERPRINT
                else "comparison_validation"
            ),
            unavailable_reason_code=reason,
            affected_candidate_identities=affected,
        )

    valid_converged_records = [
        record
        for record in candidate_records
        if record["optimizer_converged"]
        and record["candidate_validation_status"] == "valid"
    ]
    denominator = [
        attempted_by_id[record["model_identity"]]
        for record in valid_converged_records
    ]
    identities = _identity_sets(offered, attempted, converged, denominator, failed)
    if not valid_converged_records:
        return _assemble_record(
            selection_context_fingerprint=selection_context_fingerprint,
            candidate_generation_route=candidate_generation_route,
            legacy_model_selection_fingerprint=legacy_model_selection_fingerprint,
            legacy_selected_label=legacy_selected_label,
            legacy_model_weights=legacy_model_weights,
            delivery_effective_constraint_origins=delivery_effective_constraint_origins,
            runtime_increment_seconds=runtime_increment_seconds,
            identity_sets=identities,
            candidate_records=candidate_records,
            selection_complete=False,
            selection_status="unavailable",
            standard_selected_label=None,
            standard_model_weights=[],
            unavailable_stage="comparison",
            unavailable_reason_code=SHADOW_REASON_NO_DENOMINATOR,
            affected_candidate_identities=[
                item["model_identity"] for item in offered
            ],
        )

    records_by_label = {record["label"]: record for record in valid_converged_records}
    score_inputs = [
        SimpleNamespace(
            label=record["label"],
            k=record["k"],
            ndof=record["n"] - record["k"],
            cost=record["cost"],
            optimizer_converged=True,
            params=dict(_get(candidate_by_id[record["model_identity"]], "params", {}) or {}),
            fit_semantics=record["fit_semantics"],
        )
        for record in sorted(
            valid_converged_records,
            key=lambda item: (item["model_identity"], item["label"]),
        )
    ]
    scores = score_models(score_inputs)
    identity_by_label = {
        record["label"]: record["model_identity"] for record in valid_converged_records
    }
    for score in scores:
        record = records_by_label[score.label]
        record["aic"] = float(score.aic)
        record["aicc"] = (
            float(score.aicc) if math.isfinite(float(score.aicc)) else None
        )
        record["bic"] = float(score.bic)
        record["delta_aic"] = float(score.delta_aic)
        record["weight"] = float(score.weight)
    selected_score = min(
        scores,
        key=lambda score: (float(score.aic), identity_by_label[score.label]),
    )
    standard_weights = sorted(
        (
            {
                "label": score.label,
                "model_identity": identity_by_label[score.label],
                "weight": float(score.weight),
            }
            for score in scores
        ),
        key=lambda item: (item["model_identity"], item["label"]),
    )
    status = (
        "degenerate_single_candidate"
        if len(valid_converged_records) == 1
        else "complete"
    )
    return _assemble_record(
        selection_context_fingerprint=selection_context_fingerprint,
        candidate_generation_route=candidate_generation_route,
        legacy_model_selection_fingerprint=legacy_model_selection_fingerprint,
        legacy_selected_label=legacy_selected_label,
        legacy_model_weights=legacy_model_weights,
        delivery_effective_constraint_origins=delivery_effective_constraint_origins,
        runtime_increment_seconds=runtime_increment_seconds,
        identity_sets=identities,
        candidate_records=candidate_records,
        selection_complete=True,
        selection_status=status,
        standard_selected_label=selected_score.label,
        standard_model_weights=standard_weights,
        unavailable_stage=None,
        unavailable_reason_code=None,
        affected_candidate_identities=[],
    )

"""Adapt authenticated target-QoI projections into immutable EIG evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

from isar.predictive import (
    PREDICTIVE_ARTIFACT_SCHEMA,
    PREDICTIVE_MODEL_WEIGHT_FLOOR,
    TARGET_PROJECTION_SCHEMA,
    TARGET_PROJECTION_VERSION,
    TARGET_QOI,
    PredictiveArtifactError,
    target_projection_set_fingerprint,
    validate_master_predictive_artifact,
)
from isar.schema.statistics import SemanticsConstructionError, validate_predictive_semantics


QOI_EVIDENCE_CONTRACT = "isar.eig_qoi_projection_evidence/o36.2.1"
OMEGA0_ADAPTER_REVISION = "isar.target_projection_omega0_adapter/3"

_SOURCE_SCOPES = {
    "eig_shadow": "ineligible",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_DERIVED_SCOPES = {
    "eig_shadow": "candidate",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_QOI_STATUSES = {"missing", "invalid", "qualified", "valid"}
_AUTHORITY_FIELDS = (
    "action_role", "master_revision", "track_revision", "track_state",
    "candidate_id", "candidate_state", "selected_candidate_index",
    "calibration_fingerprint", "policy_fingerprint",
)


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _frozen_pairs(value: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))


def _diagnostics(**values: Any) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((name, _freeze(value)) for name, value in values.items()))


def _fingerprint(value: Any) -> str:
    payload = json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _QoIModel:
    """Private normalized model evidence retained behind the public QoI seam."""

    label: str
    identity_label: str
    weight: float
    source_weight: float
    draw_basis: str | None
    draw_weighting: str | None
    draw_mass_retained: float | None
    draw_weights: tuple[float, ...]
    target_present: tuple[bool, ...]
    qoi_draws: tuple[float | None, ...]


def _models_payload(models: tuple[_QoIModel, ...]) -> list[dict[str, Any]]:
    return [{
        "label": item.label,
        "identity_label": item.identity_label,
        "weight": item.weight,
        "source_weight": item.source_weight,
        "draw_basis": item.draw_basis,
        "draw_weighting": item.draw_weighting,
        "draw_mass_retained": item.draw_mass_retained,
        "draw_weights": list(item.draw_weights),
        "target_present_by_draw": list(item.target_present),
        "target_qoi_draws": list(item.qoi_draws),
    } for item in models]


def _qoi_evidence_fingerprint_payload(evidence: Any, models: Any) -> dict[str, Any]:
    """The complete ``o36.2.1`` semantic envelope, excluding diagnostics."""
    return {
        "contract": evidence.contract,
        "adapter_revision": evidence.adapter_revision,
        "qoi_id": evidence.qoi_id,
        "qoi_unit": evidence.qoi_unit,
        "source_qoi": evidence.source_qoi,
        "status": evidence.status,
        "scientific_codes": list(evidence.scientific_codes),
        "observability_codes": list(evidence.observability_codes),
        "predictive_basis_fingerprint": evidence.predictive_basis_fingerprint,
        "predictive_retained_set_fingerprint": evidence.predictive_retained_set_fingerprint,
        "predictive_statistical_semantics_fingerprint": evidence.predictive_statistical_semantics_fingerprint,
        "predictive_source_model_selection_fingerprint": evidence.predictive_source_model_selection_fingerprint,
        "target_projection_set_fingerprint": evidence.target_projection_set_fingerprint,
        "projection_statistical_semantics_fingerprint": evidence.projection_statistical_semantics_fingerprint,
        "projection_authority": _thaw(dict(evidence.projection_authority)),
        "projection_authority_fingerprint": evidence.projection_authority_fingerprint,
        "source_authority_scopes": _thaw(dict(evidence.source_authority_scopes)),
        "authority_scopes": _thaw(dict(evidence.authority_scopes)),
        "models": _thaw(models),
    }


def _validate_qoi_projection_evidence_invariants(
        evidence: Any, models: Any, *, require_evidence_fingerprint: bool = True) -> None:
    """Validate the semantic envelope shared by construction and public reads."""
    expected_metadata = {
        "contract": QOI_EVIDENCE_CONTRACT,
        "adapter_revision": OMEGA0_ADAPTER_REVISION,
        "qoi_id": "excitation.energy",
        "qoi_unit": "meV",
        "source_qoi": "omega0",
    }
    if any(getattr(evidence, name) != expected for name, expected in expected_metadata.items()):
        raise ValueError("QoI evidence metadata does not match the o36.2.1 contract")
    if evidence.status not in _QOI_STATUSES:
        raise ValueError("QoI evidence has an invalid status")
    for name in ("scientific_codes", "observability_codes"):
        codes = getattr(evidence, name)
        if (not isinstance(codes, tuple)
                or any(not isinstance(code, str) or not code for code in codes)
                or len(codes) != len(set(codes))):
            raise ValueError(f"QoI evidence {name} must be a unique non-empty string tuple")
    has_codes = bool(evidence.scientific_codes or evidence.observability_codes)
    if (evidence.status == "valid") == has_codes:
        raise ValueError("QoI evidence valid status and reason codes disagree")
    expected_scopes = _DERIVED_SCOPES if evidence.status == "valid" else _SOURCE_SCOPES
    if tuple(evidence.authority_scopes) != _frozen_pairs(expected_scopes):
        raise ValueError("QoI evidence derived authority scopes disagree with status")
    try:
        source_scopes = dict(evidence.source_authority_scopes)
    except (TypeError, ValueError) as error:
        raise ValueError("QoI evidence source authority scopes are malformed") from error
    for source, scopes in source_scopes.items():
        if not isinstance(source, str) or not isinstance(scopes, Mapping):
            raise ValueError("QoI evidence source authority scopes are malformed")
        if any(scopes.get(name) != expected for name, expected in _SOURCE_SCOPES.items()):
            raise ValueError("QoI evidence source authority scopes exceed their authority")
    if evidence.status == "valid" and not {
            "predictive_semantics", "projection_semantics", "projection"}.issubset(source_scopes):
        raise ValueError("valid QoI evidence is missing source authority records")
    if evidence.projection_fingerprint != evidence.target_projection_set_fingerprint:
        raise ValueError("projection_fingerprint disagrees with target_projection_set_fingerprint")
    try:
        authority = _thaw(dict(evidence.projection_authority))
    except (TypeError, ValueError) as error:
        raise ValueError("QoI evidence projection authority is malformed") from error
    expected_authority_fingerprint = _fingerprint(authority) if authority else None
    if evidence.projection_authority_fingerprint != expected_authority_fingerprint:
        raise ValueError("projection_authority_fingerprint does not authenticate projection_authority")
    if require_evidence_fingerprint:
        expected_evidence_fingerprint = _fingerprint(
            _qoi_evidence_fingerprint_payload(evidence, models))
        if evidence.qoi_evidence_fingerprint != expected_evidence_fingerprint:
            raise ValueError("qoi_evidence_fingerprint does not authenticate QoI evidence")


@dataclass(frozen=True)
class QoIProjectionEvidence:
    """Immutable generic view of a predictive target-QoI projection.

    ``projection_fingerprint`` remains a construction and read compatibility
    alias.  New serialized evidence names its meaning explicitly as
    ``target_projection_set_fingerprint``.
    """

    contract: str
    adapter_revision: str
    qoi_id: str
    qoi_unit: str
    source_qoi: str
    status: str
    scientific_codes: tuple[str, ...] = ()
    observability_codes: tuple[str, ...] = ()
    predictive_basis_fingerprint: str | None = None
    predictive_retained_set_fingerprint: str | None = None
    predictive_statistical_semantics_fingerprint: str | None = None
    predictive_source_model_selection_fingerprint: str | None = None
    target_projection_set_fingerprint: str | None = None
    projection_statistical_semantics_fingerprint: str | None = None
    projection_authority: tuple[tuple[str, Any], ...] = ()
    projection_authority_fingerprint: str | None = None
    qoi_evidence_fingerprint: str | None = None
    # Compatibility-only constructor alias.  Do not serialize it.
    projection_fingerprint: str | None = None
    source_authority_scopes: tuple[tuple[str, Any], ...] = ()
    authority_scopes: tuple[tuple[str, Any], ...] = ()
    diagnostics: tuple[tuple[str, Any], ...] = ()
    _models: tuple[_QoIModel, ...] = ()

    def __post_init__(self) -> None:
        target_set = self.target_projection_set_fingerprint
        legacy = self.projection_fingerprint
        if target_set is None:
            target_set = legacy
        elif legacy is not None and legacy != target_set:
            raise ValueError("projection_fingerprint disagrees with target_projection_set_fingerprint")
        object.__setattr__(self, "target_projection_set_fingerprint", target_set)
        object.__setattr__(self, "projection_fingerprint", target_set)
        try:
            authority = _thaw(dict(self.projection_authority))
        except (TypeError, ValueError) as error:
            raise ValueError("QoI evidence projection authority is malformed") from error
        expected_authority_fingerprint = _fingerprint(authority) if authority else None
        authority_fingerprint = self.projection_authority_fingerprint
        if authority_fingerprint is None:
            authority_fingerprint = expected_authority_fingerprint
        elif authority_fingerprint != expected_authority_fingerprint:
            raise ValueError("projection_authority_fingerprint does not authenticate projection_authority")
        object.__setattr__(self, "projection_authority_fingerprint", authority_fingerprint)
        models = _models_payload(self._models)
        _validate_qoi_projection_evidence_invariants(
            self, models, require_evidence_fingerprint=False)
        expected_evidence_fingerprint = _fingerprint(
            _qoi_evidence_fingerprint_payload(self, models))
        if self.qoi_evidence_fingerprint is None:
            object.__setattr__(self, "qoi_evidence_fingerprint", expected_evidence_fingerprint)
        elif self.qoi_evidence_fingerprint != expected_evidence_fingerprint:
            raise ValueError("qoi_evidence_fingerprint does not authenticate QoI evidence")

    @property
    def models(self) -> tuple[Mapping[str, Any], ...]:
        """Read-only diagnostic model views; the normalized implementation stays private."""
        return tuple(MappingProxyType(_freeze(item)) for item in _models_payload(self._models))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "adapter_revision": self.adapter_revision,
            "qoi_id": self.qoi_id,
            "qoi_unit": self.qoi_unit,
            "source_qoi": self.source_qoi,
            "status": self.status,
            "scientific_codes": list(self.scientific_codes),
            "observability_codes": list(self.observability_codes),
            "predictive_basis_fingerprint": self.predictive_basis_fingerprint,
            "predictive_retained_set_fingerprint": self.predictive_retained_set_fingerprint,
            "predictive_statistical_semantics_fingerprint": self.predictive_statistical_semantics_fingerprint,
            "predictive_source_model_selection_fingerprint": self.predictive_source_model_selection_fingerprint,
            "target_projection_set_fingerprint": self.target_projection_set_fingerprint,
            "projection_statistical_semantics_fingerprint": self.projection_statistical_semantics_fingerprint,
            "projection_authority": _thaw(dict(self.projection_authority)),
            "projection_authority_fingerprint": self.projection_authority_fingerprint,
            "qoi_evidence_fingerprint": self.qoi_evidence_fingerprint,
            "source_authority_scopes": _thaw(dict(self.source_authority_scopes)),
            "authority_scopes": _thaw(dict(self.authority_scopes)),
            "diagnostics": _thaw(dict(self.diagnostics)),
            "models": [_thaw(item) for item in self.models],
        }


def validate_qoi_projection_evidence(evidence: Any) -> QoIProjectionEvidence:
    """Strictly re-authenticate one immutable ``o36.2.1`` evidence object.

    The returned object is the same instance.  This seam deliberately accepts
    only the real frozen class so a duck-typed public view cannot inherit its
    authentication claim.
    """
    if not isinstance(evidence, QoIProjectionEvidence):
        raise TypeError("evidence must be a QoIProjectionEvidence")
    try:
        public_models = list(evidence.models)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("QoI evidence contains malformed immutable records") from error
    _validate_qoi_projection_evidence_invariants(evidence, public_models)
    return evidence


def _safe_source_scopes(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain only source scope records that preserve all three EIG denials."""
    if not isinstance(value, Mapping):
        return {}
    safe = {}
    for source, scopes in value.items():
        if (isinstance(scopes, Mapping)
                and all(scopes.get(name) == expected for name, expected in _SOURCE_SCOPES.items())):
            safe[str(source)] = scopes
    return safe


def _evidence(*, status: str, science: tuple[str, ...] = (), gaps: tuple[str, ...] = (),
              predictive: Mapping[str, Any] | None = None, projection: Mapping[str, Any] | None = None,
              predictive_semantics: Mapping[str, Any] | None = None,
              projection_semantics: Mapping[str, Any] | None = None,
              source_scopes: Mapping[str, Any] | None = None, models: tuple[_QoIModel, ...] = (),
              **diagnostic_values: Any) -> QoIProjectionEvidence:
    authority = projection.get("authority") if isinstance(projection, Mapping) else None
    authority_pairs = _frozen_pairs(authority if isinstance(authority, Mapping) else None)
    authority_payload = _thaw(dict(authority_pairs))
    return QoIProjectionEvidence(
        contract=QOI_EVIDENCE_CONTRACT,
        adapter_revision=OMEGA0_ADAPTER_REVISION,
        qoi_id="excitation.energy",
        qoi_unit="meV",
        source_qoi="omega0",
        status=status,
        scientific_codes=tuple(dict.fromkeys(science)),
        observability_codes=tuple(dict.fromkeys(gaps)),
        predictive_basis_fingerprint=(predictive.get("predictive_basis_fingerprint")
                                      if isinstance(predictive, Mapping)
                                      and isinstance(predictive.get("predictive_basis_fingerprint"), str) else None),
        predictive_retained_set_fingerprint=(predictive.get("predictive_retained_set_fingerprint")
                                              if isinstance(predictive, Mapping)
                                              and isinstance(predictive.get("predictive_retained_set_fingerprint"), str)
                                              else None),
        predictive_statistical_semantics_fingerprint=(predictive.get("statistical_semantics_fingerprint")
                                                       if isinstance(predictive, Mapping)
                                                       and isinstance(predictive.get("statistical_semantics_fingerprint"), str)
                                                       else None),
        predictive_source_model_selection_fingerprint=(
            predictive_semantics.get("source_model_selection_fingerprint")
            if isinstance(predictive_semantics, Mapping)
            and isinstance(predictive_semantics.get("source_model_selection_fingerprint"), str) else None),
        target_projection_set_fingerprint=(projection.get("target_projection_set_fingerprint")
                                            if isinstance(projection, Mapping)
                                            and isinstance(projection.get("target_projection_set_fingerprint"), str)
                                            else None),
        projection_statistical_semantics_fingerprint=(projection.get("statistical_semantics_fingerprint")
                                                       if isinstance(projection, Mapping)
                                                       and isinstance(projection.get("statistical_semantics_fingerprint"), str)
                                                       else None),
        projection_authority=authority_pairs,
        projection_authority_fingerprint=(_fingerprint(authority_payload) if authority_payload else None),
        source_authority_scopes=_frozen_pairs(_safe_source_scopes(source_scopes)),
        authority_scopes=_frozen_pairs(_DERIVED_SCOPES if status == "valid" else _SOURCE_SCOPES),
        diagnostics=_diagnostics(**diagnostic_values),
        _models=models,
    )


def _validated_semantics(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    semantics = record.get("statistical_semantics")
    if not isinstance(semantics, Mapping):
        return None
    try:
        validated = validate_predictive_semantics(semantics)
    except (SemanticsConstructionError, TypeError, ValueError, OverflowError):
        return None
    if record.get("statistical_semantics_fingerprint") != validated.get("statistical_semantics_fingerprint"):
        return None
    return validated


def _authority_state(projection: Mapping[str, Any], expected: Mapping[str, Any]) -> str:
    authority = projection.get("authority")
    if not isinstance(authority, Mapping):
        return "invalid"
    if any(name not in expected for name in _AUTHORITY_FIELDS):
        return "unknown"
    unknown = False
    for name in _AUTHORITY_FIELDS:
        expected_value = expected[name]
        actual = authority.get(name)
        if expected_value is None:
            unknown = True
        elif actual != expected_value:
            return "stale"
    return "unknown" if unknown else "current"


def _source_scope_state(predictive: Mapping[str, Any], predictive_semantics: Mapping[str, Any],
                        projection_semantics: Mapping[str, Any],
                        projection: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """Authenticate and retain the ineligible scope of every source record."""
    sources = {
        "predictive_semantics": predictive_semantics.get("authority_scopes"),
        "projection_semantics": projection_semantics.get("authority_scopes"),
        "projection": projection.get("authority_scopes"),
    }
    artifact_scopes = predictive.get("authority_scopes")
    if artifact_scopes is not None:
        sources["predictive"] = artifact_scopes
    for scopes in sources.values():
        if not isinstance(scopes, Mapping):
            return "invalid", sources
        for name, expected in _SOURCE_SCOPES.items():
            if scopes.get(name) != expected:
                return "invalid", sources
    return "valid", sources


def _normalized_draw_weights(model: Mapping[str, Any], draw_count: int) -> tuple[tuple[float, ...] | None, str | None]:
    weighting = model.get("draw_weighting")
    if draw_count < 1:
        return None, "projection_join_mismatch"
    if weighting == "equal_weight_resampled":
        if model.get("draw_weights") is not None:
            return None, "projection_join_mismatch"
        return tuple(1.0 / draw_count for _ in range(draw_count)), None
    if weighting != "explicit_weights":
        return None, "projection_join_mismatch"
    raw = model.get("draw_weights")
    if not isinstance(raw, list) or len(raw) != draw_count or not all(_is_finite(item) for item in raw):
        return None, "projection_join_mismatch"
    values = tuple(float(item) for item in raw)
    if any(item < 0.0 for item in values):
        return None, "projection_join_mismatch"
    total = sum(values)
    if not math.isfinite(total) or total <= 0.0:
        return None, "projection_join_mismatch"
    return tuple(item / total for item in values), None


def _projection_models(predictive: Mapping[str, Any], projection: Mapping[str, Any]) -> tuple[
        tuple[_QoIModel, ...] | None, str | None]:
    pmodels = predictive.get("models")
    rmodels = projection.get("models")
    if not isinstance(pmodels, list) or not isinstance(rmodels, list):
        return None, "projection_record_invalid"
    if len(pmodels) != len(rmodels):
        return None, "projection_join_mismatch"
    normalized: list[_QoIModel] = []
    for model, projected in zip(pmodels, rmodels):
        if not isinstance(model, Mapping) or not isinstance(projected, Mapping):
            return None, "projection_record_invalid"
        label = model.get("label")
        identity = model.get("identity_label")
        if (projected.get("label") != label or projected.get("identity_label") != identity
                or projected.get("weight") != model.get("weight")):
            return None, "projection_join_mismatch"
        draw_count = model.get("retained_draw_count")
        target = projected.get("target_present_by_draw")
        qoi = projected.get("target_qoi_draws")
        if (not isinstance(draw_count, int) or draw_count < 1 or not isinstance(target, list)
                or not isinstance(qoi, list)):
            return None, "projection_record_invalid"
        if len(target) != draw_count or len(qoi) != draw_count:
            return None, "projection_join_mismatch"
        if not all(isinstance(value, bool) for value in target):
            return None, "projection_record_invalid"
        for present, item in zip(target, qoi):
            if present and not _is_finite(item):
                return None, "projection_record_invalid"
            if not present and item is not None:
                return None, "projection_record_invalid"
        if not isinstance(label, str) or not isinstance(identity, str):
            return None, "projection_record_invalid"
        if not _is_finite(model.get("weight")) or not _is_finite(model.get("source_weight")):
            return None, "predictive_record_invalid"
        draw_weights, weight_problem = _normalized_draw_weights(model, draw_count)
        if weight_problem:
            return None, weight_problem
        retention = model.get("draw_mass_retained")
        if retention is not None and (not _is_finite(retention) or not 0.0 <= float(retention) <= 1.0):
            return None, "predictive_record_invalid"
        normalized.append(_QoIModel(
            label=label,
            identity_label=identity,
            weight=float(model["weight"]),
            source_weight=float(model["source_weight"]),
            draw_basis=model.get("draw_basis") if isinstance(model.get("draw_basis"), str) else None,
            draw_weighting=(model.get("draw_weighting")
                            if isinstance(model.get("draw_weighting"), str) else None),
            draw_mass_retained=float(retention) if _is_finite(retention) else None,
            draw_weights=draw_weights or (),
            target_present=tuple(target),
            qoi_draws=tuple(float(item) if item is not None else None for item in qoi),
        ))
    return tuple(normalized), None


def _particle_codes(predictive: Mapping[str, Any], models: tuple[_QoIModel, ...]) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]]:
    science: list[str] = []
    gaps: list[str] = []
    mass = {"bootstrap": 0.0, "nested": 0.0, "laplace": 0.0, "basis_unknown": 0.0,
            "retention_unknown": 0.0, "dropped_model": 0.0, "dropped_draw": 0.0,
            "target_present": 0.0, "target_absent": 0.0, "qoi_unavailable": 0.0}
    for model in models:
        weight = model.source_weight
        if model.draw_basis == "parametric_bootstrap":
            mass["bootstrap"] += weight
        elif model.draw_basis == "nested_sampling":
            mass["nested"] += weight
        elif model.draw_basis == "laplace_hesse":
            mass["laplace"] += weight
        else:
            mass["basis_unknown"] += weight
        if model.draw_mass_retained is None:
            mass["retention_unknown"] += weight
        else:
            mass["dropped_draw"] += weight * (1.0 - model.draw_mass_retained)
        mass["target_present"] += weight * sum(
            draw_weight for present, draw_weight in zip(model.target_present, model.draw_weights) if present)
        mass["target_absent"] += weight * sum(
            draw_weight for present, draw_weight in zip(model.target_present, model.draw_weights) if not present)
    completeness = predictive.get("completeness")
    dropped = completeness.get("dropped_models") if isinstance(completeness, Mapping) else None
    if isinstance(dropped, list):
        for item in dropped:
            if isinstance(item, Mapping) and _is_finite(item.get("original_weight")):
                mass["dropped_model"] += float(item["original_weight"])
            else:
                mass["basis_unknown"] += PREDICTIVE_MODEL_WEIGHT_FLOOR
    else:
        mass["basis_unknown"] += PREDICTIVE_MODEL_WEIGHT_FLOOR
    if mass["nested"] + mass["laplace"] >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        science.append("predictive_material_nonbootstrap_mass")
    if mass["basis_unknown"] + mass["dropped_model"] >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        gaps.append("predictive_material_basis_unknown")
    if mass["retention_unknown"] >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        gaps.append("predictive_material_retention_unknown")
    return tuple(science), tuple(gaps), {key: round(value, 12) for key, value in mass.items()}


def adapt_omega0_target_projection(predictive_artifact: Any, target_projection: Any, *,
                                   expected_authority: Mapping[str, Any] | None) -> QoIProjectionEvidence:
    """Adapt current `/2` records; every unauthenticated join publishes no particles."""
    if predictive_artifact is None:
        return _evidence(status="missing", gaps=("predictive_record_missing",),
                         projection=target_projection if isinstance(target_projection, Mapping) else None)
    if not isinstance(predictive_artifact, Mapping):
        return _evidence(status="invalid", gaps=("predictive_record_invalid",))
    try:
        validate_master_predictive_artifact(predictive_artifact)
    except PredictiveArtifactError as error:
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection if isinstance(target_projection, Mapping) else None,
                         predictive_validation_reason=error.reason_code)
    predictive_semantics = _validated_semantics(predictive_artifact)
    if predictive_artifact.get("schema") != PREDICTIVE_ARTIFACT_SCHEMA or predictive_semantics is None:
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection if isinstance(target_projection, Mapping) else None,
                         predictive_validation_reason="statistical_semantics_missing_or_invalid")
    if target_projection is None:
        return _evidence(status="missing", gaps=("projection_record_missing",), predictive=predictive_artifact,
                         predictive_semantics=predictive_semantics)
    if not isinstance(target_projection, Mapping):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         predictive_semantics=predictive_semantics)
    projection_semantics = _validated_semantics(target_projection)
    if (target_projection.get("schema") != TARGET_PROJECTION_SCHEMA
            or target_projection.get("qoi") != TARGET_QOI
            or target_projection.get("projection_version") != TARGET_PROJECTION_VERSION
            or projection_semantics is None):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics)
    if any(predictive_artifact.get(name) != predictive_semantics.get(name) for name in (
            "predictive_retained_set_fingerprint", "construction_complete")):
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics)
    scope_state, source_scopes = _source_scope_state(
        predictive_artifact, predictive_semantics, projection_semantics, target_projection)
    if scope_state != "valid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes,
                         projection_scope_state=scope_state)
    if not isinstance(expected_authority, Mapping):
        return _evidence(status="invalid", gaps=("projection_currentness_unknown",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)
    authority = _authority_state(target_projection, expected_authority)
    if authority == "invalid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)

    science: list[str] = []
    gaps: list[str] = []
    if authority == "stale":
        science.append("projection_stale")
    elif authority == "unknown":
        gaps.append("projection_currentness_unknown")

    join_pairs = (
        (predictive_artifact.get("predictive_basis_fingerprint"), target_projection.get("predictive_basis_fingerprint")),
        (predictive_artifact.get("predictive_retained_set_fingerprint"), target_projection.get("source_predictive_retained_set_fingerprint")),
        (predictive_artifact.get("statistical_semantics_fingerprint"), target_projection.get("source_predictive_semantics_fingerprint")),
    )
    for left, right in join_pairs:
        if not isinstance(left, str) or not isinstance(right, str):
            gaps.append("projection_join_unknown")
        elif left != right:
            science.append("projection_join_mismatch")
    # The target projection repeats these source identities at the envelope and
    # semantic-payload layers.  Validate both representations before the
    # envelope can authenticate a predictive join: a self-consistent semantic
    # payload for another source must not piggyback on unchanged top-level links.
    for envelope_name, semantics_name in (
            ("source_predictive_retained_set_fingerprint",
             "source_predictive_retained_set_fingerprint"),
            ("source_predictive_semantics_fingerprint",
             "source_predictive_semantics_fingerprint"),
            ("target_projection_set_fingerprint",
             "target_projection_set_fingerprint"),
            ("complete", "construction_complete")):
        if target_projection.get(envelope_name) != projection_semantics.get(semantics_name):
            science.append("projection_join_mismatch")
    try:
        recomputed_set = target_projection_set_fingerprint(target_projection.get("models"))
    except (SemanticsConstructionError, TypeError, ValueError):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)
    for stored in (
            target_projection.get("target_projection_set_fingerprint"),
            projection_semantics.get("target_projection_set_fingerprint")):
        if not isinstance(stored, str):
            gaps.append("projection_join_unknown")
        elif stored != recomputed_set:
            science.append("projection_join_mismatch")
    if target_projection.get("complete") != projection_semantics.get("construction_complete"):
        science.append("projection_join_mismatch")

    models, problem = _projection_models(predictive_artifact, target_projection)
    if problem == "projection_record_invalid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)
    if problem == "predictive_record_invalid":
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)
    if problem == "projection_join_mismatch":
        science.append(problem)
    if "projection_join_mismatch" in science or "projection_join_unknown" in gaps:
        models = ()

    complete = target_projection.get("complete")
    target_complete = target_projection.get("target_projection_complete")
    if complete is not target_complete or not isinstance(complete, bool):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, predictive_semantics=predictive_semantics,
                         projection_semantics=projection_semantics, source_scopes=source_scopes)
    if not complete:
        science.append("projection_incomplete")
    if models:
        particle_science, particle_gaps, masses = _particle_codes(predictive_artifact, models)
        science.extend(particle_science)
        gaps.extend(particle_gaps)
    else:
        masses = {}
    status = "valid" if not gaps and not science else "qualified"
    return _evidence(status=status, science=tuple(science), gaps=tuple(gaps), predictive=predictive_artifact,
                     projection=target_projection, predictive_semantics=predictive_semantics,
                     projection_semantics=projection_semantics, source_scopes=source_scopes,
                     models=tuple(models or ()), authority_state=authority,
                     recomputed_target_projection_set_fingerprint=recomputed_set,
                     particle_source_masses=masses)

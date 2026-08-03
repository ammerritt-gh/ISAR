"""Target-QoI evidence adapters used by the EIG eligibility module.

This module owns the current omega0 projection adapter.  Its public result is
generic enough that later QoI producers can use the same eligibility seam
without exposing their persistence layout to callers.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    validate_master_predictive_artifact,
)
from isar.schema.statistics import SemanticsConstructionError, validate_predictive_semantics


QOI_EVIDENCE_CONTRACT = "isar.eig_qoi_projection_evidence/o36.1"
OMEGA0_ADAPTER_REVISION = "isar.target_projection_omega0_adapter/1"

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
_AUTHORITY_FIELDS = (
    "action_role", "master_revision", "track_revision", "track_state",
    "candidate_id", "candidate_state", "selected_candidate_index",
    "calibration_fingerprint", "policy_fingerprint",
)


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _frozen_pairs(value: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _diagnostics(**values: Any) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((name, _freeze(value)) for name, value in values.items()))


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


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
    target_present: tuple[bool, ...]
    qoi_draws: tuple[float | None, ...]


@dataclass(frozen=True)
class QoIProjectionEvidence:
    """Immutable generic view of a predictive target-QoI projection.

    ``scientific_codes`` and ``observability_codes`` are already normalized to
    the shared EIG vocabulary.  Consumers never need to inspect producer-specific
    artifact paths or invent their own fallback policy.
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
    projection_fingerprint: str | None = None
    source_authority_scopes: tuple[tuple[str, Any], ...] = ()
    authority_scopes: tuple[tuple[str, Any], ...] = ()
    diagnostics: tuple[tuple[str, Any], ...] = ()
    _models: tuple[_QoIModel, ...] = ()

    @property
    def models(self) -> tuple[Mapping[str, Any], ...]:
        """Read-only diagnostic model views; the normalized implementation stays private."""
        return tuple(MappingProxyType({
            "label": item.label,
            "identity_label": item.identity_label,
            "weight": item.weight,
            "source_weight": item.source_weight,
            "draw_basis": item.draw_basis,
            "draw_weighting": item.draw_weighting,
            "draw_mass_retained": item.draw_mass_retained,
            "target_present_by_draw": item.target_present,
            "target_qoi_draws": item.qoi_draws,
        }) for item in self._models)

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
            "projection_fingerprint": self.projection_fingerprint,
            "source_authority_scopes": dict(self.source_authority_scopes),
            "authority_scopes": dict(self.authority_scopes),
            "diagnostics": _thaw(dict(self.diagnostics)),
            "models": [_thaw(item) for item in self.models],
        }


def _evidence(*, status: str, science: tuple[str, ...] = (), gaps: tuple[str, ...] = (),
              predictive: Mapping[str, Any] | None = None, projection: Mapping[str, Any] | None = None,
              source_scopes: Mapping[str, Any] | None = None, models: tuple[_QoIModel, ...] = (),
              **diagnostic_values: Any) -> QoIProjectionEvidence:
    predictive_fingerprint = (
        predictive.get("predictive_basis_fingerprint")
        if isinstance(predictive, Mapping) and isinstance(predictive.get("predictive_basis_fingerprint"), str)
        else None
    )
    projection_fingerprint = (
        projection.get("target_projection_set_fingerprint")
        if isinstance(projection, Mapping) and isinstance(projection.get("target_projection_set_fingerprint"), str)
        else None
    )
    return QoIProjectionEvidence(
        contract=QOI_EVIDENCE_CONTRACT,
        adapter_revision=OMEGA0_ADAPTER_REVISION,
        qoi_id="excitation.energy",
        qoi_unit="meV",
        source_qoi="omega0",
        status=status,
        scientific_codes=tuple(dict.fromkeys(science)),
        observability_codes=tuple(dict.fromkeys(gaps)),
        predictive_basis_fingerprint=predictive_fingerprint,
        projection_fingerprint=projection_fingerprint,
        source_authority_scopes=_frozen_pairs(source_scopes),
        authority_scopes=_frozen_pairs(_DERIVED_SCOPES),
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
    # A partial authority snapshot cannot prove that a previously resolved
    # projection remains current.  O36 requires the complete producer-owned
    # tuple, including the fit/refit role which is deliberately separate from
    # the acquisition-action gate.
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
    """Authenticate, and preserve, the ineligible scopes of all source records.

    Source artifacts are inputs, not an authority upgrade.  The restricted O36
    candidate scope is carried only by the evidence derived from their joined
    contents below.
    """
    sources = {
        "predictive_semantics": predictive_semantics.get("authority_scopes"),
        "projection_semantics": projection_semantics.get("authority_scopes"),
        "projection": projection.get("authority_scopes"),
    }
    # Current master-predictive `/2` puts authority scopes in its authenticated
    # semantics payload.  Accept that producer form, but if an additive
    # artifact-level declaration is present it is source evidence too and may
    # not silently contradict the semantics.
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
        if (not isinstance(draw_count, int) or draw_count < 0 or not isinstance(target, list)
                or not isinstance(qoi, list) or len(target) != draw_count or len(qoi) != draw_count):
            return None, "projection_record_invalid"
        if not all(isinstance(value, bool) for value in target):
            return None, "projection_record_invalid"
        for present, value in zip(target, qoi):
            if present:
                if not _is_finite(value):
                    return None, "projection_record_invalid"
            elif value is not None:
                return None, "projection_record_invalid"
        if not isinstance(label, str) or not isinstance(identity, str):
            return None, "projection_record_invalid"
        if not _is_finite(model.get("weight")) or not _is_finite(model.get("source_weight")):
            return None, "predictive_record_invalid"
        retention = model.get("draw_mass_retained")
        normalized.append(_QoIModel(
            label=label,
            identity_label=identity,
            weight=float(model["weight"]),
            source_weight=float(model["source_weight"]),
            draw_basis=model.get("draw_basis") if isinstance(model.get("draw_basis"), str) else None,
            draw_weighting=(model.get("draw_weighting")
                            if isinstance(model.get("draw_weighting"), str) else None),
            draw_mass_retained=float(retention) if _is_finite(retention) else None,
            target_present=tuple(target),
            qoi_draws=tuple(float(value) if value is not None else None for value in qoi),
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
        draw_count = len(model.target_present)
        if draw_count:
            present_count = sum(model.target_present)
            mass["target_present"] += weight * present_count / draw_count
            mass["target_absent"] += weight * (draw_count - present_count) / draw_count
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
    """Adapt authenticated current `/2` omega0 artifacts into generic QoI evidence.

    Invalid or incomplete producer inputs return a closed evidence object rather than throwing, so
    every caller can obtain the shared eligibility verdict and its stable diagnostic reason.
    """
    if predictive_artifact is None:
        return _evidence(status="missing", gaps=("predictive_record_missing",),
                         predictive=None, projection=target_projection if isinstance(target_projection, Mapping) else None)
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
        return _evidence(status="missing", gaps=("projection_record_missing",), predictive=predictive_artifact)
    if not isinstance(target_projection, Mapping):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact)
    projection_semantics = _validated_semantics(target_projection)
    if (target_projection.get("schema") != TARGET_PROJECTION_SCHEMA
            or target_projection.get("qoi") != TARGET_QOI
            or target_projection.get("projection_version") != TARGET_PROJECTION_VERSION
            or projection_semantics is None):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection)
    # The projection's duplicated top-level linkage fields are intentionally
    # authenticated against its semantics payload before positional data are
    # interpreted.  A valid semantics fingerprint alone cannot authenticate a
    # later mutation of one of those additive top-level fields.
    if any(target_projection.get(name) != projection_semantics.get(name) for name in (
            "source_predictive_retained_set_fingerprint",
            "source_predictive_semantics_fingerprint",
            "target_projection_set_fingerprint",
    )) or target_projection.get("complete") != projection_semantics.get("construction_complete"):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection)
    if any(predictive_artifact.get(name) != predictive_semantics.get(name) for name in (
            "predictive_retained_set_fingerprint", "construction_complete")):
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection)
    scope_state, source_scopes = _source_scope_state(
        predictive_artifact, predictive_semantics, projection_semantics, target_projection)
    if scope_state != "valid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes,
                         projection_scope_state=scope_state)
    if not isinstance(expected_authority, Mapping):
        return _evidence(status="invalid", gaps=("projection_currentness_unknown",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes)
    authority = _authority_state(target_projection, expected_authority)
    if authority == "invalid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes)
    science: list[str] = []
    gaps: list[str] = []
    if authority == "stale":
        science.append("projection_stale")
    elif authority == "unknown":
        gaps.append("projection_currentness_unknown")
    for predictive_key, projection_key in (
        ("predictive_basis_fingerprint", "predictive_basis_fingerprint"),
        ("predictive_retained_set_fingerprint", "source_predictive_retained_set_fingerprint"),
        ("statistical_semantics_fingerprint", "source_predictive_semantics_fingerprint"),
    ):
        left = predictive_artifact.get(predictive_key)
        right = target_projection.get(projection_key)
        if not isinstance(left, str) or not isinstance(right, str):
            gaps.append("projection_join_unknown")
        elif left != right:
            science.append("projection_join_mismatch")
    models, problem = _projection_models(predictive_artifact, target_projection)
    if problem == "projection_record_invalid":
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes)
    if problem == "predictive_record_invalid":
        return _evidence(status="invalid", gaps=("predictive_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes)
    if problem == "projection_join_mismatch":
        science.append(problem)
        models = ()
    complete = target_projection.get("complete")
    target_complete = target_projection.get("target_projection_complete")
    if complete is not target_complete or not isinstance(complete, bool):
        return _evidence(status="invalid", gaps=("projection_record_invalid",), predictive=predictive_artifact,
                         projection=target_projection, source_scopes=source_scopes)
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
                     projection=target_projection, source_scopes=source_scopes, models=tuple(models or ()),
                     authority_state=authority, particle_source_masses=masses)

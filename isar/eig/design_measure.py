"""Pure construction of the homogeneous scan-window EIG design measure."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from isar.predictive import (
    PREDICTIVE_MAX_DROPPED_DRAW_MASS,
    PREDICTIVE_MAX_PRUNED_MASS,
    PREDICTIVE_MODEL_WEIGHT_FLOOR,
    PredictiveArtifactError,
    validate_master_predictive_artifact,
)
from isar.schema.statistics import semantics_fingerprint

from .eligibility import ELIGIBILITY_CONTRACT, EligibilityInput, evaluate_eig_eligibility
from .qoi import validate_qoi_projection_evidence


DESIGN_MEASURE_CONTRACT = "isar.scan_window_design_measure/o36.3"
SOURCE_DRAW_IDENTITY_DOMAIN = "isar.source_draw_identity/o36.3"
PREDICTIVE_SCENARIO_DOMAIN = "isar.predictive_scenario/o36.3"
DESIGN_MEASURE_FINGERPRINT_DOMAIN = "isar.scan_window_design_measure_fingerprint/o36.3"

_INELIGIBLE_SCOPES = {
    "eig_shadow": "ineligible",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_AVAILABLE_SCOPES = {
    "eig_shadow": "candidate",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_SEMANTICS = {
    "model_weight_kind": "akaike_relative_weight",
    "scenario_source_kind": "parametric_bootstrap_sampling_distribution",
    "scenario_measure_kind": "weighted_empirical_predictive_scenarios",
    "inference_paradigm": "frequentist_sampling_distribution",
    "formal_bayesian": False,
    "conditioning": "authenticated_target_qoi_available",
}
_NORMALIZATION_REL_TOL = 1e-12
_NORMALIZATION_ABS_TOL = 1e-12
log = logging.getLogger(__name__)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _pairs(value: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))


def _fingerprint(domain: str, payload: Any) -> str:
    return semantics_fingerprint(domain, _thaw(payload))


def _source_draw_identity_fingerprint(
        records: Sequence[Mapping[str, Any]]) -> str:
    """Bind the producer-ordered model/source-draw identity used across EIG slices."""
    payload = [{
        "model_label": record["model_label"],
        "source_draw_ids": list(record["source_draw_ids"]),
    } for record in records]
    return _fingerprint(SOURCE_DRAW_IDENTITY_DOMAIN, payload)


def _predictive_scenario_id(
        predictive_basis_fingerprint: str,
        model_label: str,
        source_draw_id: str) -> str:
    """Return the one Slice 3/4 join identity for a predictive scenario."""
    return _fingerprint(PREDICTIVE_SCENARIO_DOMAIN, {
        "predictive_basis_fingerprint": predictive_basis_fingerprint,
        "model_label": model_label,
        "source_draw_id": source_draw_id,
    })


def _dedupe(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _close(left: Any, right: Any) -> bool:
    return (_finite(left) and _finite(right)
            and math.isclose(float(left), float(right),
                             rel_tol=_NORMALIZATION_REL_TOL,
                             abs_tol=_NORMALIZATION_ABS_TOL))


def _materiality_values() -> dict[str, float]:
    return {
        "predictive_model_weight_floor": PREDICTIVE_MODEL_WEIGHT_FLOOR,
        "normalization_relative_tolerance": _NORMALIZATION_REL_TOL,
        "normalization_absolute_tolerance": _NORMALIZATION_ABS_TOL,
    }


@dataclass(frozen=True)
class _DesignModel:
    label: str
    identity_label: str
    source_weight: float
    published_predictive_weight: float
    retained_predictive_weight: float
    design_ensemble_weight: float
    draw_basis: str
    draw_weighting: str
    source_draw_count: int
    retained_source_draw_count: int
    conditional_scenario_count: int
    source_target_qoi_available_mass: float
    source_target_qoi_unavailable_mass: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "identity_label": self.identity_label,
            "source_weight": self.source_weight,
            "published_predictive_weight": self.published_predictive_weight,
            "retained_predictive_weight": self.retained_predictive_weight,
            "design_ensemble_weight": self.design_ensemble_weight,
            "draw_basis": self.draw_basis,
            "draw_weighting": self.draw_weighting,
            "source_draw_count": self.source_draw_count,
            "retained_source_draw_count": self.retained_source_draw_count,
            "conditional_scenario_count": self.conditional_scenario_count,
            "source_target_qoi_available_mass": self.source_target_qoi_available_mass,
            "source_target_qoi_unavailable_mass": self.source_target_qoi_unavailable_mass,
        }


@dataclass(frozen=True)
class _PredictiveScenario:
    scenario_id: str
    model_label: str
    identity_label: str
    source_draw_id: str
    qoi_value: float
    source_weight: float
    published_predictive_weight: float
    retained_predictive_weight: float
    source_draw_weight: float
    preconditioning_scenario_mass: float
    conditional_scenario_weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "model_label": self.model_label,
            "identity_label": self.identity_label,
            "source_draw_id": self.source_draw_id,
            "qoi_value": self.qoi_value,
            "source_weight": self.source_weight,
            "published_predictive_weight": self.published_predictive_weight,
            "retained_predictive_weight": self.retained_predictive_weight,
            "source_draw_weight": self.source_draw_weight,
            "preconditioning_scenario_mass": self.preconditioning_scenario_mass,
            "conditional_scenario_weight": self.conditional_scenario_weight,
        }


@dataclass(frozen=True)
class ScanWindowDesignMeasure:
    """Immutable ``o36.3`` empirical measure consumed by later EIG slices."""

    contract: str
    status: str
    reason_codes: tuple[str, ...]
    construction_codes: tuple[str, ...]
    eligibility: tuple[tuple[str, Any], ...]
    qoi: tuple[tuple[str, Any], ...] = ()
    semantics: tuple[tuple[str, Any], ...] = field(default_factory=lambda: _pairs(_SEMANTICS))
    source_identities: tuple[tuple[str, Any], ...] = ()
    materiality: tuple[tuple[str, Any], ...] = field(
        default_factory=lambda: _pairs(_materiality_values()))
    mass_diagnostics: tuple[tuple[str, Any], ...] = ()
    excluded_models: tuple[Mapping[str, Any], ...] = ()
    authority_scopes: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: tuple(sorted(_INELIGIBLE_SCOPES.items())))
    source_draw_identity_fingerprint: str | None = None
    design_measure_fingerprint: str = ""
    diagnostics: tuple[tuple[str, Any], ...] = ()
    _models: tuple[_DesignModel, ...] = ()
    _predictive_scenarios: tuple[_PredictiveScenario, ...] = ()

    @property
    def models(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(MappingProxyType(_freeze(model.to_dict())) for model in self._models)

    @property
    def predictive_scenarios(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(MappingProxyType(_freeze(item.to_dict())) for item in self._predictive_scenarios)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "construction_codes": list(self.construction_codes),
            "eligibility": _thaw(dict(self.eligibility)),
            "qoi": _thaw(dict(self.qoi)) if self.qoi else None,
            "semantics": _thaw(dict(self.semantics)),
            "source_identities": _thaw(dict(self.source_identities)) if self.source_identities else None,
            "materiality": _thaw(dict(self.materiality)),
            "mass_diagnostics": _thaw(dict(self.mass_diagnostics)),
            "excluded_models": [_thaw(item) for item in self.excluded_models],
            "models": [_thaw(item) for item in self.models],
            "predictive_scenarios": [_thaw(item) for item in self.predictive_scenarios],
            "authority_scopes": dict(self.authority_scopes),
            "source_draw_identity_fingerprint": self.source_draw_identity_fingerprint,
            "design_measure_fingerprint": self.design_measure_fingerprint,
            "diagnostics": _thaw(dict(self.diagnostics)),
        }


def _design_measure_fingerprint_payload(measure: ScanWindowDesignMeasure) -> dict[str, Any]:
    """Return the complete ``o36.3`` semantic envelope, excluding diagnostics."""
    payload = measure.to_dict()
    payload.pop("design_measure_fingerprint")
    payload.pop("diagnostics")
    return payload


def _valid_fingerprint(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64 and value.lower() == value
            and all(character in "0123456789abcdef" for character in value))


def _validate_measure_codes(measure: ScanWindowDesignMeasure) -> None:
    for name in ("reason_codes", "construction_codes"):
        codes = getattr(measure, name)
        if (not isinstance(codes, tuple)
                or any(not isinstance(code, str) or not code for code in codes)
                or len(codes) != len(set(codes))):
            raise ValueError(f"design measure {name} must be a unique non-empty string tuple")
    eligibility = dict(measure.eligibility)
    scientific = eligibility.get("scientific_codes")
    observability = eligibility.get("observability_codes")
    if (not isinstance(scientific, tuple) or not isinstance(observability, tuple)
            or any(not isinstance(code, str) or not code
                   for code in (*scientific, *observability))
            or len(scientific) != len(set(scientific))
            or len(observability) != len(set(observability))):
        raise ValueError("design measure eligibility codes are malformed")
    expected = _dedupe(scientific, observability, measure.construction_codes)
    if measure.reason_codes != expected:
        raise ValueError("design measure reason codes disagree with their sources")


def _validate_available_design_measure(measure: ScanWindowDesignMeasure) -> None:
    eligibility = dict(measure.eligibility)
    if (eligibility.get("contract") != ELIGIBILITY_CONTRACT
            or eligibility.get("status") != "eligible"
            or eligibility.get("scientific_codes") != ()
            or eligibility.get("observability_codes") != ()
            or not _valid_fingerprint(eligibility.get("input_fingerprint"))
            or not _valid_fingerprint(eligibility.get("result_fingerprint"))):
        raise ValueError("available design measure eligibility is not authenticated and eligible")
    if measure.reason_codes or measure.construction_codes:
        raise ValueError("available design measure cannot carry refusal codes")
    if tuple(measure.semantics) != _pairs(_SEMANTICS):
        raise ValueError("available design measure semantics do not match o36.3")
    if tuple(measure.materiality) != _pairs(_materiality_values()):
        raise ValueError("available design measure materiality does not match o36.3")
    if tuple(measure.authority_scopes) != tuple(sorted(_AVAILABLE_SCOPES.items())):
        raise ValueError("available design measure authority scopes exceed o36.3")

    qoi = dict(measure.qoi)
    if (set(qoi) != {"id", "unit", "source_qoi", "conditioning"}
            or any(not isinstance(qoi.get(name), str) or not qoi[name]
                   for name in ("id", "unit", "source_qoi"))
            or qoi.get("conditioning") != _SEMANTICS["conditioning"]):
        raise ValueError("available design measure QoI metadata is malformed")
    source_identities = dict(measure.source_identities)
    expected_source_names = {
        "predictive_basis_fingerprint",
        "predictive_retained_set_fingerprint",
        "predictive_statistical_semantics_fingerprint",
        "source_model_selection_fingerprint",
        "target_projection_set_fingerprint",
        "projection_statistical_semantics_fingerprint",
        "projection_authority_fingerprint",
        "qoi_evidence_fingerprint",
    }
    if (set(source_identities) != expected_source_names
            or any(not _valid_fingerprint(value) for value in source_identities.values())
            or not _valid_fingerprint(measure.source_draw_identity_fingerprint)):
        raise ValueError("available design measure source identities are malformed")

    models = list(measure.models)
    scenarios = list(measure.predictive_scenarios)
    if not models or not scenarios:
        raise ValueError("available design measure has an empty design ensemble")
    model_labels = [model.get("label") for model in models]
    if (any(not isinstance(label, str) or not label for label in model_labels)
            or len(model_labels) != len(set(model_labels))):
        raise ValueError("available design measure model labels are malformed")
    excluded_labels = [model.get("label") for model in measure.excluded_models
                       if isinstance(model, Mapping)]
    if (len(excluded_labels) != len(measure.excluded_models)
            or any(not isinstance(label, str) or not label for label in excluded_labels)
            or len(excluded_labels) != len(set(excluded_labels))
            or set(excluded_labels) & set(model_labels)):
        raise ValueError("available design measure excluded models are malformed")

    scenario_ids: list[str] = []
    join_keys: list[tuple[str, str]] = []
    preconditioning_masses: list[float] = []
    scenario_weights: list[float] = []
    predictive_basis = source_identities["predictive_basis_fingerprint"]
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id")
        model_label = scenario.get("model_label")
        source_draw_id = scenario.get("source_draw_id")
        if (not _valid_fingerprint(scenario_id)
                or not isinstance(model_label, str) or not model_label
                or not isinstance(source_draw_id, str) or not source_draw_id
                or model_label not in model_labels
                or scenario_id != _predictive_scenario_id(
                    predictive_basis, model_label, source_draw_id)):
            raise ValueError("available design measure scenario identity is malformed")
        numeric_names = (
            "qoi_value", "source_weight", "published_predictive_weight",
            "retained_predictive_weight", "source_draw_weight",
            "preconditioning_scenario_mass", "conditional_scenario_weight",
        )
        if any(not _finite(scenario.get(name)) for name in numeric_names):
            raise ValueError("available design measure scenario values must be finite")
        if any(float(scenario[name]) <= 0.0 for name in numeric_names if name != "qoi_value"):
            raise ValueError("available design measure scenario masses must be positive")
        weight = float(scenario["conditional_scenario_weight"])
        scenario_ids.append(scenario_id)
        join_keys.append((model_label, source_draw_id))
        preconditioning_masses.append(float(scenario["preconditioning_scenario_mass"]))
        scenario_weights.append(weight)
    if len(scenario_ids) != len(set(scenario_ids)) or len(join_keys) != len(set(join_keys)):
        raise ValueError("available design measure scenario identities are not unique")
    if join_keys != sorted(join_keys):
        raise ValueError("available design measure scenario order is not canonical")
    if not _close(math.fsum(scenario_weights), 1.0):
        raise ValueError("available design measure scenario weights are not normalized")
    preconditioning_total = math.fsum(preconditioning_masses)
    if (preconditioning_total <= 0.0
            or any(not _close(
                scenario["preconditioning_scenario_mass"],
                float(scenario["retained_predictive_weight"])
                * float(scenario["source_draw_weight"]),
            ) for scenario in scenarios)
            or any(not _close(
                scenario["conditional_scenario_weight"],
                float(scenario["preconditioning_scenario_mass"]) / preconditioning_total,
            ) for scenario in scenarios)):
        raise ValueError("available design measure scenario mass semantics disagree")

    model_weights: list[float] = []
    retained_model_weights: list[float] = []
    for model in models:
        numeric_names = (
            "source_weight", "published_predictive_weight", "retained_predictive_weight",
            "design_ensemble_weight", "source_target_qoi_available_mass",
            "source_target_qoi_unavailable_mass",
        )
        if (not isinstance(model.get("identity_label"), str) or not model["identity_label"]
                or model.get("draw_basis") != "parametric_bootstrap"
                or model.get("draw_weighting") != "equal_weight_resampled"
                or any(not _finite(model.get(name)) or float(model[name]) < 0.0
                       for name in numeric_names)):
            raise ValueError("available design measure model values are malformed")
        for name in ("source_draw_count", "retained_source_draw_count",
                     "conditional_scenario_count"):
            value = model.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("available design measure model counts are malformed")
        if (model["source_draw_count"] < model["retained_source_draw_count"]
                or model["retained_source_draw_count"] < model["conditional_scenario_count"]):
            raise ValueError("available design measure model counts disagree")
        matching = [scenario for scenario in scenarios
                    if scenario["model_label"] == model["label"]]
        if (len(matching) != model["conditional_scenario_count"]
                or not _close(math.fsum(float(item["conditional_scenario_weight"])
                                        for item in matching),
                              model["design_ensemble_weight"])
                or any(item["identity_label"] != model["identity_label"]
                       or not _close(item["source_weight"], model["source_weight"])
                       or not _close(item["published_predictive_weight"],
                                     model["published_predictive_weight"])
                       or not _close(item["retained_predictive_weight"],
                                     model["retained_predictive_weight"])
                       or not _close(
                           item["source_draw_weight"],
                           1.0 / model["retained_source_draw_count"],
                       )
                       for item in matching)):
            raise ValueError("available design measure model/scenario masses disagree")
        model_weights.append(float(model["design_ensemble_weight"]))
        retained_model_weights.append(float(model["retained_predictive_weight"]))
    if not _close(math.fsum(model_weights), 1.0):
        raise ValueError("available design measure model weights are not normalized")
    if not _close(math.fsum(retained_model_weights), 1.0):
        raise ValueError("available design measure retained model weights are not normalized")


def validate_scan_window_design_measure(measure: Any) -> ScanWindowDesignMeasure:
    """Strictly re-authenticate one immutable ``o36.3`` design measure."""
    if not isinstance(measure, ScanWindowDesignMeasure):
        raise TypeError("measure must be a ScanWindowDesignMeasure")
    if measure.contract != DESIGN_MEASURE_CONTRACT or measure.status not in {
            "available", "unavailable"}:
        raise ValueError("design measure metadata does not match o36.3")
    _validate_measure_codes(measure)
    eligibility = dict(measure.eligibility)
    if (set(eligibility) != {
            "contract", "status", "scientific_codes", "observability_codes",
            "input_fingerprint", "result_fingerprint"}
            or eligibility.get("contract") != ELIGIBILITY_CONTRACT
            or eligibility.get("status") not in {"eligible", "ineligible", "unassessable"}
            or not _valid_fingerprint(eligibility.get("input_fingerprint"))
            or not _valid_fingerprint(eligibility.get("result_fingerprint"))):
        raise ValueError("design measure eligibility envelope is malformed")
    if tuple(measure.semantics) != _pairs(_SEMANTICS):
        raise ValueError("design measure semantics do not match o36.3")
    if tuple(measure.materiality) != _pairs(_materiality_values()):
        raise ValueError("design measure materiality does not match o36.3")
    expected_scopes = _AVAILABLE_SCOPES if measure.status == "available" else _INELIGIBLE_SCOPES
    if tuple(measure.authority_scopes) != tuple(sorted(expected_scopes.items())):
        raise ValueError("design measure authority scopes disagree with status")
    if measure.status == "available":
        _validate_available_design_measure(measure)
    elif measure.models or measure.predictive_scenarios:
        raise ValueError("unavailable design measure contains a partial design ensemble")
    try:
        expected = _fingerprint(
            DESIGN_MEASURE_FINGERPRINT_DOMAIN,
            _design_measure_fingerprint_payload(measure),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        log.warning("design measure read gate could not canonicalize evidence: %s", error)
        raise ValueError("design measure contains malformed immutable records") from error
    if (not _valid_fingerprint(measure.design_measure_fingerprint)
            or expected != measure.design_measure_fingerprint):
        raise ValueError("design_measure_fingerprint does not authenticate the design measure")
    return measure


def _eligibility_payload(result: Any) -> dict[str, Any]:
    return {
        "contract": result.contract,
        "status": result.status,
        "scientific_codes": list(result.scientific_codes),
        "observability_codes": list(result.observability_codes),
        "input_fingerprint": result.input_fingerprint,
        "result_fingerprint": result.result_fingerprint,
    }


def _finish(*, eligibility: Any, construction_codes: Sequence[str] = (),
            status: str = "unavailable", qoi: Mapping[str, Any] | None = None,
            source_identities: Mapping[str, Any] | None = None,
            mass_diagnostics: Mapping[str, Any] | None = None,
            excluded_models: Sequence[Mapping[str, Any]] = (),
            source_draw_identity_fingerprint: str | None = None,
            models: Sequence[_DesignModel] = (), scenarios: Sequence[_PredictiveScenario] = (),
            diagnostic_values: Mapping[str, Any] | None = None) -> ScanWindowDesignMeasure:
    scientific_codes = tuple(eligibility.scientific_codes)
    observability_codes = tuple(eligibility.observability_codes)
    construction_codes = tuple(dict.fromkeys(str(code) for code in construction_codes))
    reason_codes = _dedupe(scientific_codes, observability_codes, construction_codes)
    scopes = _AVAILABLE_SCOPES if status == "available" else _INELIGIBLE_SCOPES
    values = {
        "contract": DESIGN_MEASURE_CONTRACT,
        "status": status,
        "reason_codes": reason_codes,
        "construction_codes": construction_codes,
        "eligibility": _pairs(_eligibility_payload(eligibility)),
        "qoi": _pairs(qoi),
        "semantics": _pairs(_SEMANTICS),
        "source_identities": _pairs(source_identities),
        "mass_diagnostics": _pairs(mass_diagnostics),
        "excluded_models": tuple(_freeze(item) for item in excluded_models),
        "authority_scopes": tuple(sorted(scopes.items())),
        "source_draw_identity_fingerprint": source_draw_identity_fingerprint,
        "diagnostics": _pairs(diagnostic_values),
        "_models": tuple(models),
        "_predictive_scenarios": tuple(scenarios),
    }
    provisional = ScanWindowDesignMeasure(**values)
    fingerprint = _fingerprint(
        DESIGN_MEASURE_FINGERPRINT_DOMAIN,
        _design_measure_fingerprint_payload(provisional),
    )
    return ScanWindowDesignMeasure(**values, design_measure_fingerprint=fingerprint)


def _normalized_draw_weights(model: Mapping[str, Any], draw_count: int) -> tuple[float, ...] | None:
    weighting = model.get("draw_weighting")
    if weighting == "equal_weight_resampled":
        return tuple(1.0 / draw_count for _ in range(draw_count))
    if weighting != "explicit_weights":
        return None
    values = model.get("draw_weights")
    if (not isinstance(values, Sequence) or isinstance(values, (str, bytes))
            or len(values) != draw_count or not all(_finite(item) and float(item) >= 0 for item in values)):
        return None
    total = sum(float(item) for item in values)
    if not math.isfinite(total) or total <= 0.0:
        return None
    return tuple(float(item) / total for item in values)


def _basis_name(value: Any) -> str:
    if value == "parametric_bootstrap":
        return "bootstrap"
    if value == "nested_sampling":
        return "nested_sampling"
    if value == "laplace_hesse":
        return "laplace_hesse"
    return "unknown_basis"


def _source_mass_state(artifact: Mapping[str, Any], models: Sequence[Mapping[str, Any]],
                       qoi_models: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    masses = {
        "bootstrap": 0.0, "nested": 0.0, "laplace": 0.0, "basis_unknown": 0.0,
        "retention_unknown": 0.0, "dropped_model": 0.0, "dropped_draw": 0.0,
        "target_present": 0.0, "target_absent": 0.0, "qoi_unavailable": 0.0,
    }
    for model, qmodel in zip(models, qoi_models):
        source_weight = float(model["source_weight"])
        basis = _basis_name(model.get("draw_basis"))
        key = {"bootstrap": "bootstrap", "nested_sampling": "nested",
               "laplace_hesse": "laplace", "unknown_basis": "basis_unknown"}[basis]
        masses[key] += source_weight
        retention = model.get("draw_mass_retained")
        if not _finite(retention):
            masses["retention_unknown"] += source_weight
        else:
            masses["dropped_draw"] += source_weight * (1.0 - float(retention))
        masses["target_present"] += source_weight * sum(
            float(draw_weight) for present, draw_weight in zip(
                qmodel["target_present_by_draw"], qmodel["draw_weights"]) if present)
        masses["target_absent"] += source_weight * sum(
            float(draw_weight) for present, draw_weight in zip(
                qmodel["target_present_by_draw"], qmodel["draw_weights"]) if not present)
    completeness = artifact.get("completeness")
    dropped = completeness.get("dropped_models") if isinstance(completeness, Mapping) else None
    if isinstance(dropped, list):
        for item in dropped:
            if isinstance(item, Mapping) and _finite(item.get("original_weight")):
                masses["dropped_model"] += float(item["original_weight"])
            else:
                masses["basis_unknown"] += PREDICTIVE_MODEL_WEIGHT_FLOOR
    else:
        masses["basis_unknown"] += PREDICTIVE_MODEL_WEIGHT_FLOOR
    return masses


def _compatibility_masses_match(qoi_evidence: Any, recomputed: Mapping[str, float]) -> bool:
    """Compare at the 12-decimal precision published by the frozen Slice 2 adapter."""
    diagnostics = dict(qoi_evidence.diagnostics)
    published = diagnostics.get("particle_source_masses")
    if not isinstance(published, Mapping) or set(published) != set(recomputed):
        return False
    return all(_finite(published[key]) and float(published[key]) == round(float(value), 12)
               for key, value in recomputed.items())


def build_scan_window_design_measure(
        predictive_artifact: Mapping[str, Any] | None,
        eligibility_input: EligibilityInput) -> ScanWindowDesignMeasure:
    """Build a conditional predictive-scenario measure without I/O or authority expansion."""
    eligibility = evaluate_eig_eligibility(eligibility_input)
    if eligibility.status != "eligible":
        return _finish(eligibility=eligibility)

    if predictive_artifact is None:
        return _finish(eligibility=eligibility, construction_codes=("predictive_record_missing",))
    try:
        artifact = validate_master_predictive_artifact(predictive_artifact)
    except PredictiveArtifactError as error:
        log.warning("predictive design-measure input refused (%s): %s",
                    error.reason_code, error.message)
        return _finish(
            eligibility=eligibility, construction_codes=(error.reason_code,),
            diagnostic_values={"predictive_validation_message": error.message})

    qoi_evidence = eligibility_input.qoi_evidence
    if qoi_evidence is None:
        return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))
    try:
        qoi_evidence = validate_qoi_projection_evidence(qoi_evidence)
    except (TypeError, ValueError) as error:
        log.warning("QoI design-measure input refused: %s", error)
        return _finish(eligibility=eligibility,
                       construction_codes=("design_measure_input_mismatch",))
    artifact_models = artifact.get("models")
    qoi_models = qoi_evidence.models
    if not isinstance(artifact_models, list) or len(artifact_models) != len(qoi_models):
        return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))
    model_labels = tuple(model.get("label") for model in artifact_models)
    if len(model_labels) != len(set(model_labels)):
        return _finish(eligibility=eligibility,
                       construction_codes=("scenario_identity_mismatch",))

    identity_pairs = (
        (qoi_evidence.predictive_basis_fingerprint, artifact.get("predictive_basis_fingerprint")),
        (qoi_evidence.predictive_retained_set_fingerprint,
         artifact.get("predictive_retained_set_fingerprint")),
        (qoi_evidence.predictive_statistical_semantics_fingerprint,
         artifact.get("statistical_semantics_fingerprint")),
    )
    if (qoi_evidence.status != "valid" or not qoi_evidence.qoi_evidence_fingerprint
            or any(not isinstance(left, str) or left != right for left, right in identity_pairs)):
        return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))

    source_draw_records: list[dict[str, Any]] = []
    for model, qmodel in zip(artifact_models, qoi_models):
        draws = model.get("parameter_draws")
        draw_count = len(draws) if isinstance(draws, Sequence) else 0
        retained_draw_count = model.get("retained_draw_count")
        source_draw_count = model.get("source_draw_count")
        if (not isinstance(retained_draw_count, int) or isinstance(retained_draw_count, bool)
                or retained_draw_count != draw_count
                or not isinstance(source_draw_count, int) or isinstance(source_draw_count, bool)
                or source_draw_count < retained_draw_count):
            return _finish(eligibility=eligibility,
                           construction_codes=("design_measure_input_mismatch",))
        draw_ids = model.get("draw_ids")
        if draw_ids is None or (isinstance(draw_ids, Sequence) and not isinstance(draw_ids, (str, bytes))
                                and any(not isinstance(item, str) or not item for item in draw_ids)):
            return _finish(eligibility=eligibility, construction_codes=("scenario_identity_missing",))
        if (not isinstance(draw_ids, Sequence) or isinstance(draw_ids, (str, bytes))
                or len(draw_ids) != draw_count or len(set(draw_ids)) != draw_count):
            return _finish(eligibility=eligibility, construction_codes=("scenario_identity_mismatch",))
        normalized = _normalized_draw_weights(model, draw_count)
        join_ok = (
            qmodel.get("label") == model.get("label")
            and qmodel.get("identity_label") == model.get("identity_label")
            and _close(qmodel.get("weight"), model.get("weight"))
            and _close(qmodel.get("source_weight"), model.get("source_weight"))
            and qmodel.get("draw_basis") == model.get("draw_basis")
            and qmodel.get("draw_weighting") == model.get("draw_weighting")
            and qmodel.get("draw_mass_retained") == model.get("draw_mass_retained")
            and normalized is not None
            and len(qmodel.get("draw_weights", ())) == draw_count
            and all(_close(left, right) for left, right in zip(qmodel.get("draw_weights", ()), normalized or ()))
            and len(qmodel.get("target_present_by_draw", ())) == draw_count
            and len(qmodel.get("target_qoi_draws", ())) == draw_count
        )
        if not join_ok:
            return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))
        # Current authenticated predictive semantics describe an equal-weight empirical measure.
        # Keep explicit-weight normalization available for an additive source-contract revision,
        # but do not claim those weights are authenticated by today's semantics.
        if model.get("draw_weighting") != "equal_weight_resampled":
            return _finish(eligibility=eligibility,
                           construction_codes=("source_draw_weighting_unsupported",))
        source_draw_records.append({
            "model_label": model["label"], "source_draw_ids": list(draw_ids)})

    scenario_join_keys = [
        (record["model_label"], source_draw_id)
        for record in source_draw_records for source_draw_id in record["source_draw_ids"]
    ]
    if len(scenario_join_keys) != len(set(scenario_join_keys)):
        return _finish(eligibility=eligibility,
                       construction_codes=("scenario_identity_mismatch",))

    recomputed_masses = _source_mass_state(artifact, artifact_models, qoi_models)
    if not _compatibility_masses_match(qoi_evidence, recomputed_masses):
        return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))
    if (recomputed_masses["bootstrap"] < PREDICTIVE_MODEL_WEIGHT_FLOOR
            or recomputed_masses["basis_unknown"] + recomputed_masses["dropped_model"]
            >= PREDICTIVE_MODEL_WEIGHT_FLOOR):
        # Slice 2 owns these vetoes. Reaching this branch after an eligible result means the
        # authenticated evidence and its source have diverged. The bootstrap half mirrors
        # ``predictive_insufficient_bootstrap_mass``: what disqualifies a master is too little
        # usable mass, not the presence of non-bootstrap models — those are excluded and the
        # remainder renormalised a few lines below.
        return _finish(eligibility=eligibility, construction_codes=("design_measure_input_mismatch",))

    source_draw_fingerprint = _source_draw_identity_fingerprint(source_draw_records)
    qoi = {
        "id": qoi_evidence.qoi_id,
        "unit": qoi_evidence.qoi_unit,
        "source_qoi": qoi_evidence.source_qoi,
        "conditioning": _SEMANTICS["conditioning"],
    }
    source_identities = {
        "predictive_basis_fingerprint": qoi_evidence.predictive_basis_fingerprint,
        "predictive_retained_set_fingerprint": qoi_evidence.predictive_retained_set_fingerprint,
        "predictive_statistical_semantics_fingerprint": qoi_evidence.predictive_statistical_semantics_fingerprint,
        "source_model_selection_fingerprint": qoi_evidence.predictive_source_model_selection_fingerprint,
        "target_projection_set_fingerprint": qoi_evidence.target_projection_set_fingerprint,
        "projection_statistical_semantics_fingerprint": qoi_evidence.projection_statistical_semantics_fingerprint,
        "projection_authority_fingerprint": qoi_evidence.projection_authority_fingerprint,
        "qoi_evidence_fingerprint": qoi_evidence.qoi_evidence_fingerprint,
    }

    retained_indices = [index for index, model in enumerate(artifact_models)
                        if model.get("draw_basis") == "parametric_bootstrap"]
    retained_total = math.fsum(float(artifact_models[index]["weight"])
                               for index in retained_indices)
    if not math.isfinite(retained_total) or retained_total <= 0.0:
        return _finish(
            eligibility=eligibility, construction_codes=("conditional_target_qoi_ensemble_empty",),
            qoi=qoi, source_identities=source_identities,
            source_draw_identity_fingerprint=source_draw_fingerprint)

    excluded_models: list[dict[str, Any]] = []
    provisional: list[dict[str, Any]] = []
    model_state: list[dict[str, Any]] = []
    available_masses: list[float] = []
    unavailable_masses: list[float] = []
    for index, (model, qmodel) in enumerate(zip(artifact_models, qoi_models)):
        basis = _basis_name(model.get("draw_basis"))
        if basis != "bootstrap":
            excluded_models.append({
                "label": model["label"], "identity_label": model["identity_label"],
                "draw_basis": model.get("draw_basis"), "basis_class": basis,
                "source_weight": float(model["source_weight"]),
                "published_predictive_weight": float(model["weight"]),
                "reason": ("unknown_source_basis" if basis == "unknown_basis"
                           else "nonbootstrap_source_basis"),
            })
            continue
        retained_weight = float(model["weight"]) / retained_total
        source_available_masses: list[float] = []
        source_unavailable_masses: list[float] = []
        conditional_count = 0
        for draw_id, draw_weight, present, qoi_value in zip(
                model["draw_ids"], qmodel["draw_weights"],
                qmodel["target_present_by_draw"], qmodel["target_qoi_draws"]):
            draw_weight = float(draw_weight)
            scenario_mass = retained_weight * draw_weight
            if present and _finite(qoi_value) and scenario_mass > 0.0:
                source_available_masses.append(float(model["source_weight"]) * draw_weight)
                available_masses.append(scenario_mass)
                conditional_count += 1
                provisional.append({
                    "scenario_id": _predictive_scenario_id(
                        artifact["predictive_basis_fingerprint"], model["label"], draw_id),
                    "model_label": model["label"], "identity_label": model["identity_label"],
                    "source_draw_id": draw_id, "qoi_value": float(qoi_value),
                    "source_weight": float(model["source_weight"]),
                    "published_predictive_weight": float(model["weight"]),
                    "retained_predictive_weight": retained_weight,
                    "source_draw_weight": draw_weight,
                    "preconditioning_scenario_mass": scenario_mass,
                })
            else:
                source_unavailable_masses.append(float(model["source_weight"]) * draw_weight)
                unavailable_masses.append(scenario_mass)
        model_state.append({
            "model": model, "qmodel": qmodel, "retained_weight": retained_weight,
            "source_available": math.fsum(source_available_masses),
            "source_unavailable": math.fsum(source_unavailable_masses),
            "conditional_count": conditional_count,
        })

    scenario_ids = [item["scenario_id"] for item in provisional]
    if len(scenario_ids) != len(set(scenario_ids)):
        return _finish(
            eligibility=eligibility, construction_codes=("scenario_identity_mismatch",),
            qoi=qoi, source_identities=source_identities,
            excluded_models=excluded_models,
            source_draw_identity_fingerprint=source_draw_fingerprint)
    available_mass = math.fsum(available_masses)
    unavailable_mass = math.fsum(unavailable_masses)

    source_aic_scale = {
        "bootstrap_model_mass": recomputed_masses["bootstrap"],
        "nested_sampling_model_mass": recomputed_masses["nested"],
        "laplace_hesse_model_mass": recomputed_masses["laplace"],
        "unknown_basis_model_mass": recomputed_masses["basis_unknown"],
        "dropped_model_mass": recomputed_masses["dropped_model"],
        "dropped_draw_mass": recomputed_masses["dropped_draw"],
    }
    completeness = artifact["completeness"]
    completeness_numbers = (
        "source_model_weight_total", "retained_model_weight_total", "pruned_model_weight",
        "model_weight_floor", "max_pruned_mass", "max_dropped_draw_mass",
    )
    if (any(not _finite(completeness.get(name)) or float(completeness[name]) < 0.0
            for name in completeness_numbers)
            or completeness.get("predictive_complete_for_eig") is not True
            or not _close(completeness["retained_model_weight_total"], math.fsum(
                float(model["source_weight"]) for model in artifact_models))
            or not _close(completeness["source_model_weight_total"],
                          math.fsum(float(model["source_weight"]) for model in artifact_models)
                          + recomputed_masses["dropped_model"])
            or not _close(completeness["pruned_model_weight"], max(
                float(completeness["source_model_weight_total"])
                - float(completeness["retained_model_weight_total"]), 0.0))
            or not _close(completeness["model_weight_floor"], PREDICTIVE_MODEL_WEIGHT_FLOOR)
            or not _close(completeness["max_pruned_mass"], PREDICTIVE_MAX_PRUNED_MASS)
            or not _close(completeness["max_dropped_draw_mass"],
                          PREDICTIVE_MAX_DROPPED_DRAW_MASS)):
        return _finish(
            eligibility=eligibility, construction_codes=("design_measure_input_mismatch",),
            qoi=qoi, source_identities=source_identities,
            source_draw_identity_fingerprint=source_draw_fingerprint)
    source_completeness = {
        "source_model_weight_total": completeness.get("source_model_weight_total"),
        "retained_model_weight_total": completeness.get("retained_model_weight_total"),
        "pruned_model_weight": completeness.get("pruned_model_weight"),
        "model_weight_floor": completeness.get("model_weight_floor"),
        "max_pruned_mass": completeness.get("max_pruned_mass"),
        "max_dropped_draw_mass": completeness.get("max_dropped_draw_mass"),
        "predictive_complete_for_eig": completeness.get("predictive_complete_for_eig"),
    }
    included_scenario_weight = (
        math.fsum(item["preconditioning_scenario_mass"] / available_mass
                  for item in provisional)
        if provisional and math.isfinite(available_mass) and available_mass > 0.0 else 0.0)
    mass_diagnostics = {
        "source_aic_scale": source_aic_scale,
        "source_completeness": source_completeness,
        "bootstrap_retained_scale": {
            "target_qoi_available_mass_before_conditioning": available_mass,
            "target_qoi_unavailable_mass_before_conditioning": unavailable_mass,
        },
        "conditional_design_scale": {
            "included_scenario_weight": included_scenario_weight,
        },
    }
    if not provisional or not math.isfinite(available_mass) or available_mass <= 0.0:
        return _finish(
            eligibility=eligibility, construction_codes=("conditional_target_qoi_ensemble_empty",),
            qoi=qoi, source_identities=source_identities, mass_diagnostics=mass_diagnostics,
            excluded_models=excluded_models,
            source_draw_identity_fingerprint=source_draw_fingerprint)

    scenarios = tuple(sorted((
        _PredictiveScenario(**item,
                            conditional_scenario_weight=item["preconditioning_scenario_mass"] / available_mass)
        for item in provisional), key=lambda item: (item.model_label, item.source_draw_id)))
    models: list[_DesignModel] = []
    for item in model_state:
        model = item["model"]
        model_conditional_mass = math.fsum(
            scenario.conditional_scenario_weight for scenario in scenarios
            if scenario.model_label == model["label"])
        models.append(_DesignModel(
            label=model["label"], identity_label=model["identity_label"],
            source_weight=float(model["source_weight"]),
            published_predictive_weight=float(model["weight"]),
            retained_predictive_weight=item["retained_weight"],
            design_ensemble_weight=model_conditional_mass,
            draw_basis=model["draw_basis"], draw_weighting=model["draw_weighting"],
            source_draw_count=model["source_draw_count"],
            retained_source_draw_count=model["retained_draw_count"],
            conditional_scenario_count=item["conditional_count"],
            source_target_qoi_available_mass=item["source_available"],
            source_target_qoi_unavailable_mass=item["source_unavailable"],
        ))
    if (not math.isclose(math.fsum(item.conditional_scenario_weight for item in scenarios), 1.0,
                         rel_tol=_NORMALIZATION_REL_TOL, abs_tol=_NORMALIZATION_ABS_TOL)
            or not math.isclose(math.fsum(item.design_ensemble_weight for item in models), 1.0,
                                rel_tol=_NORMALIZATION_REL_TOL, abs_tol=_NORMALIZATION_ABS_TOL)):
        return _finish(
            eligibility=eligibility, construction_codes=("design_measure_normalization_failed",),
            qoi=qoi, source_identities=source_identities, mass_diagnostics=mass_diagnostics,
            excluded_models=excluded_models,
            source_draw_identity_fingerprint=source_draw_fingerprint)

    return _finish(
        eligibility=eligibility, status="available", qoi=qoi,
        source_identities=source_identities, mass_diagnostics=mass_diagnostics,
        excluded_models=excluded_models, source_draw_identity_fingerprint=source_draw_fingerprint,
        models=models, scenarios=scenarios)


__all__ = [
    "ScanWindowDesignMeasure",
    "build_scan_window_design_measure",
    "validate_scan_window_design_measure",
]

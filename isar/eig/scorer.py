"""Pure full-vector Poisson information scoring for same-action scan windows."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np
import scipy
from scipy.special import logsumexp
from scipy.stats import poisson

from isar.predictive import (
    PREDICTIVE_DESIGN_EVALUATION_SCHEMA,
    PredictiveDesignEvaluation,
)
from isar.schema.statistics import semantics_fingerprint

from .design_measure import (
    ScanWindowDesignMeasure,
    _predictive_scenario_id,
    _source_draw_identity_fingerprint,
    validate_scan_window_design_measure,
)


SCALAR_QOI_DISCRETIZATION_CONTRACT = "isar.scalar_qoi_discretization/o36.4"
SCALAR_QOI_DISCRETIZATION_REVISION = "isar.scalar_qoi_floor_offset_partition/1"
SCAN_WINDOW_SCORE_SET_CONTRACT = "isar.scan_window_eig_score_set/o36.4"
SCORER_REVISION = "isar.full_vector_poisson_mixture_scorer/o36.4"
LIKELIHOOD_REVISION = "isar.full_vector_poisson_likelihood/1"
CRN_REVISION = "isar.sha256_energy_keyed_poisson_crn/1"

_DISCRETIZATION_FINGERPRINT_DOMAIN = "isar.scalar_qoi_discretization_fingerprint/o36.4"
_GRID_FINGERPRINT_DOMAIN = "isar.executed_grid_fingerprint/o36.4"
_EVALUATION_FINGERPRINT_DOMAIN = "isar.scorer_candidate_evaluation_fingerprint/o36.4"
_CANDIDATE_SCORE_FINGERPRINT_DOMAIN = "isar.scan_window_candidate_score_fingerprint/o36.4"
_PAIRWISE_FINGERPRINT_DOMAIN = "isar.scan_window_pairwise_comparison_fingerprint/o36.4"
_SCORE_SET_FINGERPRINT_DOMAIN = "isar.scan_window_eig_score_set_fingerprint/o36.4"
_SCENARIO_CRN_DOMAIN = "isar.scan_window_scenario_crn/o36.4"
_POISSON_CRN_DOMAIN = "isar.scan_window_poisson_crn/o36.4"

_AVAILABLE_SCOPES = {
    "eig_shadow": "diagnostic",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
}
_INELIGIBLE_SCOPES = {
    "eig_shadow": "ineligible",
    "eig_authoritative": "ineligible",
    "acquisition_selection": "ineligible",
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


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float, np.integer, np.floating))
            and not isinstance(value, (bool, np.bool_))
            and math.isfinite(float(value)))


def _close(left: Any, right: Any) -> bool:
    return (_finite_number(left) and _finite_number(right)
            and math.isclose(float(left), float(right),
                             rel_tol=_NORMALIZATION_REL_TOL,
                             abs_tol=_NORMALIZATION_ABS_TOL))


def _valid_fingerprint(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64 and value.lower() == value
            and all(character in "0123456789abcdef" for character in value))


def _canonical_energy(value: Any) -> float:
    if not _finite_number(value):
        raise ValueError("energy must be finite")
    numeric = float(value)
    return 0.0 if numeric == 0.0 else numeric


def _energy_identity(value: Any) -> str:
    return _canonical_energy(value).hex()


def _backend_identities() -> dict[str, str]:
    return {
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "poisson_inverse_cdf": "scipy.stats.poisson.ppf",
        "mixture_logsumexp": "scipy.special.logsumexp",
    }


def _discretization_payload(discretization: "ScalarQoIDiscretization") -> dict[str, Any]:
    return {
        "contract": discretization.contract,
        "qoi_id": discretization.qoi_id,
        "qoi_unit": discretization.qoi_unit,
        "revision": discretization.revision,
        "origin": discretization.origin,
        "bin_width": discretization.bin_width,
        "offsets": list(discretization.offsets),
    }


def _canonical_scalar(value: float) -> float:
    value = float(value)
    return 0.0 if value == 0.0 else value


def _validated_discretization(
        discretization: Any,
        *,
        qoi_id: str | None = None,
        qoi_unit: str | None = None) -> "ScalarQoIDiscretization":
    if not isinstance(discretization, ScalarQoIDiscretization):
        raise TypeError("qoi_discretization must be a ScalarQoIDiscretization")
    if (discretization.contract != SCALAR_QOI_DISCRETIZATION_CONTRACT
            or discretization.revision != SCALAR_QOI_DISCRETIZATION_REVISION
            or not isinstance(discretization.qoi_id, str) or not discretization.qoi_id
            or not isinstance(discretization.qoi_unit, str) or not discretization.qoi_unit
            or not _finite_number(discretization.origin)
            or not _finite_number(discretization.bin_width)
            or float(discretization.bin_width) <= 0.0
            or not isinstance(discretization.offsets, tuple)
            or not discretization.offsets):
        raise ValueError("scalar QoI discretization metadata is invalid")
    width = float(discretization.bin_width)
    offsets = discretization.offsets
    if any(not _finite_number(offset) or float(offset) < 0.0 or float(offset) >= width
           for offset in offsets):
        raise ValueError("scalar QoI discretization offsets are invalid")
    if (float(discretization.origin).hex() != _canonical_scalar(
            discretization.origin).hex()
            or any(float(offset).hex() != _canonical_scalar(offset).hex()
                   for offset in offsets)):
        raise ValueError("scalar QoI discretization signed zero is not canonical")
    if (tuple(sorted(float(offset) for offset in offsets)) != offsets
            or len(offsets) != len(set(offsets))):
        raise ValueError("scalar QoI discretization offsets are invalid")
    if qoi_id is not None and (discretization.qoi_id != qoi_id
                               or discretization.qoi_unit != qoi_unit):
        raise ValueError("scalar QoI discretization does not match the design measure")
    expected = _fingerprint(
        _DISCRETIZATION_FINGERPRINT_DOMAIN,
        _discretization_payload(discretization),
    )
    if discretization.discretization_fingerprint != expected:
        raise ValueError("discretization_fingerprint does not authenticate the discretization")
    return discretization


@dataclass(frozen=True)
class ScalarQoIDiscretization:
    """One versioned family of offset scalar-QoI partitions."""

    qoi_id: str
    qoi_unit: str
    origin: float
    bin_width: float
    offsets: tuple[float, ...]
    contract: str = field(default=SCALAR_QOI_DISCRETIZATION_CONTRACT, init=False)
    revision: str = field(default=SCALAR_QOI_DISCRETIZATION_REVISION, init=False)
    discretization_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.qoi_id, str) or not self.qoi_id:
            raise ValueError("qoi_id must be a non-empty string")
        if not isinstance(self.qoi_unit, str) or not self.qoi_unit:
            raise ValueError("qoi_unit must be a non-empty string")
        if not _finite_number(self.origin):
            raise ValueError("origin must be finite")
        if not _finite_number(self.bin_width) or float(self.bin_width) <= 0.0:
            raise ValueError("bin_width must be finite and positive")
        if (not isinstance(self.offsets, Sequence)
                or isinstance(self.offsets, (str, bytes)) or not self.offsets):
            raise ValueError("offsets must be a non-empty sequence")
        width = float(self.bin_width)
        origin = _canonical_scalar(self.origin)
        offsets = tuple(_canonical_scalar(offset) for offset in self.offsets
                        if _finite_number(offset))
        if (len(offsets) != len(self.offsets)
                or any(offset < 0.0 or offset >= width for offset in offsets)
                or len(offsets) != len(set(offsets))):
            raise ValueError("offsets must be unique, finite, and inside one bin width")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "bin_width", width)
        object.__setattr__(self, "offsets", tuple(sorted(offsets)))
        object.__setattr__(self, "discretization_fingerprint", _fingerprint(
            _DISCRETIZATION_FINGERPRINT_DOMAIN,
            _discretization_payload(self),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            **_discretization_payload(self),
            "discretization_fingerprint": self.discretization_fingerprint,
        }


@dataclass(frozen=True)
class CandidateEvaluationInput:
    candidate_id: str
    evaluation: PredictiveDesignEvaluation


@dataclass(frozen=True)
class _CandidateScore:
    candidate_id: str
    status: str
    reason_codes: tuple[str, ...]
    source_reason_code: str | None
    predictive_basis_fingerprint: str | None
    source_draw_identity_fingerprint: str | None
    executed_grid_fingerprint: str | None
    candidate_evaluation_fingerprint: str | None
    energy_meV: tuple[float, ...]
    executed_point_count: int | None
    exposure_per_point: float | None
    conditional_target_qoi_information_nats: float | None
    conditional_target_qoi_information_standard_error_nats: float | None
    information_per_executed_point_nats: float | None
    information_per_executed_point_standard_error_nats: float | None
    partition_information_nats: tuple[float, ...]
    block_information_nats: tuple[float, ...]
    simulation_count: int | None
    block_count: int | None
    authority_scopes: tuple[tuple[str, str], ...]
    candidate_score_fingerprint: str
    diagnostics: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "source_reason_code": self.source_reason_code,
            "predictive_basis_fingerprint": self.predictive_basis_fingerprint,
            "source_draw_identity_fingerprint": self.source_draw_identity_fingerprint,
            "executed_grid_fingerprint": self.executed_grid_fingerprint,
            "candidate_evaluation_fingerprint": self.candidate_evaluation_fingerprint,
            "energy_meV": list(self.energy_meV),
            "executed_point_count": self.executed_point_count,
            "exposure_per_point": self.exposure_per_point,
            "conditional_target_qoi_information_nats": self.conditional_target_qoi_information_nats,
            "conditional_target_qoi_information_standard_error_nats": (
                self.conditional_target_qoi_information_standard_error_nats),
            "information_per_executed_point_nats": self.information_per_executed_point_nats,
            "information_per_executed_point_standard_error_nats": (
                self.information_per_executed_point_standard_error_nats),
            "partition_information_nats": list(self.partition_information_nats),
            "block_information_nats": list(self.block_information_nats),
            "simulation_count": self.simulation_count,
            "block_count": self.block_count,
            "authority_scopes": dict(self.authority_scopes),
            "candidate_score_fingerprint": self.candidate_score_fingerprint,
            "diagnostics": _thaw(dict(self.diagnostics)),
        }


@dataclass(frozen=True)
class _PairwiseComparison:
    candidate_a_id: str
    candidate_b_id: str
    candidate_a_score_fingerprint: str
    candidate_b_score_fingerprint: str
    delta_information_nats: float
    delta_information_standard_error_nats: float
    delta_information_per_executed_point_nats: float
    delta_information_per_executed_point_standard_error_nats: float
    paired_block_deltas: tuple[float, ...]
    paired_block_deltas_per_executed_point_nats: tuple[float, ...]
    pairwise_comparison_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_a_id": self.candidate_a_id,
            "candidate_b_id": self.candidate_b_id,
            "candidate_a_score_fingerprint": self.candidate_a_score_fingerprint,
            "candidate_b_score_fingerprint": self.candidate_b_score_fingerprint,
            "delta_information_nats": self.delta_information_nats,
            "delta_information_standard_error_nats": self.delta_information_standard_error_nats,
            "delta_information_per_executed_point_nats": (
                self.delta_information_per_executed_point_nats),
            "delta_information_per_executed_point_standard_error_nats": (
                self.delta_information_per_executed_point_standard_error_nats),
            "paired_block_deltas": list(self.paired_block_deltas),
            "paired_block_deltas_per_executed_point_nats": list(
                self.paired_block_deltas_per_executed_point_nats),
            "pairwise_comparison_fingerprint": self.pairwise_comparison_fingerprint,
        }


@dataclass(frozen=True)
class ScanWindowScoreSet:
    """One immutable set of candidate scores produced by a shared simulation schedule."""

    contract: str
    scorer_revision: str
    likelihood_revision: str
    crn_revision: str
    status: str
    reason_codes: tuple[str, ...]
    design_measure_fingerprint: str | None
    source_draw_identity_fingerprint: str | None
    qoi_discretization_fingerprint: str | None
    seed: int | None
    simulation_count: int | None
    block_count: int | None
    numerical_backend: tuple[tuple[str, Any], ...]
    authority_scopes: tuple[tuple[str, str], ...]
    score_set_fingerprint: str
    diagnostics: tuple[tuple[str, Any], ...] = ()
    _candidate_scores: tuple[_CandidateScore, ...] = ()
    _pairwise_comparisons: tuple[_PairwiseComparison, ...] = ()

    @property
    def candidate_scores(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(MappingProxyType(_freeze(item.to_dict())) for item in self._candidate_scores)

    @property
    def pairwise_comparisons(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(MappingProxyType(_freeze(item.to_dict()))
                     for item in self._pairwise_comparisons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "scorer_revision": self.scorer_revision,
            "likelihood_revision": self.likelihood_revision,
            "crn_revision": self.crn_revision,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "design_measure_fingerprint": self.design_measure_fingerprint,
            "source_draw_identity_fingerprint": self.source_draw_identity_fingerprint,
            "qoi_discretization_fingerprint": self.qoi_discretization_fingerprint,
            "seed": self.seed,
            "simulation_count": self.simulation_count,
            "block_count": self.block_count,
            "numerical_backend": _thaw(dict(self.numerical_backend)),
            "candidate_scores": [_thaw(item) for item in self.candidate_scores],
            "pairwise_comparisons": [_thaw(item) for item in self.pairwise_comparisons],
            "authority_scopes": dict(self.authority_scopes),
            "score_set_fingerprint": self.score_set_fingerprint,
            "diagnostics": _thaw(dict(self.diagnostics)),
        }


class _CandidateRefusal(Exception):
    def __init__(self, reason_code: str, message: str,
                 *, source_reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.source_reason_code = source_reason_code


@dataclass(frozen=True)
class _PreparedCandidate:
    candidate_id: str
    predictive_basis_fingerprint: str
    source_draw_identity_fingerprint: str
    executed_grid_fingerprint: str
    candidate_evaluation_fingerprint: str
    energy_meV: tuple[float, ...]
    energy_identities: tuple[str, ...]
    exposure_per_point: float
    rates: np.ndarray


def _grid_fingerprint(energy_identities: Sequence[str], exposure: float) -> str:
    return _fingerprint(_GRID_FINGERPRINT_DOMAIN, {
        "energy_identities": list(energy_identities),
        "exposure_per_point_hex": exposure.hex(),
        "executed_point_count": len(energy_identities),
    })


def _prepare_candidate(
        item: CandidateEvaluationInput,
        measure: ScanWindowDesignMeasure) -> _PreparedCandidate:
    evaluation = item.evaluation
    if not isinstance(evaluation, PredictiveDesignEvaluation):
        raise _CandidateRefusal(
            "predictive_evaluation_unavailable",
            "candidate evaluation must be a PredictiveDesignEvaluation",
        )
    if evaluation.schema != PREDICTIVE_DESIGN_EVALUATION_SCHEMA:
        raise _CandidateRefusal(
            "predictive_evaluation_unavailable",
            "candidate evaluation schema is unsupported",
        )
    if evaluation.available is not True or evaluation.reason_code is not None:
        raise _CandidateRefusal(
            "predictive_evaluation_unavailable",
            "candidate predictive evaluation is unavailable",
            source_reason_code=(evaluation.reason_code
                                if isinstance(evaluation.reason_code, str) else None),
        )
    grid = evaluation.energy_meV
    if (not isinstance(grid, Sequence) or isinstance(grid, (str, bytes)) or not grid
            or any(not _finite_number(value) for value in grid)):
        raise _CandidateRefusal("candidate_grid_invalid", "candidate energy grid is invalid")
    energies = tuple(_canonical_energy(value) for value in grid)
    energy_identities = tuple(value.hex() for value in energies)
    if len(energy_identities) != len(set(energy_identities)):
        raise _CandidateRefusal("candidate_grid_invalid", "candidate energy grid is not unique")
    if (not _finite_number(evaluation.exposure_per_point)
            or float(evaluation.exposure_per_point) <= 0.0):
        raise _CandidateRefusal(
            "candidate_exposure_invalid", "candidate exposure must be finite and positive")
    exposure = float(evaluation.exposure_per_point)

    source_identities = dict(measure.source_identities)
    predictive_basis = source_identities["predictive_basis_fingerprint"]
    if evaluation.predictive_basis_fingerprint != predictive_basis:
        raise _CandidateRefusal(
            "predictive_basis_mismatch", "candidate predictive basis does not match the measure")
    models = evaluation.models
    if (not isinstance(models, Sequence) or isinstance(models, (str, bytes)) or not models
            or any(not isinstance(model, Mapping) for model in models)):
        raise _CandidateRefusal(
            "scenario_evaluation_join_mismatch", "candidate models are malformed")

    source_records: list[dict[str, Any]] = []
    model_by_label: dict[str, Mapping[str, Any]] = {}
    rows: dict[tuple[str, str], tuple[float, ...]] = {}
    for model in models:
        label = model.get("label")
        identity_label = model.get("identity_label")
        draw_ids = model.get("draw_ids")
        expected = model.get("expected_counts")
        if (not isinstance(label, str) or not label or label in model_by_label
                or not isinstance(identity_label, str) or not identity_label
                or not _finite_number(model.get("weight"))
                or float(model["weight"]) < 0.0
                or not isinstance(model.get("draw_basis"), str) or not model["draw_basis"]
                or not isinstance(model.get("draw_weighting"), str) or not model["draw_weighting"]):
            raise _CandidateRefusal(
                "scenario_evaluation_join_mismatch", "candidate model identity is malformed")
        if (not isinstance(draw_ids, Sequence) or isinstance(draw_ids, (str, bytes))
                or not draw_ids
                or any(not isinstance(draw_id, str) or not draw_id for draw_id in draw_ids)
                or len(draw_ids) != len(set(draw_ids))):
            raise _CandidateRefusal(
                "scenario_evaluation_join_mismatch", "candidate source draw IDs are malformed")
        if (not isinstance(expected, Sequence) or isinstance(expected, (str, bytes))
                or len(expected) != len(draw_ids)):
            raise _CandidateRefusal(
                "expected_counts_invalid", "candidate count rows do not align with source draws")
        for draw_id, row in zip(draw_ids, expected):
            if (not isinstance(row, Sequence) or isinstance(row, (str, bytes))
                    or len(row) != len(energies)
                    or any(not _finite_number(value) or float(value) < 0.0 for value in row)):
                raise _CandidateRefusal(
                    "expected_counts_invalid", "candidate expected counts are malformed")
            key = (label, draw_id)
            if key in rows:
                raise _CandidateRefusal(
                    "scenario_evaluation_join_mismatch", "candidate source draw join is duplicated")
            rows[key] = tuple(float(value) for value in row)
        source_records.append({"model_label": label, "source_draw_ids": list(draw_ids)})
        model_by_label[label] = model

    source_fingerprint = _source_draw_identity_fingerprint(source_records)
    if source_fingerprint != measure.source_draw_identity_fingerprint:
        raise _CandidateRefusal(
            "source_draw_identity_mismatch",
            "candidate source draw identity does not match the design measure",
        )

    design_models = {model["label"]: model for model in measure.models}
    excluded_labels = {
        model.get("label") for model in measure.excluded_models
        if isinstance(model, Mapping) and isinstance(model.get("label"), str)
    }
    consumed: list[dict[str, Any]] = []
    rate_rows: list[tuple[float, ...]] = []
    included_keys: set[tuple[str, str]] = set()
    for scenario in measure.predictive_scenarios:
        key = (scenario["model_label"], scenario["source_draw_id"])
        model = model_by_label.get(key[0])
        design_model = design_models.get(key[0])
        if key not in rows or model is None or design_model is None:
            raise _CandidateRefusal(
                "scenario_evaluation_join_mismatch", "candidate is missing an included scenario")
        if (model.get("identity_label") != design_model["identity_label"]
                or not _close(model.get("weight"), design_model["published_predictive_weight"])
                or model.get("draw_basis") != design_model["draw_basis"]
                or model.get("draw_weighting") != design_model["draw_weighting"]
                or scenario["scenario_id"] != _predictive_scenario_id(
                    predictive_basis, key[0], key[1])):
            raise _CandidateRefusal(
                "scenario_evaluation_join_mismatch", "candidate scenario metadata does not join")
        included_keys.add(key)
        rate_rows.append(rows[key])
        consumed.append({
            "scenario_id": scenario["scenario_id"],
            "model_label": key[0],
            "source_draw_id": key[1],
            "expected_counts": list(rows[key]),
        })

    ignored: list[dict[str, str]] = []
    for model_label, source_draw_id in rows:
        key = (model_label, source_draw_id)
        if key in included_keys:
            continue
        if model_label in excluded_labels:
            category = "excluded_model"
        elif model_label in design_models:
            category = "conditional_target_qoi_unavailable"
        else:
            raise _CandidateRefusal(
                "scenario_evaluation_join_mismatch",
                "candidate contains a source row outside the design measure",
            )
        ignored.append({
            "model_label": model_label,
            "source_draw_id": source_draw_id,
            "category": category,
        })
    ignored.sort(key=lambda value: (value["model_label"], value["source_draw_id"]))

    grid_fingerprint = _grid_fingerprint(energy_identities, exposure)
    evaluation_fingerprint = _fingerprint(_EVALUATION_FINGERPRINT_DOMAIN, {
        "candidate_id": item.candidate_id,
        "predictive_basis_fingerprint": predictive_basis,
        "source_draw_identity_fingerprint": source_fingerprint,
        "executed_grid_fingerprint": grid_fingerprint,
        "producer_source_identity": source_records,
        "consumed_scenarios": consumed,
        "ignored_source_rows": ignored,
    })
    rates = np.asarray(rate_rows, dtype=float)
    rates.setflags(write=False)
    return _PreparedCandidate(
        candidate_id=item.candidate_id,
        predictive_basis_fingerprint=predictive_basis,
        source_draw_identity_fingerprint=source_fingerprint,
        executed_grid_fingerprint=grid_fingerprint,
        candidate_evaluation_fingerprint=evaluation_fingerprint,
        energy_meV=energies,
        energy_identities=energy_identities,
        exposure_per_point=exposure,
        rates=rates,
    )


def _hash_uniform(domain: str, payload: Mapping[str, Any]) -> float:
    digest = bytes.fromhex(_fingerprint(domain, payload))
    raw64 = int.from_bytes(digest[:8], "big")
    mantissa52 = raw64 >> 12
    return (mantissa52 + 0.5) / (2 ** 52)


def _scenario_schedule(
        measure: ScanWindowDesignMeasure,
        seed: int,
        simulation_count: int) -> np.ndarray:
    weights = [float(scenario["conditional_scenario_weight"])
               for scenario in measure.predictive_scenarios]
    total = math.fsum(weights)
    normalized = np.asarray([weight / total for weight in weights], dtype=float)
    cumulative = np.cumsum(normalized)
    cumulative[-1] = 1.0
    uniforms = np.fromiter((
        _hash_uniform(_SCENARIO_CRN_DOMAIN, {
            "design_measure_fingerprint": measure.design_measure_fingerprint,
            "seed": seed,
            "simulation_index": index,
        }) for index in range(simulation_count)), dtype=float, count=simulation_count)
    return np.searchsorted(cumulative, uniforms, side="right")


def _observation_matrix(
        prepared: _PreparedCandidate,
        measure: ScanWindowDesignMeasure,
        scenario_indices: np.ndarray,
        seed: int) -> np.ndarray:
    scenario_ids = [scenario["scenario_id"] for scenario in measure.predictive_scenarios]
    simulation_count = len(scenario_indices)
    uniforms = np.empty((simulation_count, len(prepared.energy_identities)), dtype=float)
    for simulation_index, scenario_index in enumerate(scenario_indices):
        scenario_id = scenario_ids[int(scenario_index)]
        for point_index, energy_identity in enumerate(prepared.energy_identities):
            uniforms[simulation_index, point_index] = _hash_uniform(_POISSON_CRN_DOMAIN, {
                "design_measure_fingerprint": measure.design_measure_fingerprint,
                "seed": seed,
                "simulation_index": simulation_index,
                "selected_scenario_id": scenario_id,
                "energy_identity": energy_identity,
            })
    selected_rates = prepared.rates[scenario_indices, :]
    observations = np.zeros_like(selected_rates, dtype=float)
    positive = selected_rates > 0.0
    if np.any(positive):
        try:
            values = np.asarray(
                poisson.ppf(uniforms[positive], selected_rates[positive]), dtype=float)
        except (FloatingPointError, OverflowError, ValueError) as error:
            log.warning("Poisson inverse-CDF backend failed: %s", error)
            raise _CandidateRefusal(
                "poisson_simulation_failed", "Poisson inverse-CDF backend failed") from error
        valid = np.isfinite(values) & (values >= 0.0) & (values == np.floor(values))
        if not np.all(valid):
            raise _CandidateRefusal(
                "poisson_simulation_failed", "Poisson inverse-CDF returned invalid counts")
        observations[positive] = values
    return observations


def _likelihood_matrix(observations: np.ndarray, rates: np.ndarray) -> np.ndarray:
    positive = rates > 0.0
    log_rates = np.zeros_like(rates, dtype=float)
    log_rates[positive] = np.log(rates[positive])
    likelihoods = observations @ log_rates.T
    likelihoods -= np.sum(rates, axis=1, dtype=float)[None, :]
    impossible = (observations > 0.0).astype(np.int32) @ (~positive).astype(np.int32).T > 0
    likelihoods[impossible] = -np.inf
    return likelihoods


def _partition_bins(
        scenarios: Sequence[Mapping[str, Any]],
        discretization: ScalarQoIDiscretization) -> tuple[tuple[int, ...], ...]:
    partitions: list[tuple[int, ...]] = []
    for offset in discretization.offsets:
        bins: list[int] = []
        for scenario in scenarios:
            quotient = (
                (float(scenario["qoi_value"]) - discretization.origin - offset)
                / discretization.bin_width
            )
            if not math.isfinite(quotient):
                raise ValueError("scalar QoI bin arithmetic is non-finite")
            bins.append(math.floor(quotient))
        partitions.append(tuple(bins))
    return tuple(partitions)


def _information_contributions(
        likelihoods: np.ndarray,
        scenario_indices: np.ndarray,
        scenarios: Sequence[Mapping[str, Any]],
        discretization: ScalarQoIDiscretization,
        partition_bins: Sequence[Sequence[int]] | None = None,
        ) -> tuple[np.ndarray, tuple[float, ...]]:
    weights = [float(scenario["conditional_scenario_weight"]) for scenario in scenarios]
    total = math.fsum(weights)
    normalized = np.asarray([weight / total for weight in weights], dtype=float)
    log_weights = np.log(normalized)
    weighted_likelihoods = likelihoods + log_weights[None, :]
    marginal = logsumexp(weighted_likelihoods, axis=1)
    partition_rows: list[np.ndarray] = []
    partition_means: list[float] = []
    resolved_bins = (partition_bins if partition_bins is not None
                     else _partition_bins(scenarios, discretization))
    for partition in resolved_bins:
        bins = tuple(int(value) for value in partition)
        unique_bins = tuple(sorted(set(bins)))
        if len(unique_bins) == 1:
            contributions = np.zeros(len(scenario_indices), dtype=float)
        else:
            selected_bins = np.asarray([bins[int(index)] for index in scenario_indices], dtype=object)
            conditional = np.empty(len(scenario_indices), dtype=float)
            for bin_value in unique_bins:
                scenario_mask = np.asarray([value == bin_value for value in bins], dtype=bool)
                simulation_mask = selected_bins == bin_value
                bin_mass = math.fsum(normalized[scenario_mask].tolist())
                conditional[simulation_mask] = (
                    logsumexp(weighted_likelihoods[simulation_mask][:, scenario_mask], axis=1)
                    - math.log(bin_mass)
                )
            contributions = conditional - marginal
        if not np.all(np.isfinite(contributions)):
            raise _CandidateRefusal(
                "information_evaluation_failed", "information contributions are non-finite")
        partition_rows.append(contributions)
        partition_means.append(math.fsum(contributions.tolist()) / len(contributions))
    per_simulation = np.sum(np.stack(partition_rows, axis=0), axis=0, dtype=float)
    per_simulation /= len(partition_rows)
    return per_simulation, tuple(partition_means)


def _candidate_semantic_payload(
        candidate: Mapping[str, Any],
        *,
        include_fingerprint: bool) -> dict[str, Any]:
    payload = dict(candidate)
    payload.pop("diagnostics")
    if not include_fingerprint:
        payload.pop("candidate_score_fingerprint")
    return payload


def _candidate_fingerprint_payload(
        score: _CandidateScore,
        *,
        measure_fingerprint: str | None,
        discretization_fingerprint: str | None,
        seed: int | None,
        simulation_count: int | None,
        block_count: int | None,
        backend: Mapping[str, Any]) -> dict[str, Any]:
    payload = _candidate_semantic_payload(
        score.to_dict(), include_fingerprint=False)
    return {
        "scorer_revision": SCORER_REVISION,
        "likelihood_revision": LIKELIHOOD_REVISION,
        "crn_revision": CRN_REVISION,
        "design_measure_fingerprint": measure_fingerprint,
        "qoi_discretization_fingerprint": discretization_fingerprint,
        "seed": seed,
        "simulation_count": simulation_count,
        "block_count": block_count,
        "numerical_backend": dict(backend),
        "candidate": payload,
    }


def _finish_candidate(
        *,
        candidate_id: str,
        status: str,
        reason_codes: Sequence[str],
        source_reason_code: str | None,
        measure_fingerprint: str | None,
        source_draw_identity_fingerprint: str | None,
        discretization_fingerprint: str | None,
        seed: int | None,
        simulation_count: int | None,
        block_count: int | None,
        backend: Mapping[str, Any],
        prepared: _PreparedCandidate | None = None,
        information: float | None = None,
        standard_error: float | None = None,
        per_point_information: float | None = None,
        per_point_standard_error: float | None = None,
        partition_information: Sequence[float] = (),
        block_information: Sequence[float] = (),
        diagnostic_values: Mapping[str, Any] | None = None) -> _CandidateScore:
    scopes = _AVAILABLE_SCOPES if status == "available" else _INELIGIBLE_SCOPES
    values = {
        "candidate_id": candidate_id,
        "status": status,
        "reason_codes": tuple(dict.fromkeys(str(code) for code in reason_codes)),
        "source_reason_code": source_reason_code,
        "predictive_basis_fingerprint": (prepared.predictive_basis_fingerprint
                                           if prepared is not None else None),
        "source_draw_identity_fingerprint": (prepared.source_draw_identity_fingerprint
                                               if prepared is not None else None),
        "executed_grid_fingerprint": (prepared.executed_grid_fingerprint
                                      if prepared is not None else None),
        "candidate_evaluation_fingerprint": (prepared.candidate_evaluation_fingerprint
                                               if prepared is not None else None),
        "energy_meV": prepared.energy_meV if prepared is not None else (),
        "executed_point_count": len(prepared.energy_meV) if prepared is not None else None,
        "exposure_per_point": prepared.exposure_per_point if prepared is not None else None,
        "conditional_target_qoi_information_nats": information,
        "conditional_target_qoi_information_standard_error_nats": standard_error,
        "information_per_executed_point_nats": per_point_information,
        "information_per_executed_point_standard_error_nats": per_point_standard_error,
        "partition_information_nats": tuple(float(value) for value in partition_information),
        "block_information_nats": tuple(float(value) for value in block_information),
        "simulation_count": simulation_count,
        "block_count": block_count,
        "authority_scopes": tuple(sorted(scopes.items())),
        "candidate_score_fingerprint": "",
        "diagnostics": _pairs(diagnostic_values),
    }
    provisional = _CandidateScore(**values)
    fingerprint = _fingerprint(
        _CANDIDATE_SCORE_FINGERPRINT_DOMAIN,
        _candidate_fingerprint_payload(
            provisional,
            measure_fingerprint=measure_fingerprint,
            discretization_fingerprint=discretization_fingerprint,
            seed=seed,
            simulation_count=simulation_count,
            block_count=block_count,
            backend=backend,
        ),
    )
    return _CandidateScore(**{**values, "candidate_score_fingerprint": fingerprint})


def _score_candidate(
        prepared: _PreparedCandidate,
        measure: ScanWindowDesignMeasure,
        discretization: ScalarQoIDiscretization,
        scenario_indices: np.ndarray,
        partition_bins: Sequence[Sequence[int]],
        seed: int,
        simulation_count: int,
        block_count: int,
        backend: Mapping[str, Any]) -> _CandidateScore:
    observations = _observation_matrix(prepared, measure, scenario_indices, seed)
    likelihoods = _likelihood_matrix(observations, prepared.rates)
    contributions, partition_information = _information_contributions(
        likelihoods,
        scenario_indices,
        measure.predictive_scenarios,
        discretization,
        partition_bins,
    )
    block_size = simulation_count // block_count
    block_information = tuple(
        math.fsum(contributions[start:start + block_size].tolist()) / block_size
        for start in range(0, simulation_count, block_size)
    )
    information = math.fsum(block_information) / block_count
    standard_error = float(np.std(np.asarray(block_information), ddof=1) / math.sqrt(block_count))
    point_count = len(prepared.energy_meV)
    return _finish_candidate(
        candidate_id=prepared.candidate_id,
        status="available",
        reason_codes=(),
        source_reason_code=None,
        measure_fingerprint=measure.design_measure_fingerprint,
        source_draw_identity_fingerprint=measure.source_draw_identity_fingerprint,
        discretization_fingerprint=discretization.discretization_fingerprint,
        seed=seed,
        simulation_count=simulation_count,
        block_count=block_count,
        backend=backend,
        prepared=prepared,
        information=information,
        standard_error=standard_error,
        per_point_information=information / point_count,
        per_point_standard_error=standard_error / point_count,
        partition_information=partition_information,
        block_information=block_information,
    )


def _pairwise_fingerprint_payload(
        comparison: _PairwiseComparison,
        *,
        measure_fingerprint: str,
        discretization_fingerprint: str,
        seed: int,
        simulation_count: int,
        block_count: int,
        backend: Mapping[str, Any]) -> dict[str, Any]:
    payload = comparison.to_dict()
    payload.pop("pairwise_comparison_fingerprint")
    return {
        "scorer_revision": SCORER_REVISION,
        "likelihood_revision": LIKELIHOOD_REVISION,
        "crn_revision": CRN_REVISION,
        "design_measure_fingerprint": measure_fingerprint,
        "qoi_discretization_fingerprint": discretization_fingerprint,
        "seed": seed,
        "simulation_count": simulation_count,
        "block_count": block_count,
        "numerical_backend": dict(backend),
        "comparison": payload,
    }


def _pairwise_comparison(
        first: _CandidateScore,
        second: _CandidateScore,
        *,
        measure_fingerprint: str,
        discretization_fingerprint: str,
        seed: int,
        simulation_count: int,
        block_count: int,
        backend: Mapping[str, Any]) -> _PairwiseComparison:
    absolute_blocks = tuple(
        right - left for left, right in zip(
            first.block_information_nats, second.block_information_nats))
    first_points = int(first.executed_point_count)
    second_points = int(second.executed_point_count)
    per_point_blocks = tuple(
        right / second_points - left / first_points
        for left, right in zip(first.block_information_nats, second.block_information_nats))
    absolute_center = math.fsum(absolute_blocks) / block_count
    per_point_center = math.fsum(per_point_blocks) / block_count
    absolute_se = math.sqrt(
        math.fsum((value - absolute_center) ** 2 for value in absolute_blocks)
        / (block_count - 1),
    ) / math.sqrt(block_count)
    per_point_se = math.sqrt(
        math.fsum((value - per_point_center) ** 2 for value in per_point_blocks)
        / (block_count - 1),
    ) / math.sqrt(block_count)
    values = {
        "candidate_a_id": first.candidate_id,
        "candidate_b_id": second.candidate_id,
        "candidate_a_score_fingerprint": first.candidate_score_fingerprint,
        "candidate_b_score_fingerprint": second.candidate_score_fingerprint,
        "delta_information_nats": (
            float(second.conditional_target_qoi_information_nats)
            - float(first.conditional_target_qoi_information_nats)),
        "delta_information_standard_error_nats": absolute_se,
        "delta_information_per_executed_point_nats": (
            float(second.information_per_executed_point_nats)
            - float(first.information_per_executed_point_nats)),
        "delta_information_per_executed_point_standard_error_nats": per_point_se,
        "paired_block_deltas": absolute_blocks,
        "paired_block_deltas_per_executed_point_nats": per_point_blocks,
        "pairwise_comparison_fingerprint": "",
    }
    provisional = _PairwiseComparison(**values)
    fingerprint = _fingerprint(
        _PAIRWISE_FINGERPRINT_DOMAIN,
        _pairwise_fingerprint_payload(
            provisional,
            measure_fingerprint=measure_fingerprint,
            discretization_fingerprint=discretization_fingerprint,
            seed=seed,
            simulation_count=simulation_count,
            block_count=block_count,
            backend=backend,
        ),
    )
    return _PairwiseComparison(**{**values, "pairwise_comparison_fingerprint": fingerprint})


def _score_set_payload(score_set: ScanWindowScoreSet) -> dict[str, Any]:
    payload = score_set.to_dict()
    payload.pop("score_set_fingerprint")
    payload.pop("diagnostics")
    payload["candidate_scores"] = [
        _candidate_semantic_payload(candidate, include_fingerprint=True)
        for candidate in payload["candidate_scores"]
    ]
    return payload


def _finish_score_set(
        *,
        status: str,
        reason_codes: Sequence[str],
        measure_fingerprint: str | None,
        source_draw_identity_fingerprint: str | None,
        discretization_fingerprint: str | None,
        seed: int | None,
        simulation_count: int | None,
        block_count: int | None,
        backend: Mapping[str, Any],
        candidate_scores: Sequence[_CandidateScore] = (),
        comparisons: Sequence[_PairwiseComparison] = (),
        diagnostic_values: Mapping[str, Any] | None = None) -> ScanWindowScoreSet:
    scopes = _AVAILABLE_SCOPES if status in {"complete", "partial"} else _INELIGIBLE_SCOPES
    values = {
        "contract": SCAN_WINDOW_SCORE_SET_CONTRACT,
        "scorer_revision": SCORER_REVISION,
        "likelihood_revision": LIKELIHOOD_REVISION,
        "crn_revision": CRN_REVISION,
        "status": status,
        "reason_codes": tuple(dict.fromkeys(str(code) for code in reason_codes)),
        "design_measure_fingerprint": measure_fingerprint,
        "source_draw_identity_fingerprint": source_draw_identity_fingerprint,
        "qoi_discretization_fingerprint": discretization_fingerprint,
        "seed": seed,
        "simulation_count": simulation_count,
        "block_count": block_count,
        "numerical_backend": _pairs(backend),
        "authority_scopes": tuple(sorted(scopes.items())),
        "score_set_fingerprint": "",
        "diagnostics": _pairs(diagnostic_values),
        "_candidate_scores": tuple(candidate_scores),
        "_pairwise_comparisons": tuple(comparisons),
    }
    provisional = ScanWindowScoreSet(**values)
    fingerprint = _fingerprint(_SCORE_SET_FINGERPRINT_DOMAIN, _score_set_payload(provisional))
    return ScanWindowScoreSet(**{**values, "score_set_fingerprint": fingerprint})


def _safe_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _global_refusal(
        reason_code: str,
        *,
        authenticated_measure: ScanWindowDesignMeasure | None,
        authenticated_discretization: ScalarQoIDiscretization | None,
        seed: Any,
        simulation_count: Any,
        block_count: Any,
        message: str) -> ScanWindowScoreSet:
    log.warning("scan-window score set refused (%s): %s", reason_code, message)
    measure_fingerprint = (authenticated_measure.design_measure_fingerprint
                           if authenticated_measure is not None else None)
    if not _valid_fingerprint(measure_fingerprint):
        measure_fingerprint = None
    source_fingerprint = (authenticated_measure.source_draw_identity_fingerprint
                          if authenticated_measure is not None else None)
    if not _valid_fingerprint(source_fingerprint):
        source_fingerprint = None
    discretization_fingerprint = (
        authenticated_discretization.discretization_fingerprint
        if authenticated_discretization is not None else None)
    if not _valid_fingerprint(discretization_fingerprint):
        discretization_fingerprint = None
    return _finish_score_set(
        status="unavailable",
        reason_codes=(reason_code,),
        measure_fingerprint=measure_fingerprint,
        source_draw_identity_fingerprint=source_fingerprint,
        discretization_fingerprint=discretization_fingerprint,
        seed=_safe_int(seed),
        simulation_count=_safe_int(simulation_count),
        block_count=_safe_int(block_count),
        backend=_backend_identities(),
        diagnostic_values={"message": message},
    )


def score_scan_window_candidates(
        design_measure: ScanWindowDesignMeasure,
        candidates: Sequence[CandidateEvaluationInput],
        *,
        qoi_discretization: ScalarQoIDiscretization,
        seed: int,
        simulation_count: int = 512,
        block_count: int = 8) -> ScanWindowScoreSet:
    """Estimate conditional target-QoI information for supplied predictive evaluations."""
    try:
        measure = validate_scan_window_design_measure(design_measure)
    except (TypeError, ValueError) as error:
        return _global_refusal(
            "design_measure_invalid", authenticated_measure=None,
            authenticated_discretization=None, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message=str(error),
        )
    if measure.status != "available":
        return _global_refusal(
            "design_measure_unavailable", authenticated_measure=measure,
            authenticated_discretization=None, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="design measure is unavailable",
        )
    qoi = dict(measure.qoi)
    try:
        discretization = _validated_discretization(
            qoi_discretization, qoi_id=qoi["id"], qoi_unit=qoi["unit"])
    except (KeyError, TypeError, ValueError) as error:
        log.warning("QoI discretization refused: %s", error)
        return _global_refusal(
            "qoi_discretization_invalid", authenticated_measure=measure,
            authenticated_discretization=None, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message=str(error),
        )
    if (not isinstance(seed, int) or isinstance(seed, bool)
            or not isinstance(simulation_count, int) or isinstance(simulation_count, bool)
            or not isinstance(block_count, int) or isinstance(block_count, bool)
            or simulation_count < 2 or block_count < 2
            or block_count > simulation_count
            or simulation_count % block_count != 0):
        return _global_refusal(
            "simulation_configuration_invalid", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="simulation and block configuration is invalid",
        )
    if (not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes))
            or not candidates):
        return _global_refusal(
            "candidate_input_empty", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="candidate inputs must be a non-empty sequence",
        )
    if any(not isinstance(item, CandidateEvaluationInput) for item in candidates):
        return _global_refusal(
            "candidate_id_invalid", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="every candidate must be a CandidateEvaluationInput",
        )
    candidate_ids = [item.candidate_id for item in candidates]
    if any(not isinstance(value, str) or not value or value != value.strip()
           for value in candidate_ids):
        return _global_refusal(
            "candidate_id_invalid", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="candidate IDs must be non-empty stripped strings",
        )
    if len(candidate_ids) != len(set(candidate_ids)):
        return _global_refusal(
            "candidate_id_duplicate", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message="candidate IDs must be unique",
        )

    backend = _backend_identities()
    try:
        partition_bins = _partition_bins(measure.predictive_scenarios, discretization)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        log.warning("QoI discretization bin arithmetic refused: %s", error)
        return _global_refusal(
            "qoi_discretization_invalid", authenticated_measure=measure,
            authenticated_discretization=discretization, seed=seed,
            simulation_count=simulation_count, block_count=block_count,
            message=str(error),
        )
    scenario_indices = _scenario_schedule(measure, seed, simulation_count)
    candidate_scores: list[_CandidateScore] = []
    for item in sorted(candidates, key=lambda value: value.candidate_id):
        prepared: _PreparedCandidate | None = None
        try:
            prepared = _prepare_candidate(item, measure)
            score = _score_candidate(
                prepared, measure, discretization, scenario_indices, partition_bins,
                seed, simulation_count, block_count, backend)
        except _CandidateRefusal as error:
            log.warning("scan-window candidate %s refused (%s): %s",
                        item.candidate_id, error.reason_code, error.message)
            score = _finish_candidate(
                candidate_id=item.candidate_id,
                status="unavailable",
                reason_codes=(error.reason_code,),
                source_reason_code=error.source_reason_code,
                measure_fingerprint=measure.design_measure_fingerprint,
                source_draw_identity_fingerprint=measure.source_draw_identity_fingerprint,
                discretization_fingerprint=discretization.discretization_fingerprint,
                seed=seed,
                simulation_count=simulation_count,
                block_count=block_count,
                backend=backend,
                prepared=prepared,
                diagnostic_values={"message": error.message},
            )
        candidate_scores.append(score)

    available = [score for score in candidate_scores if score.status == "available"]
    comparisons = [
        _pairwise_comparison(
            first, second,
            measure_fingerprint=measure.design_measure_fingerprint,
            discretization_fingerprint=discretization.discretization_fingerprint,
            seed=seed,
            simulation_count=simulation_count,
            block_count=block_count,
            backend=backend,
        )
        for first_index, first in enumerate(available)
        for second in available[first_index + 1:]
    ]
    status = ("complete" if len(available) == len(candidate_scores)
              else "partial" if available else "unavailable")
    return _finish_score_set(
        status=status,
        reason_codes=(),
        measure_fingerprint=measure.design_measure_fingerprint,
        source_draw_identity_fingerprint=measure.source_draw_identity_fingerprint,
        discretization_fingerprint=discretization.discretization_fingerprint,
        seed=seed,
        simulation_count=simulation_count,
        block_count=block_count,
        backend=backend,
        candidate_scores=candidate_scores,
        comparisons=comparisons,
    )


__all__ = [
    "CandidateEvaluationInput",
    "ScalarQoIDiscretization",
    "ScanWindowScoreSet",
    "score_scan_window_candidates",
]

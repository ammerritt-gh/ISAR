"""Strict, authenticated specifications for the STAT-2b evidence campaign."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping
import math
import tomllib
from types import MappingProxyType

from isar.schema.statistics import semantics_fingerprint


CAMPAIGN_SCHEMA = "isar.stat2b_campaign/1"
CRITICAL_STRATA = frozenset(
    {
        "near_elastic",
        "weak_signal",
        "partially_resolved_two_mode",
        "target_absent",
        "misspecified_background",
    }
)
TRUTH_KINDS = frozenset(
    {"target_absent", "one_mode", "two_mode", "misspecified_background", "continuum"}
)
PHONON_SIDES = frozenset({"stokes", "stokes+astokes"})
CONSTRAINT_KINDS = frozenset(
    {"unconstrained", "soft_prior_predictive", "hard_prior_predictive", "post_rail_rescue"}
)
CONSTRAINT_ORIGINS = frozenset(
    {"none", "prespecified_scientific", "prior_predictive", "data_dependent_rescue"}
)
EXPOSURE_PATTERNS = frozenset({"uniform", "linear", "alternating"})
TARGET_RANKS = frozenset({"none", "single", "lower", "upper"})
PEAK_COUNT_LEVELS = frozenset({15.0, 30.0, 100.0, 300.0})


def _finite(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _strict_keys(payload: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{context} contains unknown fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class ScenarioSpec:
    """One equal-weight cell in the frozen STAT-2b scenario matrix."""

    id: str
    truth_kind: str
    target_present: bool
    target_rank: str
    peak_counts: float
    x_min: float
    x_max: float
    x_step: float
    gauss_fwhm: float
    lorentz_fwhm: float
    T: float
    gamma: float
    energies: tuple[float, ...]
    phonon_sides: str = "stokes+astokes"
    elastic_height_ratio: float = 0.0
    qe_height_ratio: float = 0.0
    qe_fwhm: float = 2.0
    background_height_ratio: float = 0.02
    background_slope_fraction: float = 0.0
    continuum_height_ratio: float = 0.0
    continuum_center: float = 0.0
    continuum_fwhm: float = 4.0
    calibration_offset: float = 0.0
    resolution_scale: float = 1.0
    exposure_pattern: str = "uniform"
    exposure_min_scale: float = 1.0
    exposure_max_scale: float = 1.0
    constraint_kind: str = "unconstrained"
    constraint_origin: str = "none"
    constraint_offset_sign: int = 0
    constraint_offset_mev: float = 0.0
    constraint_sigma_mev: float = 0.1
    critical_strata: tuple[str, ...] = ()
    oracle_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("scenario.id must be a nonempty string")
        if self.truth_kind not in TRUTH_KINDS:
            raise ValueError(f"scenario {self.id}: unsupported truth_kind {self.truth_kind!r}")
        if not isinstance(self.target_present, bool):
            raise ValueError(f"scenario {self.id}: target_present must be boolean")
        if self.target_rank not in TARGET_RANKS:
            raise ValueError(f"scenario {self.id}: invalid target_rank")
        if self.phonon_sides not in PHONON_SIDES:
            raise ValueError(f"scenario {self.id}: invalid phonon_sides")
        if self.constraint_kind not in CONSTRAINT_KINDS:
            raise ValueError(f"scenario {self.id}: invalid constraint_kind")
        if self.constraint_origin not in CONSTRAINT_ORIGINS:
            raise ValueError(f"scenario {self.id}: invalid constraint_origin")
        if self.exposure_pattern not in EXPOSURE_PATTERNS:
            raise ValueError(f"scenario {self.id}: invalid exposure_pattern")
        if not isinstance(self.oracle_required, bool):
            raise ValueError(f"scenario {self.id}: oracle_required must be boolean")

        numeric = {
            "peak_counts": (self.peak_counts, True, False),
            "x_step": (self.x_step, True, False),
            "gauss_fwhm": (self.gauss_fwhm, True, False),
            "lorentz_fwhm": (self.lorentz_fwhm, False, True),
            "T": (self.T, True, False),
            "gamma": (self.gamma, True, False),
            "elastic_height_ratio": (self.elastic_height_ratio, False, True),
            "qe_height_ratio": (self.qe_height_ratio, False, True),
            "qe_fwhm": (self.qe_fwhm, True, False),
            "background_height_ratio": (self.background_height_ratio, False, True),
            "continuum_height_ratio": (self.continuum_height_ratio, False, True),
            "continuum_fwhm": (self.continuum_fwhm, True, False),
            "resolution_scale": (self.resolution_scale, True, False),
            "exposure_min_scale": (self.exposure_min_scale, True, False),
            "exposure_max_scale": (self.exposure_max_scale, True, False),
            "constraint_offset_mev": (self.constraint_offset_mev, False, True),
            "constraint_sigma_mev": (self.constraint_sigma_mev, True, False),
        }
        for name, (value, positive, nonnegative) in numeric.items():
            _finite(value, f"scenario {self.id}.{name}", positive=positive, nonnegative=nonnegative)
        for name, value in (
            ("x_min", self.x_min),
            ("x_max", self.x_max),
            ("background_slope_fraction", self.background_slope_fraction),
            ("continuum_center", self.continuum_center),
            ("calibration_offset", self.calibration_offset),
        ):
            _finite(value, f"scenario {self.id}.{name}")
        if float(self.x_min) >= float(self.x_max):
            raise ValueError(f"scenario {self.id}: x_min must be less than x_max")
        if abs(float(self.background_slope_fraction)) > 1.0:
            raise ValueError(
                f"scenario {self.id}: background_slope_fraction must remain within [-1, 1]"
            )
        if float(self.peak_counts) not in PEAK_COUNT_LEVELS:
            raise ValueError(
                f"scenario {self.id}: peak_counts must be one of {sorted(PEAK_COUNT_LEVELS)}"
            )
        if float(self.exposure_min_scale) > float(self.exposure_max_scale):
            raise ValueError(f"scenario {self.id}: exposure scale bounds are reversed")

        energies = tuple(_finite(v, f"scenario {self.id}.energies", positive=True)
                         for v in self.energies)
        if tuple(sorted(energies)) != energies or len(set(energies)) != len(energies):
            raise ValueError(f"scenario {self.id}: energies must be unique and increasing")
        if any(value <= float(self.gamma) for value in energies):
            raise ValueError(f"scenario {self.id}: every energy must exceed gamma")
        expected_modes = {"target_absent": 0, "one_mode": 1, "two_mode": 2}
        if self.truth_kind in expected_modes and len(energies) != expected_modes[self.truth_kind]:
            raise ValueError(
                f"scenario {self.id}: {self.truth_kind} requires "
                f"{expected_modes[self.truth_kind]} energies"
            )
        if self.truth_kind in {"misspecified_background", "continuum"} and len(energies) > 2:
            raise ValueError(f"scenario {self.id}: at most two target modes are supported")
        if self.target_present != bool(energies):
            raise ValueError(f"scenario {self.id}: target_present disagrees with energies")
        expected_ranks = {0: {"none"}, 1: {"single"}, 2: {"lower", "upper"}}
        if self.target_rank not in expected_ranks[len(energies)]:
            raise ValueError(f"scenario {self.id}: target_rank disagrees with planted modes")
        if self.truth_kind == "misspecified_background" and self.background_slope_fraction == 0:
            raise ValueError(f"scenario {self.id}: misspecified background requires a slope")
        if self.truth_kind == "continuum" and self.continuum_height_ratio <= 0:
            raise ValueError(f"scenario {self.id}: continuum truth requires continuum signal")

        if isinstance(self.constraint_offset_sign, bool) or self.constraint_offset_sign not in (-1, 0, 1):
            raise ValueError(f"scenario {self.id}: constraint_offset_sign must be -1, 0, or 1")
        if self.constraint_kind == "unconstrained":
            if self.constraint_origin != "none" or self.constraint_offset_sign != 0:
                raise ValueError(f"scenario {self.id}: unconstrained cells have no origin or offset")
        else:
            if self.constraint_origin == "none" or self.constraint_offset_sign == 0:
                raise ValueError(f"scenario {self.id}: constrained cells require origin and sign")
        if self.constraint_kind == "post_rail_rescue" \
                and self.constraint_origin != "data_dependent_rescue":
            raise ValueError(f"scenario {self.id}: rescue constraints require rescue origin")

        strata = tuple(self.critical_strata)
        if len(set(strata)) != len(strata) or any(s not in CRITICAL_STRATA for s in strata):
            raise ValueError(f"scenario {self.id}: invalid or duplicate critical_strata")
        if (len(energies) == 2 or not self.target_present
                or self.truth_kind == "misspecified_background") \
                and not self.oracle_required:
            raise ValueError(f"scenario {self.id}: this truth regime requires oracle evaluation")

        object.__setattr__(self, "energies", energies)
        object.__setattr__(self, "critical_strata", tuple(sorted(strata)))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            field.name: (
                list(value) if isinstance(value := getattr(self, field.name), tuple) else value
            )
            for field in fields(self)
        }

    @property
    def fingerprint(self) -> str:
        return semantics_fingerprint("isar.stat2b_scenario/1", self.canonical_payload())


@dataclass(frozen=True)
class CampaignSpec:
    """A complete campaign definition, independent of execution phase and scheduling."""

    schema: str
    campaign_id: str
    base_seed: int
    phase_seed_namespaces: Mapping[str, str]
    development_realizations: int
    heldout_realizations: int
    delivery_replicates: int
    certification_tolerance_mev: float
    cell_weighting: str
    critical_strata: tuple[str, ...]
    scenarios: tuple[ScenarioSpec, ...]
    heldout_gate_path: str | None = None

    def __post_init__(self) -> None:
        if self.schema != CAMPAIGN_SCHEMA:
            raise ValueError(f"unsupported campaign schema {self.schema!r}")
        if not isinstance(self.campaign_id, str) or not self.campaign_id.strip():
            raise ValueError("campaign_id must be a nonempty string")
        if isinstance(self.base_seed, bool) or not isinstance(self.base_seed, int) \
                or self.base_seed < 0:
            raise ValueError("base_seed must be a nonnegative integer")
        namespaces = dict(self.phase_seed_namespaces)
        if set(namespaces) != {"development", "heldout"} \
                or any(not isinstance(value, str) or not value for value in namespaces.values()) \
                or namespaces["development"] == namespaces["heldout"]:
            raise ValueError("phase_seed_namespaces must contain distinct development and heldout values")
        if self.delivery_replicates != 200:
            raise ValueError("STAT-2b full delivery requires delivery_replicates = 200")
        _finite(
            self.certification_tolerance_mev,
            "certification_tolerance_mev",
            positive=True,
        )
        if isinstance(self.development_realizations, bool) \
                or not isinstance(self.development_realizations, int) \
                or self.development_realizations < 20:
            raise ValueError("development_realizations must be an integer >= 20")
        if isinstance(self.heldout_realizations, bool) \
                or not isinstance(self.heldout_realizations, int) \
                or self.heldout_realizations < 0:
            raise ValueError("heldout_realizations must be a nonnegative integer")
        if self.cell_weighting != "equal":
            raise ValueError("STAT-2b requires equal cell weighting")
        strata = tuple(self.critical_strata)
        if set(strata) != CRITICAL_STRATA or len(strata) != len(CRITICAL_STRATA):
            raise ValueError("campaign critical_strata must contain the five frozen veto strata")
        if not self.scenarios:
            raise ValueError("campaign must contain at least one scenario")
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario ids must be unique")
        covered = {stratum for scenario in self.scenarios for stratum in scenario.critical_strata}
        missing = sorted(CRITICAL_STRATA - covered)
        if missing:
            raise ValueError(f"campaign has no scenario for critical strata: {', '.join(missing)}")
        if self.heldout_gate_path is not None \
                and (not isinstance(self.heldout_gate_path, str) or not self.heldout_gate_path):
            raise ValueError("heldout_gate_path must be a nonempty string when supplied")

        object.__setattr__(self, "phase_seed_namespaces", MappingProxyType(namespaces))
        object.__setattr__(self, "critical_strata", tuple(sorted(strata)))
        object.__setattr__(self, "scenarios", tuple(sorted(self.scenarios, key=lambda item: item.id)))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "campaign_id": self.campaign_id,
            "base_seed": self.base_seed,
            "phase_seed_namespaces": dict(sorted(self.phase_seed_namespaces.items())),
            "development_realizations": self.development_realizations,
            "heldout_realizations": self.heldout_realizations,
            "delivery_replicates": self.delivery_replicates,
            "certification_tolerance_mev": self.certification_tolerance_mev,
            "cell_weighting": self.cell_weighting,
            "critical_strata": list(self.critical_strata),
            "heldout_gate_path": self.heldout_gate_path,
            "scenarios": [scenario.canonical_payload() for scenario in self.scenarios],
        }

    @property
    def fingerprint(self) -> str:
        return semantics_fingerprint(CAMPAIGN_SCHEMA, self.canonical_payload())

    def scenario(self, scenario_id: str) -> ScenarioSpec:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(scenario_id)


def _scenario_from_mapping(payload: Any, index: int) -> ScenarioSpec:
    if not isinstance(payload, Mapping):
        raise ValueError(f"scenarios[{index}] must be a table")
    allowed = {field.name for field in fields(ScenarioSpec)}
    _strict_keys(payload, allowed, f"scenarios[{index}]")
    required = {
        "id", "truth_kind", "target_present", "target_rank", "peak_counts",
        "x_min", "x_max", "x_step", "gauss_fwhm", "lorentz_fwhm", "T", "gamma",
        "energies", "critical_strata", "oracle_required",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"scenarios[{index}] missing fields: {', '.join(missing)}")
    data = dict(payload)
    for name in ("energies", "critical_strata"):
        value = data.get(name)
        if not isinstance(value, list):
            raise ValueError(f"scenarios[{index}].{name} must be an array")
        data[name] = tuple(value)
    return ScenarioSpec(**data)


def load_campaign_spec(path: str | Path) -> CampaignSpec:
    """Load a strict TOML campaign specification and compute its canonical fingerprint."""
    source = Path(path)
    with source.open("rb") as stream:
        payload = tomllib.load(stream)
    if not isinstance(payload, Mapping):
        raise ValueError("campaign specification must be a TOML table")
    allowed = {
        "schema", "campaign_id", "base_seed", "phase_seed_namespaces",
        "development_realizations", "heldout_realizations",
        "delivery_replicates", "certification_tolerance_mev",
        "cell_weighting", "critical_strata",
        "heldout_gate_path", "scenarios", "scenario_matrix_path",
    }
    _strict_keys(payload, allowed, "campaign")
    required = allowed - {
        "heldout_gate_path", "scenarios", "scenario_matrix_path"
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"campaign missing fields: {', '.join(missing)}")
    if ("scenarios" in payload) == ("scenario_matrix_path" in payload):
        raise ValueError(
            "campaign must declare exactly one of scenarios or scenario_matrix_path"
        )
    scenarios = payload.get("scenarios")
    if "scenario_matrix_path" in payload:
        matrix_reference = payload["scenario_matrix_path"]
        if not isinstance(matrix_reference, str) or not matrix_reference:
            raise ValueError("campaign.scenario_matrix_path must be a nonempty string")
        matrix_path = source.parent / matrix_reference
        with matrix_path.open("rb") as stream:
            matrix_payload = tomllib.load(stream)
        scenarios = matrix_payload.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("campaign.scenarios must be an array of tables")
    strata = payload["critical_strata"]
    if not isinstance(strata, list):
        raise ValueError("campaign.critical_strata must be an array")
    namespaces = payload["phase_seed_namespaces"]
    if not isinstance(namespaces, Mapping):
        raise ValueError("campaign.phase_seed_namespaces must be a table")
    _strict_keys(namespaces, {"development", "heldout"}, "phase_seed_namespaces")
    return CampaignSpec(
        schema=payload["schema"],
        campaign_id=payload["campaign_id"],
        base_seed=payload["base_seed"],
        phase_seed_namespaces=dict(namespaces),
        development_realizations=payload["development_realizations"],
        heldout_realizations=payload["heldout_realizations"],
        delivery_replicates=payload["delivery_replicates"],
        certification_tolerance_mev=payload["certification_tolerance_mev"],
        cell_weighting=payload["cell_weighting"],
        critical_strata=tuple(strata),
        heldout_gate_path=payload.get("heldout_gate_path"),
        scenarios=tuple(
            _scenario_from_mapping(scenario, index) for index, scenario in enumerate(scenarios)
        ),
    )

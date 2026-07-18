"""Frozen excitation-hypothesis and steering-policy contracts.

Hypotheses describe physics; :class:`SteeringPolicy` describes acquisition tactics.  Policy TOML is
human-owned, while the resolved dictionaries produced here are copied into the objective before a
campaign starts.  Numeric code therefore never consults a mutable preset or policy file mid-run.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any


HYPOTHESIS_SCHEMA = "isar.excitation-hypothesis/1"
PRESET_VERSION = "isar.hypothesis-presets/1"
ACQUISITION_ESTIMATOR_VERSION = "isar.branch-mi/gh32-component-v1"
DISPERSION_TYPES = ("branch", "level", "continuum")
IMPLEMENTED_DISPERSION_TYPES = ("branch",)
PRESET_NAMES = ("acoustic", "optic", "null")


@dataclass(frozen=True)
class GaussianPrior:
    mean: float
    sigma: float

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.mean)):
            raise ValueError("prior mean must be finite")
        if not math.isfinite(float(self.sigma)) or float(self.sigma) <= 0.0:
            raise ValueError("prior sigma must be finite and positive")

    def to_dict(self) -> dict[str, float]:
        return {"mean": float(self.mean), "sigma": float(self.sigma)}


@dataclass(frozen=True)
class ExcitationHypothesis:
    """One resolved, non-negative-energy excitation hypothesis."""

    name: str
    dispersion_type: str = "branch"
    zc_energy: GaussianPrior = field(default_factory=lambda: GaussianPrior(0.0, 1e3))
    slope_zc: GaussianPrior = field(default_factory=lambda: GaussianPrior(0.0, 1e3))
    confidence: float = 0.0
    preset: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("hypothesis name must not be empty")
        if self.dispersion_type not in DISPERSION_TYPES:
            raise ValueError(f"dispersion_type {self.dispersion_type!r} not in {DISPERSION_TYPES}")
        if self.dispersion_type not in IMPLEMENTED_DISPERSION_TYPES:
            raise ValueError(f"dispersion_type {self.dispersion_type!r} is declared but not implemented")
        if float(self.zc_energy.mean) < 0.0:
            raise ValueError("zc_energy mean must be non-negative")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("hypothesis confidence must lie in [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HYPOTHESIS_SCHEMA,
            "name": self.name,
            "dispersion_type": self.dispersion_type,
            "zc_energy": self.zc_energy.to_dict(),
            "slope_zc": self.slope_zc.to_dict(),
            "confidence": float(self.confidence),
            "preset": self.preset,
            "preset_version": PRESET_VERSION,
        }


@dataclass(frozen=True)
class SteeringPolicy:
    """Typed acquisition constants frozen with an objective.

    ``minimum_information_per_cost`` is the single O4 diminishing-returns threshold.  GH32 is part
    of the estimator version rather than a tuneable implementation detail.
    """

    classification_weight: float = 1.0
    differentiation_tolerance_meV: float | None = None
    assignment_entropy_stop_nats: float = 0.2
    minimum_information_per_cost: float = 0.0
    minimum_target_support_points: int = 6
    max_scan_points: int = 100
    absent_signal_widen_step: float = 0.5
    absent_signal_widen_steps: int = 3
    quadrature_order: int = 32
    acquisition_estimator_version: str = ACQUISITION_ESTIMATOR_VERSION
    mismatch_z_threshold: float = 3.0
    quicklook_ncount: int = 10_000_000
    production_batch_ncount: int = 25_000_000
    maximum_production_batches_per_q: int = 12
    dynamic_ncount: bool = False
    max_ncount_per_scan: int = 4_000_000_000
    ncount_growth_cap: float = 4.0
    tolerance_margin: float = 0.8
    interval_floor_window: int = 3
    interval_floor_min_improvement: float = 0.05
    interval_floor_min_exposure_ratio: float = 2.0

    def __post_init__(self) -> None:
        finite_values = {
            "classification_weight": self.classification_weight,
            "assignment_entropy_stop_nats": self.assignment_entropy_stop_nats,
            "minimum_information_per_cost": self.minimum_information_per_cost,
            "absent_signal_widen_step": self.absent_signal_widen_step,
            "mismatch_z_threshold": self.mismatch_z_threshold,
            "ncount_growth_cap": self.ncount_growth_cap,
            "tolerance_margin": self.tolerance_margin,
            "interval_floor_min_improvement": self.interval_floor_min_improvement,
            "interval_floor_min_exposure_ratio": self.interval_floor_min_exposure_ratio,
        }
        if self.differentiation_tolerance_meV is not None:
            finite_values["differentiation_tolerance_meV"] = self.differentiation_tolerance_meV
        for name, value in finite_values.items():
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        integer_values = {
            "minimum_target_support_points": self.minimum_target_support_points,
            "max_scan_points": self.max_scan_points,
            "absent_signal_widen_steps": self.absent_signal_widen_steps,
            "quadrature_order": self.quadrature_order,
            "quicklook_ncount": self.quicklook_ncount,
            "production_batch_ncount": self.production_batch_ncount,
            "maximum_production_batches_per_q": self.maximum_production_batches_per_q,
            "max_ncount_per_scan": self.max_ncount_per_scan,
            "interval_floor_window": self.interval_floor_window,
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if not isinstance(self.dynamic_ncount, bool):
            raise ValueError("dynamic_ncount must be a boolean")
        if float(self.classification_weight) < 0.0:
            raise ValueError("classification_weight must be non-negative")
        if self.differentiation_tolerance_meV is not None and float(self.differentiation_tolerance_meV) <= 0:
            raise ValueError("differentiation_tolerance_meV must be positive")
        if float(self.assignment_entropy_stop_nats) < 0.0:
            raise ValueError("assignment_entropy_stop_nats must be non-negative")
        if float(self.minimum_information_per_cost) < 0.0:
            raise ValueError("minimum_information_per_cost must be non-negative")
        if int(self.minimum_target_support_points) < 1 or int(self.max_scan_points) < 2:
            raise ValueError("point-count policy values are too small")
        if float(self.absent_signal_widen_step) <= 0.0 or int(self.absent_signal_widen_steps) <= 0:
            raise ValueError("absent-signal widening values must be positive")
        if int(self.quadrature_order) != 32:
            raise ValueError("the v1 acquisition estimator requires committed GH32 quadrature")
        if float(self.mismatch_z_threshold) <= 0.0:
            raise ValueError("mismatch_z_threshold must be positive")
        if int(self.quicklook_ncount) <= 0 or int(self.production_batch_ncount) <= 0:
            raise ValueError("count batches must be positive")
        if int(self.maximum_production_batches_per_q) < 2:
            raise ValueError("maximum_production_batches_per_q must be at least two")
        if int(self.max_ncount_per_scan) < int(self.production_batch_ncount):
            raise ValueError("max_ncount_per_scan must be at least the production batch floor")
        if float(self.ncount_growth_cap) < 1.0:
            raise ValueError("ncount_growth_cap must be at least one")
        if not 0.0 < float(self.tolerance_margin) <= 1.0:
            raise ValueError("tolerance_margin must be in (0, 1]")
        if int(self.interval_floor_window) < 3:
            raise ValueError("interval_floor_window must be at least three")
        if not 0.0 <= float(self.interval_floor_min_improvement) < 1.0:
            raise ValueError("interval_floor_min_improvement must be in [0, 1)")
        if float(self.interval_floor_min_exposure_ratio) <= 1.0:
            raise ValueError("interval_floor_min_exposure_ratio must exceed one")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prior(value: Any, default: GaussianPrior, *, energy: bool = False) -> GaussianPrior:
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError("prior must be a {mean|mode, sigma} table")
    mean = value.get("mean", value.get("mode", default.mean))
    prior = GaussianPrior(float(mean), float(value.get("sigma", default.sigma)))
    if energy and prior.mean < 0.0:
        raise ValueError("zc_energy mean must be non-negative")
    return prior


def preset(name: str, energy_search_meV: tuple[float, float] = (-2.0, 12.0)) -> ExcitationHypothesis:
    """Resolve a shipped preset; optic defaults are derived once from the positive search band."""
    if name == "acoustic":
        return ExcitationHypothesis(name="acoustic", preset="acoustic",
                                    zc_energy=GaussianPrior(0.0, 0.2),
                                    slope_zc=GaussianPrior(12.0, 12.0), confidence=0.8)
    if name == "optic":
        lo, hi = map(float, energy_search_meV)
        positive_lo = max(0.0, lo)
        width = max(hi - positive_lo, 1e-6)
        return ExcitationHypothesis(name="optic", preset="optic",
                                    zc_energy=GaussianPrior(positive_lo + width / 2.0, width / 4.0),
                                    slope_zc=GaussianPrior(0.0, 8.0), confidence=0.6)
    if name == "null":
        return ExcitationHypothesis(name="null", preset="null",
                                    zc_energy=GaussianPrior(0.0, 1e3),
                                    slope_zc=GaussianPrior(0.0, 1e3), confidence=0.0)
    raise ValueError(f"unknown hypothesis preset {name!r}")


def resolve_hypothesis(name: str, declaration: dict[str, Any] | None = None, *,
                       energy_search_meV: tuple[float, float] = (-2.0, 12.0)) -> ExcitationHypothesis:
    """Resolve a named preset plus human overrides, or a fully custom branch declaration."""
    declaration = dict(declaration or {})
    base_name = str(declaration.pop("preset", name if name in PRESET_NAMES else ""))
    base = preset(base_name, energy_search_meV) if base_name else ExcitationHypothesis(name=name)
    allowed = {"dispersion_type", "zc_energy", "slope_zc", "flatness", "confidence"}
    unknown = sorted(set(declaration) - allowed)
    if unknown:
        raise ValueError(f"unknown hypothesis field(s): {', '.join(unknown)}")
    slope = _prior(declaration.get("slope_zc"), base.slope_zc)
    flatness = declaration.get("flatness")
    if flatness is not None:
        if flatness not in ("low", "high"):
            raise ValueError("flatness must be 'low' or 'high'")
        slope = GaussianPrior(slope.mean, 4.0 if flatness == "high" else 16.0)
    return ExcitationHypothesis(
        name=name,
        preset=base_name or None,
        dispersion_type=str(declaration.get("dispersion_type", base.dispersion_type)),
        zc_energy=_prior(declaration.get("zc_energy"), base.zc_energy, energy=True),
        slope_zc=slope,
        confidence=float(declaration.get("confidence", base.confidence)),
    )


def content_fingerprint(hypotheses: list[dict[str, Any]], steering: dict[str, Any]) -> str:
    """Stable fingerprint over every resolved input that can affect branch acquisition."""
    payload = {"hypotheses": hypotheses, "steering": steering,
               "preset_version": PRESET_VERSION,
               "acquisition_estimator_version": ACQUISITION_ESTIMATOR_VERSION}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

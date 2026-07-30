"""Deterministic STAT-2b truth generation using production fit components."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import numpy as np

from isar.analyze.components import (
    CompositeModel,
    ConstantBackground,
    Elastic,
    PhononDHO,
    QuasiElastic,
)
from isar.analyze.context import ScanContext
from isar.schema.statistics import semantics_fingerprint
from isar.schema.steering import ParamConstraints
from isar.synth._ff import Voigt

from .spec import CampaignSpec, ScenarioSpec


@dataclass(frozen=True)
class TruthDataset:
    """One paired fit/held-out Poisson realization and its separated planted truth."""

    fit_context: ScanContext
    lambda_counts: np.ndarray
    heldout_counts: np.ndarray
    truth_manifest: dict[str, Any]
    dataset_id: str
    seed: int


def _stream_seed(
    spec: CampaignSpec,
    scenario: ScenarioSpec,
    phase: str,
    realization_index: int,
    stream: str,
) -> int:
    if phase not in spec.phase_seed_namespaces:
        raise ValueError("phase must be 'development' or 'heldout'")
    if isinstance(realization_index, bool) or not isinstance(realization_index, int) \
            or realization_index < 0:
        raise ValueError("realization_index must be a nonnegative integer")
    digest = semantics_fingerprint(
        "isar.stat2b_rng_stream/1",
        {
            "base_seed": spec.base_seed,
            "phase_namespace": spec.phase_seed_namespaces[phase],
            "scenario_fingerprint": scenario.fingerprint,
            "realization_index": realization_index,
            "stream": stream,
        },
    )
    return int(digest[:16], 16)


def _energy_grid(scenario: ScenarioSpec) -> np.ndarray:
    n = int(round((scenario.x_max - scenario.x_min) / scenario.x_step)) + 1
    x = scenario.x_min + scenario.x_step * np.arange(n, dtype=float)
    if not np.isclose(x[-1], scenario.x_max, rtol=0.0, atol=1e-10):
        raise ValueError(f"scenario {scenario.id}: window is not divisible by x_step")
    return x


def _exposure_scale(scenario: ScenarioSpec, n: int) -> np.ndarray | None:
    if scenario.exposure_pattern == "uniform" \
            and scenario.exposure_min_scale == scenario.exposure_max_scale == 1.0:
        return None
    lo = float(scenario.exposure_min_scale)
    hi = float(scenario.exposure_max_scale)
    if scenario.exposure_pattern == "uniform":
        return np.full(n, lo, dtype=float)
    if scenario.exposure_pattern == "linear":
        return np.linspace(lo, hi, n, dtype=float)
    values = np.empty(n, dtype=float)
    values[0::2] = lo
    values[1::2] = hi
    return values


def _constraints(scenario: ScenarioSpec, target_omega_d: float | None) -> ParamConstraints:
    if scenario.constraint_kind in {"unconstrained", "post_rail_rescue"}:
        # A rescue cell starts unconstrained; the production delivery path decides whether it rails.
        return ParamConstraints()
    if target_omega_d is None:
        raise ValueError(f"scenario {scenario.id}: a target constraint requires a target mode")
    value = (
        target_omega_d
        + scenario.constraint_offset_sign * scenario.constraint_offset_mev
    )
    if value <= 0:
        raise ValueError(f"scenario {scenario.id}: target constraint must remain positive")
    name = "phonon2.Omega_d" if scenario.target_rank == "upper" else "phonon.Omega_d"
    origins = {name: scenario.constraint_origin}
    if scenario.constraint_kind == "soft_prior_predictive":
        return ParamConstraints(
            soft={name: (value, scenario.constraint_sigma_mev)}, origins=origins
        )
    return ParamConstraints(hard={name: value}, origins=origins)


def _target_index(scenario: ScenarioSpec) -> int | None:
    if scenario.target_rank == "none":
        return None
    if scenario.target_rank in {"single", "lower"}:
        return 0
    return 1


def _build_truth_model(
    scenario: ScenarioSpec,
    x: np.ndarray,
    truth_context: ScanContext,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    components = []
    theta: list[float] = []
    modes: list[dict[str, Any]] = []
    peak_kind = scenario.phonon_sides
    exposure = truth_context.exposure_for(x)
    exposure_multiplier = 1.0 if exposure is None else exposure

    for index, omega0 in enumerate(scenario.energies):
        prefix = "phonon" if index == 0 else f"phonon{index + 1}"
        component = PhononDHO(prefix=prefix, peak=peak_kind)
        omega_d = math.sqrt(omega0**2 - scenario.gamma**2)
        unit = component.evaluate(
            x, {"Omega_d": omega_d, "Gamma": scenario.gamma, "A": 1.0}, truth_context
        )
        amplitude = float(scenario.peak_counts) / float(np.max(unit * exposure_multiplier))
        components.append(component)
        theta.extend((omega_d, scenario.gamma, amplitude))
        modes.append(
            {
                "rank": "lower" if index == 0 and len(scenario.energies) == 2
                        else "upper" if index == 1 else "single",
                "omega0": float(omega0),
                "omega_d": omega_d,
                "gamma": float(scenario.gamma),
                "amplitude": amplitude,
                "phonon_sides": peak_kind,
            }
        )

    if scenario.elastic_height_ratio > 0:
        component = Elastic()
        unit_height = float(
            np.max(component.evaluate(x, {"E": 1.0}, truth_context) * exposure_multiplier)
        )
        components.append(component)
        theta.append(float(scenario.peak_counts) * scenario.elastic_height_ratio / unit_height)
    if scenario.qe_height_ratio > 0:
        component = QuasiElastic()
        unit_height = float(
            np.max(component.evaluate(x, {"QE": 1.0}, truth_context) * exposure_multiplier)
        )
        components.append(component)
        theta.append(float(scenario.peak_counts) * scenario.qe_height_ratio / unit_height)
    components.append(ConstantBackground())
    theta.append(float(scenario.peak_counts) * scenario.background_height_ratio)

    lam = CompositeModel(components).evaluate(x, theta, truth_context)
    if scenario.background_slope_fraction != 0:
        scale_x = 2.0 * (x - x.min()) / (x.max() - x.min()) - 1.0
        slope = (
            float(scenario.peak_counts)
            * scenario.background_height_ratio
            * scenario.background_slope_fraction
            * scale_x
        )
        lam = lam + slope * exposure_multiplier
    if scenario.continuum_height_ratio > 0:
        unit = Voigt(
            x, 1.0, scenario.continuum_fwhm, 0.0, scenario.continuum_center
        )
        area = (
            float(scenario.peak_counts)
            * scenario.continuum_height_ratio
            / float(np.max(unit))
        )
        lam = lam + Voigt(
            x, area, scenario.continuum_fwhm, 0.0, scenario.continuum_center
        ) * exposure_multiplier
    if np.any(~np.isfinite(lam)) or np.any(lam < 0):
        raise ValueError(f"scenario {scenario.id}: planted mean is not finite and nonnegative")
    return np.asarray(lam, dtype=float), modes


def generate_dataset(
    spec: CampaignSpec,
    scenario: ScenarioSpec | str,
    realization_index: int,
    *,
    phase: str = "development",
) -> TruthDataset:
    """Generate paired Poisson draws without exposing planted truth to the fit context."""
    if isinstance(scenario, str):
        scenario = spec.scenario(scenario)
    elif not isinstance(scenario, ScenarioSpec):
        raise TypeError("scenario must be a ScenarioSpec or scenario id")
    canonical = spec.scenario(scenario.id)
    if canonical.fingerprint != scenario.fingerprint:
        raise ValueError("scenario does not belong to the supplied campaign specification")

    x = _energy_grid(scenario)
    exposure = _exposure_scale(scenario, len(x))
    mask = np.ones(x.shape, dtype=bool)
    empty_counts = np.zeros(x.shape, dtype=float)
    true_center = float(scenario.calibration_offset)
    true_gauss = float(scenario.gauss_fwhm * scenario.resolution_scale)
    true_lorentz = float(scenario.lorentz_fwhm * scenario.resolution_scale)
    truth_context = ScanContext(
        x=x,
        counts=empty_counts,
        mask=mask,
        T=float(scenario.T),
        center=true_center,
        gauss_fwhm=true_gauss,
        lorentz_fwhm=true_lorentz,
        instrument="STAT2B_SYNTH",
        energy_status="absolute",
        quasielastic_fwhm=float(scenario.qe_fwhm),
        exposure_scale=exposure,
        exposure_reference=1.0 if exposure is not None else None,
    )
    lam, modes = _build_truth_model(scenario, x, truth_context)
    fit_seed = _stream_seed(spec, scenario, phase, realization_index, "fit")
    heldout_seed = _stream_seed(spec, scenario, phase, realization_index, "heldout")
    fit_counts = np.random.default_rng(fit_seed).poisson(lam)
    heldout_counts = np.random.default_rng(heldout_seed).poisson(lam)

    target_index = _target_index(scenario)
    target_omega_d = None if target_index is None else modes[target_index]["omega_d"]
    fit_context = ScanContext(
        x=x,
        counts=fit_counts,
        mask=mask,
        T=float(scenario.T),
        center=0.0,
        gauss_fwhm=float(scenario.gauss_fwhm),
        lorentz_fwhm=float(scenario.lorentz_fwhm),
        instrument="STAT2B_SYNTH",
        energy_status="absolute",
        quasielastic_fwhm=float(scenario.qe_fwhm),
        constraints=_constraints(scenario, target_omega_d),
        exposure_scale=exposure,
        exposure_reference=1.0 if exposure is not None else None,
    )
    dataset_id = semantics_fingerprint(
        "isar.stat2b_dataset/1",
        {
            "campaign_fingerprint": spec.fingerprint,
            "phase_namespace": spec.phase_seed_namespaces[phase],
            "scenario_fingerprint": scenario.fingerprint,
            "realization_index": realization_index,
        },
    )
    manifest = {
        "schema": "isar.stat2b_truth/1",
        "dataset_id": dataset_id,
        "campaign_fingerprint": spec.fingerprint,
        "scenario_id": scenario.id,
        "scenario_fingerprint": scenario.fingerprint,
        "phase": phase,
        "realization_index": realization_index,
        "fit_seed": fit_seed,
        "heldout_seed": heldout_seed,
        "truth_kind": scenario.truth_kind,
        "target_present": scenario.target_present,
        "target_rank": scenario.target_rank,
        "target_mode_index": target_index,
        "modes": modes,
        "true_center": true_center,
        "fit_center": 0.0,
        "true_gauss_fwhm": true_gauss,
        "true_lorentz_fwhm": true_lorentz,
        "fit_gauss_fwhm": float(scenario.gauss_fwhm),
        "fit_lorentz_fwhm": float(scenario.lorentz_fwhm),
        "elastic_height_ratio": float(scenario.elastic_height_ratio),
        "qe_height_ratio": float(scenario.qe_height_ratio),
        "background_height_ratio": float(scenario.background_height_ratio),
        "background_slope_fraction": float(scenario.background_slope_fraction),
        "continuum_height_ratio": float(scenario.continuum_height_ratio),
        "continuum_center": float(scenario.continuum_center),
        "continuum_fwhm": float(scenario.continuum_fwhm),
        "exposure_pattern": scenario.exposure_pattern,
        "constraint_kind": scenario.constraint_kind,
        "constraint_origin": scenario.constraint_origin,
        "critical_strata": list(scenario.critical_strata),
        "oracle_required": scenario.oracle_required,
        "lambda_fingerprint": semantics_fingerprint(
            "isar.stat2b_lambda/1", {"values": lam.tolist()}
        ),
    }
    manifest["truth_fingerprint"] = semantics_fingerprint(
        "isar.stat2b_truth_manifest/1", manifest
    )
    return TruthDataset(
        fit_context=fit_context,
        lambda_counts=lam,
        heldout_counts=heldout_counts,
        truth_manifest=manifest,
        dataset_id=dataset_id,
        seed=fit_seed,
    )

"""Qt-free posterior-predictive information scoring shared by analysis and steering.

Two independent surfaces live here:

* :func:`predictive_score` — the existing outer action/Q ranking estimate (unchanged);
* :func:`evaluate_master_predictive` — the pure arbitrary-grid evaluator over an
  ``isar.master_predictive/2`` artifact. It owns prediction only: no fitting, no state mutation, no
  utility, no candidate generation, no instrument truth.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np


def _discrete_information(latent, observed) -> float:
    latent = np.asarray(latent, dtype=int)
    observed = np.asarray(observed, dtype=int)
    if latent.size == 0:
        return 0.0
    joint, latent_counts, observed_counts = {}, {}, {}
    for left, right in zip(latent.tolist(), observed.tolist()):
        joint[(left, right)] = joint.get((left, right), 0) + 1
        latent_counts[left] = latent_counts.get(left, 0) + 1
        observed_counts[right] = observed_counts.get(right, 0) + 1
    n = float(latent.size)
    return float(sum(
        (count / n) * math.log((count * n) /
                               (latent_counts[left] * observed_counts[right]))
        for (left, right), count in joint.items() if count))


def predictive_score(models: list[dict], *, seed: int, n_simulations: int = 512) -> dict:
    """Seeded information estimate for one incremental scan from full-parameter model draws."""
    draw_models, draw_omega, draw_rates, draw_weights = [], [], [], []
    for model_index, model in enumerate(models):
        rates = np.asarray(model.get("expected_counts") or (), dtype=float)
        omega = np.asarray(model.get("omega0_draws") or (), dtype=float)
        weight = float(model.get("weight") or 0.0)
        if (rates.ndim != 2 or rates.shape[0] == 0 or rates.shape[1] == 0
                or omega.ndim != 1 or omega.size not in (0, rates.shape[0])
                or not np.all(np.isfinite(rates)) or np.any(rates < 0.0)
                or (omega.size and not np.all(np.isfinite(omega)))
                or not math.isfinite(weight) or weight <= 0.0):
            continue
        draw_models.extend([model_index] * rates.shape[0])
        # A no-phonon model has a valid predictive count distribution but no target-ω draw.  Keep it
        # in the joint/model latent state using a dedicated missing-ω state below.
        draw_omega.extend(omega.tolist() if omega.size else [float("nan")] * rates.shape[0])
        draw_rates.extend(rates.tolist())
        draw_weights.extend([weight / rates.shape[0]] * rates.shape[0])
    if not draw_rates or sum(draw_weights) <= 0.0:
        return {"usable": False, "reason": "no valid full-parameter predictive draws"}
    weights = np.asarray(draw_weights, dtype=float)
    weights /= weights.sum()
    rng = np.random.default_rng(int(seed))
    choices = rng.choice(len(draw_rates), size=max(int(n_simulations), 64), p=weights)
    rates = np.maximum(np.asarray(draw_rates, dtype=float)[choices], 0.0)
    counts = rng.poisson(rates)
    energy_index = np.arange(counts.shape[1], dtype=float)
    totals = counts.sum(axis=1)
    centroids = np.divide(counts @ energy_index, totals,
                          out=np.zeros_like(totals, dtype=float), where=totals > 0)
    omega = np.asarray(draw_omega, dtype=float)[choices]
    models_drawn = np.asarray(draw_models, dtype=int)[choices]

    def quantile_bins(values, bins=8):
        edges = np.unique(np.quantile(values, np.linspace(0.0, 1.0, bins + 1)))
        return (np.digitize(values, edges[1:-1]) if edges.size > 2
                else np.zeros(values.size, dtype=int))

    observed = quantile_bins(centroids) * 8 + quantile_bins(totals)
    finite_omega = np.isfinite(omega)
    omega_bins = np.zeros(omega.size, dtype=int)
    if np.any(finite_omega):
        finite_bins = quantile_bins(omega[finite_omega])
        omega_bins[finite_omega] = finite_bins
        omega_bins[~finite_omega] = int(finite_bins.max(initial=0)) + 1
    joint_latent = omega_bins * max(len(models), 1) + models_drawn
    joint_information = _discrete_information(joint_latent, observed)
    omega_information = (_discrete_information(omega_bins[finite_omega], observed[finite_omega])
                         if np.any(finite_omega) else 0.0)
    identity_information = _discrete_information(models_drawn, observed)
    finite_values = omega[finite_omega]
    current_halfwidth = (0.5 * (np.quantile(finite_values, 0.84) -
                                np.quantile(finite_values, 0.16))
                         if finite_values.size else 0.0)
    posterior_widths = []
    for observed_bin in np.unique(observed):
        selected = omega[(observed == observed_bin) & finite_omega]
        if selected.size > 2:
            posterior_widths.append((selected.size, 0.5 * (
                np.quantile(selected, 0.84) - np.quantile(selected, 0.16))))
    expected_width = (sum(n * width for n, width in posterior_widths) /
                      sum(n for n, _ in posterior_widths)) if posterior_widths else current_halfwidth
    return {
        "usable": True, "method": "posterior_predictive_mc",
        "expected_information_nats": float(joint_information),
        "omega_information_nats": float(omega_information),
        "model_branch_information_nats": float(identity_information),
        "expected_omega_halfwidth_reduction_meV": max(
            float(current_halfwidth - expected_width), 0.0),
        "simulation_count": int(len(choices)),
        "score_standard_error_nats": float(
            max(joint_information, 1e-9) / math.sqrt(len(choices))),
        "seed": int(seed), "common_random_seed": int(seed),
    }


PREDICTIVE_ARTIFACT_SCHEMA = "isar.master_predictive/2"
PREDICTIVE_DESIGN_EVALUATION_SCHEMA = "isar.predictive_design_evaluation/1"

#: Stable refusal codes. A consumer routes on these, never on message text.
ARTIFACT_SCHEMA_UNSUPPORTED = "artifact_schema_unsupported"
MISSING_EXPOSURE_BASIS = "missing_exposure_basis"
MALFORMED_DRAW_SHAPE = "malformed_draw_shape"
UNKNOWN_COMPONENT_KIND = "unknown_component_kind"
NONFINITE_PREDICTION = "nonfinite_prediction"
INVALID_ENERGY_GRID = "invalid_energy_grid"
INVALID_REQUESTED_EXPOSURE = "invalid_requested_exposure"
PREDICTIVE_BASIS_INCOMPLETE = "predictive_basis_incomplete"


@dataclass(frozen=True)
class PredictiveDesignEvaluation:
    """One ``isar.predictive_design_evaluation/1`` result: available, or refused with a reason."""

    available: bool
    schema: str = PREDICTIVE_DESIGN_EVALUATION_SCHEMA
    reason_code: str | None = None
    message: str | None = None
    energy_meV: tuple = ()
    exposure_per_point: float | None = None
    exposure_ratio: float | None = None
    predictive_basis_fingerprint: str | None = None
    models: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema, "available": self.available,
            "reason_code": self.reason_code, "message": self.message,
            "energy_meV": list(self.energy_meV),
            "exposure_per_point": self.exposure_per_point,
            "exposure_ratio": self.exposure_ratio,
            "predictive_basis_fingerprint": self.predictive_basis_fingerprint,
            "models": [dict(model) for model in self.models],
        }


def _refuse(reason_code: str, message: str, **echo) -> PredictiveDesignEvaluation:
    return PredictiveDesignEvaluation(available=False, reason_code=reason_code,
                                      message=message, **echo)


def evaluate_master_predictive(artifact, energy_meV, exposure_per_point
                               ) -> PredictiveDesignEvaluation:
    """Evaluate a published predictive basis on an arbitrary grid at one requested exposure.

    Every retained model is reconstructed from its own manifest and every draw is substituted **by
    name** through the shared evaluation path (:mod:`isar.analyze.model_manifest`) — the same path
    that produced the artifact's stored matrices — then evaluated directly at each requested energy
    and multiplied by the scalar ratio ``exposure_per_point / fit_reference_exposure``. Stored
    expected counts are never interpolated.

    The scalar ratio is valid on every pool, including exposure-heterogeneous ones: a fitted
    amplitude is a rate (counts per ``fit_reference_exposure`` monitor counts), and a candidate scan
    requests one uniform exposure per point (EIG-0 §7.1). ``exposure_homogeneous`` gates only the
    artifact's own scalar fitted-grid parity claim, not this evaluation.

    Deterministic and order-stable: model and draw order follow the artifact. Any unsupported input
    returns ``available=False`` with a stable ``reason_code`` — nothing is dropped and continued.
    """
    from isar.analyze import model_manifest as manifest_mod

    if not isinstance(artifact, Mapping):
        return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED, "predictive artifact must be an object")
    schema = artifact.get("schema")
    if schema != PREDICTIVE_ARTIFACT_SCHEMA:
        return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED,
                       f"unsupported predictive artifact schema {schema!r}")
    fingerprint = artifact.get("predictive_basis_fingerprint")
    try:
        grid = manifest_mod.energy_grid(energy_meV)
    except manifest_mod.ModelManifestError as error:
        return _refuse(error.reason_code, error.message,
                       predictive_basis_fingerprint=fingerprint)
    echo = {"energy_meV": tuple(float(value) for value in grid),
            "predictive_basis_fingerprint": fingerprint}
    try:
        exposure = float(exposure_per_point)
    except (TypeError, ValueError):
        return _refuse(INVALID_REQUESTED_EXPOSURE,
                       "requested exposure per point must be numeric", **echo)
    if not math.isfinite(exposure) or exposure <= 0.0:
        return _refuse(INVALID_REQUESTED_EXPOSURE,
                       "requested exposure per point must be finite and positive", **echo)
    echo["exposure_per_point"] = exposure

    basis = artifact.get("exposure_basis")
    reference = basis.get("fit_reference_exposure") if isinstance(basis, Mapping) else None
    try:
        reference = float(reference)
    except (TypeError, ValueError):
        reference = float("nan")
    if not math.isfinite(reference) or reference <= 0.0:
        # The genuine no-monitor case: the master fitted on the uniform path, so its amplitudes
        # have no absolute exposure basis to scale (EIG-0 §4.6).
        return _refuse(MISSING_EXPOSURE_BASIS,
                       "predictive artifact has no finite positive fit reference exposure", **echo)
    ratio = exposure / reference
    echo["exposure_ratio"] = ratio

    completeness = artifact.get("completeness")
    if not isinstance(completeness, Mapping) \
            or completeness.get("predictive_complete_for_eig") is not True:
        return _refuse(PREDICTIVE_BASIS_INCOMPLETE,
                       "predictive basis is not complete enough to score (ADR-014)", **echo)

    context_values = artifact.get("evaluation_context")
    if not isinstance(context_values, Mapping):
        return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED,
                       "predictive artifact has no evaluation context", **echo)
    try:
        context = manifest_mod.EvaluationContext(
            center=context_values.get("center_meV"),
            gauss_fwhm=context_values.get("gaussian_fwhm_meV"),
            lorentz_fwhm=context_values.get("lorentzian_fwhm_meV"),
            T=context_values.get("temperature_K"),
            quasielastic_fwhm=context_values.get("quasielastic_fwhm_meV"),
            exposure_ratio=ratio)
    except manifest_mod.ModelManifestError as error:
        return _refuse(error.reason_code, error.message, **echo)

    models = artifact.get("models")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)) or not models:
        return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED,
                       "predictive artifact has no retained models", **echo)
    evaluated = []
    for index, model in enumerate(models):
        if not isinstance(model, Mapping):
            return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED,
                           f"predictive model {index} must be an object", **echo)
        try:
            weight = float(model.get("weight"))
        except (TypeError, ValueError):
            weight = float("nan")
        if not math.isfinite(weight) or weight <= 0.0:
            return _refuse(ARTIFACT_SCHEMA_UNSUPPORTED,
                           f"predictive model {index} has no usable weight", **echo)
        draws = model.get("parameter_draws")
        if not isinstance(draws, Sequence) or isinstance(draws, (str, bytes)) or not draws:
            return _refuse(MALFORMED_DRAW_SHAPE,
                           f"predictive model {index} has no parameter draws", **echo)
        names = model.get("parameter_names")
        expected = []
        for draw in draws:
            try:
                expected.append([float(value) for value in manifest_mod.expected_counts(
                    model.get("model_manifest"), names, draw, context, grid)])
            except manifest_mod.ModelManifestError as error:
                return _refuse(error.reason_code,
                               f"predictive model {index}: {error.message}", **echo)
        raw_omega = model.get("omega0_draws") or ()
        if isinstance(raw_omega, (str, bytes)) or not isinstance(raw_omega, Sequence):
            return _refuse(MALFORMED_DRAW_SHAPE,
                           f"predictive model {index} omega0_draws must be a sequence", **echo)
        try:
            omega0_draws = [float(value) for value in raw_omega]
        except (TypeError, ValueError):
            return _refuse(MALFORMED_DRAW_SHAPE,
                           f"predictive model {index} has non-numeric omega0_draws", **echo)
        evaluated.append({
            "label": model.get("label"),
            "identity_label": model.get("identity_label", model.get("label")),
            "weight": weight,
            "draw_basis": model.get("draw_basis"),
            "draw_weighting": model.get("draw_weighting"),
            "draw_weights": model.get("draw_weights"),
            "draw_ids": list(model.get("draw_ids") or ()),
            "parameter_names": list(names or ()),
            # Coerced defensively: this function's contract is that ANY unusable artifact returns a
            # stable reason_code, so a non-numeric omega draw must refuse here rather than escape as
            # a ValueError past every structural check above.
            "omega0_draws": omega0_draws,
            "target_present_by_draw": list(model.get("target_present_by_draw") or ()),
            "target_qoi_draws": list(model.get("target_qoi_draws") or ()),
            "expected_counts": expected,
        })
    return PredictiveDesignEvaluation(available=True, models=tuple(evaluated), **echo)

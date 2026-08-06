"""Qt-free posterior-predictive information scoring shared by analysis and steering.

Two independent surfaces live here:

* :func:`predictive_score` — the existing outer action/Q ranking estimate (unchanged);
* :func:`evaluate_master_predictive` — the pure arbitrary-grid evaluator over an
  ``isar.master_predictive/2`` artifact. It owns prediction only: no fitting, no state mutation, no
  utility, no candidate generation, no instrument truth.
"""
from __future__ import annotations

import hashlib
import json
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

#: The published predictive **contract revision** — the schema label plus the revision of the payload
#: shape carried under it. It is the single definition (re-exported by
#: :mod:`isar.session.certification` and :mod:`isar.session.driver`, never re-declared) and it joins
#: the master-fit cache fingerprint, so bumping it makes every cached master regenerate its
#: predictive artifact under the new shape without a refit of the science.
#:
#: ``+physical_only.1`` is the ADR-019 reshape: ``/2`` is physical-only (manifests, named draws,
#: expected counts, ``omega0_draws``, per-draw ``mode_energies``) and the target projection moved out
#: to its own authority-bound ``isar.target_projection/2`` artifact.
PREDICTIVE_CONTRACT = f"{PREDICTIVE_ARTIFACT_SCHEMA}+physical_only.1"

#: The separated target-projection artifact (ADR-019). Resolved after branch triage and the connected
#: tracker, never inside ``/2``.
TARGET_PROJECTION_SCHEMA = "isar.target_projection/2"
#: The registered v1 QoI projection, now resolved against the AUTHORITATIVE target energy (the
#: post-triage, post-tracker selection) rather than the fit's local best.
TARGET_PROJECTION_VERSION = "isar.omega0_authoritative_mode/2"
TARGET_QOI = "omega0"

# ADR-014 completeness constants. They live in the artifact and are hashed into
# ``predictive_basis_fingerprint``, so a basis pruned under one rule can never be scored under
# another.
PREDICTIVE_MODEL_WEIGHT_FLOOR = 0.005
#: The share of the production Akaike weight the design basis should RETAIN before the scan-window
#: design measure can fairly be said to speak for the master.  **Recorded, not yet enforced.**
#: Nothing gates on it today: the retained mass is published on every measure as
#: ``mass_diagnostics.source_aic_scale.bootstrap_model_mass``, and this constant names the reviewed
#: bound so a future gate and a fresh census reference one number rather than each inventing one.
#: It is not wired in because an observability code would make eligibility ``unassessable``
#: (``isar/eig/eligibility.py``), which both the design measure and the O41 selector treat as a
#: refusal — so a bound routed that way would switch EIG off for roughly half of all masters
#: instead of qualifying them.  Gating needs a route that degrades without refusing.
#:
#: It bounds what is kept, not what is discarded, so mass that is unaccounted for
#: (``basis_unknown``, pruned models) cannot slip through the gap a discarded-mass ceiling would
#: leave.  The structures that split an Akaike set
#: are the ones that add a component, and those move omega0 rather than only the linewidth, so
#: discarded model mass is discarded omega0 information — not a cosmetic difference.
#:
#: 0.5 is a REVIEWED PLACEHOLDER, not an empirical calibration: the Slice 1 census aggregate is
#: 43.6 % bootstrap / 56.4 % laplace / 0.0 % nested across 234 masters, so 0.5 flags roughly the
#: worse half.  It cannot be set empirically from that census — 230 of its 234 masters carry
#: ``projection_predictive_basis_unknown`` and every ``particle_source_masses`` field is 0.0, so it
#: predates a working projection path and holds no retained-mass distribution.  A fresh census pass
#: over a working projection path is the follow-up that can replace this number with a measured one.
MINIMUM_AUTHORITATIVE_DESIGN_MASS = 0.5
PREDICTIVE_MAX_PRUNED_MASS = 0.01
PREDICTIVE_MAX_DROPPED_DRAW_MASS = 0.01

#: The declared draw-measure vocabulary (ADR-014). A model states which one it produced; nothing is
#: inferred from the shape of ``draw_weights``.
DRAW_WEIGHTING_VOCABULARY = ("equal_weight_resampled", "explicit_weights")

#: Stable refusal codes. A consumer routes on these, never on message text.
ARTIFACT_SCHEMA_UNSUPPORTED = "artifact_schema_unsupported"
MISSING_EXPOSURE_BASIS = "missing_exposure_basis"
MALFORMED_DRAW_SHAPE = "malformed_draw_shape"
UNKNOWN_COMPONENT_KIND = "unknown_component_kind"
NONFINITE_PREDICTION = "nonfinite_prediction"
NEGATIVE_PREDICTION = "negative_prediction"
INVALID_ENERGY_GRID = "invalid_energy_grid"
INVALID_REQUESTED_EXPOSURE = "invalid_requested_exposure"
PREDICTIVE_BASIS_INCOMPLETE = "predictive_basis_incomplete"
#: The stored basis fingerprint does not reproduce from the artifact's own content.
PREDICTIVE_BASIS_FINGERPRINT_MISMATCH = "predictive_basis_fingerprint_mismatch"
#: Structural/measure violations found by :func:`validate_master_predictive_artifact`.
PREDICTIVE_ARTIFACT_INVALID = "predictive_artifact_invalid"
#: A recognized additive statistical-semantics record does not authenticate against its contents.
PREDICTIVE_SEMANTICS_INVALID = "predictive_semantics_invalid"


class PredictiveArtifactError(ValueError):
    """A published predictive artifact this validator refuses, with a stable reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = str(reason_code)
        self.message = str(message)


def json_fingerprint(value) -> str:
    """Canonical sha256 over a JSON-serializable payload (sorted keys, no whitespace)."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def predictive_basis_fingerprint(*, models, energy_meV, evaluation_context,
                                 exposure_basis) -> str:
    """Stable hash of exactly the scientific + evaluation content a score depends on.

    Deliberately EXCLUDES diagnostics, timings, identity stamps, and the ensemble seed: two bases
    that predict the same counts from the same draws must fingerprint the same, and any change to a
    model, a draw, the grid, the context, the exposure basis, the negative-prediction tolerance or
    the pruning rule must change it.

    Since ADR-019 the target projection is NOT hashed here — it is no longer part of ``/2``. The
    per-draw ``mode_energies`` that a later projection selects from are.
    """
    from isar.analyze import model_manifest as manifest_mod

    payload = {
        "schema": PREDICTIVE_CONTRACT,
        "evaluator_version": manifest_mod.EVALUATOR_VERSION,
        "negative_prediction_eps": manifest_mod.NEGATIVE_PREDICTION_EPS,
        # The value alone is not the rule: the same 1e-9 applied absolutely to the exposure-scaled
        # mean and applied per unit exposure give different verdicts on the same model.
        "negative_prediction_eps_rule": manifest_mod.NEGATIVE_PREDICTION_EPS_RULE,
        "models": [{
            "label": model["label"],
            "identity_label": model["identity_label"],
            "source_weight": model["source_weight"],
            "model_manifest": model["model_manifest"],
            "parameter_names": model["parameter_names"],
            "parameter_draws": model["parameter_draws"],
            "draw_weighting": model["draw_weighting"],
            "draw_weights": model["draw_weights"],
            "mode_energies": model["mode_energies"],
        } for model in models],
        "energy_meV": list(energy_meV),
        "evaluation_context": evaluation_context,
        "exposure_basis": exposure_basis,
        "completeness_constants": {
            "model_weight_floor": PREDICTIVE_MODEL_WEIGHT_FLOOR,
            "max_pruned_mass": PREDICTIVE_MAX_PRUNED_MASS,
            "max_dropped_draw_mass": PREDICTIVE_MAX_DROPPED_DRAW_MASS,
        },
    }
    return json_fingerprint(payload)


def target_projection_set_fingerprint(models: Sequence[Mapping]) -> str:
    """Fingerprint the actual records in an ``isar.target_projection/2`` model set.

    The target projection is deliberately outside the physical predictive `/2`
    artifact.  Its producer and every adapting reader therefore share this
    small, structural identity calculation rather than trusting a duplicated
    stored value.  It intentionally captures the historic projection-set
    identity only: model label, model weight, and the aligned draw count.
    """
    records = []
    for index, model in enumerate(models):
        if not isinstance(model, Mapping):
            raise ValueError(f"target projection model {index} must be a mapping")
        label = model.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"target projection model {index} has no label")
        try:
            weight = float(model.get("weight"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"target projection model {label!r} has no finite weight") from error
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError(f"target projection model {label!r} has no finite positive weight")
        present = model.get("target_present_by_draw")
        if not _is_list(present):
            raise ValueError(f"target projection model {label!r} has no target-presence draws")
        records.append({"label": label, "weight": model.get("weight"), "draw_count": len(present)})
    from isar.schema.statistics import semantics_fingerprint

    return semantics_fingerprint(
        "isar.target_projection_model_set/1",
        sorted(records, key=lambda item: str(item["label"])),
    )


# --------------------------------------------------------------------------------------------
# The one strict validator (ADR-020)
# --------------------------------------------------------------------------------------------
_EXPOSURE_BASIS_FIELDS = (
    "unit", "request_unit", "request_to_exposure_factor", "fit_reference_exposure", "n_members",
    "reference_incremental_exposure", "per_energy_monitor", "stored_exposure_ratio",
    "scalar_exposure_eligible", "exposure_homogeneous", "full_member_support",
    "exposure_ratio_span", "kept_point_count")


def _invalid(message: str, reason_code: str = PREDICTIVE_ARTIFACT_INVALID):
    raise PredictiveArtifactError(reason_code, message)


def _is_list(value) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _finite_vector(value, label: str, *, reason_code: str = PREDICTIVE_ARTIFACT_INVALID):
    if not _is_list(value):
        _invalid(f"{label} must be a sequence", reason_code)
    out = []
    for index, item in enumerate(value):
        try:
            number = float(item)
        except (TypeError, ValueError):
            _invalid(f"{label}[{index}] must be numeric", reason_code)
        if not math.isfinite(number):
            _invalid(f"{label}[{index}] must be finite", reason_code)
        out.append(number)
    return out


def _validate_exposure_basis(basis, n_energy: int) -> None:
    if not isinstance(basis, Mapping):
        _invalid("predictive artifact has no exposure basis", MISSING_EXPOSURE_BASIS)
    missing = [name for name in _EXPOSURE_BASIS_FIELDS if name not in basis]
    if missing:
        _invalid(f"exposure basis is missing {missing}")
    for name in ("unit", "request_unit"):
        if not isinstance(basis[name], str) or not basis[name]:
            _invalid(f"exposure basis {name} must be a non-empty string")
    try:
        factor = float(basis["request_to_exposure_factor"])
    except (TypeError, ValueError):
        factor = float("nan")
    if not math.isfinite(factor) or factor <= 0.0:
        _invalid("exposure basis request_to_exposure_factor must be finite and positive")
    if not isinstance(basis["n_members"], int) or isinstance(basis["n_members"], bool) \
            or basis["n_members"] < 1:
        _invalid("exposure basis n_members must be a positive integer")
    if basis["kept_point_count"] != n_energy:
        _invalid("exposure basis kept_point_count does not match the fitted grid")
    for name in ("per_energy_monitor", "stored_exposure_ratio"):
        values = basis[name]
        if values is None:
            continue
        vector = _finite_vector(values, f"exposure basis {name}")
        if len(vector) != n_energy:
            _invalid(f"exposure basis {name} is not aligned to the fitted grid")
        if any(value < 0.0 for value in vector):
            _invalid(f"exposure basis {name} must be non-negative")
    reference = basis["fit_reference_exposure"]
    if reference is not None:
        try:
            reference = float(reference)
        except (TypeError, ValueError):
            reference = float("nan")
        if not math.isfinite(reference) or reference <= 0.0:
            # The genuine no-monitor case is `None`; a present-but-unusable value is a malformed
            # artifact, and both are refused with the same stable code a consumer already routes on.
            _invalid("exposure basis fit_reference_exposure must be finite and positive",
                     MISSING_EXPOSURE_BASIS)
    else:
        _invalid("predictive artifact has no finite positive fit reference exposure",
                 MISSING_EXPOSURE_BASIS)


def _validate_model(model, index: int, n_energy: int, quasielastic_fwhm, context) -> float:
    from isar.analyze import model_manifest as manifest_mod

    if not isinstance(model, Mapping):
        _invalid(f"predictive model {index} must be an object", ARTIFACT_SCHEMA_UNSUPPORTED)
    # ``label``, ``identity_label`` and ``source_weight`` are all hashed into the basis fingerprint.
    # Checking them here is what makes a missing one report as a MISSING FIELD rather than surfacing
    # later as an unexplained fingerprint mismatch.
    for name in ("label", "identity_label"):
        if not isinstance(model.get(name), str) or not model[name]:
            _invalid(f"predictive model {index} has no {name}")
    try:
        weight = float(model.get("weight"))
    except (TypeError, ValueError):
        weight = float("nan")
    if not math.isfinite(weight) or weight <= 0.0:
        _invalid(f"predictive model {index} has no usable weight", ARTIFACT_SCHEMA_UNSUPPORTED)
    try:
        source_weight = float(model.get("source_weight"))
    except (TypeError, ValueError):
        source_weight = float("nan")
    if not math.isfinite(source_weight) or source_weight <= 0.0:
        _invalid(f"predictive model {index} has no usable source_weight")

    names = model.get("parameter_names")
    if not _is_list(names) or not all(isinstance(name, str) for name in names) or not names:
        _invalid(f"predictive model {index} parameter_names must be a list of strings",
                 MALFORMED_DRAW_SHAPE)
    if len(set(names)) != len(names):
        _invalid(f"predictive model {index} has duplicate parameter names", MALFORMED_DRAW_SHAPE)
    draws = model.get("parameter_draws")
    if not _is_list(draws) or not draws:
        _invalid(f"predictive model {index} has no parameter draws", MALFORMED_DRAW_SHAPE)
    n_draws = len(draws)

    # The manifest and every draw are validated through the shared evaluation path, so an unknown
    # component kind or an unmappable draw refuses here exactly as it would at evaluation time.
    composite = manifest_mod.build_composite(model.get("model_manifest"), quasielastic_fwhm)
    for draw in draws:
        manifest_mod.draw_theta(composite, names, draw)

    weighting = model.get("draw_weighting")
    if weighting not in DRAW_WEIGHTING_VOCABULARY:
        _invalid(f"predictive model {index} declares unknown draw_weighting {weighting!r}")
    draw_weights = model.get("draw_weights")
    if weighting == "equal_weight_resampled":
        if draw_weights is not None:
            _invalid(f"predictive model {index} declares equal_weight_resampled but carries "
                     "explicit draw weights")
    else:
        vector = _finite_vector(draw_weights, f"predictive model {index} draw_weights")
        if len(vector) != n_draws:
            _invalid(f"predictive model {index} draw_weights are not aligned to its draws")
        if any(value < 0.0 for value in vector):
            _invalid(f"predictive model {index} draw_weights must be non-negative")
        if sum(vector) <= 0.0:
            _invalid(f"predictive model {index} draw_weights carry no positive mass")

    draw_ids = model.get("draw_ids")
    if draw_ids is not None:
        if not _is_list(draw_ids) or len(draw_ids) != n_draws:
            _invalid(f"predictive model {index} draw_ids are not aligned to its draws")
        if len(set(map(str, draw_ids))) != n_draws:
            _invalid(f"predictive model {index} draw_ids are not unique")

    omega0 = model.get("omega0_draws")
    if omega0 is None:
        _invalid(f"predictive model {index} must declare omega0_draws (empty means no target mode)",
                 MALFORMED_DRAW_SHAPE)
    omega_vector = _finite_vector(omega0, f"predictive model {index} omega0_draws",
                                  reason_code=MALFORMED_DRAW_SHAPE)
    if len(omega_vector) not in (0, n_draws):
        _invalid(f"predictive model {index} omega0_draws are neither absent nor aligned",
                 MALFORMED_DRAW_SHAPE)

    modes = model.get("mode_energies")
    if not _is_list(modes) or len(modes) != n_draws:
        _invalid(f"predictive model {index} mode_energies are not aligned to its draws",
                 MALFORMED_DRAW_SHAPE)
    for draw_index, row in enumerate(modes):
        _finite_vector(row, f"predictive model {index} mode_energies[{draw_index}]",
                       reason_code=MALFORMED_DRAW_SHAPE)

    expected = model.get("expected_counts")
    if expected is not None:
        if not _is_list(expected) or len(expected) != n_draws:
            _invalid(f"predictive model {index} expected_counts are not aligned to its draws",
                     MALFORMED_DRAW_SHAPE)
        for row_index, row in enumerate(expected):
            values = _finite_vector(row, f"predictive model {index} expected_counts[{row_index}]",
                                    reason_code=NONFINITE_PREDICTION)
            if len(values) != n_energy:
                _invalid(f"predictive model {index} expected_counts[{row_index}] is not aligned "
                         "to the fitted grid", MALFORMED_DRAW_SHAPE)
            if any(value < 0.0 for value in values):
                _invalid(f"predictive model {index} expected_counts[{row_index}] is negative",
                         NEGATIVE_PREDICTION)
    del context
    return weight


def validate_master_predictive_artifact(artifact) -> Mapping:
    """The one strict gate every ``isar.master_predictive/2`` reader passes through (ADR-020).

    Raises :class:`PredictiveArtifactError` with a stable ``reason_code``; returns the artifact
    unchanged when it is usable. Checks run structure-first and the stored
    ``predictive_basis_fingerprint`` recomputation LAST, so a reader always learns *what* is wrong
    rather than only that the hash moved.
    """
    from isar.analyze.model_manifest import ModelManifestError

    try:
        return _validate_master_predictive_artifact(artifact)
    except ModelManifestError as error:
        # The shared evaluation path already publishes the stable code this refusal deserves; it is
        # republished verbatim rather than flattened into a generic structural failure.
        raise PredictiveArtifactError(error.reason_code, error.message) from error


def _validate_master_predictive_artifact(artifact) -> Mapping:
    from isar.analyze import model_manifest as manifest_mod
    from isar.schema.statistics import (
        SemanticsConstructionError,
        validate_predictive_semantics,
    )

    if not isinstance(artifact, Mapping):
        _invalid("predictive artifact must be an object", ARTIFACT_SCHEMA_UNSUPPORTED)
    schema = artifact.get("schema")
    if schema != PREDICTIVE_ARTIFACT_SCHEMA:
        _invalid(f"unsupported master predictive schema {schema!r}", ARTIFACT_SCHEMA_UNSUPPORTED)

    energy = artifact.get("energy_meV")
    grid = _finite_vector(energy, "predictive artifact energy_meV",
                          reason_code=INVALID_ENERGY_GRID)
    if not grid:
        _invalid("predictive artifact energy_meV must be non-empty", INVALID_ENERGY_GRID)

    context_values = artifact.get("evaluation_context")
    if not isinstance(context_values, Mapping):
        _invalid("predictive artifact has no evaluation context", ARTIFACT_SCHEMA_UNSUPPORTED)
    context = manifest_mod.EvaluationContext(
        center=context_values.get("center_meV"),
        gauss_fwhm=context_values.get("gaussian_fwhm_meV"),
        lorentz_fwhm=context_values.get("lorentzian_fwhm_meV"),
        T=context_values.get("temperature_K"),
        quasielastic_fwhm=context_values.get("quasielastic_fwhm_meV"))

    _validate_exposure_basis(artifact.get("exposure_basis"), len(grid))

    completeness = artifact.get("completeness")
    if not isinstance(completeness, Mapping):
        _invalid("predictive artifact has no completeness declaration")
    if completeness.get("predictive_complete_for_eig") is not True:
        _invalid("predictive basis is not complete enough to score (ADR-014)",
                 PREDICTIVE_BASIS_INCOMPLETE)

    models = artifact.get("models")
    if not _is_list(models) or not models:
        _invalid("predictive artifact has no retained models", ARTIFACT_SCHEMA_UNSUPPORTED)
    weights = [_validate_model(model, index, len(grid), context.quasielastic_fwhm, context)
               for index, model in enumerate(models)]
    if not math.isclose(sum(weights), 1.0, rel_tol=1e-6, abs_tol=1e-9):
        _invalid("predictive model weights are not normalized")

    semantics = artifact.get("statistical_semantics")
    if semantics is not None:
        try:
            validated_semantics = validate_predictive_semantics(semantics)
        except (SemanticsConstructionError, TypeError, ValueError, OverflowError) as error:
            detail = (
                error.reason_code
                if isinstance(error, SemanticsConstructionError)
                else type(error).__name__
            )
            _invalid(
                f"predictive statistical semantics failed authentication ({detail})",
                PREDICTIVE_SEMANTICS_INVALID,
            )
        semantics_fingerprint = validated_semantics["statistical_semantics_fingerprint"]
        if artifact.get("statistical_semantics_fingerprint") != semantics_fingerprint:
            _invalid(
                "artifact statistical_semantics_fingerprint does not match its semantics record",
                PREDICTIVE_SEMANTICS_INVALID,
            )
        for name in (
            "predictive_retained_set_fingerprint",
            "construction_complete",
            "homogeneous_design_eligible",
        ):
            if artifact.get(name) != validated_semantics.get(name):
                _invalid(
                    f"artifact {name} does not match its authenticated semantics record",
                    PREDICTIVE_SEMANTICS_INVALID,
                )
        semantic_models = {
            str(item.get("label")): item
            for item in validated_semantics.get("models") or ()
            if isinstance(item, Mapping)
        }
        if set(semantic_models) != {str(model["label"]) for model in models}:
            _invalid(
                "predictive statistical semantics model set does not match the physical artifact",
                PREDICTIVE_SEMANTICS_INVALID,
            )
        for model in models:
            label = str(model["label"])
            if model.get("statistical_semantics") != semantic_models[label]:
                _invalid(
                    f"predictive model {label!r} semantics do not match the authenticated ensemble",
                    PREDICTIVE_SEMANTICS_INVALID,
                )

    stored = artifact.get("predictive_basis_fingerprint")
    if not isinstance(stored, str) or not stored:
        _invalid("predictive artifact has no basis fingerprint",
                 PREDICTIVE_BASIS_FINGERPRINT_MISMATCH)
    try:
        recomputed = predictive_basis_fingerprint(
            models=models, energy_meV=grid, evaluation_context=context_values,
            exposure_basis=artifact.get("exposure_basis"))
    except KeyError as error:
        raise PredictiveArtifactError(
            PREDICTIVE_BASIS_FINGERPRINT_MISMATCH,
            f"predictive artifact is missing a fingerprinted field: {error}") from error
    if recomputed != stored:
        _invalid("stored predictive_basis_fingerprint does not reproduce from the artifact "
                 "content", PREDICTIVE_BASIS_FINGERPRINT_MISMATCH)
    return artifact


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
    and multiplied by the scalar ratio
    ``exposure_per_point × request_to_exposure_factor / fit_reference_exposure``. Stored expected
    counts are never interpolated.

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
    fingerprint = artifact.get("predictive_basis_fingerprint")
    # ADR-020: one shared strict validator, before any evaluation. Nothing below re-derives a
    # weaker subset of these checks, so the three readers cannot drift apart.
    try:
        validate_master_predictive_artifact(artifact)
    except PredictiveArtifactError as error:
        return _refuse(error.reason_code, error.message,
                       predictive_basis_fingerprint=(fingerprint if isinstance(fingerprint, str)
                                                     else None))
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

    basis = artifact["exposure_basis"]
    reference = float(basis["fit_reference_exposure"])
    # ADR-020: ``exposure_per_point`` is a REQUEST quantity (TAVI ``number_neutrons``); the fitted
    # amplitudes live in exposure-basis units (monitor counts). Convert before dividing, so an
    # instrument whose monitor is not its request is scaled rather than silently mis-scaled. The map
    # is the identity on TAVI today (EIG-0 §4.1) and the factor is 1.0 there.
    factor = float(basis["request_to_exposure_factor"])
    ratio = (exposure * factor) / reference
    echo["exposure_ratio"] = ratio

    context_values = artifact["evaluation_context"]
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

    evaluated = []
    for index, model in enumerate(artifact["models"]):
        names = model.get("parameter_names")
        expected = []
        for draw in model["parameter_draws"]:
            try:
                expected.append([float(value) for value in manifest_mod.expected_counts(
                    model.get("model_manifest"), names, draw, context, grid)])
            except manifest_mod.ModelManifestError as error:
                return _refuse(error.reason_code,
                               f"predictive model {index}: {error.message}", **echo)
        evaluated.append({
            "label": model.get("label"),
            "identity_label": model.get("identity_label", model.get("label")),
            "weight": float(model["weight"]),
            "draw_basis": model.get("draw_basis"),
            "draw_weighting": model.get("draw_weighting"),
            "draw_weights": model.get("draw_weights"),
            "draw_ids": list(model.get("draw_ids") or ()),
            "parameter_names": list(names or ()),
            "omega0_draws": [float(value) for value in model["omega0_draws"]],
            # Physical-only (ADR-019): the per-draw all-mode energies a later target projection
            # selects from. No target state is carried here.
            "mode_energies": [[float(value) for value in row] for row in model["mode_energies"]],
            "expected_counts": expected,
        })
    return PredictiveDesignEvaluation(available=True, models=tuple(evaluated), **echo)

"""The single manifest → expected-counts evaluation path (Qt-free, analysis-owned).

One declarative ``isar.model_manifest/1`` payload plus one **named** parameter draw, an evaluation
context and an energy grid produce one expected-count vector. Every producer and consumer of a
predictive artifact goes through :func:`expected_counts`:

* :mod:`isar.session.driver` builds the stored ``isar.master_predictive/2`` matrices with it,
* :func:`isar.predictive.evaluate_master_predictive` re-evaluates the same draws on an arbitrary
  requested grid with it,
* :mod:`isar.workspace_api.evaluate` rebuilds the delivered workspace curve with it.

There is deliberately no second (positional) reconstruction path: a stored matrix and an
arbitrary-grid prediction that disagree would be undetectable.

Two rules make the mapping fail-closed rather than plausible:

* **Substitution is by name.** A draw is mapped from its own ``parameter_names`` into
  ``CompositeModel.param_names()`` order; a duplicate, missing or extra name invalidates the model
  instead of silently shifting a parameter into the wrong slot (which a positional implementation
  cannot detect).
* **An unknown component kind refuses the whole evaluation.** Dropping the unknown component and
  continuing would publish a plausible but incomplete model.

Failures raise :class:`ModelManifestError`, which carries the stable ``reason_code`` the predictive
evaluator republishes. The refusal taxonomy is the module constants below; two of them carry
non-obvious scope:

* ``artifact_schema_unsupported`` covers every malformed *declarative* input — the manifest itself,
  its stored parameters, and the evaluation context. An evaluation context is malformed when a
  value is non-finite **or physically impossible**: ``temperature_K`` must be strictly positive,
  because the Bose factor ``exp(-ℏω/k_BT)`` divides by T (T = 0 raises) and a negative T inverts
  detailed balance into a meaningless anti-Stokes explosion that would otherwise be published with
  ``available=True``.
* ``nonfinite_prediction`` covers a structurally valid input whose *evaluation* is unusable.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

MANIFEST_SCHEMA = "isar.model_manifest/1"

#: Version of this evaluation path. It is hashed into ``predictive_basis_fingerprint`` so a score
#: computed under one evaluator can never be reused under another.
EVALUATOR_VERSION = "isar.model_manifest_evaluator/1"

#: Stable refusal codes raised from here (republished verbatim by the predictive evaluator).
UNKNOWN_COMPONENT_KIND = "unknown_component_kind"
MALFORMED_MANIFEST = "artifact_schema_unsupported"
MALFORMED_DRAW_SHAPE = "malformed_draw_shape"
INVALID_ENERGY_GRID = "invalid_energy_grid"
INVALID_REQUESTED_EXPOSURE = "invalid_requested_exposure"
NONFINITE_PREDICTION = "nonfinite_prediction"
NEGATIVE_PREDICTION = "negative_prediction"

#: How far below zero an expected count may fall and still be read as floating-point noise around a
#: genuinely zero mean. Anything below ``-NEGATIVE_PREDICTION_EPS`` is a real negative prediction —
#: a broken model, not a rounding artefact — and refuses instead of being clamped away. It is hashed
#: alongside :data:`EVALUATOR_VERSION` into ``predictive_basis_fingerprint``, so a score computed
#: under one tolerance can never be reused under another (ADR-020).
NEGATIVE_PREDICTION_EPS = 1e-9

#: The registered PhononDHO peak vocabulary. ``Fitting_Functions.convolute_voigt_dho`` lowercases
#: ``peak`` and either takes the literal ``all`` or splits it on ``+`` into these parts; an
#: unrecognised part is silently dropped there, producing a plausible but wrong lineshape.
PHONON_PEAK_PARTS = ("stokes", "astokes", "elastic")
PHONON_PEAK_ALL = "all"


class ModelManifestError(ValueError):
    """A manifest, draw, grid or exposure that this evaluator refuses, with a stable reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = str(reason_code)
        self.message = str(message)


def _finite_float(value, label: str, *, reason_code: str = MALFORMED_DRAW_SHAPE) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelManifestError(reason_code, f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ModelManifestError(reason_code, f"{label} must be finite")
    return result


def _validated_peak(value, index: int) -> str:
    """One PhononDHO ``peak`` selector checked against the registered vocabulary."""
    if not isinstance(value, str) or not value:
        raise ModelManifestError(MALFORMED_MANIFEST,
                                 f"model component {index} peak must be a non-empty string")
    lowered = value.lower()
    if lowered == PHONON_PEAK_ALL:
        return value
    parts = lowered.split("+")
    unknown = [part for part in parts if part not in PHONON_PEAK_PARTS]
    if not parts or unknown:
        raise ModelManifestError(
            MALFORMED_MANIFEST,
            f"model component {index} declares unregistered peak parts {unknown or parts}")
    return value


def build_composite(manifest, quasielastic_fwhm=None):
    """Reconstruct the registered :class:`~isar.analyze.components.CompositeModel` of a manifest.

    ``quasielastic_fwhm`` is required only when the manifest declares a quasielastic component (its
    width is a card default, never a fitted parameter).
    """
    from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic,
                                         PhononDHO, QuasiElastic)
    if not isinstance(manifest, Mapping):
        raise ModelManifestError(MALFORMED_MANIFEST, "model manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ModelManifestError(
            MALFORMED_MANIFEST,
            f"unsupported model manifest schema {manifest.get('schema')!r}")
    factories = {
        "phonon_dho": lambda item, index: PhononDHO(
            prefix=str(item["prefix"]),
            peak=_validated_peak(item.get("peak", "stokes+astokes"), index)),
        "elastic": lambda item, index: Elastic(),
        "quasielastic": lambda item, index: QuasiElastic(),
        "constant_background": lambda item, index: ConstantBackground(),
    }
    components = []
    seen_prefixes = set()
    for index, item in enumerate(manifest.get("components") or []):
        if not isinstance(item, Mapping):
            raise ModelManifestError(MALFORMED_MANIFEST,
                                     f"model component {index} must be an object")
        kind = str(item.get("type"))
        if kind not in factories:
            raise ModelManifestError(UNKNOWN_COMPONENT_KIND,
                                     f"unsupported model component type {kind!r}")
        prefix = item.get("prefix")
        if not isinstance(prefix, str) or not prefix:
            raise ModelManifestError(MALFORMED_MANIFEST,
                                     f"model component {index} has no valid prefix")
        if prefix in seen_prefixes:
            raise ModelManifestError(MALFORMED_MANIFEST,
                                     f"duplicate model component prefix {prefix!r}")
        seen_prefixes.add(prefix)
        component = factories[kind](item, index)
        if str(component.prefix) != prefix:
            raise ModelManifestError(MALFORMED_MANIFEST,
                                     f"component {kind!r} cannot use prefix {prefix!r}")
        components.append(component)
    if not components:
        raise ModelManifestError(MALFORMED_MANIFEST, "model manifest contains no components")
    composite = CompositeModel(components)
    if any(isinstance(component, QuasiElastic) for component in components) \
            and quasielastic_fwhm is None:
        raise ModelManifestError(MALFORMED_MANIFEST,
                                 "quasielastic component has no persisted width")
    return composite


def manifest_theta(composite, manifest) -> list[float]:
    """The manifest's own stored ``parameters`` mapped by name into composite order."""
    values = manifest.get("parameters") or {}
    if not isinstance(values, Mapping):
        raise ModelManifestError(MALFORMED_MANIFEST,
                                 "model manifest parameters must be an object")
    missing = [name for name in composite.param_names() if name not in values]
    if missing:
        raise ModelManifestError(MALFORMED_MANIFEST,
                                 f"model manifest is missing parameters {missing}")
    return [_finite_float(values[name], f"model parameter {name}",
                          reason_code=MALFORMED_MANIFEST)
            for name in composite.param_names()]


def compose_manifest(manifest, quasielastic_fwhm=None):
    """``(composite, theta)`` from a manifest that carries its own parameter values."""
    composite = build_composite(manifest, quasielastic_fwhm)
    return composite, manifest_theta(composite, manifest)


def draw_theta(composite, parameter_names, draw) -> list[float]:
    """Map one named parameter draw into ``composite.param_names()`` order.

    The mapping is total and exact in both directions: duplicate, missing or extra names are a
    refusal, never a reordering that happens to have the right length.
    """
    if isinstance(parameter_names, (str, bytes)) or not isinstance(parameter_names, Sequence):
        raise ModelManifestError(MALFORMED_DRAW_SHAPE, "parameter names must be a sequence")
    # Names are compared, never coerced: str(1) == "1" would let a malformed payload masquerade as
    # a parameter name instead of being refused.
    if any(not isinstance(name, str) for name in parameter_names):
        raise ModelManifestError(MALFORMED_DRAW_SHAPE, "parameter names must be strings")
    names = list(parameter_names)
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ModelManifestError(MALFORMED_DRAW_SHAPE,
                                 f"duplicate parameter names {duplicates}")
    if isinstance(draw, Mapping) or isinstance(draw, (str, bytes)):
        raise ModelManifestError(MALFORMED_DRAW_SHAPE, "parameter draw must be a sequence")
    try:
        values = np.asarray(draw, dtype=object)
    except ValueError as exc:
        raise ModelManifestError(MALFORMED_DRAW_SHAPE, "parameter draw must be a flat "
                                 "sequence of numbers") from exc
    if values.ndim != 1:
        raise ModelManifestError(MALFORMED_DRAW_SHAPE,
                                 "parameter draw must be a flat sequence of numbers")
    if values.size != len(names):
        raise ModelManifestError(
            MALFORMED_DRAW_SHAPE,
            f"parameter draw has {int(values.size)} values for {len(names)} names")
    required = composite.param_names()
    supplied = dict(zip(names, values.tolist()))
    missing = [name for name in required if name not in supplied]
    extra = [name for name in names if name not in set(required)]
    if missing or extra:
        raise ModelManifestError(
            MALFORMED_DRAW_SHAPE,
            f"parameter draw does not match the model parameters (missing {missing}, "
            f"extra {extra})")
    return [_finite_float(supplied[name], f"parameter draw value {name}") for name in required]


class EvaluationContext:
    """The frozen fit context a component needs, plus the exposure applied to the model mean.

    Duck-typed against :class:`isar.analyze.context.ScanContext`: components read ``center``,
    ``gauss_fwhm``, ``lorentz_fwhm``, ``T`` and ``quasielastic_fwhm``, and
    :meth:`~isar.analyze.components.CompositeModel.evaluate` multiplies by ``exposure_for(x)``.
    """

    __slots__ = ("center", "gauss_fwhm", "lorentz_fwhm", "T", "quasielastic_fwhm", "_exposure")

    def __init__(self, *, center, gauss_fwhm, lorentz_fwhm, T, quasielastic_fwhm=None,
                 exposure_ratio=None):
        self.center = _finite_float(center, "evaluation context center_meV",
                                    reason_code=MALFORMED_MANIFEST)
        self.gauss_fwhm = _finite_float(gauss_fwhm, "evaluation context gaussian_fwhm_meV",
                                        reason_code=MALFORMED_MANIFEST)
        self.lorentz_fwhm = _finite_float(lorentz_fwhm, "evaluation context lorentzian_fwhm_meV",
                                          reason_code=MALFORMED_MANIFEST)
        self.T = _finite_float(T, "evaluation context temperature_K",
                               reason_code=MALFORMED_MANIFEST)
        # Strictly positive, not merely finite: the Bose factor divides by T, so T = 0 (or -0.0)
        # escapes this evaluator as a ZeroDivisionError, and T < 0 evaluates to a physically
        # meaningless anti-Stokes amplitude that would be published as an available prediction.
        if not self.T > 0.0:
            raise ModelManifestError(
                MALFORMED_MANIFEST,
                "evaluation context temperature_K must be positive")
        # Resolution widths are physical bounds, not merely finite numbers (ADR-020). A zero or
        # negative Gaussian FWHM makes the resolution kernel degenerate/undefined, and a negative
        # Lorentzian FWHM is a nonsensical width that still evaluates to a plausible-looking curve.
        if not self.gauss_fwhm > 0.0:
            raise ModelManifestError(
                MALFORMED_MANIFEST, "evaluation context gaussian_fwhm_meV must be positive")
        if self.lorentz_fwhm < 0.0:
            raise ModelManifestError(
                MALFORMED_MANIFEST, "evaluation context lorentzian_fwhm_meV must be non-negative")
        self.quasielastic_fwhm = (None if quasielastic_fwhm is None else _finite_float(
            quasielastic_fwhm, "evaluation context quasielastic_fwhm_meV",
            reason_code=MALFORMED_MANIFEST))
        if self.quasielastic_fwhm is not None and not self.quasielastic_fwhm > 0.0:
            raise ModelManifestError(
                MALFORMED_MANIFEST,
                "evaluation context quasielastic_fwhm_meV must be positive when present")
        self._exposure = exposure_ratio

    def exposure_for(self, x):
        """The exposure ratio aligned to ``x`` (``None`` keeps the unscaled model mean)."""
        if self._exposure is None:
            return None
        target = np.asarray(x, dtype=float)
        ratio = np.asarray(self._exposure, dtype=float)
        if ratio.ndim == 0:
            ratio = np.full(target.shape, float(ratio))
        elif ratio.shape != target.shape:
            raise ModelManifestError(
                INVALID_REQUESTED_EXPOSURE,
                f"exposure ratio shape {ratio.shape} does not match the energy grid "
                f"{target.shape}")
        if not np.all(np.isfinite(ratio)) or np.any(ratio < 0.0):
            raise ModelManifestError(INVALID_REQUESTED_EXPOSURE,
                                     "exposure ratio must be finite and non-negative")
        return ratio


def energy_grid(energy_meV) -> np.ndarray:
    """Validate and return the ordered evaluation grid."""
    try:
        grid = np.asarray(energy_meV, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ModelManifestError(INVALID_ENERGY_GRID, "energy grid must be numeric") from exc
    if grid.ndim != 1 or grid.size == 0:
        raise ModelManifestError(INVALID_ENERGY_GRID,
                                 "energy grid must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(grid)):
        raise ModelManifestError(INVALID_ENERGY_GRID, "energy grid must be finite")
    return grid


def expected_counts(manifest, parameter_names, draw, context, energy_meV,
                    exposure_ratio=None) -> np.ndarray:
    """Expected counts for one named draw of one manifest on one grid at one exposure.

    ``context`` is an :class:`EvaluationContext` (or any object with the same surface);
    ``exposure_ratio`` is the dimensionless multiplier requested-exposure / fit-reference-exposure,
    scalar or aligned to ``energy_meV``.

    A value below ``-NEGATIVE_PREDICTION_EPS`` **refuses** with ``negative_prediction``: an expected
    count is a Poisson mean, so a substantially negative one is a broken model, and clamping it to
    zero published a plausible prediction from an impossible one. Only floating-point noise around a
    genuinely zero mean is clipped.
    """
    grid = energy_grid(energy_meV)
    composite = build_composite(manifest, getattr(context, "quasielastic_fwhm", None))
    theta = draw_theta(composite, parameter_names, draw)
    if exposure_ratio is not None:
        context = EvaluationContext(
            center=context.center, gauss_fwhm=context.gauss_fwhm,
            lorentz_fwhm=context.lorentz_fwhm, T=context.T,
            quasielastic_fwhm=getattr(context, "quasielastic_fwhm", None),
            exposure_ratio=exposure_ratio)
    values = np.asarray(composite.evaluate(grid, theta, context), dtype=float)
    if values.shape != grid.shape:
        raise ModelManifestError(NONFINITE_PREDICTION,
                                 "model evaluation did not return one value per energy")
    if not np.all(np.isfinite(values)):
        raise ModelManifestError(NONFINITE_PREDICTION,
                                 "model evaluation produced a non-finite expected count")
    if np.any(values < -NEGATIVE_PREDICTION_EPS):
        raise ModelManifestError(
            NEGATIVE_PREDICTION,
            f"model evaluation produced an expected count of {float(values.min())!r}, below the "
            f"negative-prediction tolerance {NEGATIVE_PREDICTION_EPS!r}")
    return np.maximum(values, 0.0)

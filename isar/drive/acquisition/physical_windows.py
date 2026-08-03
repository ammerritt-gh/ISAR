"""The physical target-window generator: candidate *geometry* for one selected scientific action.

ADR-016 seam.  :mod:`isar.drive.acquisition.scan_design` is provider-neutral — it owns the audit
value types, the count request, dedup, refusal packaging, and selection.  This module owns the one
thing that is unavoidably *physical*: which energy windows are worth proposing given a fitted
prediction, a resolution, and an authorized domain.  A future model-agnostic discovery provider adds
a sibling module here and declares its own generator id/version, without touching the inventory,
realization, validation, or submission contracts.

Everything below returns plain geometry — a :class:`PlannedWindow` or a :class:`WindowRefusal`.
Pricing a window (exposure, budget, instrument limits) and turning it into an auditable candidate is
the neutral layer's job, which is also what keeps the import direction one-way.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from isar.drive.acquisition.inventory import canonical_json, json_number
from isar.drive.acquisition.scan_grid import (
    COARSEN_FROM_DESIGNED_SPAN,
    MAX_POINTS,
    UniformGrid,
    resolve_uniform_grid,
)

if TYPE_CHECKING:                                  # pragma: no cover - typing only
    from isar.drive.acquisition.scan_design import ScanDesignContext


LOW_MARGIN_MEV = 2.0
HIGH_RES_FACTOR = 1.5
STEP_RES_FRACTION = 0.25
DEFAULT_FWHM_MEV = 0.5
SURVEY_SIGMA_K = 3.0
SURVEY_E_HI_MEV = 7.0

# ``generator_id`` and ``candidate_family`` are versioned *strings*, never closed enums: an
# unfamiliar family from a later generator must be dispatched through its declared generator
# version, not rejected at the schema layer.
PHYSICAL_WINDOW_GENERATOR_ID = "physical_window_generator"
PHYSICAL_WINDOW_GENERATOR_VERSION = 2

FAMILY_LEGACY_UNIFORM = "legacy_uniform/1"
FAMILY_TARGET_CORE = "target_core/1"
FAMILY_TARGET_PAIRED = "target_paired/1"
FAMILY_TARGET_LOW_ANCHOR = "target_low_anchor/1"
FAMILY_TARGET_HIGH_ANCHOR = "target_high_anchor/1"
FAMILY_TARGET_PLUS_RIVAL = "target_plus_rival/1"
FAMILY_BROAD_RECOVERY = "broad_recovery/1"

#: Order is part of the contract: it fixes dedup precedence (first construction keeps its identity,
#: later collisions become aliases) and therefore the persisted candidate identities.
SHADOW_CANDIDATE_FAMILIES = (
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    FAMILY_TARGET_PAIRED,
    FAMILY_TARGET_LOW_ANCHOR,
    FAMILY_TARGET_HIGH_ANCHOR,
    FAMILY_TARGET_PLUS_RIVAL,
    FAMILY_BROAD_RECOVERY,
)

#: Families constructed *about the target prediction*.  Each of them promises to measure the target,
#: so each must still hold the predicted centre — and enough sampled support around it — after the
#: authorized-domain clip.  ``broad_recovery/1`` is deliberately excluded: its whole point is to look
#: where the target is predicted *not* to be, and gating it on target coverage would delete it.
TARGET_WINDOW_FAMILIES = frozenset({
    FAMILY_TARGET_CORE,
    FAMILY_TARGET_PAIRED,
    FAMILY_TARGET_LOW_ANCHOR,
    FAMILY_TARGET_HIGH_ANCHOR,
    FAMILY_TARGET_PLUS_RIVAL,
})

#: Relative slack when asking whether a clipped window still holds the *whole* intended core.  The
#: clip is exact arithmetic on the same expressions, so this only absorbs representation noise.
_CORE_CONTAINMENT_TOL = 1e-9

_DetailValue = bool | int | float | str | None


def _detail_items(values: dict[str, _DetailValue]) -> tuple[tuple[str, _DetailValue], ...]:
    return tuple(sorted(values.items()))


@dataclass(frozen=True)
class PhysicalWindowGeneratorConfig:
    """Frozen construction constants of the physical target-window generator.

    This dataclass is the **only** source of candidate-construction constants, and it is versioned
    separately from the generator that consumes it: a later release may source the same values from
    policy without migrating the audit schema.  Every inventory carries a serialized copy, so a
    persisted candidate can always be reconstructed from its record alone.

    Numeric *precision* is deliberately absent: how many decimals a grid identity carries is a fact
    about the instrument that executes it, owned by the grid adapter
    (:mod:`isar.drive.acquisition.grid_adapters`), not a construction constant of this generator.
    """

    config_schema: str = "isar.physical_window_generator_config/2"
    config_version: int = 2
    # --- shared window arithmetic -------------------------------------------------------------
    step_resolution_fraction: float = STEP_RES_FRACTION
    core_fwhm_multiple: float = 2.0
    core_sigma_multiple: float = 3.0
    anchor_extension_fwhm_multiple: float = 3.0
    rival_band_sigma_multiple: float = 2.0
    target_support_sigma_multiple: float = 2.0
    # --- broad-recovery constants (tagged distinctly: this family is a survey-style sweep, not a
    # tuned target window, and its constants must never be read as target geometry).
    #
    # ``broad_recovery/1`` is NOT the misspecification escape hatch, and calling it one was wrong:
    # a closed-world scorer that believes the wrong model rejects a broad window precisely when a
    # broad window is what would have found the truth, so no candidate in this family can rescue it.
    # The escape hatch is the ADR-012 predictive-trust gate, which routes cold start, surprise, edge
    # pressure, and missed-window recovery to legacy/broad acquisition *before* any EIG is computed.
    # ------------------------------------------------------------------------------------------
    broad_recovery_default_lo_meV: float = -2.0
    broad_recovery_default_hi_meV: float = SURVEY_E_HI_MEV

    def __post_init__(self) -> None:
        if (self.config_schema != "isar.physical_window_generator_config/2"
                or not isinstance(self.config_version, int)
                or isinstance(self.config_version, bool)
                or self.config_version != 2):
            raise ValueError("physical-window generator configuration contract is unsupported")
        positives = {
            "step_resolution_fraction": self.step_resolution_fraction,
            "core_fwhm_multiple": self.core_fwhm_multiple,
            "core_sigma_multiple": self.core_sigma_multiple,
            "anchor_extension_fwhm_multiple": self.anchor_extension_fwhm_multiple,
            "rival_band_sigma_multiple": self.rival_band_sigma_multiple,
            "target_support_sigma_multiple": self.target_support_sigma_multiple,
        }
        for name, value in positives.items():
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(float(value)) or float(value) <= 0.0):
                raise ValueError(f"{name} must be finite and positive")
        if (not isinstance(self.broad_recovery_default_lo_meV, (int, float))
                or isinstance(self.broad_recovery_default_lo_meV, bool)
                or not isinstance(self.broad_recovery_default_hi_meV, (int, float))
                or isinstance(self.broad_recovery_default_hi_meV, bool)
                or not math.isfinite(float(self.broad_recovery_default_lo_meV))
                or not math.isfinite(float(self.broad_recovery_default_hi_meV))
                or self.broad_recovery_default_hi_meV
                <= self.broad_recovery_default_lo_meV):
            raise ValueError("broad-recovery defaults must be [low, high] with low < high")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PhysicalWindowGeneratorConfig":
        """Strictly reconstruct one persisted version-2 generator configuration."""
        if not isinstance(value, Mapping):
            raise TypeError("physical-window generator configuration must be a mapping")
        expected = {
            "config_schema",
            "config_version",
            "step_resolution_fraction",
            "core_fwhm_multiple",
            "core_sigma_multiple",
            "anchor_extension_fwhm_multiple",
            "rival_band_sigma_multiple",
            "target_support_sigma_multiple",
            "broad_recovery",
        }
        if set(value) != expected:
            raise ValueError("physical-window generator configuration fields are invalid")
        broad = value.get("broad_recovery")
        if (not isinstance(broad, Mapping)
                or set(broad) != {"default_lo_meV", "default_hi_meV"}):
            raise ValueError("physical-window broad-recovery configuration is invalid")
        return cls(
            config_schema=value["config_schema"],
            config_version=value["config_version"],
            step_resolution_fraction=value["step_resolution_fraction"],
            core_fwhm_multiple=value["core_fwhm_multiple"],
            core_sigma_multiple=value["core_sigma_multiple"],
            anchor_extension_fwhm_multiple=value["anchor_extension_fwhm_multiple"],
            rival_band_sigma_multiple=value["rival_band_sigma_multiple"],
            target_support_sigma_multiple=value["target_support_sigma_multiple"],
            broad_recovery_default_lo_meV=broad["default_lo_meV"],
            broad_recovery_default_hi_meV=broad["default_hi_meV"],
        )

    def to_dict(self) -> dict:
        return {
            "config_schema": self.config_schema,
            "config_version": int(self.config_version),
            "step_resolution_fraction": json_number(self.step_resolution_fraction),
            "core_fwhm_multiple": json_number(self.core_fwhm_multiple),
            "core_sigma_multiple": json_number(self.core_sigma_multiple),
            "anchor_extension_fwhm_multiple": json_number(
                self.anchor_extension_fwhm_multiple),
            "rival_band_sigma_multiple": json_number(self.rival_band_sigma_multiple),
            "target_support_sigma_multiple": json_number(
                self.target_support_sigma_multiple),
            "broad_recovery": {
                "default_lo_meV": json_number(self.broad_recovery_default_lo_meV),
                "default_hi_meV": json_number(self.broad_recovery_default_hi_meV),
            },
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


DEFAULT_WINDOW_GENERATOR_CONFIG = PhysicalWindowGeneratorConfig()


@dataclass(frozen=True)
class PlannedWindow:
    """One family's resolved executable window, before it is priced and packaged."""

    candidate_family: str
    grid: UniformGrid


@dataclass(frozen=True)
class WindowRefusal:
    """Why one family cannot exist for this context — a structured non-result, never an omission."""

    candidate_family: str
    reason_code: str
    detail: tuple[tuple[str, _DetailValue], ...] = ()


# --- legacy window arithmetic (the control the campaign actually executes) ------------------------
#
# The window *geometry* below — every bound, and the step including its cap-coarsened form — is
# frozen: it is rendered verbatim into the submitted ``deltaE`` command, so changing it would change
# the scan ISAR has always executed.  The **point count** is not part of that request; it is the
# instrument's answer to it, and it comes from the one adapter seam
# (:func:`~isar.drive.acquisition.scan_grid.resolve_uniform_grid`) exactly as every other family's
# does.  Deriving it here a second time from ``floor(span/step + 0.5) + 1`` on the *designed* span
# was ADR-018's own counterexample: the adapter counts over the **wire** span, so a rendering that
# moved a bound made ISAR record (and price, and score) one more point than TAVI would visit —
# ``deltaE -5.44493 7 0.162679`` was recorded at 78 points and executed at 77, the extra one naming
# an energy at 7.081 meV the instrument never reaches.  The old cap branch compounded it by
# *clamping* the count to the cap rather than re-asking.

def _frozen_window_count(e_lo: float, e_hi: float, e_step: float,
                         point_cap: int) -> tuple[float, float, float, int]:
    """``(e_lo, e_hi, e_step, n_points)`` for a frozen legacy window, counted by the adapter.

    Coarsening at the cap keeps the historical designed-span rule
    (:data:`~isar.drive.acquisition.scan_grid.COARSEN_FROM_DESIGNED_SPAN`) so the emitted command is
    byte-identical; only the count changes, and only to the instrument's own answer.
    """
    grid = resolve_uniform_grid(e_lo, e_hi, e_step, max_points=point_cap,
                                coarsen_span=COARSEN_FROM_DESIGNED_SPAN)
    return grid.e_lo, grid.e_hi, grid.e_step, grid.n_points


def _energy_window(e_pred: float, fwhm: float, e_sigma: float = 0.0, *,
                   survey: bool = False, energy_search_meV=None, target_band=None,
                   rival_band=None, widen: float = 1.0, positive_only: bool = False,
                   max_points: int = MAX_POINTS) -> tuple[float, float, float, int]:
    """Legacy target-window arithmetic, moved intact behind the scan-design seam."""
    e = max(float(e_pred), 0.0)
    fwhm = max(float(fwhm), 1e-3)
    e_lo = -(e + LOW_MARGIN_MEV)
    e_hi = e + HIGH_RES_FACTOR * fwhm
    sig = float(e_sigma)
    search_lo, search_hi = ((float(energy_search_meV[0]), float(energy_search_meV[1]))
                            if energy_search_meV is not None else (-2.0, SURVEY_E_HI_MEV))
    if survey:
        e_lo = min(e_lo, search_lo)
        e_hi = max(e_hi, search_hi)
    elif math.isfinite(sig) and sig > fwhm:
        e_hi = max(e_hi, min(e + SURVEY_SIGMA_K * sig, search_hi))
    if target_band is not None:
        target_mu, target_sigma = map(float, target_band)
        e_lo = min(e_lo, target_mu - 2.0 * target_sigma)
        e_hi = max(e_hi, target_mu + 2.0 * target_sigma)
    if rival_band is not None:
        rival_mu, rival_sigma = map(float, rival_band)
        e_lo = min(e_lo, rival_mu - 2.0 * rival_sigma)
        e_hi = max(e_hi, rival_mu + 2.0 * rival_sigma)
    if widen > 1.0:
        e_hi = e + (e_hi - e) * float(widen)
        if widen >= 2.5:
            e_hi = search_hi
    if target_band is not None or rival_band is not None or widen > 1.0 or positive_only:
        e_lo, e_hi = max(e_lo, search_lo), min(e_hi, search_hi)
    if positive_only:
        e_lo = max(e_lo, 0.5 * fwhm)
    if e_hi <= e_lo:
        e_hi = e_lo + fwhm
    return _frozen_window_count(e_lo, e_hi, STEP_RES_FRACTION * fwhm, max_points)


def _declared_window(e_lo: float, e_hi: float, fwhm: float,
                     max_points: int = MAX_POINTS) -> tuple[float, float, float, int]:
    """Legacy generator-declared window realization; geometry intact, count from the adapter."""
    e_lo, e_hi = float(e_lo), float(e_hi)
    fwhm = max(float(fwhm), 1e-3)
    if e_hi <= e_lo:
        e_hi = e_lo + fwhm
    return _frozen_window_count(e_lo, e_hi, STEP_RES_FRACTION * fwhm, max_points)


def uses_declared_rival_window(context: "ScanDesignContext") -> bool:
    """Whether this action's window was declared by the rival-probe generator, not derived."""
    return (context.window_family == "rival"
            and context.declared_energy_lo is not None
            and context.declared_energy_hi is not None)


def legacy_window(context: "ScanDesignContext") -> tuple[float, float, float, int]:
    """The exact historical uniform window for ``context`` — the control, always generated.

    Constructed only from :func:`_energy_window` / :func:`_declared_window`, unchanged — including
    the historical point cap ``min(policy_max_points, runtime_max_points)`` — so for any given
    context the executed request is exactly what the legacy-only designer emitted.  It is
    deliberately **not** re-clipped to the authorized domain or gated on target support the way the
    alternative families are: those invariants exist to keep a *newly invented* window honest, and
    applying them here would change the scan ISAR has always submitted.
    """
    point_cap = min(int(context.policy_max_points), int(context.runtime_max_points))
    if uses_declared_rival_window(context):
        return _declared_window(
            context.declared_energy_lo,
            context.declared_energy_hi,
            context.resolution_fwhm_meV,
            max_points=point_cap,
        )
    return _energy_window(
        context.design_prediction_mean_meV,
        context.resolution_fwhm_meV,
        context.design_prediction_sigma_meV,
        survey=context.survey,
        energy_search_meV=context.energy_search_meV,
        target_band=context.target_band,
        rival_band=context.rival_band,
        widen=context.widen_factor,
        positive_only=context.positive_only,
        max_points=point_cap,
    )


# --- alternative families -------------------------------------------------------------------------

def authorized_domain(context: "ScanDesignContext",
                      config: PhysicalWindowGeneratorConfig) -> tuple[float, float]:
    """The frozen energy domain every generated candidate must stay inside.

    For an action whose window was declared by the rival-probe generator, the declared bounds *are*
    the authorization; otherwise it is the campaign's target search band.
    """
    if uses_declared_rival_window(context):
        return float(context.declared_energy_lo), float(context.declared_energy_hi)
    if context.energy_search_meV is not None:
        return float(context.energy_search_meV[0]), float(context.energy_search_meV[1])
    return (float(config.broad_recovery_default_lo_meV),
            float(config.broad_recovery_default_hi_meV))


def _target_core(context: "ScanDesignContext",
                 config: PhysicalWindowGeneratorConfig) -> tuple[float, float]:
    """``(e_pred, half_core)`` — the target core every target family is built around.

    Derived from the prediction and the resolution alone, so it exists even when the action declares
    no ``target_band`` — which is exactly when the support gate would otherwise have nothing to
    measure support against.
    """
    e_pred = max(float(context.design_prediction_mean_meV), 0.0)
    fwhm = max(float(context.resolution_fwhm_meV), 1e-3)
    sigma = float(context.design_prediction_sigma_meV)
    if not math.isfinite(sigma) or sigma < 0.0:
        sigma = 0.0
    return e_pred, max(config.core_fwhm_multiple * fwhm, config.core_sigma_multiple * sigma)


def _support_band(family: str, context: "ScanDesignContext",
                  config: PhysicalWindowGeneratorConfig) -> tuple[
                      tuple[float, float] | None, str | None]:
    """The band whose grid coverage counts as target support, and where that band came from.

    A declared ``target_band`` is authoritative.  Without one, a *target* family still has a target —
    the prediction it was constructed around — so it is held to a resolution-relative fallback band
    spanning exactly the intended core.  Before this fallback existed the support gate silently did
    not run for undeclared-band actions (rival probes especially), which is how a window clipped down
    to a 0.1 meV sliver of its core could be presented as a target candidate.

    Non-target families (``broad_recovery/1``) get no fallback: they make no promise about target
    coverage, and gating them on it would delete the one family that looks elsewhere.
    """
    if context.target_band is not None:
        return (float(context.target_band[0]), float(context.target_band[1])), "declared"
    if family in TARGET_WINDOW_FAMILIES:
        center, halfwidth = _target_core(context, config)
        sigma = float(halfwidth) / float(config.target_support_sigma_multiple)
        return (center, sigma), "resolution_fallback"
    return None, None


def _anti_stokes_admissible(context: "ScanDesignContext",
                            domain: tuple[float, float]) -> tuple[bool, str | None]:
    """Whether a paired Stokes/anti-Stokes window may be generated for this action.

    Reuses the gates the legacy designer already honours — ``positive_only`` (the detailed-balance /
    optic-survey restriction that forbids the energy-gain side) and the authorized domain, which must
    actually reach negative energy transfer.
    """
    if context.positive_only:
        return False, "anti_stokes_not_admissible"
    if float(context.design_prediction_mean_meV) <= 0.0:
        return False, "no_stokes_peak_to_pair"
    if float(domain[0]) >= 0.0:
        return False, "anti_stokes_outside_authorized_domain"
    return True, None


def _family_window(family: str, context: "ScanDesignContext",
                   config: PhysicalWindowGeneratorConfig,
                   domain: tuple[float, float]) -> tuple[
                       tuple[float, float] | None, str | None]:
    """The unclipped ``(e_lo, e_hi)`` for one family, or ``(None, reason_code)`` when inapplicable."""
    e_pred, half_core = _target_core(context, config)
    fwhm = max(float(context.resolution_fwhm_meV), 1e-3)
    extension = config.anchor_extension_fwhm_multiple * fwhm

    if family == FAMILY_TARGET_CORE:
        return (e_pred - half_core, e_pred + half_core), None
    if family == FAMILY_TARGET_PAIRED:
        admissible, reason = _anti_stokes_admissible(context, domain)
        if not admissible:
            return None, reason
        return (-(e_pred + half_core), e_pred + half_core), None
    if family == FAMILY_TARGET_LOW_ANCHOR:
        return (e_pred - half_core - extension, e_pred + half_core), None
    if family == FAMILY_TARGET_HIGH_ANCHOR:
        return (e_pred - half_core, e_pred + half_core + extension), None
    if family == FAMILY_TARGET_PLUS_RIVAL:
        if context.rival_band is None:
            return None, "no_rival_band_declared"
        rival_mu, rival_sigma = (float(context.rival_band[0]), float(context.rival_band[1]))
        if not (math.isfinite(rival_mu) and math.isfinite(rival_sigma)) or rival_sigma < 0.0:
            return None, "invalid_rival_band"
        rival_lo = rival_mu - config.rival_band_sigma_multiple * rival_sigma
        rival_hi = rival_mu + config.rival_band_sigma_multiple * rival_sigma
        # Contiguous union: a single uniform scan cannot express a gap.  The core is inside this
        # *unclipped* union by construction, but that is not enough on its own — the authorized
        # domain can still cut the core away and leave a rival-only window, which the post-clip
        # target gates below refuse.
        return (min(e_pred - half_core, rival_lo), max(e_pred + half_core, rival_hi)), None
    if family == FAMILY_BROAD_RECOVERY:
        if context.energy_search_meV is not None:
            return (float(context.energy_search_meV[0]),
                    float(context.energy_search_meV[1])), None
        return (float(config.broad_recovery_default_lo_meV),
                float(config.broad_recovery_default_hi_meV)), None
    raise ValueError(f"unknown scan-design candidate family {family!r}")


def _target_gates(family: str, grid: UniformGrid, context: "ScanDesignContext",
                  config: PhysicalWindowGeneratorConfig,
                  domain: tuple[float, float]) -> WindowRefusal | None:
    """Post-clip invariants every *target* family must still satisfy, or the refusal that names one.

    Three distinct promises, refused with three distinct codes:

    1. **The predicted centre is measured.**  A window whose clipped bounds no longer contain
       ``e_pred`` is not a scan of the target, whatever fraction of the core it retained.
    2. **``target_core/1`` keeps its whole core.**  That family *is* the core; a truncated core is a
       different, smaller window that was never proposed or priced as such.  Anchors and
       ``target_plus_rival/1`` are allowed to lose core margin — losing it is what the domain clip is
       for — provided the centre and the support below survive.
    3. **Enough sampled support around the target** (checked by the caller, against the declared or
       fallback band).

    The previous gate refused only *complete* disjointness, so a core of ``[3.4, 6.6]`` clipped to
    ``[6.5, 7.0]`` survived on a 0.1 meV sliver.
    """
    if family not in TARGET_WINDOW_FAMILIES:
        return None
    center, halfwidth = _target_core(context, config)
    core_lo, core_hi = center - halfwidth, center + halfwidth
    shared = {
        "e_lo": grid.e_lo, "e_hi": grid.e_hi, "e_step": grid.e_step,
        "n_points": grid.n_points,
        "target_center": center,
        "target_core_lo": core_lo, "target_core_hi": core_hi,
        "domain_lo": float(domain[0]), "domain_hi": float(domain[1]),
    }
    if not (grid.e_lo <= center <= grid.e_hi):
        return WindowRefusal(
            candidate_family=family,
            reason_code=("rival_only_after_domain_clip"
                         if family == FAMILY_TARGET_PLUS_RIVAL
                         else "target_core_lost_after_domain_clip"),
            detail=_detail_items(shared))
    if family == FAMILY_TARGET_CORE:
        slack = _CORE_CONTAINMENT_TOL * max(abs(core_lo), abs(core_hi), 1.0)
        if grid.e_lo > core_lo + slack or grid.e_hi < core_hi - slack:
            return WindowRefusal(
                candidate_family=family,
                reason_code="target_core_truncated_after_domain_clip",
                detail=_detail_items(shared))
    return None


def alternative_window(family: str, context: "ScanDesignContext",
                       config: PhysicalWindowGeneratorConfig,
                       domain: tuple[float, float]) -> PlannedWindow | WindowRefusal:
    """Resolve one non-legacy family into an executable window, or say why it cannot exist."""
    if uses_declared_rival_window(context):
        # A rival probe's window is an authorization, not a prediction about the target: widening or
        # re-centring it would spend an auxiliary-sink scan outside the domain it was granted.
        return WindowRefusal(
            candidate_family=family,
            reason_code="declared_rival_window_is_authoritative",
            detail=_detail_items({
                "declared_energy_lo": float(context.declared_energy_lo),
                "declared_energy_hi": float(context.declared_energy_hi),
            }))

    window, reason = _family_window(family, context, config, domain)
    if window is None:
        return WindowRefusal(candidate_family=family, reason_code=str(reason), detail=())

    e_lo = max(float(window[0]), float(domain[0]))
    e_hi = min(float(window[1]), float(domain[1]))
    if e_hi <= e_lo:
        return WindowRefusal(
            candidate_family=family,
            reason_code="empty_after_domain_clip",
            detail=_detail_items({
                "requested_e_lo": float(window[0]), "requested_e_hi": float(window[1]),
                "domain_lo": float(domain[0]), "domain_hi": float(domain[1])}))

    fwhm = max(float(context.resolution_fwhm_meV), 1e-3)
    point_cap = min(int(context.policy_max_points), int(context.runtime_max_points))
    e_step = config.step_resolution_fraction * fwhm
    # A degenerate input (an instrument reporting an infinite resolution, an unbounded search band)
    # makes the point-count arithmetic overflow or go NaN.  That is a fact about this family, so it
    # becomes a structured refusal on this family — never an exception that destroys the whole
    # inventory, including the legacy control that was already built successfully.
    if not all(math.isfinite(value) for value in (e_lo, e_hi, e_step)):
        return WindowRefusal(
            candidate_family=family,
            reason_code="nonfinite_window_arithmetic",
            detail=_detail_items({
                "e_lo": e_lo, "e_hi": e_hi, "e_step": e_step,
                "domain_lo": float(domain[0]), "domain_hi": float(domain[1])}))
    try:
        grid = resolve_uniform_grid(e_lo, e_hi, e_step, max_points=point_cap)
    except (OverflowError, ValueError) as error:
        return WindowRefusal(
            candidate_family=family,
            reason_code="nonfinite_window_arithmetic",
            detail=_detail_items({
                "e_lo": e_lo, "e_hi": e_hi, "e_step": e_step,
                "error": type(error).__name__}))

    # Order matters: a window that has abandoned its target is a different (worse) failure than one
    # that merely undersamples it, and reporting the undersampling first would hide it.
    gate_refusal = _target_gates(family, grid, context, config, domain)
    if gate_refusal is not None:
        return gate_refusal

    support_band, band_source = _support_band(family, context, config)
    minimum_support = int(context.minimum_target_support_points)
    support = grid.support_points(support_band, config.target_support_sigma_multiple)
    if support is not None and minimum_support > 0 and support < minimum_support:
        return WindowRefusal(
            candidate_family=family,
            reason_code="insufficient_target_support",
            detail=_detail_items({
                "e_lo": grid.e_lo, "e_hi": grid.e_hi, "e_step": grid.e_step,
                "n_points": grid.n_points,
                "support_band_source": band_source,
                "support_band_mean": float(support_band[0]),
                "support_band_sigma": float(support_band[1]),
                "target_support_points": support, "required": minimum_support}))

    # Last, because it is the least informative diagnosis: a family that also lost its target should
    # say so.  A window the *instrument* cannot accept is nevertheless not a candidate — TAVI refuses
    # any single-point request outright (HTTP 400 ``Only 1 scan point!``) and an inverted window
    # renders a ``deltaE`` whose bounds run backwards — so it is refused here rather than emitted and
    # rejected downstream, however well it would have scored.  It is reachable from ordinary state:
    # ``broad_recovery/1`` on a narrow authorized domain such as ``(6.9, 7.0)`` resolves to exactly
    # one point at any resolution-derived step wider than the domain.
    if grid.n_points < 2 or grid.e_hi < grid.e_lo:
        return WindowRefusal(
            candidate_family=family,
            reason_code="degenerate_window",
            detail=_detail_items({
                "e_lo": grid.e_lo, "e_hi": grid.e_hi, "e_step": grid.e_step,
                "n_points": grid.n_points, "minimum_points": 2}))

    return PlannedWindow(candidate_family=family, grid=grid)


__all__ = [
    "DEFAULT_FWHM_MEV",
    "DEFAULT_WINDOW_GENERATOR_CONFIG",
    "FAMILY_BROAD_RECOVERY",
    "FAMILY_LEGACY_UNIFORM",
    "FAMILY_TARGET_CORE",
    "FAMILY_TARGET_HIGH_ANCHOR",
    "FAMILY_TARGET_LOW_ANCHOR",
    "FAMILY_TARGET_PAIRED",
    "FAMILY_TARGET_PLUS_RIVAL",
    "HIGH_RES_FACTOR",
    "LOW_MARGIN_MEV",
    "PHYSICAL_WINDOW_GENERATOR_ID",
    "PHYSICAL_WINDOW_GENERATOR_VERSION",
    "PhysicalWindowGeneratorConfig",
    "PlannedWindow",
    "SHADOW_CANDIDATE_FAMILIES",
    "STEP_RES_FRACTION",
    "SURVEY_E_HI_MEV",
    "SURVEY_SIGMA_K",
    "TARGET_WINDOW_FAMILIES",
    "WindowRefusal",
    "alternative_window",
    "authorized_domain",
    "legacy_window",
    "uses_declared_rival_window",
]

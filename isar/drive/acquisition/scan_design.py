"""Pure scan-window/count design for one already-selected scientific action.

The action inventory decides *what scientific question to ask*.  This module decides how that one
action is realized as a uniform constant-Q scan.  Its interface accepts only frozen, serializable
values and returns a content-addressable inventory; models, GridQ objects, callbacks, and live policy
dictionaries stay on the acquisition side of the seam.

This version generates the bounded family of conventional uniform windows for the selected action and
persists all of them.  It does **not** calculate EIG, score, or rank: the legacy candidate is always
generated with the exact historical arithmetic and is always the selected/executed one.  Which
candidates are generated is governed by ``scan_window_eig_mode`` (``off`` → legacy only, ``shadow`` →
the whole family, unscored).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace

from isar.drive.acquisition.inventory import CANONICALIZATION_VERSION, canonical_json
from isar.drive.acquisition.scan_grid import (
    MAX_POINTS,
    UniformGrid,
    points_in_window,
    resolve_uniform_grid,
)
from isar.drive.supervisor.stopping import PRODUCTION_NCOUNT, QUICKLOOK_NCOUNT
from isar.schema.hypotheses import (
    MAX_NCOUNT_PER_SCAN,
    SCAN_WINDOW_EIG_AUTHORITATIVE_UNAVAILABLE,
    SCAN_WINDOW_EIG_MODES,
)


LOW_MARGIN_MEV = 2.0
HIGH_RES_FACTOR = 1.5
STEP_RES_FRACTION = 0.25
DEFAULT_FWHM_MEV = 0.5
SURVEY_SIGMA_K = 3.0
SURVEY_E_HI_MEV = 7.0
AUXILIARY_DETECTION_KINDS = frozenset({"identity_rival_probe"})

SCAN_DESIGN_INVENTORY_SCHEMA = "isar.scan_design_inventory/2"
SCAN_DESIGN_CANDIDATE_SCHEMA = "isar.scan_design_candidate/2"

# ``generator_id`` and ``candidate_family`` are versioned *strings*, never closed enums: an
# unfamiliar family from a later generator must be dispatched through its declared generator
# version, not rejected at the schema layer.
PHYSICAL_WINDOW_GENERATOR_ID = "physical_window_generator"
PHYSICAL_WINDOW_GENERATOR_VERSION = 2

# Provider identity for the generic audit envelope.  Only the *physical* predictive provider and the
# physical target-window generator exist in this release; a future discovery provider slots in beside
# them by declaring different identities, without changing this envelope.
PREDICTIVE_PROVIDER_ID = "physical_master"
PREDICTIVE_PROVIDER_SCHEMA = "isar.master_predictive/2"
OBJECTIVE_PROJECTION_SCHEMA = "isar.target_projection/1"
STEERING_REGIME = "physical_inference"

SELECTION_BASIS_LEGACY_CONTROL_OFF = "legacy_control_off"
SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED = "shadow_legacy_control_unscored"

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

_DiagnosticValue = bool | int | float | None


class BudgetInsufficientForMinimumBatch(ValueError):
    """The remaining neutron budget cannot fund one policy-floor scan."""


class InstrumentLimitBelowMinimumBatch(ValueError):
    """The instrument cannot accept one policy-floor scan."""


def _json_number(value: float | int | None) -> float | int | str | None:
    """Return a strict-JSON representation without losing non-finite provenance."""
    if value is None or isinstance(value, int):
        return value
    number = float(value)
    if math.isnan(number):
        return "nan"
    if math.isinf(number):
        return "+inf" if number > 0.0 else "-inf"
    return number


def _json_pair(value: tuple[float, float] | None) -> list[float | str] | None:
    if value is None:
        return None
    return [_json_number(value[0]), _json_number(value[1])]


def _diagnostic_items(values: dict[str, _DiagnosticValue]) -> tuple[
        tuple[str, _DiagnosticValue], ...]:
    return tuple(sorted(values.items()))


def _diagnostic_dict(values: tuple[tuple[str, _DiagnosticValue], ...]) -> dict[
        str, _DiagnosticValue]:
    return dict(values)


@dataclass(frozen=True)
class ScanDesignContext:
    """Resolved, immutable inputs to the legacy uniform scan designer."""

    scientific_action_fingerprint: str
    hkl: tuple[float, float, float]
    q: float
    kind: str
    reason: str
    window_family: str
    declared_energy_lo: float | None
    declared_energy_hi: float | None
    target_prediction_mean_meV: float
    target_prediction_sigma_meV: float
    design_prediction_mean_meV: float
    design_prediction_sigma_meV: float
    resolution_fwhm_meV: float
    survey: bool
    energy_search_meV: tuple[float, float] | None
    target_band: tuple[float, float] | None
    rival_band: tuple[float, float] | None
    widen_factor: float
    positive_only: bool
    policy_max_points: int
    runtime_max_points: int
    tolerance_meV: float
    scan_window_eig_mode: str = "off"
    minimum_target_support_points: int = 6
    quicklook_ncount: int = QUICKLOOK_NCOUNT
    production_ncount: int = PRODUCTION_NCOUNT
    identity_detection_ncount: int = QUICKLOOK_NCOUNT
    dynamic_ncount: bool = False
    tolerance_margin: float = 0.8
    ncount_growth_cap: float = 4.0
    max_ncount_per_scan: int = MAX_NCOUNT_PER_SCAN
    diagnostic_halfwidth_meV: float | None = None
    certification_halfwidth_meV: float | None = None
    effective_exposure: float | None = None
    submitted_exposure: float | None = None
    production_scans: int = 0
    last_submitted_ncount: int | None = None
    runtime_max_neutrons_per_point: int | None = None
    runtime_queue_neutron_budget: float | None = None
    budget_kind: str | None = None
    budget_amount: float | None = None
    budget_spent: float = 0.0

    def __post_init__(self) -> None:
        if len(self.hkl) != 3:
            raise ValueError("scan-design hkl must contain exactly three values")
        if self.energy_search_meV is not None and len(self.energy_search_meV) != 2:
            raise ValueError("scan-design energy_search_meV must contain two bounds")
        if self.target_band is not None and len(self.target_band) != 2:
            raise ValueError("scan-design target_band must contain mean and sigma")
        if self.rival_band is not None and len(self.rival_band) != 2:
            raise ValueError("scan-design rival_band must contain mean and sigma")
        if self.scan_window_eig_mode not in SCAN_WINDOW_EIG_MODES:
            raise ValueError(
                "scan_window_eig_mode must be one of "
                f"{', '.join(SCAN_WINDOW_EIG_MODES)}")
        if self.scan_window_eig_mode == "authoritative":
            # Defence in depth: the policy contract already refuses this at campaign setup, so a
            # context carrying it means the refusal was bypassed — never silently run as shadow.
            raise ValueError(
                f"{SCAN_WINDOW_EIG_AUTHORITATIVE_UNAVAILABLE}: scan-window EIG selection is not "
                "implemented")

    def to_dict(self) -> dict:
        """Strict-JSON view used for audit and the context fingerprint."""
        return {
            "scientific_action_fingerprint": self.scientific_action_fingerprint,
            "hkl": [_json_number(value) for value in self.hkl],
            "q": _json_number(self.q),
            "kind": self.kind,
            "reason": self.reason,
            "window_family": self.window_family,
            "declared_energy_lo": _json_number(self.declared_energy_lo),
            "declared_energy_hi": _json_number(self.declared_energy_hi),
            "target_prediction_mean_meV": _json_number(self.target_prediction_mean_meV),
            "target_prediction_sigma_meV": _json_number(self.target_prediction_sigma_meV),
            "design_prediction_mean_meV": _json_number(self.design_prediction_mean_meV),
            "design_prediction_sigma_meV": _json_number(self.design_prediction_sigma_meV),
            "resolution_fwhm_meV": _json_number(self.resolution_fwhm_meV),
            "survey": self.survey,
            "energy_search_meV": _json_pair(self.energy_search_meV),
            "target_band": _json_pair(self.target_band),
            "rival_band": _json_pair(self.rival_band),
            "widen_factor": _json_number(self.widen_factor),
            "positive_only": self.positive_only,
            "policy_max_points": self.policy_max_points,
            "runtime_max_points": self.runtime_max_points,
            "tolerance_meV": _json_number(self.tolerance_meV),
            "scan_window_eig_mode": self.scan_window_eig_mode,
            "minimum_target_support_points": int(self.minimum_target_support_points),
            "count_policy": {
                "quicklook_ncount": self.quicklook_ncount,
                "production_ncount": self.production_ncount,
                "identity_detection_ncount": self.identity_detection_ncount,
                "dynamic_ncount": self.dynamic_ncount,
                "tolerance_margin": _json_number(self.tolerance_margin),
                "ncount_growth_cap": _json_number(self.ncount_growth_cap),
                "max_ncount_per_scan": self.max_ncount_per_scan,
            },
            "count_state": {
                "diagnostic_halfwidth_meV": _json_number(self.diagnostic_halfwidth_meV),
                "certification_halfwidth_meV": _json_number(
                    self.certification_halfwidth_meV),
                "effective_exposure": _json_number(self.effective_exposure),
                "submitted_exposure": _json_number(self.submitted_exposure),
                "production_scans": self.production_scans,
                "last_submitted_ncount": self.last_submitted_ncount,
            },
            "runtime_limits": {
                "max_neutrons_per_point": self.runtime_max_neutrons_per_point,
                "queue_neutron_budget": _json_number(self.runtime_queue_neutron_budget),
            },
            "budget": {
                "kind": self.budget_kind,
                "amount": _json_number(self.budget_amount),
                "spent": _json_number(self.budget_spent),
            },
        }

    @property
    def fingerprint(self) -> str:
        payload = canonical_json(self.to_dict())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PhysicalWindowGeneratorConfig:
    """Frozen construction constants of the physical target-window generator.

    This dataclass is the **only** source of candidate-construction constants, and it is versioned
    separately from the generator that consumes it: a later release may source the same values from
    policy without migrating the audit schema.  Every inventory carries a serialized copy, so a
    persisted candidate can always be reconstructed from its record alone.
    """

    config_schema: str = "isar.physical_window_generator_config/1"
    config_version: int = 1
    # --- shared window arithmetic -------------------------------------------------------------
    step_resolution_fraction: float = STEP_RES_FRACTION
    core_fwhm_multiple: float = 2.0
    core_sigma_multiple: float = 3.0
    anchor_extension_fwhm_multiple: float = 3.0
    rival_band_sigma_multiple: float = 2.0
    target_support_sigma_multiple: float = 2.0
    grid_energy_decimals: int = 6
    # --- broad-recovery constants (tagged distinctly: this family is the misspecification escape
    # hatch, not a tuned target window, and its constants must never be read as target geometry) ---
    broad_recovery_default_lo_meV: float = -2.0
    broad_recovery_default_hi_meV: float = SURVEY_E_HI_MEV

    def __post_init__(self) -> None:
        positives = {
            "step_resolution_fraction": self.step_resolution_fraction,
            "core_fwhm_multiple": self.core_fwhm_multiple,
            "core_sigma_multiple": self.core_sigma_multiple,
            "anchor_extension_fwhm_multiple": self.anchor_extension_fwhm_multiple,
            "rival_band_sigma_multiple": self.rival_band_sigma_multiple,
            "target_support_sigma_multiple": self.target_support_sigma_multiple,
        }
        for name, value in positives.items():
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if self.broad_recovery_default_hi_meV <= self.broad_recovery_default_lo_meV:
            raise ValueError("broad-recovery defaults must be [low, high] with low < high")

    def to_dict(self) -> dict:
        return {
            "config_schema": self.config_schema,
            "config_version": int(self.config_version),
            "step_resolution_fraction": _json_number(self.step_resolution_fraction),
            "core_fwhm_multiple": _json_number(self.core_fwhm_multiple),
            "core_sigma_multiple": _json_number(self.core_sigma_multiple),
            "anchor_extension_fwhm_multiple": _json_number(
                self.anchor_extension_fwhm_multiple),
            "rival_band_sigma_multiple": _json_number(self.rival_band_sigma_multiple),
            "target_support_sigma_multiple": _json_number(
                self.target_support_sigma_multiple),
            "grid_energy_decimals": int(self.grid_energy_decimals),
            "broad_recovery": {
                "default_lo_meV": _json_number(self.broad_recovery_default_lo_meV),
                "default_hi_meV": _json_number(self.broad_recovery_default_hi_meV),
            },
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


DEFAULT_WINDOW_GENERATOR_CONFIG = PhysicalWindowGeneratorConfig()


@dataclass(frozen=True)
class ScanDesignCandidate:
    """One complete planned uniform scan before loop-level realization clamps."""

    hkl: tuple[float, float, float]
    q: float
    e_lo: float
    e_hi: float
    e_step: float
    n_points: int
    ncount: int
    kind: str
    reason: str
    nominal_cost: int
    count_diagnostics: tuple[tuple[str, _DiagnosticValue], ...]
    candidate_family: str = FAMILY_LEGACY_UNIFORM
    generator_id: str = PHYSICAL_WINDOW_GENERATOR_ID
    generator_version: int = PHYSICAL_WINDOW_GENERATOR_VERSION
    #: Families that deduplicated onto this exact grid + exposure, in generation order.  Provenance
    #: only: excluded from :attr:`candidate_id` so one physical design keeps one identity however
    #: many construction rules happen to reach it.
    alias_families: tuple[str, ...] = ()

    @property
    def grid(self) -> UniformGrid:
        return UniformGrid(e_lo=self.e_lo, e_hi=self.e_hi, e_step=self.e_step,
                           n_points=self.n_points)

    @property
    def energies(self) -> tuple[float, ...]:
        return self.grid.energies

    def _content_dict(self) -> dict:
        grid = self.grid
        return {
            "candidate_family": self.candidate_family,
            "generator_id": self.generator_id,
            "generator_version": int(self.generator_version),
            "hkl": [float(value) for value in self.hkl],
            "q": _json_number(self.q),
            "energy_window": {
                "e_lo": _json_number(self.e_lo),
                "e_hi": _json_number(self.e_hi),
                "e_step": _json_number(self.e_step),
                "n_points": self.n_points,
            },
            "scan_command": grid.scan_command(),
            "scoring_grid": [_json_number(value) for value in grid.energies],
            "grid_fingerprint": grid.fingerprint(self.ncount),
            "ncount": self.ncount,
            "kind": self.kind,
            "reason": self.reason,
            "nominal_cost": {
                "kind": "point_neutrons",
                "value": self.nominal_cost,
            },
            "count_diagnostics": {
                key: _json_number(value) if isinstance(value, float) else value
                for key, value in self.count_diagnostics
            },
        }

    def to_dict(self) -> dict:
        value = self._content_dict()
        value["alias_families"] = list(self.alias_families)
        value["candidate_id"] = self.candidate_id
        return value

    @property
    def candidate_id(self) -> str:
        payload = canonical_json({
            "candidate_schema": SCAN_DESIGN_CANDIDATE_SCHEMA,
            **self._content_dict(),
        })
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def diagnostics(self) -> dict[str, _DiagnosticValue]:
        return _diagnostic_dict(self.count_diagnostics)


@dataclass(frozen=True)
class ScanDesignRefusal:
    """A family that could not satisfy its invariants, kept as an explicit non-result.

    A family is never silently omitted: an absent candidate and an impossible candidate are
    different facts, and only the recorded reason distinguishes them after the fact.
    """

    candidate_family: str
    reason_code: str
    detail: tuple[tuple[str, _DiagnosticValue], ...] = ()

    def to_dict(self) -> dict:
        return {
            "candidate_family": self.candidate_family,
            "generator_id": PHYSICAL_WINDOW_GENERATOR_ID,
            "generator_version": PHYSICAL_WINDOW_GENERATOR_VERSION,
            "refused": True,
            "reason_code": self.reason_code,
            "detail": {
                key: _json_number(value) if isinstance(value, float) else value
                for key, value in self.detail
            },
        }


@dataclass(frozen=True)
class ScanDesignInventory:
    """The complete scan-design space for one selected scientific action."""

    context: ScanDesignContext
    candidates: tuple[ScanDesignCandidate, ...]
    selected_candidate_id: str
    selection_basis: str = SELECTION_BASIS_LEGACY_CONTROL_OFF
    inventory_version: str = SCAN_DESIGN_INVENTORY_SCHEMA
    refusals: tuple[ScanDesignRefusal, ...] = ()
    generator_config: PhysicalWindowGeneratorConfig = field(
        default_factory=lambda: DEFAULT_WINDOW_GENERATOR_CONFIG)

    def __post_init__(self) -> None:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if not self.candidates:
            raise ValueError("scan-design inventory must contain at least one candidate")
        if candidate_ids.count(self.selected_candidate_id) != 1:
            raise ValueError("selected scan-design candidate must occur exactly once")

    @property
    def selected_candidate(self) -> ScanDesignCandidate:
        return next(candidate for candidate in self.candidates
                    if candidate.candidate_id == self.selected_candidate_id)

    def to_full_dict(self) -> dict:
        return {
            "inventory_version": self.inventory_version,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "context_fingerprint": self.context.fingerprint,
            "context": self.context.to_dict(),
            "generator": {
                "id": PHYSICAL_WINDOW_GENERATOR_ID,
                "version": PHYSICAL_WINDOW_GENERATOR_VERSION,
                "config": self.generator_config.to_dict(),
                "config_fingerprint": self.generator_config.fingerprint,
            },
            # Generic audit envelope: which provider produced the predictive basis, which generator
            # produced the candidates, and which objective projection they are about.  A later
            # discovery provider declares different values here without changing this contract.
            "provider_identity": {
                "predictive_provider_id": PREDICTIVE_PROVIDER_ID,
                "predictive_provider_schema": PREDICTIVE_PROVIDER_SCHEMA,
                "generator_id": PHYSICAL_WINDOW_GENERATOR_ID,
                "generator_version": PHYSICAL_WINDOW_GENERATOR_VERSION,
                "objective_projection_schema": OBJECTIVE_PROJECTION_SCHEMA,
                "steering_regime": STEERING_REGIME,
            },
            "selection": {
                "basis": self.selection_basis,
                "selected_candidate_id": self.selected_candidate_id,
                "scan_window_eig_mode": self.context.scan_window_eig_mode,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "refused_candidates": [refusal.to_dict() for refusal in self.refusals],
        }

    def content_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_full_dict()).encode("utf-8")).hexdigest()


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
    e_step = STEP_RES_FRACTION * fwhm
    span = e_hi - e_lo
    point_cap = max(int(max_points), 2)
    n_points = int(math.floor(span / e_step + 0.5)) + 1
    if n_points > point_cap:
        e_step = span / (point_cap - 1)
        n_points = point_cap
    return e_lo, e_hi, e_step, n_points


def _declared_window(e_lo: float, e_hi: float, fwhm: float,
                     max_points: int = MAX_POINTS) -> tuple[float, float, float, int]:
    """Legacy generator-declared window realization, moved intact."""
    e_lo, e_hi = float(e_lo), float(e_hi)
    fwhm = max(float(fwhm), 1e-3)
    if e_hi <= e_lo:
        e_hi = e_lo + fwhm
    e_step = STEP_RES_FRACTION * fwhm
    span = e_hi - e_lo
    point_cap = max(int(max_points), 2)
    n_points = int(math.floor(span / e_step + 0.5)) + 1
    if n_points > point_cap:
        e_step = span / (point_cap - 1)
        n_points = point_cap
    return e_lo, e_hi, e_step, n_points


def _ceil_production_ladder(exposure: float, floor: int) -> int:
    floor = max(int(floor), 1)
    exposure = max(float(exposure), float(floor))
    if exposure <= floor:
        return floor
    return floor * (2 ** int(math.ceil(math.log2(exposure / floor))))


def _count_request(context: ScanDesignContext, n_points: int) -> tuple[
        int, tuple[tuple[str, _DiagnosticValue], ...]]:
    quicklook_floor = int(context.quicklook_ncount)
    production_floor = int(context.production_ncount)
    dynamic = bool(context.dynamic_ncount)
    diagnostics: dict[str, _DiagnosticValue] = {
        "dynamic_ncount": dynamic,
        "exposure_required": None,
        "exposure_current": None,
        "exposure_remaining": None,
    }
    if context.kind == "quicklook":
        requested = quicklook_floor
        minimum = quicklook_floor
    elif context.kind in AUXILIARY_DETECTION_KINDS:
        requested = int(context.identity_detection_ncount)
        minimum = requested
    elif not dynamic:
        requested = production_floor
        minimum = production_floor
    else:
        halfwidth = context.diagnostic_halfwidth_meV
        if halfwidth is None:
            halfwidth = context.certification_halfwidth_meV
        current = context.effective_exposure
        if current is None:
            current = context.submitted_exposure
        try:
            current = float(current)
        except (TypeError, ValueError):
            current = 0.0
        if not math.isfinite(current) or current <= 0.0:
            current = 0.0
        remaining = float(production_floor * (2 ** max(int(context.production_scans), 0)))
        required = None
        if halfwidth is not None and math.isfinite(float(halfwidth)) and current > 0.0:
            required = current * (
                float(halfwidth) / (float(context.tolerance_margin)
                                    * float(context.tolerance_meV))) ** 2
            remaining = max(required - current, 0.0)
        requested = _ceil_production_ladder(remaining, production_floor)
        previous = int(context.last_submitted_ncount or production_floor)
        requested = min(
            requested,
            int(math.floor(previous * float(context.ncount_growth_cap))),
            int(context.max_ncount_per_scan),
        )
        requested = max(requested, production_floor)
        minimum = production_floor
        diagnostics.update({
            "exposure_required": required,
            "exposure_current": current,
            "exposure_remaining": remaining,
        })

    runtime_ncount_limit = context.runtime_max_neutrons_per_point
    if context.runtime_queue_neutron_budget is not None:
        queue_cap = int(math.floor(
            float(context.runtime_queue_neutron_budget) / max(int(n_points), 1)))
        runtime_ncount_limit = (
            queue_cap if runtime_ncount_limit is None
            else min(int(runtime_ncount_limit), queue_cap)
        )
    if runtime_ncount_limit is not None:
        runtime_ncount_limit = int(runtime_ncount_limit)
        if runtime_ncount_limit < minimum:
            raise InstrumentLimitBelowMinimumBatch(
                f"instrument_limit_below_minimum_batch: {runtime_ncount_limit:g} "
                f"neutrons/point is below the {minimum:g} {context.kind} floor")
        if requested > runtime_ncount_limit:
            diagnostics["requested_before_runtime_limit"] = int(requested)
        requested = min(requested, runtime_ncount_limit)
        diagnostics["runtime_ncount_limit"] = runtime_ncount_limit
    if context.budget_kind == "neutrons" and context.budget_amount is not None:
        remaining_budget = max(
            float(context.budget_amount) - float(context.budget_spent), 0.0)
        affordable = int(math.floor(remaining_budget / max(int(n_points), 1)))
        if affordable < minimum:
            raise BudgetInsufficientForMinimumBatch(
                f"budget_insufficient_for_minimum_batch: "
                f"need {minimum * max(int(n_points), 1):g} neutrons, "
                f"have {remaining_budget:g}")
        requested = min(requested, affordable)
        diagnostics["neutron_budget_clamp"] = affordable
    return int(requested), _diagnostic_items(diagnostics)


def _uses_declared_rival_window(context: ScanDesignContext) -> bool:
    """Whether this action's window was declared by the rival-probe generator, not derived."""
    return (context.window_family == "rival"
            and context.declared_energy_lo is not None
            and context.declared_energy_hi is not None)


def _legacy_candidate(context: ScanDesignContext) -> ScanDesignCandidate:
    """The exact historical uniform scan for ``context`` — the control, always generated.

    Constructed only from :func:`_energy_window` / :func:`_declared_window` and
    :func:`_count_request`, unchanged — including the historical point cap
    ``min(policy_max_points, runtime_max_points)`` — so for any given context the executed request is
    exactly what the legacy-only designer emitted.  It is deliberately **not** re-clipped to the authorized domain or
    gated on target support the way the alternative families are: those invariants exist to keep a
    *newly invented* window honest, and applying them here would change the scan ISAR has always
    submitted.
    """
    point_cap = min(int(context.policy_max_points), int(context.runtime_max_points))
    if _uses_declared_rival_window(context):
        e_lo, e_hi, e_step, n_points = _declared_window(
            context.declared_energy_lo,
            context.declared_energy_hi,
            context.resolution_fwhm_meV,
            max_points=point_cap,
        )
    else:
        e_lo, e_hi, e_step, n_points = _energy_window(
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
    ncount, diagnostics = _count_request(context, n_points)
    return ScanDesignCandidate(
        hkl=context.hkl,
        q=context.q,
        e_lo=e_lo,
        e_hi=e_hi,
        e_step=e_step,
        n_points=n_points,
        ncount=ncount,
        kind=context.kind,
        reason=context.reason,
        nominal_cost=int(n_points) * int(ncount),
        count_diagnostics=diagnostics,
        candidate_family=FAMILY_LEGACY_UNIFORM,
    )


def _authorized_domain(context: ScanDesignContext,
                       config: PhysicalWindowGeneratorConfig) -> tuple[float, float]:
    """The frozen energy domain every generated candidate must stay inside.

    For an action whose window was declared by the rival-probe generator, the declared bounds *are*
    the authorization; otherwise it is the campaign's target search band.
    """
    if _uses_declared_rival_window(context):
        return float(context.declared_energy_lo), float(context.declared_energy_hi)
    if context.energy_search_meV is not None:
        return float(context.energy_search_meV[0]), float(context.energy_search_meV[1])
    return (float(config.broad_recovery_default_lo_meV),
            float(config.broad_recovery_default_hi_meV))


def _target_support_band(context: ScanDesignContext) -> tuple[float, float] | None:
    """The band whose grid coverage counts as target support, or ``None`` when none is declared."""
    if context.target_band is not None:
        return float(context.target_band[0]), float(context.target_band[1])
    return None


def _anti_stokes_admissible(context: ScanDesignContext,
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


def _family_window(family: str, context: ScanDesignContext,
                   config: PhysicalWindowGeneratorConfig,
                   domain: tuple[float, float]) -> tuple[
                       tuple[float, float] | None, str | None]:
    """The unclipped ``(e_lo, e_hi)`` for one family, or ``(None, reason_code)`` when inapplicable."""
    e_pred = max(float(context.design_prediction_mean_meV), 0.0)
    fwhm = max(float(context.resolution_fwhm_meV), 1e-3)
    sigma = float(context.design_prediction_sigma_meV)
    if not math.isfinite(sigma) or sigma < 0.0:
        sigma = 0.0
    half_core = max(config.core_fwhm_multiple * fwhm, config.core_sigma_multiple * sigma)
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
        # Contiguous union: a single uniform scan cannot express a gap, and the core always stays in
        # it, so a canonical target action can never be handed a rival-only window.
        return (min(e_pred - half_core, rival_lo), max(e_pred + half_core, rival_hi)), None
    if family == FAMILY_BROAD_RECOVERY:
        if context.energy_search_meV is not None:
            return (float(context.energy_search_meV[0]),
                    float(context.energy_search_meV[1])), None
        return (float(config.broad_recovery_default_lo_meV),
                float(config.broad_recovery_default_hi_meV)), None
    raise ValueError(f"unknown scan-design candidate family {family!r}")


def _alternative_candidate(family: str, context: ScanDesignContext,
                           config: PhysicalWindowGeneratorConfig,
                           domain: tuple[float, float]) -> tuple[
                               ScanDesignCandidate | None, ScanDesignRefusal | None]:
    """Build one non-legacy family, or the structured refusal explaining why it cannot exist."""
    if _uses_declared_rival_window(context):
        # A rival probe's window is an authorization, not a prediction about the target: widening or
        # re-centring it would spend an auxiliary-sink scan outside the domain it was granted.
        return None, ScanDesignRefusal(
            candidate_family=family,
            reason_code="declared_rival_window_is_authoritative",
            detail=_diagnostic_items({
                "declared_energy_lo": float(context.declared_energy_lo),
                "declared_energy_hi": float(context.declared_energy_hi),
            }))

    window, reason = _family_window(family, context, config, domain)
    if window is None:
        return None, ScanDesignRefusal(candidate_family=family,
                                       reason_code=str(reason),
                                       detail=())

    e_lo = max(float(window[0]), float(domain[0]))
    e_hi = min(float(window[1]), float(domain[1]))
    if e_hi <= e_lo:
        return None, ScanDesignRefusal(
            candidate_family=family,
            reason_code="empty_after_domain_clip",
            detail=_diagnostic_items({
                "requested_e_lo": float(window[0]), "requested_e_hi": float(window[1]),
                "domain_lo": float(domain[0]), "domain_hi": float(domain[1])}))

    fwhm = max(float(context.resolution_fwhm_meV), 1e-3)
    point_cap = min(int(context.policy_max_points), int(context.runtime_max_points))
    grid = resolve_uniform_grid(e_lo, e_hi, config.step_resolution_fraction * fwhm,
                                max_points=point_cap)

    support_band = _target_support_band(context)
    minimum_support = int(context.minimum_target_support_points)
    support = grid.support_points(support_band, config.target_support_sigma_multiple)
    if support is not None and minimum_support > 0 and support < minimum_support:
        return None, ScanDesignRefusal(
            candidate_family=family,
            reason_code="insufficient_target_support",
            detail=_diagnostic_items({
                "e_lo": grid.e_lo, "e_hi": grid.e_hi, "e_step": grid.e_step,
                "n_points": grid.n_points,
                "target_support_points": support, "required": minimum_support}))

    try:
        ncount, diagnostics = _count_request(context, grid.n_points)
    except (BudgetInsufficientForMinimumBatch, InstrumentLimitBelowMinimumBatch) as error:
        # Not swallowed: the impossibility becomes a durable record on this family.  The legacy
        # control is built first, so a limit that also blocks the executed scan has already raised.
        return None, ScanDesignRefusal(
            candidate_family=family,
            reason_code=("budget_insufficient_for_minimum_batch"
                         if isinstance(error, BudgetInsufficientForMinimumBatch)
                         else "instrument_limit_below_minimum_batch"),
            detail=_diagnostic_items({"n_points": grid.n_points}))

    candidate = ScanDesignCandidate(
        hkl=context.hkl,
        q=context.q,
        e_lo=grid.e_lo,
        e_hi=grid.e_hi,
        e_step=grid.e_step,
        n_points=grid.n_points,
        ncount=int(ncount),
        kind=context.kind,
        reason=context.reason,
        nominal_cost=int(grid.n_points) * int(ncount),
        count_diagnostics=diagnostics,
        candidate_family=family,
    )
    return candidate, None


def design_scan_windows(
        context: ScanDesignContext,
        config: PhysicalWindowGeneratorConfig = DEFAULT_WINDOW_GENERATOR_CONFIG,
) -> ScanDesignInventory:
    """Return the scan-design inventory for ``context`` under its frozen EIG mode.

    ``off`` designs the legacy window alone; ``shadow`` additionally generates and persists the
    alternative uniform-window family.  In both modes the legacy candidate is the selected one and
    the emitted request is identical — nothing here scores or ranks.
    """
    legacy = _legacy_candidate(context)
    candidates = [legacy]
    refusals: list[ScanDesignRefusal] = []

    if context.scan_window_eig_mode == "shadow":
        domain = _authorized_domain(context, config)
        by_identity: dict[tuple[tuple[float, ...], int], int] = {
            (legacy.energies, legacy.ncount): 0}
        for family in SHADOW_CANDIDATE_FAMILIES:
            if family == FAMILY_LEGACY_UNIFORM:
                continue
            candidate, refusal = _alternative_candidate(family, context, config, domain)
            if candidate is None:
                refusals.append(refusal)
                continue
            identity = (candidate.energies, candidate.ncount)
            existing = by_identity.get(identity)
            if existing is not None:
                kept = candidates[existing]
                candidates[existing] = replace(
                    kept, alias_families=kept.alias_families + (family,))
                continue
            by_identity[identity] = len(candidates)
            candidates.append(candidate)

    basis = (SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED
             if context.scan_window_eig_mode == "shadow"
             else SELECTION_BASIS_LEGACY_CONTROL_OFF)
    return ScanDesignInventory(
        context=context,
        candidates=tuple(candidates),
        selected_candidate_id=candidates[0].candidate_id,
        selection_basis=basis,
        refusals=tuple(refusals),
        generator_config=config,
    )


__all__ = [
    "PhysicalWindowGeneratorConfig",
    "ScanDesignCandidate",
    "ScanDesignContext",
    "ScanDesignInventory",
    "ScanDesignRefusal",
    "design_scan_windows",
    "points_in_window",
]

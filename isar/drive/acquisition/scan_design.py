"""Pure scan-window/count design for one already-selected scientific action.

The action inventory decides *what scientific question to ask*.  This module decides how that one
action is realized as a uniform constant-Q scan.  Its interface accepts only frozen, serializable
values and returns a content-addressable inventory; models, GridQ objects, callbacks, and live policy
dictionaries stay on the acquisition side of the seam.

This maintenance version deliberately exposes one ``legacy_uniform`` candidate.  It does not calculate
EIG, compare alternative windows, or rank designs.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from isar.drive.acquisition.inventory import CANONICALIZATION_VERSION, canonical_json
from isar.drive.supervisor.stopping import PRODUCTION_NCOUNT, QUICKLOOK_NCOUNT
from isar.schema.hypotheses import MAX_NCOUNT_PER_SCAN


LOW_MARGIN_MEV = 2.0
HIGH_RES_FACTOR = 1.5
STEP_RES_FRACTION = 0.25
MAX_POINTS = 100
DEFAULT_FWHM_MEV = 0.5
SURVEY_SIGMA_K = 3.0
SURVEY_E_HI_MEV = 7.0
AUXILIARY_DETECTION_KINDS = frozenset({"identity_rival_probe"})

SCAN_DESIGN_INVENTORY_SCHEMA = "isar.scan_design_inventory/1"
LEGACY_SCAN_DESIGN_GENERATOR = "legacy_uniform"
LEGACY_SCAN_DESIGN_GENERATOR_VERSION = 1
LEGACY_SELECTION_BASIS = "legacy_only_candidate"

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
    predicted_energy_meV: float
    predicted_sigma_meV: float
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
            "predicted_energy_meV": _json_number(self.predicted_energy_meV),
            "predicted_sigma_meV": _json_number(self.predicted_sigma_meV),
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

    def _content_dict(self) -> dict:
        return {
            "generator": LEGACY_SCAN_DESIGN_GENERATOR,
            "generator_version": LEGACY_SCAN_DESIGN_GENERATOR_VERSION,
            "hkl": [float(value) for value in self.hkl],
            "q": _json_number(self.q),
            "energy_window": {
                "e_lo": _json_number(self.e_lo),
                "e_hi": _json_number(self.e_hi),
                "e_step": _json_number(self.e_step),
                "n_points": self.n_points,
            },
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
        value["candidate_id"] = self.candidate_id
        return value

    @property
    def candidate_id(self) -> str:
        payload = canonical_json({
            "candidate_schema": "isar.scan_design_candidate/1",
            **self._content_dict(),
        })
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def diagnostics(self) -> dict[str, _DiagnosticValue]:
        return _diagnostic_dict(self.count_diagnostics)


@dataclass(frozen=True)
class ScanDesignInventory:
    """The complete scan-design space for one selected scientific action."""

    context: ScanDesignContext
    candidates: tuple[ScanDesignCandidate, ...]
    selected_candidate_id: str
    selection_basis: str = LEGACY_SELECTION_BASIS
    inventory_version: str = SCAN_DESIGN_INVENTORY_SCHEMA

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
                "id": LEGACY_SCAN_DESIGN_GENERATOR,
                "version": LEGACY_SCAN_DESIGN_GENERATOR_VERSION,
            },
            "selection": {
                "basis": self.selection_basis,
                "selected_candidate_id": self.selected_candidate_id,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    def content_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_full_dict()).encode("utf-8")).hexdigest()


def points_in_window(e_lo: float, e_hi: float, e_step: float, *,
                     max_points: int = MAX_POINTS) -> int:
    """Number of TAVI scan points, capped by the caller's frozen policy."""
    step = max(float(e_step), 1e-9)
    span = max(float(e_hi) - float(e_lo), 0.0)
    return min(int(math.floor(span / step + 0.5)) + 1, max(int(max_points), 2))


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


def design_legacy_scan(context: ScanDesignContext) -> ScanDesignInventory:
    """Return the one-candidate legacy scan-design inventory for ``context``."""
    point_cap = min(int(context.policy_max_points), int(context.runtime_max_points))
    if (context.window_family == "rival"
            and context.declared_energy_lo is not None
            and context.declared_energy_hi is not None):
        e_lo, e_hi, e_step, n_points = _declared_window(
            context.declared_energy_lo,
            context.declared_energy_hi,
            context.resolution_fwhm_meV,
            max_points=point_cap,
        )
    else:
        e_lo, e_hi, e_step, n_points = _energy_window(
            context.predicted_energy_meV,
            context.resolution_fwhm_meV,
            context.predicted_sigma_meV,
            survey=context.survey,
            energy_search_meV=context.energy_search_meV,
            target_band=context.target_band,
            rival_band=context.rival_band,
            widen=context.widen_factor,
            positive_only=context.positive_only,
            max_points=point_cap,
        )
    ncount, diagnostics = _count_request(context, n_points)
    candidate = ScanDesignCandidate(
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
    )
    return ScanDesignInventory(
        context=context,
        candidates=(candidate,),
        selected_candidate_id=candidate.candidate_id,
    )


__all__ = [
    "ScanDesignContext",
    "ScanDesignCandidate",
    "ScanDesignInventory",
    "design_legacy_scan",
]

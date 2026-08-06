"""Provider-neutral scan-design orchestration for one already-selected scientific action.

The action inventory decides *what scientific question to ask*.  This module decides how that one
action is realized as a uniform constant-Q scan.  Its interface accepts only frozen, serializable
values and returns a content-addressable inventory; models, GridQ objects, callbacks, and live policy
dictionaries stay on the acquisition side of the seam.

ADR-016 seam: what lives here is provider-neutral — the audit value types, the count request, dedup,
refusal packaging, selection, and the inventory vocabulary.  Which *windows* are worth proposing is
physical, and lives in :mod:`isar.drive.acquisition.physical_windows`; a future discovery provider
adds a sibling generator there and is packaged by exactly this code.

This version generates the bounded family of conventional uniform windows for the selected action and
persists all of them.  It does **not** calculate EIG, score, or rank: the legacy candidate is always
generated with the exact historical arithmetic and is always the selected/executed one.  Which
candidates are generated is governed by ``scan_window_eig_mode`` (``off`` → legacy only, ``shadow`` →
the whole family, unscored).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from isar.drive.acquisition.inventory import (
    CANONICALIZATION_VERSION,
    canonical_json,
    json_number,
)
from isar.drive.acquisition.physical_windows import (
    DEFAULT_FWHM_MEV,
    DEFAULT_WINDOW_GENERATOR_CONFIG,
    FAMILY_BROAD_RECOVERY,
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    FAMILY_TARGET_HIGH_ANCHOR,
    FAMILY_TARGET_LOW_ANCHOR,
    FAMILY_TARGET_PAIRED,
    FAMILY_TARGET_PLUS_RIVAL,
    HIGH_RES_FACTOR,
    LOW_MARGIN_MEV,
    PHYSICAL_WINDOW_GENERATOR_ID,
    PHYSICAL_WINDOW_GENERATOR_VERSION,
    SHADOW_CANDIDATE_FAMILIES,
    STEP_RES_FRACTION,
    SURVEY_E_HI_MEV,
    SURVEY_SIGMA_K,
    TARGET_WINDOW_FAMILIES,
    PhysicalWindowGeneratorConfig,
    PlannedWindow,
    WindowRefusal,
    _declared_window,
    _energy_window,
    alternative_window,
    authorized_domain,
    legacy_window,
)
from isar.drive.acquisition.scan_grid import (
    MAX_POINTS,
    UniformGrid,
    points_in_window,
    resolve_uniform_grid,
)
from isar.drive.supervisor.stopping import PRODUCTION_NCOUNT, QUICKLOOK_NCOUNT
from isar.predictive import (
    PREDICTIVE_ARTIFACT_SCHEMA,
    TARGET_PROJECTION_SCHEMA,
)
from isar.schema.hypotheses import (
    MAX_NCOUNT_PER_SCAN,
    SCAN_WINDOW_EIG_MODES,
)
from isar.schema.eig_shadow import (
    STATUS_COMPLETE as SHADOW_STATUS_COMPLETE,
    STATUS_PARTIAL as SHADOW_STATUS_PARTIAL,
    ScanDesignShadowScoring,
    validate_scan_design_shadow_scoring,
)


_LOG = logging.getLogger(__name__)

AUXILIARY_DETECTION_KINDS = frozenset({"identity_rival_probe"})

SCAN_DESIGN_INVENTORY_SCHEMA_V2 = "isar.scan_design_inventory/2"
SCAN_DESIGN_INVENTORY_SCHEMA = "isar.scan_design_inventory/3"
SCAN_DESIGN_CANDIDATE_SCHEMA = "isar.scan_design_candidate/2"

# Provider identity for the generic audit envelope.  Only the *physical* predictive provider and the
# physical target-window generator exist in this release; a future discovery provider slots in beside
# them by declaring different identities, without changing this envelope.
#
# Both schema strings are BOUND to their single definitions in :mod:`isar.predictive` rather than
# re-declared here.  The audit envelope names the artifacts a candidate was scored against, so a
# literal copy that drifts from the producer would make the envelope claim a basis that no longer
# exists -- which is exactly what happened when the projection moved to /2 (ADR-019).
PREDICTIVE_PROVIDER_ID = "physical_master"
PREDICTIVE_PROVIDER_SCHEMA = PREDICTIVE_ARTIFACT_SCHEMA
OBJECTIVE_PROJECTION_SCHEMA = TARGET_PROJECTION_SCHEMA
STEERING_REGIME = "physical_inference"

SELECTION_BASIS_LEGACY_CONTROL_OFF = "legacy_control_off"
SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED = "shadow_legacy_control_unscored"
SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED = "shadow_legacy_control_scored"

_FORBIDDEN_SHADOW_DECISION_FIELDS = frozenset({
    "winner", "winning_candidate", "recommended_candidate", "recommendation",
    "threshold", "selection_threshold", "eig_selected_candidate_id",
})

_DiagnosticValue = bool | int | float | None


class BudgetInsufficientForMinimumBatch(ValueError):
    """The remaining neutron budget cannot fund one policy-floor scan."""


class InstrumentLimitBelowMinimumBatch(ValueError):
    """The instrument cannot accept one policy-floor scan."""


class InstrumentPointLimitExceeded(ValueError):
    """The adapter-exact legacy request still exceeds the live instrument point ceiling."""


#: One shared non-finite-safe number rendering (:func:`isar.drive.acquisition.inventory.json_number`).
#: The grid module fingerprints through the same helper, so a degenerate window cannot be strict-JSON
#: clean in the candidate record and a raw ``ValueError`` inside the grid fingerprint.
_json_number = json_number


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


def _decoded_number(value: Any) -> Any:
    """Reverse :func:`json_number` when reconstructing a persisted audit record."""
    if value == "nan":
        return float("nan")
    if value == "+inf":
        return float("inf")
    if value == "-inf":
        return float("-inf")
    return value


def _decoded_pair(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("persisted scan-design pair must contain two values")
    return tuple(_decoded_number(item) for item in value)


def _contains_forbidden_shadow_decision(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (bool(set(value) & _FORBIDDEN_SHADOW_DECISION_FIELDS)
                or any(_contains_forbidden_shadow_decision(item)
                       for item in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_shadow_decision(item) for item in value)
    return False


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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignContext":
        if not isinstance(value, Mapping):
            raise TypeError("scan-design context must be a mapping")
        expected = {
            "scientific_action_fingerprint", "hkl", "q", "kind", "reason",
            "window_family", "declared_energy_lo", "declared_energy_hi",
            "target_prediction_mean_meV", "target_prediction_sigma_meV",
            "design_prediction_mean_meV", "design_prediction_sigma_meV",
            "resolution_fwhm_meV", "survey", "energy_search_meV", "target_band",
            "rival_band", "widen_factor", "positive_only", "policy_max_points",
            "runtime_max_points", "tolerance_meV", "scan_window_eig_mode",
            "minimum_target_support_points", "count_policy", "count_state",
            "runtime_limits", "budget",
        }
        if set(value) != expected:
            raise ValueError("scan-design context fields are invalid")
        hkl = value["hkl"]
        if not isinstance(hkl, list) or len(hkl) != 3:
            raise ValueError("scan-design hkl must contain exactly three values")
        count_policy = value["count_policy"]
        count_state = value["count_state"]
        runtime_limits = value["runtime_limits"]
        budget = value["budget"]
        if (not isinstance(count_policy, Mapping)
                or set(count_policy) != {"quicklook_ncount", "production_ncount",
                                         "identity_detection_ncount", "dynamic_ncount",
                                         "tolerance_margin", "ncount_growth_cap",
                                         "max_ncount_per_scan"}
                or not isinstance(count_state, Mapping)
                or set(count_state) != {"diagnostic_halfwidth_meV",
                                        "certification_halfwidth_meV", "effective_exposure",
                                        "submitted_exposure", "production_scans",
                                        "last_submitted_ncount"}
                or not isinstance(runtime_limits, Mapping)
                or set(runtime_limits) != {"max_neutrons_per_point", "queue_neutron_budget"}
                or not isinstance(budget, Mapping)
                or set(budget) != {"kind", "amount", "spent"}):
            raise ValueError("scan-design nested context fields are invalid")
        item = cls(
            scientific_action_fingerprint=value["scientific_action_fingerprint"],
            hkl=tuple(_decoded_number(item) for item in hkl),
            q=_decoded_number(value["q"]), kind=value["kind"], reason=value["reason"],
            window_family=value["window_family"],
            declared_energy_lo=_decoded_number(value["declared_energy_lo"]),
            declared_energy_hi=_decoded_number(value["declared_energy_hi"]),
            target_prediction_mean_meV=_decoded_number(value["target_prediction_mean_meV"]),
            target_prediction_sigma_meV=_decoded_number(value["target_prediction_sigma_meV"]),
            design_prediction_mean_meV=_decoded_number(value["design_prediction_mean_meV"]),
            design_prediction_sigma_meV=_decoded_number(value["design_prediction_sigma_meV"]),
            resolution_fwhm_meV=_decoded_number(value["resolution_fwhm_meV"]),
            survey=value["survey"], energy_search_meV=_decoded_pair(value["energy_search_meV"]),
            target_band=_decoded_pair(value["target_band"]),
            rival_band=_decoded_pair(value["rival_band"]),
            widen_factor=_decoded_number(value["widen_factor"]),
            positive_only=value["positive_only"], policy_max_points=value["policy_max_points"],
            runtime_max_points=value["runtime_max_points"],
            tolerance_meV=_decoded_number(value["tolerance_meV"]),
            scan_window_eig_mode=value["scan_window_eig_mode"],
            minimum_target_support_points=value["minimum_target_support_points"],
            quicklook_ncount=count_policy["quicklook_ncount"],
            production_ncount=count_policy["production_ncount"],
            identity_detection_ncount=count_policy["identity_detection_ncount"],
            dynamic_ncount=count_policy["dynamic_ncount"],
            tolerance_margin=_decoded_number(count_policy["tolerance_margin"]),
            ncount_growth_cap=_decoded_number(count_policy["ncount_growth_cap"]),
            max_ncount_per_scan=count_policy["max_ncount_per_scan"],
            diagnostic_halfwidth_meV=_decoded_number(count_state["diagnostic_halfwidth_meV"]),
            certification_halfwidth_meV=_decoded_number(
                count_state["certification_halfwidth_meV"]),
            effective_exposure=_decoded_number(count_state["effective_exposure"]),
            submitted_exposure=_decoded_number(count_state["submitted_exposure"]),
            production_scans=count_state["production_scans"],
            last_submitted_ncount=count_state["last_submitted_ncount"],
            runtime_max_neutrons_per_point=runtime_limits["max_neutrons_per_point"],
            runtime_queue_neutron_budget=_decoded_number(runtime_limits["queue_neutron_budget"]),
            budget_kind=budget["kind"], budget_amount=_decoded_number(budget["amount"]),
            budget_spent=_decoded_number(budget["spent"]),
        )
        if canonical_json(item.to_dict()) != canonical_json(dict(value)):
            raise ValueError("scan-design context does not reproduce from persisted content")
        return item

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
            # The energies the *instrument* executes for this request, per its adapter — not an
            # ISAR-side idealization of them.  The adapter identity travels with the grid so a
            # persisted candidate declares the execution semantics it was designed under.
            "grid_adapter": grid.adapter.identity(),
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignCandidate":
        if not isinstance(value, Mapping):
            raise TypeError("scan-design candidate must be a mapping")
        expected = {
            "candidate_family", "generator_id", "generator_version", "hkl", "q",
            "energy_window", "scan_command", "grid_adapter", "scoring_grid",
            "grid_fingerprint", "ncount", "kind", "reason", "nominal_cost",
            "count_diagnostics", "alias_families", "candidate_id",
        }
        if set(value) != expected:
            raise ValueError("scan-design candidate fields are invalid")
        window = value["energy_window"]
        nominal_cost = value["nominal_cost"]
        diagnostics = value["count_diagnostics"]
        hkl = value["hkl"]
        aliases = value["alias_families"]
        if (not isinstance(window, Mapping)
                or set(window) != {"e_lo", "e_hi", "e_step", "n_points"}
                or not isinstance(nominal_cost, Mapping)
                or set(nominal_cost) != {"kind", "value"}
                or nominal_cost["kind"] != "point_neutrons"
                or not isinstance(diagnostics, Mapping)
                or not isinstance(hkl, list) or len(hkl) != 3
                or not isinstance(aliases, list)
                or any(not isinstance(alias, str) for alias in aliases)):
            raise ValueError("scan-design candidate nested fields are invalid")
        item = cls(
            hkl=tuple(_decoded_number(part) for part in hkl),
            q=_decoded_number(value["q"]),
            e_lo=_decoded_number(window["e_lo"]), e_hi=_decoded_number(window["e_hi"]),
            e_step=_decoded_number(window["e_step"]), n_points=window["n_points"],
            ncount=value["ncount"], kind=value["kind"], reason=value["reason"],
            nominal_cost=nominal_cost["value"],
            count_diagnostics=tuple(sorted(
                (str(key), _decoded_number(item)) for key, item in diagnostics.items())),
            candidate_family=value["candidate_family"], generator_id=value["generator_id"],
            generator_version=value["generator_version"], alias_families=tuple(aliases),
        )
        if canonical_json(item.to_dict()) != canonical_json(dict(value)):
            raise ValueError("scan-design candidate does not reproduce from persisted content")
        return item

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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignRefusal":
        if not isinstance(value, Mapping) or set(value) != {
                "candidate_family", "generator_id", "generator_version", "refused",
                "reason_code", "detail"}:
            raise ValueError("scan-design refusal fields are invalid")
        if (value["generator_id"] != PHYSICAL_WINDOW_GENERATOR_ID
                or value["generator_version"] != PHYSICAL_WINDOW_GENERATOR_VERSION
                or value["refused"] is not True or not isinstance(value["detail"], Mapping)):
            raise ValueError("scan-design refusal metadata is invalid")
        item = cls(
            candidate_family=value["candidate_family"], reason_code=value["reason_code"],
            detail=tuple(sorted((str(key), _decoded_number(part))
                                for key, part in value["detail"].items())),
        )
        if canonical_json(item.to_dict()) != canonical_json(dict(value)):
            raise ValueError("scan-design refusal does not reproduce from persisted content")
        return item


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
    shadow_scoring: ScanDesignShadowScoring | None = None

    def __post_init__(self) -> None:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if not self.candidates:
            raise ValueError("scan-design inventory must contain at least one candidate")
        if candidate_ids.count(self.selected_candidate_id) != 1:
            raise ValueError("selected scan-design candidate must occur exactly once")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("scan-design candidate identities must be unique")
        legacy = [candidate for candidate in self.candidates
                  if candidate.candidate_family == FAMILY_LEGACY_UNIFORM]
        if len(legacy) != 1 or self.selected_candidate_id != legacy[0].candidate_id:
            raise ValueError("scan-design selection must retain exactly one legacy control")
        if self.inventory_version not in {
                SCAN_DESIGN_INVENTORY_SCHEMA_V2, SCAN_DESIGN_INVENTORY_SCHEMA}:
            raise ValueError("scan-design inventory contract is unsupported")
        scoring = self.shadow_scoring
        if self.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA_V2:
            if scoring is not None:
                raise ValueError("historical /2 inventories cannot carry Slice 6 evidence")
            expected_basis = (SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED
                              if self.context.scan_window_eig_mode == "shadow"
                              else SELECTION_BASIS_LEGACY_CONTROL_OFF)
        else:
            if scoring is None:
                scoring = (ScanDesignShadowScoring.not_requested()
                           if self.context.scan_window_eig_mode == "off"
                           else ScanDesignShadowScoring.unavailable(
                               "shadow_scoring_not_run"))
                object.__setattr__(self, "shadow_scoring", scoring)
            validate_scan_design_shadow_scoring(scoring)
            if self.context.scan_window_eig_mode == "off":
                if scoring.status != "not_requested":
                    raise ValueError("off-mode scan design cannot carry shadow scores")
                expected_basis = SELECTION_BASIS_LEGACY_CONTROL_OFF
            else:
                if scoring.status == "not_requested":
                    raise ValueError("shadow-mode scan design must record a scoring outcome")
                expected_basis = (
                    SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED
                    if scoring.status in {SHADOW_STATUS_COMPLETE, SHADOW_STATUS_PARTIAL}
                    else SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED)
        if self.selection_basis != expected_basis:
            raise ValueError("scan-design selection basis is inconsistent with shadow evidence")

    @property
    def selected_candidate(self) -> ScanDesignCandidate:
        return next(candidate for candidate in self.candidates
                    if candidate.candidate_id == self.selected_candidate_id)

    def to_full_dict(self) -> dict:
        value = {
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
        if self.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA:
            value["shadow_scoring"] = self.shadow_scoring.to_dict()
        return value

    def with_shadow_scoring(self, scoring: ScanDesignShadowScoring) -> "ScanDesignInventory":
        """Attach diagnostic evidence without changing the selected candidate."""
        validate_scan_design_shadow_scoring(scoring)
        basis = (SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED
                 if scoring.status in {SHADOW_STATUS_COMPLETE, SHADOW_STATUS_PARTIAL}
                 else SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED)
        return replace(self, shadow_scoring=scoring, selection_basis=basis)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignInventory":
        if not isinstance(value, Mapping):
            raise TypeError("scan-design inventory must be a mapping")
        if _contains_forbidden_shadow_decision(value):
            raise ValueError("scan-design inventory contains an authoritative EIG decision field")
        version = value.get("inventory_version")
        expected = {
            "inventory_version", "canonicalization_version", "context_fingerprint", "context",
            "generator", "provider_identity", "selection", "candidates", "refused_candidates",
        }
        if version == SCAN_DESIGN_INVENTORY_SCHEMA:
            expected.add("shadow_scoring")
        elif version != SCAN_DESIGN_INVENTORY_SCHEMA_V2:
            raise ValueError("scan-design inventory contract is unsupported")
        if set(value) != expected or value["canonicalization_version"] != CANONICALIZATION_VERSION:
            raise ValueError("scan-design inventory fields are invalid")
        generator = value["generator"]
        provider = value["provider_identity"]
        selection = value["selection"]
        if (not isinstance(generator, Mapping)
                or set(generator) != {"id", "version", "config", "config_fingerprint"}
                or generator["id"] != PHYSICAL_WINDOW_GENERATOR_ID
                or generator["version"] != PHYSICAL_WINDOW_GENERATOR_VERSION
                or not isinstance(provider, Mapping)
                or dict(provider) != {
                    "predictive_provider_id": PREDICTIVE_PROVIDER_ID,
                    "predictive_provider_schema": PREDICTIVE_PROVIDER_SCHEMA,
                    "generator_id": PHYSICAL_WINDOW_GENERATOR_ID,
                    "generator_version": PHYSICAL_WINDOW_GENERATOR_VERSION,
                    "objective_projection_schema": OBJECTIVE_PROJECTION_SCHEMA,
                    "steering_regime": STEERING_REGIME,
                }
                or not isinstance(selection, Mapping)
                or set(selection) != {"basis", "selected_candidate_id", "scan_window_eig_mode"}
                or not isinstance(value["candidates"], list)
                or not isinstance(value["refused_candidates"], list)):
            raise ValueError("scan-design inventory metadata is invalid")
        context = ScanDesignContext.from_dict(value["context"])
        config = PhysicalWindowGeneratorConfig.from_dict(generator["config"])
        if (value["context_fingerprint"] != context.fingerprint
                or generator["config_fingerprint"] != config.fingerprint
                or selection["scan_window_eig_mode"] != context.scan_window_eig_mode):
            raise ValueError("scan-design inventory nested fingerprint is invalid")
        item = cls(
            context=context,
            candidates=tuple(ScanDesignCandidate.from_dict(candidate)
                             for candidate in value["candidates"]),
            selected_candidate_id=selection["selected_candidate_id"],
            selection_basis=selection["basis"], inventory_version=version,
            refusals=tuple(ScanDesignRefusal.from_dict(refusal)
                           for refusal in value["refused_candidates"]),
            generator_config=config,
            shadow_scoring=(ScanDesignShadowScoring.from_dict(value["shadow_scoring"])
                            if version == SCAN_DESIGN_INVENTORY_SCHEMA else None),
        )
        if canonical_json(item.to_full_dict()) != canonical_json(dict(value)):
            raise ValueError("scan-design inventory does not reproduce from persisted content")
        return item

    def content_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_full_dict()).encode("utf-8")).hexdigest()


def validate_scan_design_inventory(value: Any) -> ScanDesignInventory:
    """Reauthenticate one in-memory inventory and every nested Slice 6 record."""
    if not isinstance(value, ScanDesignInventory):
        raise TypeError("value must be a ScanDesignInventory")
    inventory = ScanDesignInventory.from_dict(value.to_full_dict())
    scoring = inventory.shadow_scoring
    has_deep_evidence = bool(scoring is not None and (
        scoring.status in {SHADOW_STATUS_COMPLETE, SHADOW_STATUS_PARTIAL}
        or scoring.scoring_config is not None or scoring.policy_set is not None
            or scoring.policy_match is not None or scoring.eligibility is not None
            or scoring.design_measure is not None or scoring.candidate_pair is not None
            or scoring.seed_evidence is not None or scoring.variants))
    if inventory.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA and has_deep_evidence:
        # The construction/import path stays lightweight.  Strict public validation deliberately
        # crosses the deep EIG seam only when a /3 inventory actually contains shadow evidence.
        from isar.eig.shadow_inventory import validate_scan_design_shadow_scoring as deep_validate
        deep_validate(scoring, inventory=inventory)
    return inventory


def load_scan_design_inventory(reference: Mapping[str, Any], workspace: str | Path
                               ) -> ScanDesignInventory:
    """Strictly load one content-addressed inventory below ``report/scan-design``.

    New artifacts are canonical JSON bytes, so the filename/reference, raw-byte digest, parsed
    semantics, and reconstructed inventory all authenticate the same address.
    """
    if (not isinstance(reference, Mapping)
            or set(reference) != {"kind", "sha256", "path"}
            or reference.get("kind") != "scan_design_inventory"):
        raise ValueError("scan-design inventory reference is invalid")
    digest = reference.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)):
        raise ValueError("scan-design inventory reference digest is invalid")
    expected_relative = f"report/scan-design/{digest}.json"
    relative = reference.get("path")
    if not isinstance(relative, str) or relative.replace("\\", "/") != expected_relative:
        raise ValueError("scan-design inventory reference path is invalid")
    root = Path(workspace).resolve()
    allowed = (root / "report" / "scan-design").resolve()
    path = (root / Path(relative)).resolve()
    if path.parent != allowed or path.name != f"{digest}.json":
        raise ValueError("scan-design inventory reference escapes its allowed directory")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("scan-design inventory artifact is unreadable") from error
    if not isinstance(payload, Mapping):
        raise ValueError("scan-design inventory artifact must contain an object")
    semantic_digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    if semantic_digest != digest:
        raise ValueError("scan-design inventory artifact content address is invalid")
    if hashlib.sha256(raw).hexdigest() != digest:
        # A pre-canonical-encoding artifact: the previous release wrote ``json.dump(..., indent=2)``,
        # so the semantics authenticate but the raw bytes do not. The content address is the
        # semantics, and rejecting these would silently invalidate every historical acquisition a
        # resume or shadow replay reads. Accept, note the encoding, keep writing canonical bytes.
        _LOG.warning("legacy_artifact_encoding: scan-design inventory %s authenticates "
                     "semantically but was written with a pre-canonical encoding", path)
    inventory = validate_scan_design_inventory(ScanDesignInventory.from_dict(payload))
    if inventory.content_hash() != digest:
        raise ValueError("scan-design inventory reconstructed content address is invalid")
    return inventory


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


def _legacy_candidate(context: ScanDesignContext) -> ScanDesignCandidate:
    """The exact historical uniform scan for ``context`` — the control, always generated.

    The window comes from :func:`~isar.drive.acquisition.physical_windows.legacy_window` unchanged;
    only the exposure request and the audit packaging happen here.
    """
    e_lo, e_hi, e_step, n_points = legacy_window(context)
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


def _refusal_from(window_refusal: WindowRefusal) -> ScanDesignRefusal:
    """Lift a generator-scoped refusal into the neutral audit vocabulary."""
    return ScanDesignRefusal(candidate_family=window_refusal.candidate_family,
                             reason_code=window_refusal.reason_code,
                             detail=window_refusal.detail)


def _price_planned_window(planned: PlannedWindow, context: ScanDesignContext) -> tuple[
        ScanDesignCandidate | None, ScanDesignRefusal | None]:
    """Turn one generator window into a priced, auditable candidate — or the refusal that blocks it.

    Exposure, budget, and instrument limits are provider-neutral facts about *any* window, so they
    are applied here rather than inside the physical generator.
    """
    grid = planned.grid
    try:
        ncount, diagnostics = _count_request(context, grid.n_points)
    except (BudgetInsufficientForMinimumBatch, InstrumentLimitBelowMinimumBatch) as error:
        # Not swallowed: the impossibility becomes a durable record on this family.  The legacy
        # control is built first, so a limit that also blocks the executed scan has already raised.
        return None, ScanDesignRefusal(
            candidate_family=planned.candidate_family,
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
        candidate_family=planned.candidate_family,
    )
    return candidate, None


def _alternative_candidate(family: str, context: ScanDesignContext,
                           config: PhysicalWindowGeneratorConfig,
                           domain: tuple[float, float]) -> tuple[
                               ScanDesignCandidate | None, ScanDesignRefusal | None]:
    """Build one non-legacy family, or the structured refusal explaining why it cannot exist."""
    planned = alternative_window(family, context, config, domain)
    if isinstance(planned, WindowRefusal):
        return None, _refusal_from(planned)
    return _price_planned_window(planned, context)


def _dedup_identity(candidate: ScanDesignCandidate) -> tuple[str, tuple[float, ...], int]:
    """The key two families must share before one is absorbed as an alias of the other.

    It is deliberately a strict subset of :attr:`ScanDesignCandidate.candidate_id`'s inputs, and it
    must include everything ``candidate_id`` distinguishes that is *observable in the emitted
    request* — otherwise two families with the same visited points but different ``e_hi`` merge, and
    the surviving candidate's ``deltaE`` string is not the one the absorbed family declared.  The
    legacy control is keyed the same way and is never absorbed (it is inserted first), so this
    cannot move the executed request.
    """
    return (candidate.grid.scan_command(), candidate.energies, candidate.ncount)


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

    if context.scan_window_eig_mode in {"shadow", "authoritative"}:
        domain = authorized_domain(context, config)
        by_identity: dict[tuple[str, tuple[float, ...], int], int] = {
            _dedup_identity(legacy): 0}
        for family in SHADOW_CANDIDATE_FAMILIES:
            if family == FAMILY_LEGACY_UNIFORM:
                continue
            candidate, refusal = _alternative_candidate(family, context, config, domain)
            if candidate is None:
                refusals.append(refusal)
                continue
            identity = _dedup_identity(candidate)
            existing = by_identity.get(identity)
            if existing is not None:
                kept = candidates[existing]
                candidates[existing] = replace(
                    kept, alias_families=kept.alias_families + (family,))
                continue
            by_identity[identity] = len(candidates)
            candidates.append(candidate)

    basis = (SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED
             if context.scan_window_eig_mode in {"shadow", "authoritative"}
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
    # Re-exported from the physical generator so existing callers keep one import site for the
    # scan-design surface; the definitions live in ``physical_windows``.
    "DEFAULT_WINDOW_GENERATOR_CONFIG",
    "FAMILY_BROAD_RECOVERY",
    "FAMILY_LEGACY_UNIFORM",
    "FAMILY_TARGET_CORE",
    "FAMILY_TARGET_HIGH_ANCHOR",
    "FAMILY_TARGET_LOW_ANCHOR",
    "FAMILY_TARGET_PAIRED",
    "FAMILY_TARGET_PLUS_RIVAL",
    "PHYSICAL_WINDOW_GENERATOR_ID",
    "PHYSICAL_WINDOW_GENERATOR_VERSION",
    "PhysicalWindowGeneratorConfig",
    "SHADOW_CANDIDATE_FAMILIES",
    "TARGET_WINDOW_FAMILIES",
    # Provider-neutral surface owned here.
    "ScanDesignCandidate",
    "ScanDesignContext",
    "ScanDesignInventory",
    "ScanDesignRefusal",
    "SCAN_DESIGN_INVENTORY_SCHEMA",
    "SCAN_DESIGN_INVENTORY_SCHEMA_V2",
    "SELECTION_BASIS_LEGACY_CONTROL_OFF",
    "SELECTION_BASIS_SHADOW_LEGACY_CONTROL_SCORED",
    "SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED",
    "design_scan_windows",
    "load_scan_design_inventory",
    "points_in_window",
    "validate_scan_design_inventory",
]

"""Qt- and drive-free scientific action identity helpers.

The acquisition inventory remains the owner of the live ``MeasurementIntent`` value type.  Pure
evidence modules consume persisted mappings, however, and must not import the acquisition package
merely to authenticate the same frozen payload.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .steering import EVIDENCE_SINKS


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hkl(hkl: Any) -> tuple[float, float, float]:
    if not isinstance(hkl, (list, tuple)) or len(hkl) != 3:
        raise ValueError("HKL must contain exactly three values")
    values = tuple(float(value) for value in hkl)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("HKL values must be finite")
    # ``+ 0.0`` collapses ``-0.0`` onto ``0.0``: reduced-zone arithmetic yields negative zero for a
    # zone-centre component, and ``json.dumps`` writes it as ``-0.0``, which would mint a second
    # fingerprint for the same scientific Q and escape dedup.
    return tuple(round(value, 9) + 0.0 for value in values)


def scientific_fingerprint_from_mapping(payload: Mapping[str, object]) -> str:
    """Authenticate the unchanged scientific subset of a persisted measurement intent."""
    if not isinstance(payload, Mapping):
        raise ValueError("measurement intent must be a mapping")

    def required_text(name: str) -> str:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"measurement intent {name!r} must be a non-empty string")
        return value

    def optional_text(name: str) -> str | None:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"measurement intent {name!r} must be a string or null")
        return value

    def optional_number(name: str) -> float | None:
        if name not in payload:
            raise ValueError(f"measurement intent is missing {name!r}")
        value = payload.get(name)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"measurement intent {name!r} must be a finite number or null")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"measurement intent {name!r} must be a finite number or null") from error
        if not math.isfinite(number):
            raise ValueError(f"measurement intent {name!r} must be a finite number or null")
        return number

    action_class = required_text("action_class")
    hkl = canonical_hkl(payload.get("hkl"))
    energy_lo = optional_number("energy_lo")
    energy_hi = optional_number("energy_hi")
    window_family = required_text("window_family")
    target_branch_role = optional_text("target_branch_role")
    rival_branch_role = optional_text("rival_branch_role")
    evidence_basis_fingerprint = optional_text("evidence_basis_fingerprint")
    hypotheses_fingerprint = optional_text("hypotheses_fingerprint")
    campaign_slice_fingerprint = optional_text("campaign_slice_fingerprint")
    evidence_sink = required_text("evidence_sink")
    if evidence_sink not in EVIDENCE_SINKS:
        raise ValueError(
            f"unknown evidence_sink {evidence_sink!r}; expected one of {EVIDENCE_SINKS}")
    if "window_center_meV" in payload:
        optional_number("window_center_meV")  # validated but deliberately excluded from identity
    encoded = canonical_json([
        action_class,
        list(hkl),
        None if energy_lo is None else round(energy_lo, 6) + 0.0,   # ``+ 0.0``: see canonical_hkl
        None if energy_hi is None else round(energy_hi, 6) + 0.0,
        window_family,
        target_branch_role,
        rival_branch_role,
        evidence_basis_fingerprint,
        hypotheses_fingerprint,
        campaign_slice_fingerprint,
        evidence_sink,
    ])
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["canonical_hkl", "canonical_json", "scientific_fingerprint_from_mapping"]

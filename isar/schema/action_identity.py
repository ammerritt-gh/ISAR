"""Qt- and drive-free scientific action identity — the one implementation of the fingerprint.

The schema owns the ``MeasurementIntent`` value type and the hash derived from it. Both the live
acquisition path and the pure evidence modules that only ever see persisted mappings resolve to this
module, so there is no second hand-maintained copy of the rule that can drift: a divergence would
fork candidate identity, dedupe the same scientific question against itself in one module and not
the other, and orphan every retained inventory artifact keyed by the old digest.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from .steering import CANONICAL_MASTER, EVIDENCE_SINKS


def canonical_json(value: Any) -> str:
    """Deterministic JSON for content addressing — strict on purpose.

    ``allow_nan=False`` because NaN/Infinity are not JSON and would make the artifact unreadable by a
    conforming parser, and no ``default=`` coercion so an unserializable value raises instead of
    silently becoming a ``str`` whose text (and therefore the content address) is not stable.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hkl(hkl: Any) -> tuple[float, float, float]:
    """Canonical scientific Q identity shared by action and EIG policy.

    Fingerprints have always used nine decimal places.  Giving that rule one
    named implementation prevents a nearby-but-not-identical tolerance from
    becoming a second scientific identity rule in a later consumer.
    """
    if not isinstance(hkl, (list, tuple)) or len(hkl) != 3:
        raise ValueError("HKL must contain exactly three values")
    values = tuple(float(value) for value in hkl)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("HKL values must be finite")
    # ``+ 0.0`` collapses ``-0.0`` onto ``0.0``: reduced-zone arithmetic yields negative zero for a
    # zone-centre component, and ``json.dumps`` writes it as ``-0.0``, which would mint a second
    # fingerprint for the same scientific Q and escape dedup.
    return tuple(round(value, 9) + 0.0 for value in values)


def _identity_payload(intent: "MeasurementIntent", *, evidence_basis: bool) -> str:
    """The ordered identity inputs, optionally minus the evidence-basis field.

    One list, two readings — so a field added to the scientific identity cannot be forgotten by the
    physical key (or the reverse), which is the drift that two hand-maintained lists would invite.
    """
    return canonical_json([
        intent.action_class,
        list(canonical_hkl(intent.hkl)),
        # ``+ 0.0``: see :func:`canonical_hkl` — a negative-zero bound must not fork identity.
        None if intent.energy_lo is None else round(float(intent.energy_lo), 6) + 0.0,
        None if intent.energy_hi is None else round(float(intent.energy_hi), 6) + 0.0,
        intent.window_family,
        intent.target_branch_role,
        intent.rival_branch_role,
        *([intent.evidence_basis_fingerprint] if evidence_basis else []),
        intent.hypotheses_fingerprint,
        intent.campaign_slice_fingerprint,
        intent.evidence_sink,
    ])


def scientific_fingerprint(intent: "MeasurementIntent") -> str:
    """Stable hash of the *scientific question* an intent resolves — its dedup / anti-repeat id.

    Deliberately excludes anything about the exact emitted request (window step, ncount, feasibility):
    two intents that ask the same scientific question with different executable realizations share one
    fingerprint and must not both occupy the inventory.
    """
    return hashlib.sha256(
        _identity_payload(intent, evidence_basis=True).encode("utf-8")).hexdigest()


def physical_action_key(intent: "MeasurementIntent") -> str:
    """Stable hash of the *physical measurement* an intent asks for, INVARIANT to the scene.

    Exactly :func:`scientific_fingerprint`'s inputs minus ``evidence_basis_fingerprint`` — the one
    field that carries the scene digest (``isar.drive.steering.generators`` stamps the revision the
    action was derived against). Every other field is a property of the request itself: the action
    class, the canonical Q, the energy window and its family, the branch roles, the declared
    hypotheses, the campaign slice, and the evidence sink.

    Why this exists (SCENE-4.3 C6): the scene moves on EVERY delivery — an untrusted arena still
    enters as a synthetic observation — so re-measuring the same q with the same window mints a NEW
    scientific fingerprint each pass. Anything counting repeats of one physical action (the
    broken-scan retry bound, and the retirement filter that enforces it) must key on this instead,
    or its counter never sees the same key twice and its bound never binds. Scientific identity is
    still the right key for dedup and for the decision record, because "answered against a
    different scene" genuinely is a different question; "the detector was dead again" is not.
    """
    return hashlib.sha256(
        _identity_payload(intent, evidence_basis=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MeasurementIntent:
    """The scientific question an action resolves, independent of any exact emitted request."""

    action_class: str
    hkl: tuple[float, float, float]
    energy_lo: float | None
    energy_hi: float | None
    window_family: str
    target_branch_role: str | None = None
    rival_branch_role: str | None = None
    evidence_basis_fingerprint: str | None = None
    hypotheses_fingerprint: str | None = None
    campaign_slice_fingerprint: str | None = None
    # Where this action's evidence may go (isar.schema.steering.EVIDENCE_SINKS). Part of the scientific
    # identity, not a delivery detail: the same window at the same q routed to a different sink is
    # answered against different evidence and updates a different posterior, so the two are genuinely
    # different actions and must not dedupe onto one another.
    evidence_sink: str = CANONICAL_MASTER
    # The energy this action is *about*, when that is not the midpoint of its window. A rival probe's
    # bounds are clamped into the identity domain, so a domain that clips one side moves the midpoint
    # off the predicted rival; realization must resolve the instrument resolution and persist the
    # acquisition target at the prediction, not at the midpoint of what survived the clamp.
    #
    # Deliberately EXCLUDED from ``scientific_fingerprint``: ``energy_lo``/``energy_hi`` already carry
    # the scientific window, and folding the centre in would re-mint every fingerprint ever computed,
    # invalidating the frozen replay artifacts for no gain in what the identity distinguishes.
    window_center_meV: float | None = None

    def __post_init__(self) -> None:
        if self.evidence_sink not in EVIDENCE_SINKS:
            raise ValueError(f"unknown evidence_sink {self.evidence_sink!r}; "
                             f"expected one of {EVIDENCE_SINKS}")

    @property
    def scientific_action_fingerprint(self) -> str:
        return scientific_fingerprint(self)


def intent_from_mapping(payload: Mapping[str, object]) -> "MeasurementIntent":
    """Reconstruct and validate a persisted :class:`MeasurementIntent`.

    The one canonical constructor for a persisted intent map. Inventory consumers must never
    hand-assemble the value type from a convenient subset of the mapping: rebuilding it here keeps
    the persisted action identity subject to the same Q, finite-number, and evidence-sink rules as
    a live proposal, and keeps every optional field (``window_center_meV`` among them) from being
    silently dropped by a caller that forgot to copy it across.
    """
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
            raise ValueError(f"measurement intent {name!r} must be a finite number or null") from error
        if not math.isfinite(number):
            raise ValueError(f"measurement intent {name!r} must be a finite number or null")
        return number

    return MeasurementIntent(
        action_class=required_text("action_class"),
        hkl=canonical_hkl(payload.get("hkl")),
        energy_lo=optional_number("energy_lo"),
        energy_hi=optional_number("energy_hi"),
        window_family=required_text("window_family"),
        target_branch_role=optional_text("target_branch_role"),
        rival_branch_role=optional_text("rival_branch_role"),
        evidence_basis_fingerprint=optional_text("evidence_basis_fingerprint"),
        hypotheses_fingerprint=optional_text("hypotheses_fingerprint"),
        campaign_slice_fingerprint=optional_text("campaign_slice_fingerprint"),
        evidence_sink=required_text("evidence_sink"),
        window_center_meV=(optional_number("window_center_meV")
                           if "window_center_meV" in payload else None),
    )


def scientific_fingerprint_from_mapping(payload: Mapping[str, object]) -> str:
    """The scientific action fingerprint of a persisted intent map, via :func:`intent_from_mapping`."""
    return scientific_fingerprint(intent_from_mapping(payload))


__all__ = [
    "MeasurementIntent",
    "canonical_hkl",
    "canonical_json",
    "intent_from_mapping",
    "physical_action_key",
    "scientific_fingerprint",
    "scientific_fingerprint_from_mapping",
]

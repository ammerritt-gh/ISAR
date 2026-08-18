"""Declared per-class placement rules — findability knowledge (SCENE-4 PR 4.2a, commit A4;
relocated to `isar.schema` in the A5 boundary repair so `isar.drive` can read it without
crossing the Stage II<->III wall `tests/drive/test_boundary.py` enforces).

Findability knowledge re-enters as DECLARED DATA: where a catalogue class is worth looking for
it, for the opening book to order its survey. These are ORDERING declarations only — they never
label a measured feature, gate coverage, or touch a posterior. SCENE-5 replaces the encoded
conclusions with the real EIG computation; until then a rule is a named provisional v1 guess,
SCENE-7-calibrated.

This module is deliberately class-list-BLIND: it doesn't know which classes a catalogue profile
declares (that's `isar.analyze.catalogue.PROFILES`, analyze-only knowledge), so the
completeness/orphan validation that needs the real class list stays a thin wrapper in
`isar.analyze.catalogue` (`placement_rules`/`placement_rules_manifest`/
`placement_rules_fingerprint`, same names and semantics as before the move). What lives here is
the raw registry, keyed by catalogue profile id then class name, plus a caller-supplied-class-list
accessor and an ordering-only accessor neither side of the wall needs a class list for.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from .action_identity import canonical_json

PLACEMENT_RULES_SCHEMA = "isar.placement-rules/1"
FALLBACK_RULE_KEY = "__fallback__"

# The catalogue profile id these default rules are registered against. A literal, not an import
# of `isar.analyze.catalogue.DEFAULT_PROFILE` — that module owns the value and this one must
# never import analyze code (the wall this module exists to respect). Keep the two in sync by
# hand; `tests/analyze/test_placement_rules.py` and `tests/analyze/test_catalogue.py` both pin
# the string, so a drift is caught immediately.
_DEFAULT_CATALOGUE_PROFILE = "isar.catalogue.default/1"


@dataclass(frozen=True)
class PlacementRule:
    """One declared ordering hint for a catalogue class — never a measurement label.

    ``anchor_kind`` names WHERE in reduced-q the class is expected to be findable
    (``"zone_centre"`` / ``"zone_boundary"`` / ``"uniform"`` / ``"not_applicable"`` — no anchor
    preference, e.g. a null/background entry). ``omega_band_hint_meV`` is an optional
    ``(lo, hi)`` energy-transfer band the class is expected to show up in, or ``None`` when no
    generic band is honest (material-specific). ``order_weight`` ranks candidates against each
    other within one survey step (higher = earns an earlier look); it is not a probability and
    carries no posterior meaning. ``note`` records the physics reasoning, in prose, for the value.
    """

    anchor_kind: str
    omega_band_hint_meV: tuple[float, float] | None
    order_weight: float
    note: str

    _VALID_ANCHORS = ("zone_centre", "zone_boundary", "uniform", "not_applicable")

    def __post_init__(self) -> None:
        if self.anchor_kind not in self._VALID_ANCHORS:
            raise ValueError(f"placement rule anchor_kind {self.anchor_kind!r} not in "
                             f"{self._VALID_ANCHORS}")
        if self.omega_band_hint_meV is not None:
            lo, hi = self.omega_band_hint_meV
            if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi):
                raise ValueError(f"placement rule omega_band_hint_meV must be a finite (lo, hi) "
                                 f"with lo <= hi, got {self.omega_band_hint_meV!r}")


@dataclass(frozen=True)
class ResolvedPlacement:
    """One class's placement rule as resolved for a profile, plus whether it fell through to the
    profile-level fallback (declared explicitly, never synthesized)."""

    rule: PlacementRule
    used_fallback: bool


# The default profile's rules. Every class in DEFAULT_CATALOGUE gets an explicit row — no class
# is left to fall through silently. Values are named provisional v1, SCENE-7-calibrated.
#
# ``omega_band_hint_meV`` IS PROVENANCE ONLY (review finding 2026-08-18). It is read in exactly one
# other place — ``isar.drive.steering.planner._raw_placement_manifest``, which copies it into the
# decision basis and fingerprints it — and nothing steers on it. That is deliberate, not a gap: a
# discovery survey is asked to map the operator's whole declared energy domain, and truncating every
# scan to the band where a class USUALLY sits would miss the phenomenon ``include_unknown`` exists
# to catch (an optic branch at 8 meV, in a window narrowed to acoustic's 0-5). The hint says where a
# class lives, for classification and for targeted follow-up; it is not a mandate to narrow a
# map-everything scan. ``anchor_kind``/``order_weight`` are the fields that DO steer, through
# ``zone_centre_order_weight`` and the opening book's proposal ordering.
_DEFAULT_PLACEMENT_RULES: dict[str, PlacementRule] = {
    "acoustic": PlacementRule(
        anchor_kind="zone_centre",
        omega_band_hint_meV=(0.0, 5.0),
        order_weight=1.0,
        note="acoustic ω → 0 at the zone centre: strongest, best-predicted, extrapolatable from "
             "one detection — but offset from the elastic/Bragg line, which is a modelled "
             "phenomenon in its own right, not this anchor. Not zone boundary: weaker, poorly "
             "predicted there.",
    ),
    "optic": PlacementRule(
        anchor_kind="zone_centre",
        omega_band_hint_meV=None,
        order_weight=0.6,
        note="optic branches are typically strong and flat near the zone centre (transportable "
             "over a wide q range per the catalogue's transport declaration), but the ω location "
             "is material-specific — no generic band hint is honest without a calibrated prior.",
    ),
    "elastic": PlacementRule(
        anchor_kind="not_applicable",
        omega_band_hint_meV=None,
        order_weight=0.0,
        note="static elastic scattering sits at E=0 in every scan by construction — there is no "
             "placement DECISION to make, its presence is read off the same window every time.",
    ),
    "quasielastic": PlacementRule(
        anchor_kind="uniform",
        omega_band_hint_meV=None,
        order_weight=0.4,
        note="relaxational dynamics centred at E=0 with a card-declared width, not zone-anchored: "
             "no generic q-preference is honest, so ordering treats it as uniform in q.",
    ),
    "unknown": PlacementRule(
        anchor_kind="uniform",
        omega_band_hint_meV=None,
        order_weight=0.3,
        note="an unassigned resolved excursion carries no branch identity yet — a generic "
             "discovery rule: look everywhere, modest priority relative to the named classes.",
    ),
    "bg_only": PlacementRule(
        anchor_kind="not_applicable",
        omega_band_hint_meV=None,
        order_weight=0.0,
        note="background alone is not a discovery target — nothing to place.",
    ),
}

PLACEMENT_RULES: dict[str, dict[str, PlacementRule]] = {
    _DEFAULT_CATALOGUE_PROFILE: _DEFAULT_PLACEMENT_RULES,
}


def resolve_placement_rules(profile: str,
                             class_names: Iterable[str]) -> dict[str, ResolvedPlacement]:
    """The resolved placement rule for every name in ``class_names`` within ``profile``.

    The raw accessor: no catalogue-profile validation (this module doesn't know what a profile
    IS, only what rules are registered against its id) — the caller supplies the class list, and
    an unregistered profile or a class with neither an explicit row nor a declared fallback both
    raise loudly, never silent synthesis. A rule row naming a class outside ``class_names`` is
    refused as a typo/stale registration (same as before the move).
    """
    if profile not in PLACEMENT_RULES:
        raise KeyError(f"no placement rules registered for catalogue profile {profile!r}")
    rules_for_profile = PLACEMENT_RULES[profile]
    names = list(class_names)
    orphans = set(rules_for_profile) - {FALLBACK_RULE_KEY} - set(names)
    if orphans:
        raise KeyError(f"placement rules for profile {profile!r} name classes the profile does "
                       f"not declare: {sorted(orphans)}")
    fallback = rules_for_profile.get(FALLBACK_RULE_KEY)
    resolved: dict[str, ResolvedPlacement] = {}
    for name in names:
        if name in rules_for_profile:
            resolved[name] = ResolvedPlacement(rules_for_profile[name], used_fallback=False)
        elif fallback is not None:
            resolved[name] = ResolvedPlacement(fallback, used_fallback=True)
        else:
            raise KeyError(f"catalogue class {name!r} in profile {profile!r} has no placement "
                           f"rule and the profile declares no {FALLBACK_RULE_KEY!r} fallback")
    return resolved


def zone_centre_order_weight(profile: str) -> float:
    """Highest ``order_weight`` among ``profile``'s explicitly ``zone_centre``-anchored rules
    (including the fallback row, if one is declared and zone-centre) — ``0.0`` if none, or if
    the profile isn't registered at all. Ordering-only and NEVER raises: unlike
    :func:`resolve_placement_rules`, an opening-book proposal's ordering is advisory, so an
    unregistered or class-list-incomplete profile must stay inert rather than block the survey."""
    weights = [r.order_weight for r in PLACEMENT_RULES.get(profile, {}).values()
              if r.anchor_kind == "zone_centre"]
    return max(weights) if weights else 0.0


def _placement_rule_dict(rule: PlacementRule, used_fallback: bool) -> dict:
    return {
        "anchor_kind": rule.anchor_kind,
        "omega_band_hint_meV": (list(rule.omega_band_hint_meV)
                                if rule.omega_band_hint_meV is not None else None),
        "order_weight": rule.order_weight,
        "note": rule.note,
        "used_fallback": used_fallback,
    }


def placement_rules_manifest(profile: str, class_names: Iterable[str]) -> dict:
    """Canonical, JSON-able, deterministic manifest of every named class's resolved placement
    rule — the replayable artifact (:func:`placement_rules_fingerprint` is change-detection over
    it). Raw accessor: takes the class list explicitly, same as :func:`resolve_placement_rules`."""
    resolved = resolve_placement_rules(profile, class_names)
    return {
        "schema": PLACEMENT_RULES_SCHEMA,
        "profile": profile,
        "rules": {name: _placement_rule_dict(r.rule, r.used_fallback)
                 for name, r in sorted(resolved.items())},
    }


def placement_rules_fingerprint(profile: str, class_names: Iterable[str]) -> str:
    """Content address over :func:`placement_rules_manifest` — changes iff a rule field changes."""
    payload = canonical_json(placement_rules_manifest(profile, class_names))
    return PLACEMENT_RULES_SCHEMA + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

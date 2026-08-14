"""The question-policy defaults profile and its compile-time resolution (SCENE-4 PR 4.1).

One named, versioned defaults profile — no magic numbers in predicate code (Topic 4). The goal
compiler resolves effective values here and freezes them into the ``discover_and_map/2`` step's
REQUIRED ``resolved_policy`` block; predicates read only the frozen program, never these module
constants, so a mid-campaign change is a new compiled program revision, not drift.

**Three authority postures**, recorded per field in the block's provenance (the epoch-4 ruling):

- *target* — user ask > card guidelines > named default. Goals and preferences.
- *identity_constant* — set at compile, frozen for the LIFE of the program: the ω tile quantum
  enters emergent question identity, so a consultation-driven threshold re-freeze must never be
  able to silently re-ID every emergent question. Changing one is a program revision.
- *capability* — an epistemic or instrument limit (calibrated profile or card > conservative
  built-in default). A user ask may STRENGTHEN it, never weaken it: nobody talks an uncertainty
  floor down, a justifiable-confidence ceiling up, or a detection-exposure floor down by asking.

Every default VALUE below is a named provisional v1 declaration, SCENE-7-calibrated.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

QUESTION_POLICY_SCHEMA = "isar.question-policy/1"
DEFAULTS_PROFILE_ID = "isar.question-defaults"
DEFAULTS_PROFILE_VERSION = 1

# Authority postures (see module docstring).
AUTHORITY_TARGET = "target"
AUTHORITY_IDENTITY = "identity_constant"
AUTHORITY_CAPABILITY = "capability"

# --------------------------------------------------------------------------- named defaults
#: Target thresholds — user > card > default. Provisional v1, SCENE-7-calibrated.
TARGET_DEFAULTS: dict[str, float] = {
    "confirm_presence_min_mass": 0.9,
    "classify_identity_min_mass": 0.9,
    "connect_track_min_mass": 0.9,
    "mismatch_min_mass": 0.9,
    "emergent_open_min_mass": 0.5,
    "parameter_default_tolerance_meV": 0.5,
    "requested_zc_confidence": 0.9,
}

#: Identity-bearing derivation constants — frozen for the program's life. The ω tile quantum
#: for emergent subject identity and the completeness coverage grid; the q quantum is the /2
#: region's ``maximum_initial_spacing_rlu``.
IDENTITY_DEFAULTS: dict[str, float] = {
    "emergent_omega_tile_meV": 1.0,
}

#: Capability floors/ceilings — user may strengthen, never weaken.
#: ``maximum_justifiable_zc_confidence`` is 0.0 in v1 BY DESIGN: no probabilistic ZC
#: extrapolation model exists and no within-candidate uncertainties are persisted, so no
#: numeric endpoint confidence is justifiable — the v1 evaluator answers compatibility only.
CAPABILITY_DEFAULTS: dict[str, float] = {
    "parameter_statistical_floor_meV": 0.2,
    "maximum_justifiable_zc_confidence": 0.0,
    "min_effective_exposure": 1.0,
}

#: Which direction STRENGTHENS a capability field: a "floor" may be raised, a "ceiling" lowered.
CAPABILITY_DIRECTION: dict[str, str] = {
    "parameter_statistical_floor_meV": "floor",
    "maximum_justifiable_zc_confidence": "ceiling",
    "min_effective_exposure": "floor",
}

#: Family priority classes, breadth before polish (Topic 6): no EstimateParameter refinement
#: while DiscoverRegion has unsurveyed territory. Lower = earlier.
FAMILY_PRIORITIES: dict[str, int] = {
    "DiscoverRegion": 0,
    "ConfirmPresence": 1,
    "ClassifyIdentity": 2,
    "ConnectTrack": 3,
    "TestModelMismatch": 3,
    "EstimateParameter": 4,
    "VerifyCompleteness": 5,
}

#: Refuse-don't-ignore vocabularies (Topic 7): a mode this build cannot honour is refused at
#: validation, never accepted and silently run differently.
SUPPORTED_EMERGENT_POLICIES = ("open_in_domain",)
SUPPORTED_STALENESS_MODES = ("synchronous",)
SUPPORTED_UNRESOLVABLE_CLOSURE = ("accept_close",)
SUPPORTED_TIMEOUT_BEHAVIOUR = ("apply_default",)

PROMPT_DEFAULTS: dict[str, str] = {
    "unresolvable_closure": "accept_close",
    "timeout_behaviour": "apply_default",
}

# Discover compile defaults (moved here from goal_to_spec so every question-layer number has
# one home; goal_to_spec re-exports them, so no call site churns).
DEFAULT_DISCOVER_SPACING_RLU = 0.05
DEFAULT_CATALOGUE_PROFILE = "isar.catalogue.inelastic-default/1"
DEFAULT_DELIVERABLES: tuple[dict[str, Any], ...] = ({"kind": "phenomenon_map"},)

_ALL_THRESHOLDS = {**TARGET_DEFAULTS, **IDENTITY_DEFAULTS, **CAPABILITY_DEFAULTS}
# min-mass and confidence fields live in [0, 1]; everything else is a positive quantity.
_UNIT_INTERVAL_FIELDS = {"confirm_presence_min_mass", "classify_identity_min_mass",
                         "connect_track_min_mass", "mismatch_min_mass",
                         "emergent_open_min_mass", "requested_zc_confidence",
                         "maximum_justifiable_zc_confidence"}


def _authority_of(name: str) -> str:
    if name in TARGET_DEFAULTS:
        return AUTHORITY_TARGET
    if name in IDENTITY_DEFAULTS:
        return AUTHORITY_IDENTITY
    return AUTHORITY_CAPABILITY


def _check_value(name: str, value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: {name} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{where}: {name} must be finite")
    if name in _UNIT_INTERVAL_FIELDS:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"{where}: {name} must be within [0, 1], got {v}")
    elif v <= 0.0:
        raise ValueError(f"{where}: {name} must be positive, got {v}")
    return v


def resolve_policy(user_ask: Mapping[str, Any] | None = None,
                   card_guidelines: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve effective policy values and return the frozen ``resolved_policy`` block.

    ``user_ask`` and ``card_guidelines`` are flat ``{field: value}`` mappings over the threshold
    names above (the card lane ships WIRED BUT EMPTY in 4.1 — no ``card.policy.toml`` table
    exists yet, so production passes ``{}`` here; the precedence machinery is real and tested
    against synthetic dicts). Unknown field names refuse: a misspelled override that silently
    resolved to the default would be policy the scientist believes is applied and is not.

    A user value on a *capability* field is honoured only in the strengthening direction and
    REFUSED otherwise — by refusal rather than clamping, because a silently clamped ask is
    indistinguishable from an applied one in the very place provenance is supposed to say who
    chose each number.
    """
    user = dict(user_ask or {})
    card = dict(card_guidelines or {})
    for source_name, mapping in (("user ask", user), ("card guidelines", card)):
        unknown = sorted(set(mapping) - set(_ALL_THRESHOLDS))
        if unknown:
            raise ValueError(f"{source_name} names unknown question-policy fields: {unknown}")

    thresholds: dict[str, float] = {}
    provenance: dict[str, dict[str, str]] = {}
    for name, default in _ALL_THRESHOLDS.items():
        authority = _authority_of(name)
        value, source = default, "default"
        if name in card:
            value, source = _check_value(name, card[name], "card guidelines"), "card"
        if name in user:
            asked = _check_value(name, user[name], "user ask")
            if authority == AUTHORITY_CAPABILITY:
                direction = CAPABILITY_DIRECTION[name]
                weakens = asked < value if direction == "floor" else asked > value
                if weakens:
                    raise ValueError(
                        f"user ask would weaken capability {direction} {name} "
                        f"({value} -> {asked}); a capability limit may be strengthened by "
                        "asking, never weakened")
            value, source = asked, "user"
        thresholds[name] = value
        provenance[name] = {"source": source, "authority": authority}

    block = {
        "schema": QUESTION_POLICY_SCHEMA,
        "profile": {"id": DEFAULTS_PROFILE_ID, "version": DEFAULTS_PROFILE_VERSION},
        "thresholds": thresholds,
        "priorities": dict(FAMILY_PRIORITIES),
        "emergent_question_policy": "open_in_domain",
        "staleness_policy": {"mode": "synchronous", "lag": 0},
        "prompt_defaults": dict(PROMPT_DEFAULTS),
        "provenance": provenance,
    }
    validate_resolved_block(block, "resolved_policy (freshly resolved)")
    return block


def validate_resolved_block(block: Any, where: str = "resolved_policy") -> None:
    """Validate a frozen policy block completely — a /2 program is refused on any gap.

    Frozen may never mean "acquires today's defaults at read time" (H1): every threshold, every
    priority, and every provenance entry must be PRESENT and valid; an absent field refuses
    rather than defaulting. Unsupported staleness modes and emergent policies refuse by name.
    """
    if not isinstance(block, Mapping):
        raise ValueError(f"{where} must be an object")
    if block.get("schema") != QUESTION_POLICY_SCHEMA:
        raise ValueError(f"{where} must declare schema {QUESTION_POLICY_SCHEMA!r}, got "
                         f"{block.get('schema')!r}")
    profile = block.get("profile")
    if not isinstance(profile, Mapping) or not isinstance(profile.get("id"), str) \
            or not isinstance(profile.get("version"), int):
        raise ValueError(f"{where} needs a profile {{id: str, version: int}}")

    allowed = {"schema", "profile", "thresholds", "priorities", "emergent_question_policy",
               "staleness_policy", "prompt_defaults", "provenance"}
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown fields: {unknown}")

    thresholds = block.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError(f"{where} needs a thresholds object")
    missing = sorted(set(_ALL_THRESHOLDS) - set(thresholds))
    if missing:
        raise ValueError(f"{where}.thresholds is missing frozen values for {missing}; a frozen "
                         "program never acquires read-time defaults — recompile with "
                         "`isar drive --goal ... --compile-only`")
    unknown = sorted(set(thresholds) - set(_ALL_THRESHOLDS))
    if unknown:
        raise ValueError(f"{where}.thresholds has unknown fields: {unknown}")
    for name, value in thresholds.items():
        _check_value(name, value, f"{where}.thresholds")

    priorities = block.get("priorities")
    if not isinstance(priorities, Mapping) or set(priorities) != set(FAMILY_PRIORITIES):
        raise ValueError(f"{where}.priorities must name exactly the families "
                         f"{sorted(FAMILY_PRIORITIES)}")
    for family, rank in priorities.items():
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError(f"{where}.priorities[{family!r}] must be an integer")

    emergent = block.get("emergent_question_policy")
    if emergent not in SUPPORTED_EMERGENT_POLICIES:
        raise ValueError(f"{where}: emergent_question_policy {emergent!r} is not supported by "
                         f"this build (supported: {SUPPORTED_EMERGENT_POLICIES}); refusing "
                         "rather than accepting and ignoring it")
    staleness = block.get("staleness_policy")
    if not isinstance(staleness, Mapping) or set(staleness) != {"mode", "lag"}:
        raise ValueError(f"{where}.staleness_policy must be {{mode, lag}}")
    if staleness["mode"] not in SUPPORTED_STALENESS_MODES:
        raise ValueError(f"{where}: staleness mode {staleness['mode']!r} is not supported by "
                         f"this build (supported: {SUPPORTED_STALENESS_MODES}); refusing "
                         "rather than accepting and ignoring it")
    if staleness["lag"] != 0:
        raise ValueError(f"{where}: synchronous staleness means lag 0, got "
                         f"{staleness['lag']!r}")

    prompts = block.get("prompt_defaults")
    if not isinstance(prompts, Mapping) or set(prompts) != set(PROMPT_DEFAULTS):
        raise ValueError(f"{where}.prompt_defaults must name exactly {sorted(PROMPT_DEFAULTS)}")
    if prompts["unresolvable_closure"] not in SUPPORTED_UNRESOLVABLE_CLOSURE:
        raise ValueError(f"{where}: unresolvable_closure {prompts['unresolvable_closure']!r} "
                         f"is not supported (supported: {SUPPORTED_UNRESOLVABLE_CLOSURE})")
    if prompts["timeout_behaviour"] not in SUPPORTED_TIMEOUT_BEHAVIOUR:
        raise ValueError(f"{where}: timeout_behaviour {prompts['timeout_behaviour']!r} is not "
                         f"supported (supported: {SUPPORTED_TIMEOUT_BEHAVIOUR})")

    provenance = block.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != set(_ALL_THRESHOLDS):
        raise ValueError(f"{where}.provenance must record every threshold field")
    for name, record in provenance.items():
        if not isinstance(record, Mapping) or set(record) != {"source", "authority"}:
            raise ValueError(f"{where}.provenance[{name!r}] must be {{source, authority}}")
        if record["source"] not in ("user", "card", "default"):
            raise ValueError(f"{where}.provenance[{name!r}].source must be user|card|default")
        if record["authority"] != _authority_of(name):
            raise ValueError(f"{where}.provenance[{name!r}].authority must be "
                             f"{_authority_of(name)!r}, got {record['authority']!r} — the "
                             "authority posture is the profile's, not the document's")

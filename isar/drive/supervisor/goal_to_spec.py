"""Goal → campaign program (LLM_HARNESS_DESIGN §2.1, ruling H1).

Both entry points produce an :class:`~isar.schema.campaign_program.CampaignProgram` — the only
input contract the driver accepts:

- :func:`goal_to_program` — the LLM supervisor path. Translate a natural-language command ("map the
  acoustic phonon dispersion from A to B, resolve ω₀ to ±0.5 meV, overnight", or "scan the region
  from (2,0,0) to (3,0,0) up to 10 meV, map out all the phenomena of interest") into a one-step
  program via the shared :class:`isar.reduce.llm_client.Dispatcher`, with the program schema
  enforced as structured output. The supervisor LLM is CLOUD via LLMM pinned routing (port 8766);
  LLMM must already be running — this never auto-launches it. On a non-conforming reply it retries
  the repair prompt once and then **refuses honestly** (:class:`GoalParseError`) rather than
  inventing a plan. If LLMM is unreachable the dispatch returns an error and the refusal names it.
- :func:`program_from_json` — the LLM-free path. Load a JSON program file. This is what
  ``isar drive --program`` uses, so a campaign runs with no LLM.

P1 still holds: the LLM's output is *frozen into config* here — a targeted step is compiled to a
full :class:`~isar.drive.objective.ObjectiveSpec` before anything deterministic runs, and no LLM
sits in the runtime path of a number.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import isar.reduce.llm_client as _llm  # boundary carve-out: the shared LLMM HTTP plumbing only
import isar.drive.objective as _obj
from isar.drive.objective import ObjectiveSpec, resolve_hypothesis_snapshot
from isar.schema.campaign_program import (
    PROGRAM_SCHEMA,
    STEP_KINDS,
    CampaignProgram,
    CampaignStep,
    DiscoverAndMapSpec,
    targeted_program,
)
import isar.schema.question_policy as _qpolicy

Dispatcher = _llm.Dispatcher
LLMMClient = _llm.LLMMClient

# The supervisor is addressed by LLMM *role*, not by a model pin: the role store owns which model
# answers, its fallback chain, queue and timeout. Swapping the supervisor model or adding a backup
# is therefore an LLMM-side edit, never an ISAR release — and the role's fallback chain is what
# retires the hand-pinning workaround in TODO DRV-1 (a 503 from the primary no longer needs the
# operator). `LLMM_SUPERVISOR_MODEL` remains as a debugging escape hatch: setting it bypasses the
# role store via role="passthrough" to hit one exact ref.
DEFAULT_SUPERVISOR_ROLE = "scientific_interpreter"
DEFAULT_ENDPOINT = "http://127.0.0.1:8766"

_Q_ARRAY = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}

LLM_GOAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tolerance_meV"],
    "properties": {
        "path": {
            "type": "object",
            "additionalProperties": False,
            "required": ["h_start", "h_end", "k", "l"],
            "properties": {
                "h_start": {"type": "number"},
                "h_end": {"type": "number"},
                "k": {"type": "number"},
                "l": {"type": "number"},
            },
        },
        "step_h": {"type": "number"},
        "q_start": _Q_ARRAY,
        "q_end": _Q_ARRAY,
        "step_rlu": {"type": "number"},
        "tolerance_meV": {"type": "number"},
        "budget_hours": {"type": "number"},
        "budget_neutrons": {"type": "number"},
        "notes": {"type": "string"},
        "target_branch": {"type": "string", "enum": ["acoustic", "optic"]},
        "target_label": {"type": "string"},
        "target_selector": {"type": "string",
                            "enum": ["lowest_energy", "highest_energy", "nearest_energy",
                                     "tracked_branch"]},
        "target_energy_meV": {"type": "number"},
        "energy_search_meV": {"type": "array", "items": {"type": "number"},
                              "minItems": 2, "maxItems": 2},
        # The sample temperature the campaign is measuring at, which selects the belief slice.
        # Scientific context the supervisor may legitimately state; it grants no scanning authority.
        "target_temperature_K": {"type": "number"},
        "target_zc_energy_meV": {"type": "number"},
        "target_zc_energy_sigma_meV": {"type": "number"},
    },
}

# File specs are an additive superset.  Operator acquisition policy is deliberately absent from the
# LLM schema, so the supervisor can express scientific intent but cannot invent beam-time limits.
FILE_SPEC_SCHEMA = copy.deepcopy(LLM_GOAL_SCHEMA)
FILE_SPEC_SCHEMA["properties"]["analysis_limit_seconds"] = {"type": ["number", "null"]}
# File-only, deliberately: ``identity_search_meV`` authorises identity scans OUTSIDE the declared
# target band, which is beam-time-affecting acquisition policy rather than scientific intent. Same
# boundary as the budget fields — the supervisor can say what to look for, not how much of someone
# else's beam time to spend looking, nor where it may look beyond what was declared.
FILE_SPEC_SCHEMA["properties"]["identity_search_meV"] = {
    "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
FILE_SPEC_SCHEMA["properties"]["steering"] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "classification_weight": {"type": "number"},
        "differentiation_tolerance_meV": {"type": "number"},
        "assignment_entropy_stop_nats": {"type": "number"},
        "minimum_information_per_cost": {"type": "number"},
        "minimum_target_support_points": {"type": "integer"},
        "max_scan_points": {"type": "integer"},
        "absent_signal_widen_step": {"type": "number"},
        "absent_signal_widen_steps": {"type": "integer"},
        "quadrature_order": {"type": "integer"},
        "acquisition_estimator_version": {"type": "string"},
        "mismatch_z_threshold": {"type": "number"},
        "quicklook_ncount": {"type": "integer"},
        "production_batch_ncount": {"type": "integer"},
        "maximum_production_batches_per_q": {"type": "integer"},
        "dynamic_ncount": {"type": "boolean"},
        "max_ncount_per_scan": {"type": "integer"},
        "ncount_growth_cap": {"type": "number"},
        "tolerance_margin": {"type": "number"},
        "interval_floor_window": {"type": "integer"},
        "interval_floor_min_improvement": {"type": "number"},
        "interval_floor_min_exposure_ratio": {"type": "number"},
    },
}
# Compatibility alias for callers/tests that imported the original public name.
GOAL_SCHEMA = LLM_GOAL_SCHEMA

# --------------------------------------------------------------------------- discovery step schema
# The discovery intent the supervisor may compile. ``intrinsic_omega_domain_meV`` is the INTRINSIC
# excitation-energy domain (|ℏω| ≥ 0) — never a signed measurement window. "up to 10 meV" is a
# domain of [0, 10], and whether the instrument counts the Stokes side, the anti-Stokes side, or
# both is decided at acquisition against detailed balance, not frozen into the program.
LLM_DISCOVER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["q_start", "q_end", "intrinsic_omega_domain_meV"],
    "properties": {
        "q_start": _Q_ARRAY,
        "q_end": _Q_ARRAY,
        "maximum_initial_spacing_rlu": {"type": "number"},
        "intrinsic_omega_domain_meV": {"type": "array", "items": {"type": "number"},
                                       "minItems": 2, "maxItems": 2},
        "catalogue_profile": {"type": "string"},
        "deliverables": {"type": "array", "items": {"type": "object"}},
        "include_unknown": {"type": "boolean"},
        "notes": {"type": "string"},
    },
}

#: One step, tagged by kind — what the supervisor returns.
LLM_PROGRAM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(STEP_KINDS)},
        "targeted": LLM_GOAL_SCHEMA,
        "discover_and_map": LLM_DISCOVER_SCHEMA,
    },
}

#: Compiler defaults for a discovery step the command left unstated. Owned by the question
#: policy profile since SCENE-4 PR 4.1 (one home per question-layer number); re-exported here
#: so no call site churns.
DEFAULT_DISCOVER_SPACING_RLU = _qpolicy.DEFAULT_DISCOVER_SPACING_RLU
DEFAULT_CATALOGUE_PROFILE = _qpolicy.DEFAULT_CATALOGUE_PROFILE
DEFAULT_DELIVERABLES = _qpolicy.DEFAULT_DELIVERABLES

_EXAMPLE = {
    "path": {"h_start": 1.0, "h_end": 2.0, "k": 0.0, "l": 0.0},
    "step_h": 0.1,
    "tolerance_meV": 0.05,
    "budget_hours": 8,
    "notes": "acoustic branch (1,0,0)->(2,0,0)",
}

# Canonical compiler defaults for the energy search band when the goal states none. These are the
# single source for both the compile fallback and the LLM prompt guidance — the example
# example campaign program ships the same acoustic band, so goal-path and program-path campaigns agree.
DEFAULT_ENERGY_SEARCH_ACOUSTIC_MEV = (-9.0, 9.0)
DEFAULT_ENERGY_SEARCH_OPTIC_MEV = (-2.0, 12.0)

_DISCOVER_EXAMPLE = {
    "kind": "discover_and_map",
    "discover_and_map": {
        "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0],
        "maximum_initial_spacing_rlu": 0.1,
        "intrinsic_omega_domain_meV": [0.0, 10.0],
        "notes": "map every phenomenon of interest along (2,0,0)->(3,0,0)",
    },
}

SYSTEM = (
    "You translate a neutron triple-axis measurement command into a one-step machine campaign "
    "program for an autonomous driver. Choose kind='targeted' when the user names a specific "
    "excitation to measure or refine (a branch, a mode, an energy) and put the objective under the "
    "'targeted' key. Choose kind='discover_and_map' when the user asks what is there rather than "
    "for a named excitation — surveying, mapping everything of interest, finding the phenomena in a "
    "region — and put the discovery intent under the 'discover_and_map' key. A discovery intent is "
    "q_start [h,k,l], q_end [h,k,l], an optional maximum_initial_spacing_rlu (default 0.05), and "
    "intrinsic_omega_domain_meV [low, high] — the INTRINSIC excitation energy |hbar*omega|, which "
    "is never negative: 'up to 10 meV' is [0, 10]. Do NOT emit a signed measurement window there; "
    "whether the anti-Stokes side is counted is decided later at acquisition.\n\n"
    "For a targeted objective: express a general reciprocal-space line as q_start [h,k,l], "
    "q_end [h,k,l], and "
    "step_rlu (the maximum point spacing, default 0.05 if unstated). The legacy path/step_h shape is "
    "also valid for a segment purely along H. Extract tolerance_meV (the per-point target on the "
    "phonon energy ω₀; map '±X meV' to X; when the user states none, use the default 0.05 — the "
    "centre of a resolved peak is routinely fixed to that). Include a measurement budget as EITHER budget_hours "
    "OR budget_neutrons only when the user states one; analysis time is a separate guardrail. "
    "Set target_branch to optic when the user asks for an optic or transverse optic phonon, otherwise "
    "acoustic; retain their wording in target_label. A request to map a named branch along a path "
    "uses target_selector='tracked_branch' unless the user explicitly asks for the lowest, highest, "
    "or nearest-energy feature independently at every q. An energy stated as the identity of a branch (for "
    "example 'the optic phonon around 6 meV') is a zone-centre anchor: put it in "
    "target_zc_energy_meV and use target_selector='tracked_branch'; do not set target_energy_meV. "
    "Only when the user explicitly requests the feature nearest an energy at every q or throughout "
    "the path, put that energy in target_energy_meV and use target_selector='nearest_energy'. If the user explicitly asks for the "
    "highest optic mode use target_selector='highest_energy'. If the user states a zone-centre or gap energy, "
    "put it in target_zc_energy_meV and put an explicitly stated uncertainty in "
    "target_zc_energy_sigma_meV. For optic goals use energy_search_meV [-2,12] and for acoustic "
    "goals [-9,9] unless the user provides a range. Optionally a short notes string. Return ONLY a JSON object matching this schema — use EXACTLY "
    "these keys, no extra keys, and never enumerate grid points:\n"
    + json.dumps(LLM_PROGRAM_SCHEMA)
    + "\n\nExample. Command: 'map the acoustic phonon from (1,0,0) to (2,0,0) to +-0.5 meV, steps "
    "of 0.1, overnight'. Reply:\n" + json.dumps({"kind": "targeted", "targeted": _EXAMPLE})
    + "\n\nExample. Command: 'Scan the region from (2,0,0) to (3,0,0) up to 10meV, map out all the "
    "phenomena of interest.' Reply:\n" + json.dumps(_DISCOVER_EXAMPLE)
)

_REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid against the schema. Return ONLY a JSON object matching the "
    "schema — no prose, no markdown fences."
)

DEFAULTS: dict[str, Any] = {"step_h": 0.05, "tier": "deterministic"}

# The canonical, tier-aware budget defaults. Defined next to the dataclass they configure
# (:mod:`isar.drive.objective`) and re-exported here, which is the import site every other surface —
# the CLI, the GUI campaign form, example goal resources — should use. There is exactly one copy of
# these numbers in the tree; do not restate them.
DEFAULT_BUDGET_HOURS_BY_TIER = _obj.DEFAULT_BUDGET_HOURS_BY_TIER
DEFAULT_BUDGET_HOURS = _obj.DEFAULT_BUDGET_HOURS
DEFAULT_BUDGET_NEUTRONS = _obj.DEFAULT_BUDGET_NEUTRONS
default_budget_hours = _obj.default_budget_hours


class GoalParseError(ValueError):
    """The goal could not be turned into a valid ObjectiveSpec (LLM refusal / non-conforming output)."""


# --------------------------------------------------------------------------- LLM path
def goal_to_program(goal: str, *, dispatcher: Dispatcher | None = None,
                    defaults: dict | None = None,
                    card_hypotheses: dict[str, Any] | None = None,
                    card_question_guidelines: dict[str, Any] | None = None,
                    user_policy_ask: dict[str, Any] | None = None,
                    endpoint: str | None = None, model_pin: str | None = None,
                    temperature: float = 0.1, max_tokens: int = 800) -> CampaignProgram:
    """Compile ``goal`` into a one-step :class:`CampaignProgram` via the LLM supervisor.

    A ``targeted`` reply is compiled all the way to a frozen :class:`ObjectiveSpec` here (P1: the
    LLM's output becomes config before any deterministic stage runs); a ``discover_and_map`` reply
    is validated and frozen as a :class:`DiscoverAndMapSpec`.

    Args:
        goal: the natural-language measurement command; kept verbatim as the program's
            ``source_text``.
        dispatcher: an injected :class:`Dispatcher` (tests pass a fake). When ``None`` an
            :class:`LLMMClient` is built from ``endpoint`` and the supervisor role
            (env-overridable).
        defaults: overrides for :data:`DEFAULTS` (``step_h``, ``tier``).
        endpoint: LLMM endpoint; default from ``LLMM_SUPERVISOR_ENDPOINT`` or the module constant.
        model_pin: escape hatch — an exact model ref dispatched via ``role="passthrough"``,
            bypassing the role store. Defaults to ``LLMM_SUPERVISOR_MODEL`` if set, else unset and
            the :data:`DEFAULT_SUPERVISOR_ROLE` (or ``LLMM_SUPERVISOR_ROLE``) chooses the model.
    """
    merged = {**DEFAULTS, **(defaults or {})}
    if dispatcher is None:
        ep = endpoint or os.environ.get("LLMM_SUPERVISOR_ENDPOINT", DEFAULT_ENDPOINT)
        pin = model_pin or os.environ.get("LLMM_SUPERVISOR_MODEL") or None
        role = None if pin else os.environ.get("LLMM_SUPERVISOR_ROLE", DEFAULT_SUPERVISOR_ROLE)
        dispatcher = LLMMClient(ep, pin, role=role, node="drive.supervisor.goal_to_program")

    res = dispatcher.dispatch(system=SYSTEM, user=goal, schema=LLM_PROGRAM_SCHEMA,
                              temperature=temperature, max_tokens=max_tokens)
    data = _extract(res)
    if data is None:
        res = dispatcher.dispatch(system=SYSTEM, user=goal + _REPAIR_SUFFIX,
                                  schema=LLM_PROGRAM_SCHEMA,
                                  temperature=temperature, max_tokens=max_tokens)
        data = _extract(res)
    if data is None:
        detail = f" ({res.error})" if getattr(res, "error", None) else ""
        raw = (res.output or "")[:200]
        raise GoalParseError(
            f"goal→{PROGRAM_SCHEMA} failed: the supervisor LLM did not return a schema-valid "
            "campaign program" + detail + (f"; raw: {raw!r}" if raw else "")
            + ". Use `isar drive --program <file.json>` for an LLM-free run.")
    if data["kind"] == "discover_and_map":
        return CampaignProgram(
            steps=(CampaignStep(id="discover-1", kind="discover_and_map",
                                payload=_discover_spec(
                                    data["discover_and_map"],
                                    card_question_guidelines=card_question_guidelines,
                                    user_policy_ask=user_policy_ask)),),
            source_text=goal)
    spec = _build_spec(data["targeted"], merged, card_hypotheses=card_hypotheses,
)
    return targeted_program(spec.to_dict(), source_text=goal)


def _discover_spec(data: dict, *,
                   card_question_guidelines: dict[str, Any] | None = None,
                   user_policy_ask: dict[str, Any] | None = None) -> DiscoverAndMapSpec:
    """Freeze a supervisor discovery reply, filling only the compiler's shipped defaults.

    A default applies when the field is absent or JSON-null, never when it is merely falsy: an
    explicit spacing of 0, an explicit empty catalogue reference, and an explicit empty
    deliverables list are *wrong* declarations, not omissions, and rewriting them to the shipped
    defaults would silently run a campaign the supervisor did not ask for. Every present value is
    passed through so :class:`DiscoverAndMapSpec` validates it and refuses the invalid ones by name.

    The frozen question-policy block is resolved HERE (user ask > card guidelines > named
    defaults, :func:`isar.schema.question_policy.resolve_policy`) — compile time is the only
    moment policy may be decided, because the ``/2`` reader refuses to default anything. The
    card lane is wired but EMPTY in 4.1 (no ``[questions]`` table exists in
    ``card.policy.toml`` yet; owner names its keys later), and no NL-goal field maps onto a
    policy override yet, so production resolves pure named defaults; both override lanes are
    real and tested through this signature.
    """
    def declared(key, default):
        value = data.get(key)
        return default if value is None else value

    return DiscoverAndMapSpec(
        q_start=data["q_start"], q_end=data["q_end"],
        maximum_initial_spacing_rlu=declared("maximum_initial_spacing_rlu",
                                             DEFAULT_DISCOVER_SPACING_RLU),
        intrinsic_omega_domain_meV=data["intrinsic_omega_domain_meV"],
        catalogue_profile=declared("catalogue_profile", DEFAULT_CATALOGUE_PROFILE),
        deliverables=declared("deliverables", DEFAULT_DELIVERABLES),
        # Default only when absent/null (the declared() rule): bool() laundered "false" into
        # True and 0 into False, so an invalid explicit declaration never reached the
        # schema's boolean check (PR #31 finding L).
        include_unknown=declared("include_unknown", True),
        resolved_policy=_qpolicy.resolve_policy(user_policy_ask, card_question_guidelines))


# --------------------------------------------------------------------------- LLM-free path
def spec_from_dict(d: dict, *, defaults: dict | None = None, where: str = "spec",
                   card_hypotheses: dict[str, Any] | None = None) -> ObjectiveSpec:
    """Build an :class:`ObjectiveSpec` from an already-parsed dict — no LLM, no file I/O.

    Accepts either the goal schema (``{"path": {"h_start", …}, "step_h", "tolerance_meV", …}``) or a
    full ``ObjectiveSpec.to_dict()`` (``path`` is a list of triples). This is the shared dict path that
    both a targeted step of :func:`program_from_json` (file → dict) and the ``isar drive
    --resume-latest`` path (frozen campaign_start objective → dict) route through, so a resume never
    re-compiles a goal via the LLM.

    ``where`` is a short label (a file path, or ``"resume"``) used in error messages only.
    """
    if not isinstance(d, dict):
        raise GoalParseError(f"{where}: spec must be an object, got {type(d).__name__}")
    # A payload that names any objective schema is already frozen content: hand it to
    # ``ObjectiveSpec.from_dict``, whose refusal names the current contract for a retired one.
    if d.get("schema") is not None or isinstance(d.get("path"), list):
        return ObjectiveSpec.from_dict(d)         # already a full ObjectiveSpec
    if not _conforms(d, FILE_SPEC_SCHEMA):
        raise GoalParseError(f"{where}: not a valid goal spec (needs path{{h_start,h_end,k,l}}, step_h, "
                             "and tolerance_meV; a budget is optional and defaults per tier)")
    merged = {**DEFAULTS, **(defaults or {})}
    return _build_spec(
        d,
        merged,
        card_hypotheses=card_hypotheses,
    )


def program_from_json(path: str | Path) -> CampaignProgram:
    """Read an ``isar.campaign-program/1`` file — no LLM, no card, no compilation.

    Step payloads are validated for *identity* only (:mod:`isar.schema.campaign_program`); a
    targeted step is compiled with :func:`spec_from_dict` by the caller that knows which card
    policy, tier and overrides apply. A file naming a retired contract is refused by name, never
    adapted (ruling H1).
    """
    p = Path(path)
    return CampaignProgram.from_dict(
        json.loads(p.read_text(encoding="utf-8")), where=str(p))


def make_program(spec: ObjectiveSpec, *, source: dict,
                 source_text: str | None = None) -> CampaignProgram:
    """Freeze a compiled objective as a one-step targeted program with its source fingerprint."""
    return targeted_program(spec.to_dict(), source_text=source_text, source=dict(source))


# --------------------------------------------------------------------------- shared
def _build_spec(data: dict, merged: dict, *,
                card_hypotheses: dict[str, Any] | None = None) -> ObjectiveSpec:
    tier = str(merged.get("tier", "deterministic"))
    budget = _budget(data, tier=tier)
    branch = str(data.get("target_branch", "acoustic"))
    label = str(data.get("target_label") or branch)
    target_energy = data.get("target_energy_meV")
    selector = data.get("target_selector")
    if selector is None:
        selector = ("nearest_energy" if target_energy is not None else "tracked_branch")
    if selector == "nearest_energy" and target_energy is None:
        raise GoalParseError("nearest_energy target_selector requires target_energy_meV")
    # The tracked selector identifies a dispersing branch.  Accept target_energy_meV as an input
    # alias for its zone-centre anchor, but never carry that value into the global nearest-energy
    # field consumed at every q.
    tracked_anchor_energy = None
    if selector == "tracked_branch" and target_energy is not None:
        tracked_anchor_energy = target_energy
        target_energy = None
    energy = data.get("energy_search_meV")
    resolved_energy = tuple(energy) if energy is not None else (
        DEFAULT_ENERGY_SEARCH_OPTIC_MEV if branch == "optic"
        else DEFAULT_ENERGY_SEARCH_ACOUSTIC_MEV)
    # Record who chose the band here, where the answer is still known. Downstream, a band that equals
    # the shipped default is indistinguishable from one the scientist asked for exactly — and the
    # difference decides whether identity work may be widened past it.
    target_search_source = "operator" if energy is not None else "compiler"
    identity_energy = data.get("identity_search_meV")
    identity_search_source = "operator" if identity_energy is not None else None
    goal_override = None
    target_zc_energy = data.get("target_zc_energy_meV", tracked_anchor_energy)
    if (data.get("target_zc_energy_sigma_meV") is not None
            and target_zc_energy is None):
        raise GoalParseError(
            "target_zc_energy_sigma_meV requires target_zc_energy_meV")
    if target_zc_energy is not None:
        shipped_sigma = max((float(resolved_energy[1]) - max(float(resolved_energy[0]), 0.0)) / 4.0,
                            1e-6)
        explicit_sigma = data.get("target_zc_energy_sigma_meV")
        if explicit_sigma is not None and float(explicit_sigma) <= 0.0:
            raise GoalParseError("target_zc_energy_sigma_meV must be positive")
        goal_override = {"zc_energy": {
            "mean": float(target_zc_energy),
            "sigma": float(explicit_sigma if explicit_sigma is not None else shipped_sigma),
        }}
    frozen = resolve_hypothesis_snapshot(
        branch, resolved_energy, card_hypotheses=card_hypotheses, goal_override=goal_override,
        steering_override=data.get("steering"))
    common = dict(tolerance_meV=float(data["tolerance_meV"]), budget=budget,
                  tier=tier,
                  analysis_limit_seconds=(data.get("analysis_limit_seconds", 3600.0)
                                          if tier == "deterministic" else
                                          data.get("analysis_limit_seconds")),
                  name=str(data.get("notes", "")),
                  meta={"source": "goal_to_spec", "raw": data}, target_branch=branch,
                  target_label=label, target_selector=str(selector),
                  target_energy_meV=(float(target_energy) if target_energy is not None else None),
                  energy_search_meV=resolved_energy,
                  identity_search_meV=(tuple(identity_energy)
                                       if identity_energy is not None else None),
                  target_search_source=target_search_source,
                  identity_search_source=identity_search_source,
                  target_temperature_K=data.get("target_temperature_K"), **frozen)
    if "q_start" in data or "q_end" in data:
        if "q_start" not in data or "q_end" not in data:
            raise GoalParseError("goal spec needs both q_start and q_end")
        if list(data["q_start"]) == list(data["q_end"]):
            raise GoalParseError("natural-language path is degenerate; q_start and q_end are identical")
        objective = ObjectiveSpec.segment(
            q_start=data["q_start"], q_end=data["q_end"],
            step_rlu=float(data.get("step_rlu") or 0.05), **common)
    else:
        if "path" not in data:
            raise GoalParseError("goal spec needs q_start/q_end or legacy path")
        p = data["path"]
        objective = ObjectiveSpec.along_h(
            h_start=float(p["h_start"]), h_end=float(p["h_end"]),
            k=float(p.get("k", 0.0)), l=float(p.get("l", 0.0)),
            step=float(data.get("step_h") or merged.get("step_h") or 0.05), **common)
    return objective


def _budget(data: dict, *, tier: str) -> dict[str, Any] | None:
    """The campaign budget for this goal: what it declared, else the canonical tier default.

    A declared neutron budget wins; otherwise the budget is counting-time, and an undeclared one
    falls back to :func:`default_budget_hours`. There is deliberately no "no budget at all" outcome
    here: a goal that states nothing gets the documented tier default rather than an unbounded run.
    """
    neutrons = data.get("budget_neutrons", DEFAULT_BUDGET_NEUTRONS)
    if neutrons:
        return {"kind": "neutrons", "amount": float(neutrons)}
    hours = data.get("budget_hours") or default_budget_hours(tier)
    return {"kind": "counting_seconds", "amount": float(hours) * 3600.0}


def _extract(res) -> dict | None:
    """The schema-valid program object from a dispatch result, or None if unrecoverable."""
    for candidate in (getattr(res, "structured", None),
                      _loads_lenient(getattr(res, "output", None) or "")):
        if candidate is not None and _program_conforms(candidate):
            return candidate
    return None


def _program_conforms(value) -> bool:
    """Schema-valid AND tagged consistently: ``kind`` must name the branch that is present.

    ``_conforms`` has no oneOf, so the tag/branch agreement is checked here. A reply tagged
    ``targeted`` whose only payload is a discovery region is exactly the confusion that would
    otherwise compile a survey into a one-point objective.
    """
    if not _conforms(value, LLM_PROGRAM_SCHEMA):
        return False
    kind = value["kind"]
    other = next(k for k in STEP_KINDS if k != kind)
    return kind in value and other not in value


def _loads_lenient(text: str):
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.lstrip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    lo, hi = s.find("{"), s.rfind("}")
    if 0 <= lo < hi:
        try:
            return json.loads(s[lo:hi + 1])
        except (ValueError, TypeError):
            return None
    return None


def _conforms(value, schema) -> bool:
    """A conservative, dependency-free check for the JSON-Schema subset GOAL_SCHEMA uses."""
    t = schema.get("type")
    if t is None:
        return not schema
    if isinstance(t, list):
        return any(_conforms(value, {**schema, "type": option}) for option in t)
    if t == "null":
        return value is None
    if t == "object":
        if not isinstance(value, dict):
            return False
        props = schema.get("properties", {})
        if any(req not in value for req in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            return False
        return all(_conforms(v, props[k]) for k, v in value.items() if k in props)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "string":
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    if t == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        return all(_conforms(item, schema.get("items", {})) for item in value)
    return False

"""Goal → objective spec (LLM_HARNESS_DESIGN §2.1).

Two entry points:

- :func:`goal_to_spec` — the LLM supervisor path. Translate a natural-language goal ("map the acoustic
  phonon dispersion from A to B, resolve ω₀ to ±0.5 meV, overnight") into a machine
  :class:`~isar.drive.objective.ObjectiveSpec` via the shared
  :class:`isar.reduce.llm_client.Dispatcher`, with the goal schema enforced as structured output. The
  supervisor LLM is CLOUD via LLMM pinned routing (port 8766); LLMM must already be running — this
  never auto-launches it. On a non-conforming reply it retries the repair prompt once and then **refuses
  honestly** (:class:`GoalParseError`) rather than inventing a plan. If LLMM is unreachable the dispatch
  returns an error and the refusal names it.
- :func:`spec_from_json` — the LLM-free path. Load a JSON spec file (the goal schema, or a full
  ``ObjectiveSpec.to_dict()``). This is what ``isar drive --spec`` uses, so a campaign runs with no LLM.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from typing import Any

import isar.reduce.llm_client as _llm  # boundary carve-out: the shared LLMM HTTP plumbing only
import isar.drive.objective as _obj
from isar.drive.objective import (
    ObjectiveSpec,
    current_objective_comparison_scope,
    resolve_hypothesis_snapshot,
)
from isar.eig.policy import compile_card_eig_policy_set
from isar.schema.eig_policy import ScanWindowEIGConfig
from isar.schema.hypotheses import content_fingerprint

Dispatcher = _llm.Dispatcher
LLMMClient = _llm.LLMMClient

# Cloud pin (configurable via LLMM_SUPERVISOR_MODEL). GLM 5.2 replaced gpt-oss-120b, whose NanoGPT
# serviceability had collapsed (~23% success, frequent dropped dispatches at goal-parse time). GLM 5.2
# reports >95% health, supports structured output, and dispatches in ~1.6s — right for a one-shot
# schema-enforced goal parse. `thinking` is pinned False by the client, so the non-thinking base ref.
DEFAULT_SUPERVISOR_MODEL = "nanogpt/zai-org/glm-5.2"
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
        "maximum_bridge_steps": {"type": "integer"},
        "dynamic_ncount": {"type": "boolean"},
        "max_ncount_per_scan": {"type": "integer"},
        "ncount_growth_cap": {"type": "number"},
        "tolerance_margin": {"type": "number"},
        "interval_floor_window": {"type": "integer"},
        "interval_floor_min_improvement": {"type": "number"},
        "interval_floor_min_exposure_ratio": {"type": "number"},
        "scan_window_eig_mode": {"type": "string"},
    },
}
# Compatibility alias for callers/tests that imported the original public name.
GOAL_SCHEMA = LLM_GOAL_SCHEMA

_EXAMPLE = {
    "path": {"h_start": 1.0, "h_end": 2.0, "k": 0.0, "l": 0.0},
    "step_h": 0.1,
    "tolerance_meV": 0.5,
    "budget_hours": 8,
    "notes": "acoustic branch (1,0,0)->(2,0,0)",
}

SYSTEM = (
    "You translate a neutron triple-axis measurement goal into a machine objective for an autonomous "
    "driver. Express a general reciprocal-space line as q_start [h,k,l], q_end [h,k,l], and "
    "step_rlu (the maximum point spacing, default 0.05 if unstated). The legacy path/step_h shape is "
    "also valid for a segment purely along H. Extract tolerance_meV (the per-point target on the "
    "phonon energy ω₀; map '±X meV' to X). Include a measurement budget as EITHER budget_hours "
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
    "target_zc_energy_sigma_meV. For optic goals use energy_search_meV [-2,12] unless the user "
    "provides a range. Optionally a short notes string. Return ONLY a JSON object matching this schema — use EXACTLY "
    "these keys, no wrapper object, no extra keys, and never enumerate grid points:\n"
    + json.dumps(LLM_GOAL_SCHEMA)
    + "\n\nExample. Goal: 'map the acoustic phonon from (1,0,0) to (2,0,0) to +-0.5 meV, steps of 0.1, "
    "overnight'. Reply:\n" + json.dumps(_EXAMPLE)
)

_REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid against the schema. Return ONLY a JSON object matching the "
    "schema — no prose, no markdown fences."
)

DEFAULTS: dict[str, Any] = {"step_h": 0.05, "tier": "deterministic"}
ENVELOPE_SCHEMA = "isar.objective-envelope/2"

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
def goal_to_spec(goal: str, *, dispatcher: Dispatcher | None = None, defaults: dict | None = None,
                 card_hypotheses: dict[str, Any] | None = None,
                 card_eig_policy_set: dict[str, Any] | None = None,
                 endpoint: str | None = None, model_pin: str | None = None,
                 temperature: float = 0.1, max_tokens: int = 800) -> ObjectiveSpec:
    """Parse ``goal`` into an :class:`ObjectiveSpec` via the LLM supervisor.

    Args:
        goal: the natural-language measurement goal.
        dispatcher: an injected :class:`Dispatcher` (tests pass a fake). When ``None`` a pinned
            :class:`LLMMClient` is built from ``endpoint`` / ``model_pin`` (env-overridable).
        defaults: overrides for :data:`DEFAULTS` (``step_h``, ``tier``).
        endpoint, model_pin: LLMM endpoint and pinned model ref; default from
            ``LLMM_SUPERVISOR_ENDPOINT`` / ``LLMM_SUPERVISOR_MODEL`` or the module constants.
    """
    merged = {**DEFAULTS, **(defaults or {})}
    if dispatcher is None:
        ep = endpoint or os.environ.get("LLMM_SUPERVISOR_ENDPOINT", DEFAULT_ENDPOINT)
        pin = model_pin or os.environ.get("LLMM_SUPERVISOR_MODEL", DEFAULT_SUPERVISOR_MODEL)
        dispatcher = LLMMClient(ep, pin)

    res = dispatcher.dispatch(system=SYSTEM, user=goal, schema=LLM_GOAL_SCHEMA,
                              temperature=temperature, max_tokens=max_tokens)
    data = _extract(res)
    if data is None:
        res = dispatcher.dispatch(system=SYSTEM, user=goal + _REPAIR_SUFFIX, schema=LLM_GOAL_SCHEMA,
                                  temperature=temperature, max_tokens=max_tokens)
        data = _extract(res)
    if data is None:
        detail = f" ({res.error})" if getattr(res, "error", None) else ""
        raw = (res.output or "")[:200]
        raise GoalParseError(
            "goal→ObjectiveSpec failed: the supervisor LLM did not return a schema-valid objective"
            + detail + (f"; raw: {raw!r}" if raw else "")
            + ". Use `isar drive --spec <file.json>` for an LLM-free run.")
    return _build_spec(
        data,
        merged,
        card_hypotheses=card_hypotheses,
        card_eig_policy_set=card_eig_policy_set,
    )


# --------------------------------------------------------------------------- LLM-free path
def spec_from_dict(d: dict, *, defaults: dict | None = None, where: str = "spec",
                   card_hypotheses: dict[str, Any] | None = None,
                   card_eig_policy_set: dict[str, Any] | None = None) -> ObjectiveSpec:
    """Build an :class:`ObjectiveSpec` from an already-parsed dict — no LLM, no file I/O.

    Accepts either the goal schema (``{"path": {"h_start", …}, "step_h", "tolerance_meV", …}``) or a
    full ``ObjectiveSpec.to_dict()`` (``path`` is a list of triples). This is the shared dict path that
    both :func:`spec_from_json` (file → dict) and the ``isar drive --resume-latest`` path (frozen
    campaign_start objective → dict) route through, so a resume never re-compiles a goal via the LLM.

    ``where`` is a short label (a file path, or ``"resume"``) used in error messages only.
    """
    if not isinstance(d, dict):
        raise GoalParseError(f"{where}: spec must be an object, got {type(d).__name__}")
    if isinstance(d.get("path"), list):
        return ObjectiveSpec.from_dict(d)         # already a full ObjectiveSpec
    if not _conforms(d, FILE_SPEC_SCHEMA):
        raise GoalParseError(f"{where}: not a valid goal spec (needs path{{h_start,h_end,k,l}}, step_h, "
                             "and tolerance_meV; a budget is optional and defaults per tier)")
    merged = {**DEFAULTS, **(defaults or {})}
    return _build_spec(
        d,
        merged,
        card_hypotheses=card_hypotheses,
        card_eig_policy_set=card_eig_policy_set,
    )


def spec_from_json(path: str | Path, *, defaults: dict | None = None,
                   card_hypotheses: dict[str, Any] | None = None,
                   card_eig_policy_set: dict[str, Any] | None = None) -> ObjectiveSpec:
    """Build an :class:`ObjectiveSpec` from a JSON file — no LLM.

    Accepts either the goal schema (``{"path": {"h_start", …}, "step_h", "tolerance_meV", …}``) or a
    full ``ObjectiveSpec.to_dict()`` (``path`` is a list of triples).
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return spec_from_dict(
        d,
        defaults=defaults,
        where=str(path),
        card_hypotheses=card_hypotheses,
        card_eig_policy_set=card_eig_policy_set,
    )


def make_envelope(spec: ObjectiveSpec, *, source: dict, original_goal: str | None = None) -> dict:
    """Freeze a compiled objective together with the fingerprint of its operator source."""
    envelope = {"schema": ENVELOPE_SCHEMA, "objective": spec.to_dict(), "source": dict(source)}
    if original_goal is not None:
        envelope["original_goal"] = str(original_goal)
    return envelope


def envelope_from_json(path: str | Path) -> tuple[ObjectiveSpec, dict, str | None]:
    """Read a versioned frozen objective envelope without invoking the supervisor LLM."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != ENVELOPE_SCHEMA:
        raise GoalParseError(f"{p}: not an {ENVELOPE_SCHEMA} envelope")
    if not isinstance(data.get("source"), dict):
        raise GoalParseError(f"{p}: objective envelope has no source fingerprint")
    spec = spec_from_dict(data.get("objective"), where=f"{p}:objective")
    return spec, dict(data["source"]), data.get("original_goal")


# --------------------------------------------------------------------------- shared
def _build_spec(data: dict, merged: dict, *,
                card_hypotheses: dict[str, Any] | None = None,
                card_eig_policy_set: dict[str, Any] | None = None) -> ObjectiveSpec:
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
        (-2.0, 12.0) if branch == "optic" else (-2.0, 7.0))
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
    final_steering = dict(frozen["steering"])
    if final_steering.get("scan_window_eig_mode") == "authoritative":
        # The policy set is bound only after the objective comparison scope exists.  Construct the
        # temporary scope carrier in off mode, then freeze authoritative mode together with the
        # compiled card policy in the single final replacement below.
        construction_steering = {**final_steering, "scan_window_eig_mode": "off"}
        frozen = {**frozen, "steering": construction_steering,
                  "hypothesis_fingerprint": content_fingerprint(
                      frozen["hypotheses"], construction_steering)}
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
    scope = current_objective_comparison_scope(objective)
    policy_set = compile_card_eig_policy_set(
        card_eig_policy_set,
        scope,
        max_runtime_variants=objective.scan_window_eig.scoring_config.max_runtime_variants,
    )
    frozen = replace(
        objective,
        steering=final_steering,
        hypothesis_fingerprint=content_fingerprint(objective.hypotheses, final_steering),
        scan_window_eig=ScanWindowEIGConfig(
            policy_set=policy_set,
            scoring_config=objective.scan_window_eig.scoring_config,
        ),
    )
    if current_objective_comparison_scope(frozen) != scope:
        raise ValueError("scan-window EIG policy binding changed objective comparison scope")
    return frozen


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
    """The schema-valid goal object from a dispatch result, or None if it can't be recovered."""
    if getattr(res, "structured", None) is not None and _conforms(res.structured, LLM_GOAL_SCHEMA):
        return res.structured
    out = getattr(res, "output", None)
    if out:
        obj = _loads_lenient(out)
        if obj is not None and _conforms(obj, LLM_GOAL_SCHEMA):
            return obj
    return None


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

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

import json
import os
from pathlib import Path
from typing import Any

import isar.reduce.llm_client as _llm  # boundary carve-out: the shared LLMM HTTP plumbing only
from isar.drive.objective import ObjectiveSpec

Dispatcher = _llm.Dispatcher
LLMMClient = _llm.LLMMClient

# Cloud pin (configurable). Default matches the adjudicator bake-off's proven cloud comparator.
DEFAULT_SUPERVISOR_MODEL = "nanogpt/openai/gpt-oss-120b"
DEFAULT_ENDPOINT = "http://127.0.0.1:8766"

GOAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "step_h", "tolerance_meV"],
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
        "tolerance_meV": {"type": "number"},
        "budget_hours": {"type": "number"},
        "budget_neutrons": {"type": "number"},
        "notes": {"type": "string"},
    },
}

_EXAMPLE = {
    "path": {"h_start": 1.0, "h_end": 2.0, "k": 0.0, "l": 0.0},
    "step_h": 0.1,
    "tolerance_meV": 0.5,
    "budget_hours": 8,
    "notes": "acoustic branch (1,0,0)->(2,0,0)",
}

SYSTEM = (
    "You translate a neutron triple-axis measurement goal into a machine objective for an autonomous "
    "driver. The path is a straight segment in H at fixed K, L. Extract: path {h_start, h_end, k, l}, "
    "step_h (the H grid spacing, default 0.05 if unstated), tolerance_meV (the per-point target on the "
    "phonon energy ω₀; map '±X meV' to X, 'less than X meV' to X), and a budget as EITHER budget_hours "
    "OR budget_neutrons ('overnight' ≈ 8 hours; if no budget is stated, use budget_hours = 8). "
    "Optionally a short notes string. Return ONLY a JSON object matching this schema — use EXACTLY "
    "these keys, no wrapper object, no extra keys, and never enumerate grid points:\n"
    + json.dumps(GOAL_SCHEMA)
    + "\n\nExample. Goal: 'map the acoustic phonon from (1,0,0) to (2,0,0) to +-0.5 meV, steps of 0.1, "
    "overnight'. Reply:\n" + json.dumps(_EXAMPLE)
)

_REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid against the schema. Return ONLY a JSON object matching the "
    "schema — no prose, no markdown fences."
)

DEFAULTS: dict[str, Any] = {"step_h": 0.05, "tier": "deterministic"}


class GoalParseError(ValueError):
    """The goal could not be turned into a valid ObjectiveSpec (LLM refusal / non-conforming output)."""


# --------------------------------------------------------------------------- LLM path
def goal_to_spec(goal: str, *, dispatcher: Dispatcher | None = None, defaults: dict | None = None,
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

    res = dispatcher.dispatch(system=SYSTEM, user=goal, schema=GOAL_SCHEMA,
                              temperature=temperature, max_tokens=max_tokens)
    data = _extract(res)
    if data is None:
        res = dispatcher.dispatch(system=SYSTEM, user=goal + _REPAIR_SUFFIX, schema=GOAL_SCHEMA,
                                  temperature=temperature, max_tokens=max_tokens)
        data = _extract(res)
    if data is None:
        detail = f" ({res.error})" if getattr(res, "error", None) else ""
        raw = (res.output or "")[:200]
        raise GoalParseError(
            "goal→ObjectiveSpec failed: the supervisor LLM did not return a schema-valid objective"
            + detail + (f"; raw: {raw!r}" if raw else "")
            + ". Use `isar drive --spec <file.json>` for an LLM-free run.")
    return _build_spec(data, merged)


# --------------------------------------------------------------------------- LLM-free path
def spec_from_json(path: str | Path, *, defaults: dict | None = None) -> ObjectiveSpec:
    """Build an :class:`ObjectiveSpec` from a JSON file — no LLM.

    Accepts either the goal schema (``{"path": {"h_start", …}, "step_h", "tolerance_meV", …}``) or a
    full ``ObjectiveSpec.to_dict()`` (``path`` is a list of triples).
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise GoalParseError(f"{path}: spec JSON must be an object, got {type(d).__name__}")
    if isinstance(d.get("path"), list):
        return ObjectiveSpec.from_dict(d)         # already a full ObjectiveSpec
    if not _conforms(d, GOAL_SCHEMA):
        raise GoalParseError(f"{path}: not a valid goal spec (needs path{{h_start,h_end,k,l}}, step_h, "
                             "tolerance_meV, and a budget)")
    merged = {**DEFAULTS, **(defaults or {})}
    return _build_spec(d, merged)


# --------------------------------------------------------------------------- shared
def _build_spec(data: dict, merged: dict) -> ObjectiveSpec:
    p = data["path"]
    step = float(data.get("step_h") or merged.get("step_h") or 0.05)
    budget = _budget(data)
    spec = ObjectiveSpec.along_h(
        h_start=float(p["h_start"]), h_end=float(p["h_end"]),
        k=float(p.get("k", 0.0)), l=float(p.get("l", 0.0)), step=step,
        tolerance_meV=float(data["tolerance_meV"]), budget=budget,
        tier=str(merged.get("tier", "deterministic")),
        name=str(data.get("notes", "")),
        meta={"source": "goal_to_spec", "raw": data},
    )
    return spec


def _budget(data: dict) -> dict[str, Any]:
    if data.get("budget_neutrons"):
        return {"kind": "neutrons", "amount": float(data["budget_neutrons"])}
    if data.get("budget_hours"):
        return {"kind": "counting_seconds", "amount": float(data["budget_hours"]) * 3600.0}
    raise GoalParseError("goal spec has no budget (need budget_hours or budget_neutrons)")


def _extract(res) -> dict | None:
    """The schema-valid goal object from a dispatch result, or None if it can't be recovered."""
    if getattr(res, "structured", None) is not None and _conforms(res.structured, GOAL_SCHEMA):
        return res.structured
    out = getattr(res, "output", None)
    if out:
        obj = _loads_lenient(out)
        if obj is not None and _conforms(obj, GOAL_SCHEMA):
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
    if t == "string":
        return isinstance(value, str)
    return True

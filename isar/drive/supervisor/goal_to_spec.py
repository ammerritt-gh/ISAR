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

# Cloud pin (configurable via LLMM_SUPERVISOR_MODEL). GLM 5.2 replaced gpt-oss-120b, whose NanoGPT
# serviceability had collapsed (~23% success, frequent dropped dispatches at goal-parse time). GLM 5.2
# reports >95% health, supports structured output, and dispatches in ~1.6s — right for a one-shot
# schema-enforced goal parse. `thinking` is pinned False by the client, so the non-thinking base ref.
DEFAULT_SUPERVISOR_MODEL = "nanogpt/zai-org/glm-5.2"
DEFAULT_ENDPOINT = "http://127.0.0.1:8766"

_Q_ARRAY = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}

GOAL_SCHEMA = {
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
        "energy_search_meV": {"type": "array", "items": {"type": "number"},
                              "minItems": 2, "maxItems": 2},
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
    "driver. Express a general reciprocal-space line as q_start [h,k,l], q_end [h,k,l], and "
    "step_rlu (the maximum point spacing, default 0.05 if unstated). The legacy path/step_h shape is "
    "also valid for a segment purely along H. Extract tolerance_meV (the per-point target on the "
    "phonon energy ω₀; map '±X meV' to X, 'less than X meV' to X), and a budget as EITHER budget_hours "
    "OR budget_neutrons ('overnight' ≈ 8 hours; if no budget is stated, use budget_hours = 8). "
    "Set target_branch to optic when the user asks for an optic or transverse optic phonon, otherwise "
    "acoustic; retain their wording in target_label. For optic goals use energy_search_meV [-2,12] "
    "unless the user provides a range. Optionally a short notes string. Return ONLY a JSON object matching this schema — use EXACTLY "
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
ENVELOPE_SCHEMA = "isar.objective-envelope/1"


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
def spec_from_dict(d: dict, *, defaults: dict | None = None, where: str = "spec") -> ObjectiveSpec:
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
    if not _conforms(d, GOAL_SCHEMA):
        raise GoalParseError(f"{where}: not a valid goal spec (needs path{{h_start,h_end,k,l}}, step_h, "
                             "tolerance_meV, and a budget)")
    merged = {**DEFAULTS, **(defaults or {})}
    return _build_spec(d, merged)


def spec_from_json(path: str | Path, *, defaults: dict | None = None) -> ObjectiveSpec:
    """Build an :class:`ObjectiveSpec` from a JSON file — no LLM.

    Accepts either the goal schema (``{"path": {"h_start", …}, "step_h", "tolerance_meV", …}``) or a
    full ``ObjectiveSpec.to_dict()`` (``path`` is a list of triples).
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return spec_from_dict(d, defaults=defaults, where=str(path))


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
def _build_spec(data: dict, merged: dict) -> ObjectiveSpec:
    budget = _budget(data)
    branch = str(data.get("target_branch", "acoustic"))
    label = str(data.get("target_label") or branch)
    energy = data.get("energy_search_meV")
    common = dict(tolerance_meV=float(data["tolerance_meV"]), budget=budget,
                  tier=str(merged.get("tier", "deterministic")),
                  name=str(data.get("notes", "")),
                  meta={"source": "goal_to_spec", "raw": data}, target_branch=branch,
                  target_label=label,
                  energy_search_meV=(tuple(energy) if energy is not None else None))
    if "q_start" in data or "q_end" in data:
        if "q_start" not in data or "q_end" not in data:
            raise GoalParseError("goal spec needs both q_start and q_end")
        if list(data["q_start"]) == list(data["q_end"]):
            raise GoalParseError("natural-language path is degenerate; q_start and q_end are identical")
        return ObjectiveSpec.segment(q_start=data["q_start"], q_end=data["q_end"],
                                     step_rlu=float(data.get("step_rlu") or 0.05), **common)
    if "path" not in data:
        raise GoalParseError("goal spec needs q_start/q_end or legacy path")
    p = data["path"]
    return ObjectiveSpec.along_h(
        h_start=float(p["h_start"]), h_end=float(p["h_end"]),
        k=float(p.get("k", 0.0)), l=float(p.get("l", 0.0)),
        step=float(data.get("step_h") or merged.get("step_h") or 0.05), **common)


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
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    if t == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        return all(_conforms(item, schema.get("items", {})) for item in value)
    return True

"""adjudicate — the LLM QC decision layer (§7.3), off the critical path and non-destructive.

The deterministic QC layer *flags* candidates; this stage *decides* each flag (DESIGN §0.5):
mask a cosmic ray, keep a genuinely short scan, drop a dead detector, hold the ambiguous one for the
human — plus a peak-shape judgment and a one-line reason, plus a per-scan narrative. Decisions are
written to ``record.adjudication`` and appended to the append-only ``card.state.corrections`` audit;
**nothing is masked, dropped, or deleted here** — a later §8 correction loop applies confirmed
corrections under human review.

Structured output is requested with a strict schema. On the local llama.cpp tier LLMM hard-enforces
it; on cloud connections it is best-effort, so the response is validated (``_conforms``), lenient
re-parsed from the raw text if needed, retried once, and finally held for the human — a flagged scan
is never silently dropped. The LLMM client is injected (:class:`~isar.reduce.llm_client.Dispatcher`)
so tests never touch the network.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from isar.reduce import compact_view
from isar.reduce.llm_client import Dispatcher, LLMMClient
from isar.schema import scan as _scan

DECISIONS = ("mask", "keep", "drop_detector", "hold_for_human")
SHAPES = ("whole_peak", "clipped_peak", "no_peak", "not_applicable")

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions", "scan_narrative"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "decision", "reason", "confidence"],
                "properties": {
                    "target": {"type": "string"},
                    "decision": {"type": "string", "enum": list(DECISIONS)},
                    "shape": {"type": "string", "enum": list(SHAPES)},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "scan_narrative": {"type": "string"},
    },
}

_EXAMPLE = {
    "decisions": [
        {"target": "point:ana1:7", "decision": "mask", "shape": "not_applicable",
         "reason": "single-point spike 6000 vs neighbours ~40, classic cosmic ray", "confidence": 0.97},
    ],
    "scan_narrative": "One cosmic ray masked on ana1; rest of the scan is clean.",
}

SYSTEM = (
    "You are a neutron-scan QC adjudicator. A deterministic layer has FLAGGED candidates; you DECIDE "
    "each flag using the scan's shape and context. NEVER invent flags for detectors that were not "
    'flagged. Targets are "scan", "spectrum:<det>", or "point:<det>:<index>". decision is one of: '
    '"mask" (confirm a bad point, e.g. a cosmic ray), "keep" (the data is genuine), "drop_detector" '
    '(the analyzer is dead/non-physical), "hold_for_human" (ambiguous). Prefer "hold_for_human" when '
    "uncertain. For peak-shape candidates also set shape (whole_peak|clipped_peak|no_peak); otherwise "
    'use "not_applicable". Also write a 1-2 sentence scan_narrative. Return ONLY a JSON object '
    "matching this schema:\n" + json.dumps(DECISION_SCHEMA) + "\nWorked example:\n" + json.dumps(_EXAMPLE)
)

_REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid. Return ONLY a JSON object matching the schema — "
    "no prose, no markdown code fences."
)


@dataclass
class AdjudicationConfig:
    """Resolved adjudication settings (card ``[adjudication]`` block + env overrides)."""
    enabled: bool = False
    model_pin: str = "local_desktop/qwen3p5-9b"          # offline/privacy-first local default
    fallback_pin: str | None = "nanogpt/openai/gpt-oss-120b"   # proven cloud comparator
    endpoint: str = "http://127.0.0.1:8766"
    temperature: float = 0.2
    max_tokens: int = 1500


def adjudication_config(card) -> AdjudicationConfig:
    """Read ``card.policy.adjudication`` over the defaults, then apply env overrides.

    ``ISAR_LLM_ENDPOINT`` / ``ISAR_LLM_MODEL`` let the beamline flip offline↔cloud without editing
    the card.
    """
    block = dict(getattr(card.policy, "adjudication", {}) or {})
    cfg = AdjudicationConfig(
        enabled=bool(block.get("enabled", AdjudicationConfig.enabled)),
        model_pin=block.get("model_pin", AdjudicationConfig.model_pin),
        fallback_pin=block.get("fallback_pin", AdjudicationConfig.fallback_pin),
        endpoint=block.get("endpoint", AdjudicationConfig.endpoint),
        temperature=float(block.get("temperature", AdjudicationConfig.temperature)),
        max_tokens=int(block.get("max_tokens", AdjudicationConfig.max_tokens)),
    )
    cfg.endpoint = os.environ.get("ISAR_LLM_ENDPOINT", cfg.endpoint)
    cfg.model_pin = os.environ.get("ISAR_LLM_MODEL", cfg.model_pin)
    return cfg


class FallbackDispatcher:
    """Dispatch to the primary pin; on a transport/dispatch error, retry the fallback pin once.

    The local 9B can be unserviceable (model not launched); the cloud comparator keeps unattended
    batches moving without changing the calling code.
    """
    def __init__(self, primary: Dispatcher, fallback: Dispatcher | None = None) -> None:
        self._primary = primary
        self._fallback = fallback

    def dispatch(self, **kwargs):
        res = self._primary.dispatch(**kwargs)
        if res.error and self._fallback is not None:
            return self._fallback.dispatch(**kwargs)
        return res


def make_client(cfg: AdjudicationConfig) -> FallbackDispatcher:
    """Build the dispatcher for a config: primary pin, with the cloud fallback when distinct."""
    primary = LLMMClient(cfg.endpoint, cfg.model_pin)
    fallback = (LLMMClient(cfg.endpoint, cfg.fallback_pin)
                if cfg.fallback_pin and cfg.fallback_pin != cfg.model_pin else None)
    return FallbackDispatcher(primary, fallback)


def adjudicate_records(records, card, client: Dispatcher, *,
                       temperature: float = 0.2, max_tokens: int = 1500):
    """Adjudicate every flagged measurement record; attach decisions + append the audit (in place)."""
    for record in compact_view._flagged_records(records):
        user = json.dumps(compact_view.compact_view(record))
        res = client.dispatch(system=SYSTEM, user=user, schema=DECISION_SCHEMA,
                              temperature=temperature, max_tokens=max_tokens)
        data = _extract(res)
        if data is None:                                   # one repair retry
            res = client.dispatch(system=SYSTEM, user=user + _REPAIR_SUFFIX, schema=DECISION_SCHEMA,
                                  temperature=temperature, max_tokens=max_tokens)
            data = _extract(res)
        adj = _to_adjudication(data, res.model_id) if data is not None else _hold_for_human(res)
        record.adjudication = adj
        for d in adj.decisions:
            card.state.corrections.append(_correction_entry(record, d, adj.model_id))
    return records


def _extract(res) -> dict | None:
    """The schema-valid decisions object from this result, or None if it can't be recovered."""
    if res.structured is not None and _conforms(res.structured, DECISION_SCHEMA):
        return res.structured
    if res.output:
        obj = _loads_lenient(res.output)
        if obj is not None and _conforms(obj, DECISION_SCHEMA):
            return obj
    return None


def _to_adjudication(data: dict, model_id) -> _scan.Adjudication:
    decisions = [
        _scan.Decision(
            target=d["target"], decision=d["decision"], shape=d.get("shape"),
            reason=d["reason"], confidence=float(d["confidence"]),
        )
        for d in data["decisions"]
    ]
    return _scan.Adjudication(decisions=decisions, narrative=data["scan_narrative"], model_id=model_id)


FAILURE_NARRATIVE = "Adjudication unavailable — scan held for human review."


def _hold_for_human(res) -> _scan.Adjudication:
    detail = f" ({res.error})" if getattr(res, "error", None) else ""
    return _scan.Adjudication(
        decisions=[_scan.Decision(
            target="scan", decision="hold_for_human", shape="not_applicable",
            reason="adjudication did not return valid structured output" + detail, confidence=0.0,
        )],
        narrative=FAILURE_NARRATIVE,
        model_id=getattr(res, "model_id", None),
    )


def _correction_entry(record, decision, model_id) -> dict:
    return {
        "source": "llm-adjudicate",
        "scan_number": record.scan_number,
        "target": decision.target,
        "decision": decision.decision,
        "shape": decision.shape,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "model_id": model_id,
    }


def _loads_lenient(text: str):
    """Parse JSON that may be wrapped in markdown fences or surrounded by prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.lstrip("`")
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    lo, hi = s.find("{"), s.rfind("}")        # last resort: the outermost {...}
    if 0 <= lo < hi:
        try:
            return json.loads(s[lo:hi + 1])
        except (ValueError, TypeError):
            return None
    return None


def _conforms(value, schema) -> bool:
    """A conservative, dependency-free check for the JSON-Schema subset DECISION_SCHEMA uses."""
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
    if t == "array":
        if not isinstance(value, list):
            return False
        item = schema.get("items")
        return all(_conforms(v, item) for v in value) if item else True
    if t == "string":
        if not isinstance(value, str):
            return False
        enum = schema.get("enum")
        return value in enum if enum else True
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    enum = schema.get("enum")
    return value in enum if enum is not None else True

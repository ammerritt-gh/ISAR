"""Dependency-minimal LLMM client: one pinned ``POST /v1/dispatch`` over stdlib ``urllib``.

ISAR's adjudicator (``isar/reduce/adjudicate.py``) calls LLMM to turn a compact view of a flagged
scan into a structured QC decision. ISAR is deliberately dependency-light (numpy/scipy/matplotlib),
so this is a hand-rolled ``urllib`` wrapper — no ``requests``/``httpx``/``openai``.

The only thing the rest of ISAR depends on is the :class:`Dispatcher` protocol, so unit tests pass a
fake and never hit the network. :class:`LLMMClient` is the real implementation; its HTTP transport is
injectable too, which is how this module's own tests stay offline.

Contract (captured from ``llm-contracts``): pin one model via
``routing={"mode":"pinned","pinned":{"ref": <pin>}}``; generation params use ``max_tokens`` (not
``max_output_tokens``); ``input.structured_output_schema`` is a raw JSON-Schema dict; ``thinking`` is
False to keep Qwen3 reasoning models from emitting reasoning instead of content. The response carries
``structured`` (the schema-valid object, or ``null`` on a schema-miss with ``output`` still present)
and an optional ``error`` (``{code,message,...}``).
"""
from __future__ import annotations

import json
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class DispatchResult:
    """The slice of a DispatchResponse the adjudicator needs."""
    structured: dict | None = None     # schema-valid object, or None on schema-miss / error
    output: str | None = None          # raw text (present even when structured is None)
    error: str | None = None           # "<code>: <message>" on dispatch/transport failure, else None
    model_id: str | None = None
    usage: dict = field(default_factory=dict)


class Dispatcher(Protocol):
    """The injectable seam. Tests pass a fake; production passes :class:`LLMMClient`."""
    def dispatch(
        self, *, system: str, user: str, schema: dict,
        temperature: float, max_tokens: int,
    ) -> DispatchResult: ...


Transport = Callable[[str, bytes, float], bytes]   # (url, json-bytes, timeout) -> response-bytes


def _default_transport(url: str, data: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # nosec B310 (localhost http)
        return resp.read()


class LLMMClient:
    """Pins one model and dispatches a structured request to a running LLMM."""

    def __init__(
        self, endpoint: str, model_pin: str, *,
        timeout: float = 60.0, transport: Transport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model_pin = model_pin
        self._timeout = timeout
        self._transport = transport or _default_transport

    def dispatch(
        self, *, system: str, user: str, schema: dict,
        temperature: float, max_tokens: int,
    ) -> DispatchResult:
        body = {
            "id": uuid.uuid4().hex,
            "routing": {"mode": "pinned", "pinned": {"ref": self._model_pin}},
            "generation_params": {"temperature": temperature, "max_tokens": max_tokens},
            "input": {
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "structured_output_schema": schema,
                "thinking": False,
            },
        }
        url = f"{self._endpoint}/v1/dispatch"
        try:
            raw = self._transport(url, json.dumps(body).encode("utf-8"), self._timeout)
            data: dict[str, Any] = json.loads(raw)
        except Exception as exc:   # transport / decode failure → a clean error result, never a raise
            return DispatchResult(error=f"transport_error: {exc}")
        return _to_result(data)


def _to_result(data: dict) -> DispatchResult:
    err = data.get("error")
    error = None
    if err:
        if isinstance(err, dict):
            error = f"{err.get('code', 'error')}: {err.get('message', '')}".strip()
        else:
            error = str(err)
    return DispatchResult(
        structured=data.get("structured"),
        output=data.get("output"),
        error=error,
        model_id=data.get("model_id"),
        usage=data.get("usage") or {},
    )

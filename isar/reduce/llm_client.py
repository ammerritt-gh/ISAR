"""Dependency-minimal LLMM client: one pinned ``POST /v1/dispatch`` over stdlib ``urllib``.

ISAR's adjudicator (``isar/reduce/adjudicate.py``) calls LLMM to turn a compact view of a flagged
scan into a structured QC decision. ISAR is deliberately dependency-light (numpy/scipy/matplotlib),
so this is a hand-rolled ``urllib`` wrapper — no ``requests``/``httpx``/``openai``.

The only thing the rest of ISAR depends on is the :class:`Dispatcher` protocol, so unit tests pass a
fake and never hit the network. :class:`LLMMClient` is the real implementation; its HTTP transport is
injectable too, which is how this module's own tests stay offline.

Contract (captured from ``llm-contracts`` ``DispatchRequest``): pin one model via the top-level
``model: <ref>`` field (a direct request, no fallback chain — the older
``routing={"mode":"pinned",...}`` block is rejected with 422 ``extra_forbidden`` by current LLMM);
generation params use ``max_tokens`` (not ``max_output_tokens``); ``input.structured_output_schema``
is a raw JSON-Schema dict; ``thinking`` is False to keep reasoning models from emitting reasoning
instead of content. The response carries ``structured`` (the schema-valid object, or ``null`` on a
schema-miss with ``output`` still present) and an optional ``error`` (``{code,message,...}``).
"""
from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.request
import urllib.parse
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

# Hard ceiling on any LLM service response. A structured QC decision is a few KB; 8 MiB is
# generous headroom for a verbose raw ``output`` on a schema-miss, while a faulty or hostile
# endpoint streaming forever cannot exhaust memory. The streaming cap below is authoritative
# regardless of what (or whether) a Content-Length header claims.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _read_capped(resp, limit: int | None = None) -> bytes:
    """Read ``resp`` in chunks, raising once the body exceeds ``limit`` bytes.

    ``limit`` defaults to the module's :data:`MAX_RESPONSE_BYTES` at call time (late-bound so
    tests can shrink it without re-plumbing the transports).
    """
    if limit is None:
        limit = MAX_RESPONSE_BYTES
    length = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
    try:
        declared = int(length) if length is not None else None
    except (TypeError, ValueError):
        declared = None   # an unparseable Content-Length is ignored; the streaming cap governs
    if declared is not None and declared > limit:
        raise ValueError(f"LLM response exceeded {limit} bytes (Content-Length {declared})")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError(f"LLM response exceeded {limit} bytes")
        chunks.append(chunk)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect on the production LLM transports.

    A redirect could bypass :func:`validate_endpoint`'s HTTPS/loopback guarantee and forward the
    request body (experimental context, and on the OpenAI path an Authorization header) to an
    origin the operator never configured. Refusal surfaces as the normal ``transport_error``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} refused: LLM transports do not follow redirects",
            headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirects)


def validate_endpoint(endpoint: str) -> str:
    """Return a safe LLM service base URL, or raise :class:`ValueError`.

    LLM prompts can contain experimental context, so plaintext HTTP is deliberately limited to the
    literal loopback service.  A remote endpoint must use HTTPS; credentials in a URL are rejected
    rather than accidentally logged or sent as part of a copied configuration value.  ``localhost``
    is deliberately not accepted for HTTP because name resolution is configurable.
    """
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("LLM endpoint must be a non-empty URL")
    if any(char.isspace() for char in endpoint):
        raise ValueError("LLM endpoint must not contain whitespace")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        # Accessing port deliberately validates values such as ``:not-a-port``.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid LLM endpoint {endpoint!r}: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("LLM endpoint must use http or https")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("LLM endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("LLM endpoint must not include URL credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("LLM endpoint must not include a query or fragment")

    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    is_local = (
        isinstance(address, ipaddress.IPv4Address)
        and address in ipaddress.IPv4Network("127.0.0.0/8")
    ) or address == ipaddress.IPv6Address("::1")
    if parsed.scheme == "http" and not is_local:
        raise ValueError(
            "LLM endpoint must use HTTPS unless its host is literal 127.0.0.0/8 or ::1"
        )
    return endpoint.rstrip("/")


def _default_transport(url: str, data: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with _OPENER.open(req, timeout=timeout) as resp:   # nosec B310 (validated endpoint)
        return _read_capped(resp)


class LLMMClient:
    """Pins one model and dispatches a structured request to a running LLMM."""

    def __init__(
        self, endpoint: str, model_pin: str, *,
        timeout: float = 60.0, transport: Transport | None = None,
    ) -> None:
        self._endpoint = validate_endpoint(endpoint)
        self._model_pin = model_pin
        self._timeout = timeout
        self._transport = transport or _default_transport

    def dispatch(
        self, *, system: str, user: str, schema: dict,
        temperature: float, max_tokens: int,
    ) -> DispatchResult:
        body = {
            "id": uuid.uuid4().hex,
            "model": self._model_pin,
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


# ---------------------------------------------------------------------------
# OpenAI-compatible client (D3): route the SAME structured request to any server
# speaking the OpenAI Chat Completions API, so a machine without LLMM can still
# run `isar run --llm`. LLMM stays the default profile; this is the portable one.
# ---------------------------------------------------------------------------
OpenAITransport = Callable[[str, bytes, dict, float], bytes]  # (url, body, headers, timeout) -> bytes


def _default_openai_transport(url: str, data: bytes, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _OPENER.open(req, timeout=timeout) as resp:   # nosec B310 (operator-configured)
        return _read_capped(resp)


class OpenAIClient:
    """Dispatch a structured QC request to an OpenAI-compatible ``/v1/chat/completions`` endpoint.

    The schema is carried in the system prompt (as it already is for LLMM), and ``response_format``
    asks for a JSON object; the adjudicator validates/repairs/holds exactly as on the LLMM path, so a
    server that only loosely honours the format still degrades safely. Same :class:`Dispatcher`
    surface as :class:`LLMMClient`, so the caller is transport-agnostic.
    """

    def __init__(
        self, endpoint: str, model: str, *, api_key: str | None = None,
        timeout: float = 60.0, transport: OpenAITransport | None = None,
    ) -> None:
        self._endpoint = validate_endpoint(endpoint)
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport or _default_openai_transport

    def dispatch(
        self, *, system: str, user: str, schema: dict,
        temperature: float, max_tokens: int,
    ) -> DispatchResult:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # OpenAI base_url convention includes the /v1 segment; tolerate a bare base too.
        base = self._endpoint
        url = (f"{base}/chat/completions" if base.endswith("/v1")
               else f"{base}/v1/chat/completions")
        try:
            raw = self._transport(url, json.dumps(body).encode("utf-8"), headers, self._timeout)
            data: dict[str, Any] = json.loads(raw)
        except Exception as exc:   # transport / decode failure → a clean error result, never a raise
            return DispatchResult(error=f"transport_error: {exc}")
        return _openai_to_result(data)


def _openai_to_result(data: dict) -> DispatchResult:
    err = data.get("error")
    if err:
        if isinstance(err, dict):
            error = f"{err.get('code', 'error')}: {err.get('message', '')}".strip()
        else:
            error = str(err)
        return DispatchResult(error=error, model_id=data.get("model"))
    choices = data.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    structured = None
    if content:
        try:
            structured = json.loads(content)
        except ValueError:
            structured = None   # schema-miss: adjudicator re-parses/retries/holds on the raw text
    return DispatchResult(
        structured=structured,
        output=content,
        error=None,
        model_id=data.get("model"),
        usage=data.get("usage") or {},
    )

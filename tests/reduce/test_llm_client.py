"""The stdlib-urllib LLMM client: build a pinned /v1/dispatch body and parse the response.

The transport is injected so these tests never touch the network — they exercise the real
body-construction and DispatchResponse-parsing logic against a fake that captures the request.
"""
import json

import pytest

from isar.reduce import llm_client


class _FakeTransport:
    """Captures each POST and returns canned response bytes (no network)."""
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, url: str, data: bytes, timeout: float) -> bytes:
        self.calls.append({"url": url, "body": json.loads(data), "timeout": timeout})
        return json.dumps(self._response).encode("utf-8")


def _ok_response(**over) -> dict:
    base = {
        "request_id": "r1",
        "model_id": "local_desktop/qwen3p5-9b",
        "tier": 2,
        "output": "{...}",
        "structured": {"decisions": [], "scan_narrative": "ok"},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "error": None,
    }
    base.update(over)
    return base


def test_dispatch_builds_pinned_request_body():
    tr = _FakeTransport(_ok_response())
    client = llm_client.LLMMClient(
        "http://127.0.0.1:8766", "local_desktop/qwen3p5-9b", node="reduce.test", transport=tr
    )
    schema = {"type": "object", "required": ["decisions"]}

    client.dispatch(system="SYS", user="USER", schema=schema, temperature=0.2, max_tokens=1500)

    assert len(tr.calls) == 1
    call = tr.calls[0]
    assert call["url"] == "http://127.0.0.1:8766/v1/dispatch"
    body = call["body"]
    # Current llm-contracts DispatchRequest: every dispatch names a `role`, and a pinned `model`
    # is legal only under role="passthrough" (any other role 422s on the model field). Both
    # metadata.client and metadata.node are required — LLMM refuses an unattributed request. The
    # legacy `routing` block is rejected (422 extra_forbidden) and must never be sent.
    assert body["role"] == "passthrough"
    assert body["model"] == "local_desktop/qwen3p5-9b"
    assert body["metadata"] == {"client": "isar", "node": "reduce.test"}
    assert "routing" not in body
    assert body["generation_params"] == {"temperature": 0.2, "max_tokens": 1500}
    assert body["input"]["system"] == "SYS"
    assert body["input"]["messages"] == [{"role": "user", "content": "USER"}]
    assert body["input"]["structured_output_schema"] == schema
    assert body["input"]["thinking"] is False        # Qwen3 reasoning disabled
    assert body["id"]                                 # a non-empty request id


def test_dispatch_addresses_a_role_and_sends_no_model():
    """The normal path: name a role, let LLMM's role store choose the model and fallback."""
    tr = _FakeTransport(_ok_response())
    client = llm_client.LLMMClient(
        "http://127.0.0.1:8766", role="scientific_interpreter", voice="default",
        node="drive.supervisor.goal_to_program", transport=tr,
    )

    client.dispatch(system="SYS", user="USER", schema={}, temperature=0.1, max_tokens=800)

    body = tr.calls[0]["body"]
    assert body["role"] == "scientific_interpreter"
    assert body["voice"] == "default"
    # A pin alongside a named role is a 400 from LLMM; the role store owns the model here.
    assert "model" not in body
    assert body["metadata"] == {"client": "isar", "node": "drive.supervisor.goal_to_program"}


def test_dispatch_omits_voice_when_unset():
    tr = _FakeTransport(_ok_response())
    client = llm_client.LLMMClient("http://127.0.0.1", role="extractor", transport=tr)

    client.dispatch(system="s", user="u", schema={}, temperature=0.1, max_tokens=10)

    assert "voice" not in tr.calls[0]["body"]


@pytest.mark.parametrize("kwargs, expected", [
    ({"model_pin": "some/ref", "role": "scientific_interpreter"}, "only legal under role='passthrough'"),
    ({"role": "passthrough"}, "requires a model pin"),
    ({}, "needs either a role or a model_pin"),
])
def test_illegal_role_model_pairings_fail_at_construction(kwargs, expected):
    """Caught locally, so a misconfiguration is not a 400 on the first dispatch of a long run."""
    with pytest.raises(ValueError, match=expected):
        llm_client.LLMMClient("http://127.0.0.1", **kwargs)


def test_dispatch_parses_structured_success():
    tr = _FakeTransport(_ok_response(
        structured={"decisions": [{"target": "scan"}], "scan_narrative": "n"},
        output="raw text",
    ))
    client = llm_client.LLMMClient("http://127.0.0.1", "pin", transport=tr)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured == {"decisions": [{"target": "scan"}], "scan_narrative": "n"}
    assert res.output == "raw text"
    assert res.model_id == "local_desktop/qwen3p5-9b"
    assert res.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert res.error is None


def test_dispatch_schema_miss_yields_null_structured_with_output():
    tr = _FakeTransport(_ok_response(structured=None, output="not valid json"))
    client = llm_client.LLMMClient("http://127.0.0.1", "pin", transport=tr)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.output == "not valid json"
    assert res.error is None


def test_dispatch_surfaces_dispatch_error():
    tr = _FakeTransport(_ok_response(
        error={"code": "no_model_available", "message": "model offline"},
        structured=None, output=None,
    ))
    client = llm_client.LLMMClient("http://127.0.0.1", "pin", transport=tr)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.error is not None
    assert "no_model_available" in res.error and "model offline" in res.error


def test_dispatch_transport_error_returns_error_result():
    def boom(url, data, timeout):
        raise OSError("connection refused")

    client = llm_client.LLMMClient("http://127.0.0.1", "pin", transport=boom)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.output is None
    assert res.error is not None
    assert "connection refused" in res.error


def test_validate_endpoint_allows_only_literal_loopback_plain_http():
    assert llm_client.validate_endpoint("http://127.42.0.1") == "http://127.42.0.1"
    assert llm_client.validate_endpoint("http://[::1]:8766") == "http://[::1]:8766"


def test_validate_endpoint_requires_https_away_from_loopback():
    import pytest

    assert llm_client.validate_endpoint("https://llm.example.test/v1") == "https://llm.example.test/v1"
    with pytest.raises(ValueError, match="must use HTTPS"):
        llm_client.LLMMClient("http://llm.example.test", "pin")
    with pytest.raises(ValueError, match="must use HTTPS"):
        llm_client.LLMMClient("http://localhost:8766", "pin")


def test_validate_endpoint_rejects_malformed_urls_and_url_credentials():
    import pytest

    for endpoint in ("", "not-a-url", "ftp://localhost", "https://", "https://user:pass@host"):
        with pytest.raises(ValueError):
            llm_client.validate_endpoint(endpoint)


# ---------------------------------------------------------------------------
# Response-size cap (_read_capped): the streaming cap is authoritative; a
# Content-Length header may only reject early, never admit more.
# ---------------------------------------------------------------------------
class _FakeResp:
    """A file-like response: streams `body` through .read(n), with optional headers."""
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}
        self.reads = 0

    def read(self, n: int) -> bytes:
        self.reads += 1
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def test_read_capped_accepts_exactly_the_limit():
    body = b"x" * llm_client.MAX_RESPONSE_BYTES
    assert llm_client._read_capped(_FakeResp(body)) == body


def test_read_capped_rejects_one_byte_over_the_limit():
    import pytest

    body = b"x" * (llm_client.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ValueError, match="exceeded"):
        llm_client._read_capped(_FakeResp(body))


def test_read_capped_streaming_cap_overrides_a_lying_content_length():
    import pytest

    body = b"x" * (llm_client.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ValueError, match="exceeded"):
        llm_client._read_capped(_FakeResp(body, headers={"Content-Length": "10"}))


def test_read_capped_rejects_early_on_huge_declared_content_length():
    import pytest

    resp = _FakeResp(b"irrelevant", headers={"Content-Length": str(llm_client.MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(ValueError, match="exceeded"):
        llm_client._read_capped(resp)
    assert resp.reads == 0   # rejected before any body bytes were read


def test_read_capped_ignores_unparseable_content_length():
    body = b"ok"
    assert llm_client._read_capped(_FakeResp(body, headers={"Content-Length": "banana"})) == body


def test_oversize_response_becomes_a_clean_transport_error(monkeypatch, http_server):
    """Through the REAL default transport: an oversize body yields transport_error, no raise."""
    monkeypatch.setattr(llm_client, "MAX_RESPONSE_BYTES", 64)
    url = http_server(status=200, body=b"{" + b" " * 200 + b"}")
    client = llm_client.LLMMClient(url, "pin")

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.error is not None and res.error.startswith("transport_error")
    assert "exceeded" in res.error


# ---------------------------------------------------------------------------
# Redirect refusal: NO production LLM transport follows redirects (a redirect
# could bypass validate_endpoint's HTTPS/loopback guarantee and forward the
# prompt body — and, on the OpenAI path, the Authorization header).
# ---------------------------------------------------------------------------
import pytest


@pytest.fixture
def http_server():
    """A one-shot loopback HTTP server; returns a factory -> base URL."""
    import http.server
    import threading

    servers = []

    def make(status: int, body: bytes = b"", location: str | None = None) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):   # keep pytest output clean
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.02), daemon=True)
        t.start()
        servers.append((srv, t))
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield make
    for srv, t in servers:
        srv.shutdown()
        t.join(timeout=2.0)
        assert not t.is_alive()
        srv.server_close()


@pytest.mark.parametrize("location", [
    "http://example.test/harvest",          # cross-origin
    "https://127.0.0.1:1/v1/dispatch",      # same host, scheme flip
])
def test_llmm_default_transport_refuses_redirects(http_server, location):
    url = http_server(status=302, location=location)
    client = llm_client.LLMMClient(url, "pin")

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.error is not None and res.error.startswith("transport_error")
    assert "redirect" in res.error


@pytest.mark.parametrize("api_key", ["secret-token", None])
def test_openai_default_transport_refuses_redirects(http_server, api_key):
    """Refused both with an Authorization header (token would leak) and without (body would)."""
    url = http_server(status=302, location="http://example.test/harvest")
    client = llm_client.OpenAIClient(url, "gpt-x", api_key=api_key)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.structured is None
    assert res.error is not None and res.error.startswith("transport_error")
    assert "redirect" in res.error

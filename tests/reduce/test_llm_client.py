"""The stdlib-urllib LLMM client: build a pinned /v1/dispatch body and parse the response.

The transport is injected so these tests never touch the network — they exercise the real
body-construction and DispatchResponse-parsing logic against a fake that captures the request.
"""
import json

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
        "http://127.0.0.1:8766", "local_desktop/qwen3p5-9b", transport=tr
    )
    schema = {"type": "object", "required": ["decisions"]}

    client.dispatch(system="SYS", user="USER", schema=schema, temperature=0.2, max_tokens=1500)

    assert len(tr.calls) == 1
    call = tr.calls[0]
    assert call["url"] == "http://127.0.0.1:8766/v1/dispatch"
    body = call["body"]
    assert body["routing"] == {"mode": "pinned", "pinned": {"ref": "local_desktop/qwen3p5-9b"}}
    assert body["generation_params"] == {"temperature": 0.2, "max_tokens": 1500}
    assert body["input"]["system"] == "SYS"
    assert body["input"]["messages"] == [{"role": "user", "content": "USER"}]
    assert body["input"]["structured_output_schema"] == schema
    assert body["input"]["thinking"] is False        # Qwen3 reasoning disabled
    assert body["id"]                                 # a non-empty request id


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

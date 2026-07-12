"""OpenAI-compatible client (D3): build a /v1/chat/completions body, auth, parse choices, errors.

The transport is injected so these tests never touch the network.
"""
import json

from isar.reduce import llm_client


class _FakeTransport:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, url: str, data: bytes, headers: dict, timeout: float) -> bytes:
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers,
                           "timeout": timeout})
        return json.dumps(self._response).encode("utf-8")


def _completion(content: str, **over) -> dict:
    base = {"model": "gpt-oss", "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4}}
    base.update(over)
    return base


def test_builds_chat_completions_body_and_auth_header():
    tr = _FakeTransport(_completion('{"decisions": [], "scan_narrative": "ok"}'))
    client = llm_client.OpenAIClient("http://host:1234/v1", "gpt-oss", api_key="sk-abc", transport=tr)

    client.dispatch(system="SYS", user="USER", schema={"x": 1}, temperature=0.2, max_tokens=1500)

    call = tr.calls[0]
    assert call["url"] == "http://host:1234/v1/chat/completions"
    body = call["body"]
    assert body["model"] == "gpt-oss"
    assert body["messages"] == [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": "USER"}]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.2 and body["max_tokens"] == 1500
    assert call["headers"]["Authorization"] == "Bearer sk-abc"


def test_no_auth_header_without_key():
    tr = _FakeTransport(_completion("{}"))
    llm_client.OpenAIClient("http://h", "m", transport=tr).dispatch(
        system="s", user="u", schema={}, temperature=0.2, max_tokens=10)
    assert "Authorization" not in tr.calls[0]["headers"]


def test_parses_json_content_into_structured():
    tr = _FakeTransport(_completion('{"decisions": [{"target": "scan"}], "scan_narrative": "n"}'))
    res = llm_client.OpenAIClient("http://h", "m", transport=tr).dispatch(
        system="s", user="u", schema={}, temperature=0.2, max_tokens=10)
    assert res.structured == {"decisions": [{"target": "scan"}], "scan_narrative": "n"}
    assert res.model_id == "gpt-oss"
    assert res.usage == {"prompt_tokens": 8, "completion_tokens": 4}
    assert res.error is None


def test_non_json_content_yields_null_structured_with_output():
    tr = _FakeTransport(_completion("sorry, here is prose not json"))
    res = llm_client.OpenAIClient("http://h", "m", transport=tr).dispatch(
        system="s", user="u", schema={}, temperature=0.2, max_tokens=10)
    assert res.structured is None
    assert res.output == "sorry, here is prose not json"
    assert res.error is None


def test_api_error_is_surfaced():
    tr = _FakeTransport({"error": {"code": "invalid_api_key", "message": "bad key"}})
    res = llm_client.OpenAIClient("http://h", "m", transport=tr).dispatch(
        system="s", user="u", schema={}, temperature=0.2, max_tokens=10)
    assert res.structured is None
    assert "invalid_api_key" in res.error and "bad key" in res.error


def test_transport_error_returns_error_result():
    def boom(url, data, headers, timeout):
        raise OSError("connection refused")

    res = llm_client.OpenAIClient("http://h", "m", transport=boom).dispatch(
        system="s", user="u", schema={}, temperature=0.2, max_tokens=10)
    assert res.error is not None and "connection refused" in res.error

"""Adjudication config: defaults, the card's [adjudication] block, env overrides, and the
primary->fallback dispatcher (offline local 9B falling back to the cloud comparator)."""
from isar.parsers import spec_s30
from isar.reduce import adjudicate
from isar.reduce.llm_client import DispatchResult


class FakeDispatcher:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    def dispatch(self, **kwargs) -> DispatchResult:
        self.calls.append(kwargs)
        return self._results.pop(0)


_OK = DispatchResult(structured={"decisions": [], "scan_narrative": "ok"}, model_id="m")


def test_adjudication_config_defaults():
    card = spec_s30.card_template()
    cfg = adjudicate.adjudication_config(card)
    assert cfg.enabled is False
    assert cfg.model_pin == "local_desktop/qwen3p5-9b"
    assert cfg.fallback_pin is None
    assert cfg.endpoint == "http://127.0.0.1:8766"
    assert cfg.temperature == 0.2
    assert cfg.max_tokens == 1500


def test_adjudication_config_reads_policy_block():
    card = spec_s30.card_template()
    card.policy.adjudication = {"enabled": True, "model_pin": "nanogpt/openai/gpt-oss-120b",
                                "temperature": 0.1}
    cfg = adjudicate.adjudication_config(card)
    assert cfg.enabled is True
    assert cfg.model_pin == "nanogpt/openai/gpt-oss-120b"
    assert cfg.temperature == 0.1
    assert cfg.max_tokens == 1500          # unset fields keep their defaults


def test_env_overrides_endpoint_and_model(monkeypatch):
    monkeypatch.setenv("ISAR_LLM_ENDPOINT", "https://beamline.example.test:9000")
    monkeypatch.setenv("ISAR_LLM_MODEL", "local_desktop/qwen3p5-2b")
    card = spec_s30.card_template()
    cfg = adjudicate.adjudication_config(card)
    assert cfg.endpoint == "https://beamline.example.test:9000"
    assert cfg.model_pin == "local_desktop/qwen3p5-2b"


def test_fallback_env_is_explicit_and_empty_disables_card_fallback(monkeypatch):
    card = spec_s30.card_template()
    card.policy.adjudication = {"fallback_pin": adjudicate.DEFAULT_CLOUD_FALLBACK_MODEL}

    assert adjudicate.adjudication_config(card).fallback_pin == adjudicate.DEFAULT_CLOUD_FALLBACK_MODEL

    monkeypatch.setenv("ISAR_LLM_FALLBACK_MODEL", "")
    assert adjudicate.adjudication_config(card).fallback_pin is None

    monkeypatch.setenv("ISAR_LLM_FALLBACK_MODEL", "nanogpt/example/explicit")
    assert adjudicate.adjudication_config(card).fallback_pin == "nanogpt/example/explicit"


def test_fallback_dispatcher_uses_fallback_on_primary_error():
    primary = FakeDispatcher([DispatchResult(error="no_model_available: offline")])
    fallback = FakeDispatcher([DispatchResult(structured={"decisions": []}, model_id="cloud")])
    client = adjudicate.FallbackDispatcher(primary, fallback)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.model_id == "cloud"
    assert len(primary.calls) == 1 and len(fallback.calls) == 1


def test_fallback_dispatcher_keeps_primary_on_success():
    primary = FakeDispatcher([DispatchResult(structured={"decisions": []}, model_id="local")])
    fallback = FakeDispatcher([])
    client = adjudicate.FallbackDispatcher(primary, fallback)

    res = client.dispatch(system="s", user="u", schema={}, temperature=0.2, max_tokens=10)

    assert res.model_id == "local"
    assert len(fallback.calls) == 0

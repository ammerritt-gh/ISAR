"""Adjudication profile routing (D3): profile/api_key config + make_client picks the transport."""
from isar.parsers import spec_s30
from isar.reduce import adjudicate
from isar.reduce.llm_client import LLMMClient, OpenAIClient


def test_default_profile_is_llmm():
    cfg = adjudicate.adjudication_config(spec_s30.card_template())
    assert cfg.profile == "llmm"
    assert cfg.api_key is None
    assert isinstance(adjudicate.make_client(cfg)._primary, LLMMClient)


def test_openai_profile_from_card_block():
    card = spec_s30.card_template()
    card.policy.adjudication = {"profile": "openai", "model_pin": "gpt-oss-120b",
                                "endpoint": "http://host:1234/v1", "api_key": "sk-xyz"}
    cfg = adjudicate.adjudication_config(card)
    assert cfg.profile == "openai" and cfg.api_key == "sk-xyz"
    client = adjudicate.make_client(cfg)
    assert isinstance(client._primary, OpenAIClient)
    assert client._fallback is None            # openai profile has no auto dual-pin fallback


def test_env_overrides_profile_and_key(monkeypatch):
    monkeypatch.setenv("ISAR_LLM_PROFILE", "openai")
    monkeypatch.setenv("ISAR_LLM_API_KEY", "sk-env")
    cfg = adjudicate.adjudication_config(spec_s30.card_template())
    assert cfg.profile == "openai" and cfg.api_key == "sk-env"


def test_api_key_env_indirection(monkeypatch):
    monkeypatch.delenv("ISAR_LLM_API_KEY", raising=False)
    monkeypatch.setenv("MY_KEY_VAR", "sk-indirect")
    card = spec_s30.card_template()
    card.policy.adjudication = {"profile": "openai", "api_key_env": "MY_KEY_VAR"}
    cfg = adjudicate.adjudication_config(card)
    assert cfg.api_key == "sk-indirect"

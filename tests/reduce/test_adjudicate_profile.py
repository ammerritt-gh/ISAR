"""Adjudication profile routing (D3): profile/api_key config + make_client picks the transport."""
from isar.parsers import spec_s30
from isar.reduce import adjudicate
from isar.reduce.llm_client import LLMMClient, OpenAIClient


def test_default_profile_is_llmm():
    cfg = adjudicate.adjudication_config(spec_s30.card_template())
    assert cfg.profile == "llmm"
    assert cfg.api_key is None
    client = adjudicate.make_client(cfg)
    assert isinstance(client._primary, LLMMClient)
    assert client._fallback is None


def test_openai_profile_from_card_block(monkeypatch):
    monkeypatch.setenv("ISAR_LLM_API_KEY", "sk-env")
    card = spec_s30.card_template()
    card.policy.adjudication = {"profile": "openai", "model_pin": "gpt-oss-120b",
                                "endpoint": "https://host.example.test/v1"}
    cfg = adjudicate.adjudication_config(card)
    assert cfg.profile == "openai" and cfg.api_key == "sk-env"
    client = adjudicate.make_client(cfg)
    assert isinstance(client._primary, OpenAIClient)
    assert client._fallback is None            # openai profile has no auto dual-pin fallback


def test_env_overrides_profile_and_key(monkeypatch):
    monkeypatch.setenv("ISAR_LLM_PROFILE", "openai")
    monkeypatch.setenv("ISAR_LLM_API_KEY", "sk-env")
    cfg = adjudicate.adjudication_config(spec_s30.card_template())
    assert cfg.profile == "openai" and cfg.api_key == "sk-env"


def test_fixed_api_key_env_is_allowed(monkeypatch):
    monkeypatch.delenv("ISAR_LLM_API_KEY", raising=False)
    monkeypatch.setenv("ISAR_LLM_API_KEY", "sk-fixed")
    card = spec_s30.card_template()
    card.policy.adjudication = {"profile": "openai", "api_key_env": "ISAR_LLM_API_KEY"}
    cfg = adjudicate.adjudication_config(card)
    assert cfg.api_key == "sk-fixed"


def test_card_literal_or_arbitrary_environment_key_is_rejected():
    import pytest

    card = spec_s30.card_template()
    card.policy.adjudication = {"api_key": "sk-unsafe"}
    with pytest.raises(ValueError, match="ISAR_LLM_API_KEY"):
        adjudicate.adjudication_config(card)

    card.policy.adjudication = {"api_key_env": "UNRELATED_SECRET"}
    with pytest.raises(ValueError, match="arbitrary environment"):
        adjudicate.adjudication_config(card)

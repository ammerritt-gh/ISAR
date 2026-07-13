"""Sanctioned re-exports of ISAR's config/consent validators for the GUI (design ruling G16).

``isar_gui`` may reach ISAR's *thinking* only through this facade (G16); it must never import a
reducer/schema module directly. The GUI settings, credential, and card-policy editors legitimately
need three ISAR-owned validators — LLM endpoint safety, the cloud-fallback default, and card-policy
linting — so they are surfaced here rather than duplicated in Qt land (a duplicated URL/policy
validator would silently drift from the one the deterministic pipeline enforces).

All three are Qt-free and scipy-free pure functions/constants:
- ``validate_endpoint`` — the single LLM base-URL safety check (loopback-only plaintext, HTTPS
  otherwise, no embedded credentials); the same one :mod:`isar.reduce.llm_client` enforces.
- ``DEFAULT_CLOUD_FALLBACK_MODEL`` — the pinned cloud-fallback model the adjudicator defaults to.
- ``validate_policy_text`` / ``PolicyValidation`` — the ``card.policy.toml`` linter (hand-edited human
  intent; code never writes it, so the editor validates before the human saves).
"""
from __future__ import annotations

from isar.reduce.adjudicate import DEFAULT_CLOUD_FALLBACK_MODEL
from isar.reduce.llm_client import validate_endpoint
from isar.schema.card import PolicyValidation, validate_policy_text

__all__ = [
    "validate_endpoint",
    "DEFAULT_CLOUD_FALLBACK_MODEL",
    "validate_policy_text",
    "PolicyValidation",
]

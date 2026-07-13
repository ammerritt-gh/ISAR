"""Credential-store boundaries: endpoint-scoped names and loss-safe legacy migration."""
from __future__ import annotations

import pytest
import ctypes

from isar_gui import credentials
from isar_gui import session as gui_session


def test_credential_target_is_stable_scoped_and_does_not_expose_endpoint():
    endpoint = "https://example.test/v1"
    first = credentials.credential_target(endpoint)
    assert first == credentials.credential_target(endpoint + "/")
    assert first != credentials.credential_target("https://other.test/v1")
    assert endpoint not in first
    assert first.startswith("ISAR GUI LLM API Key/")


def test_credentials_are_not_available_off_windows(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    with pytest.raises(credentials.CredentialError, match="unavailable"):
        credentials.load_api_key("https://example.test")


def test_native_write_receives_hashed_target_and_endpoint_metadata(monkeypatch):
    written = {}

    def fake_write(pointer, flags):
        credential = ctypes.cast(pointer, credentials.PCREDENTIALW).contents
        written["target"] = credential.TargetName
        written["comment"] = credential.Comment
        written["blob"] = bytes(credential.CredentialBlob[:credential.CredentialBlobSize])
        return True

    monkeypatch.setattr(credentials, "_functions", lambda: (fake_write, None, None, None))
    credentials.store_api_key("https://example.test/v1", "secret")
    assert written["target"] == credentials.credential_target("https://example.test/v1")
    assert "example.test/v1" in written["comment"]
    assert written["blob"] == b"secret"


def test_legacy_key_migrates_before_session_scrub(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    legacy = gui_session.Session.from_dict({
        "view_state": {"settings": {"llm_endpoint": "https://example.test/v1",
                                        "llm_api_key": "old-secret"}}
    })
    calls = []
    monkeypatch.setattr(gui_session, "supported", lambda: True)
    monkeypatch.setattr(gui_session, "store_api_key", lambda endpoint, key: calls.append((endpoint, key)))
    assert gui_session.migrate_legacy_llm_key(legacy, path)
    assert calls == [("https://example.test/v1", "old-secret")]
    assert "llm_api_key" not in legacy.view_state["settings"]
    assert "old-secret" not in path.read_text(encoding="utf-8")


def test_legacy_key_is_retained_when_credential_write_fails(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    legacy = gui_session.Session.from_dict({
        "view_state": {"settings": {"llm_endpoint": "https://example.test/v1",
                                        "llm_api_key": "old-secret"}}
    })
    monkeypatch.setattr(gui_session, "supported", lambda: True)
    monkeypatch.setattr(gui_session, "store_api_key",
                        lambda *args: (_ for _ in ()).throw(gui_session.CredentialError("locked")))
    assert not gui_session.migrate_legacy_llm_key(legacy, path)
    assert legacy.view_state["settings"]["llm_api_key"] == "old-secret"
    assert legacy._retain_legacy_llm_key
    assert legacy.migration_warnings


def test_legacy_key_is_retained_when_scrub_save_fails(monkeypatch, tmp_path):
    legacy = gui_session.Session.from_dict({
        "view_state": {"settings": {"llm_endpoint": "https://example.test/v1",
                                        "llm_api_key": "old-secret"}}
    })
    monkeypatch.setattr(gui_session, "supported", lambda: True)
    monkeypatch.setattr(gui_session, "store_api_key", lambda *args: None)
    monkeypatch.setattr(gui_session, "save_session",
                        lambda *args: (_ for _ in ()).throw(OSError("read-only")))
    assert not gui_session.migrate_legacy_llm_key(legacy, tmp_path / "session.json")
    assert legacy.view_state["settings"]["llm_api_key"] == "old-secret"
    assert legacy._retain_legacy_llm_key


def test_normal_session_serialization_never_writes_plaintext_key():
    session = gui_session.Session(view_state={"settings": {"llm_api_key": "secret"}})
    assert "llm_api_key" not in session.to_dict()["view_state"]["settings"]

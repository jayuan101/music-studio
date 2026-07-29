"""Claude API key storage: OS credential store first, plaintext fallback."""

from __future__ import annotations

import keyring.errors
import pytest

from musicstudio.config import Settings
from musicstudio.core import secrets


class _FakeBackend:
    """Stands in for a real keyring backend, e.g. keyring.backends.SecretService."""

    __module__ = "keyring.backends.SecretService"


class _FailBackend:
    """Mirrors keyring's own "nothing installed" backend module path."""

    __module__ = "keyring.backends.fail"


@pytest.fixture
def fake_store(monkeypatch):
    """An in-memory stand-in for the OS credential store."""
    store: dict[tuple[str, str], str] = {}

    def fake_get(service, account):
        return store.get((service, account))

    def fake_set(service, account, value):
        store[(service, account)] = value

    def fake_delete(service, account):
        if (service, account) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, account)]

    monkeypatch.setattr(secrets.keyring, "get_password", fake_get)
    monkeypatch.setattr(secrets.keyring, "set_password", fake_set)
    monkeypatch.setattr(secrets.keyring, "delete_password", fake_delete)
    return store


# ---------------------------------------------------------------------------
# keyring_available
# ---------------------------------------------------------------------------


def test_keyring_available_true_with_a_real_backend(monkeypatch):
    monkeypatch.setattr(secrets.keyring, "get_keyring", lambda: _FakeBackend())
    assert secrets.keyring_available()


def test_keyring_available_false_with_the_fail_backend(monkeypatch):
    monkeypatch.setattr(secrets.keyring, "get_keyring", lambda: _FailBackend())
    assert not secrets.keyring_available()


def test_keyring_available_false_when_get_keyring_raises(monkeypatch):
    def boom():
        raise keyring.errors.KeyringError("no backend")

    monkeypatch.setattr(secrets.keyring, "get_keyring", boom)
    assert not secrets.keyring_available()


# ---------------------------------------------------------------------------
# get/set/delete, with keyring available
# ---------------------------------------------------------------------------


def test_set_then_get_round_trips_through_the_keyring(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings()

    stored_in_keyring = secrets.set_claude_api_key(settings, "sk-ant-real-key")

    assert stored_in_keyring
    assert settings.ai_claude_api_key == ""  # never duplicated in plaintext
    assert secrets.get_claude_api_key(settings) == "sk-ant-real-key"


def test_set_clears_any_stale_plaintext_field_when_keyring_succeeds(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings(ai_claude_api_key="stale-plaintext-key")

    secrets.set_claude_api_key(settings, "sk-ant-new-key")

    assert settings.ai_claude_api_key == ""
    assert secrets.get_claude_api_key(settings) == "sk-ant-new-key"


def test_get_falls_back_to_plaintext_when_keyring_has_nothing_stored(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings(ai_claude_api_key="plaintext-key")
    assert secrets.get_claude_api_key(settings) == "plaintext-key"


def test_delete_removes_from_both_the_keyring_and_settings(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings()
    secrets.set_claude_api_key(settings, "sk-ant-real-key")

    secrets.delete_claude_api_key(settings)

    assert settings.ai_claude_api_key == ""
    assert secrets.get_claude_api_key(settings) == ""


def test_delete_is_safe_to_call_when_nothing_was_ever_stored(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings()
    secrets.delete_claude_api_key(settings)  # must not raise
    assert secrets.get_claude_api_key(settings) == ""


def test_setting_an_empty_string_deletes_rather_than_stores(monkeypatch, fake_store):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    settings = Settings()
    secrets.set_claude_api_key(settings, "sk-ant-real-key")

    secrets.set_claude_api_key(settings, "")

    assert secrets.get_claude_api_key(settings) == ""


# ---------------------------------------------------------------------------
# Fallback path: no keyring backend available at all
# ---------------------------------------------------------------------------


def test_set_falls_back_to_plaintext_settings_when_no_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)
    settings = Settings()

    stored_in_keyring = secrets.set_claude_api_key(settings, "sk-ant-real-key")

    assert not stored_in_keyring
    assert settings.ai_claude_api_key == "sk-ant-real-key"
    assert secrets.get_claude_api_key(settings) == "sk-ant-real-key"


def test_get_reads_plaintext_when_no_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)
    settings = Settings(ai_claude_api_key="plaintext-key")
    assert secrets.get_claude_api_key(settings) == "plaintext-key"


def test_delete_falls_back_to_clearing_settings_only(monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)
    settings = Settings(ai_claude_api_key="plaintext-key")
    secrets.delete_claude_api_key(settings)
    assert settings.ai_claude_api_key == ""


# ---------------------------------------------------------------------------
# A keyring that raises mid-operation must not crash -- fall back cleanly
# ---------------------------------------------------------------------------


def test_get_password_error_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)

    def boom(service, account):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(secrets.keyring, "get_password", boom)
    settings = Settings(ai_claude_api_key="plaintext-key")
    assert secrets.get_claude_api_key(settings) == "plaintext-key"


def test_set_password_error_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: True)

    def boom(service, account, value):
        raise keyring.errors.PasswordSetError("denied")

    monkeypatch.setattr(secrets.keyring, "set_password", boom)
    settings = Settings()

    stored_in_keyring = secrets.set_claude_api_key(settings, "sk-ant-real-key")

    assert not stored_in_keyring
    assert settings.ai_claude_api_key == "sk-ant-real-key"

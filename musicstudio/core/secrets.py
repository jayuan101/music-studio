"""Storage for API credentials that should not sit in plaintext: the Claude
API key and the Spotify Client Secret.

Prefers the OS credential store (the Windows Credential Locker, via
``keyring``) so a credential never sits in plaintext on disk. Falls back to
the matching plaintext ``Settings`` field when no keyring backend is
available -- the app must still work without one, just with a visible
warning, which is the UI's responsibility (see ``config.py``).
"""

from __future__ import annotations

import keyring
import keyring.errors

from ..config import Settings

_SERVICE = "MusicStudio"


def keyring_available() -> bool:
    """Whether a real OS credential store is usable right now.

    ``keyring`` transparently falls back to its own ``fail.Keyring`` when no
    real backend is installed -- every call on it raises ``NoKeyringError``,
    so that's treated the same as "no keyring" rather than a surprising error.
    """
    try:
        backend = keyring.get_keyring()
    except keyring.errors.KeyringError:
        return False
    return type(backend).__module__ != "keyring.backends.fail"


def _get_secret(account: str, fallback: str) -> str:
    if keyring_available():
        try:
            value = keyring.get_password(_SERVICE, account)
        except keyring.errors.KeyringError:
            value = None
        if value:
            return value
    return fallback


def _set_secret(account: str, value: str, settings: Settings, field: str) -> bool:
    """Store ``value`` under ``account``, preferring the OS credential store.

    Returns True if it went into the OS store, False if it fell back to
    plaintext in ``settings`` -- the caller should warn the user when this
    happens. Either way, the field that ends up *not* authoritative is
    cleared, so the credential is never left duplicated in both places.
    """
    if not value:
        _delete_secret(account, settings, field)
        return keyring_available()

    if keyring_available():
        try:
            keyring.set_password(_SERVICE, account, value)
        except keyring.errors.KeyringError:
            pass
        else:
            setattr(settings, field, "")
            settings.save()
            return True

    setattr(settings, field, value)
    settings.save()
    return False


def _delete_secret(account: str, settings: Settings, field: str) -> None:
    setattr(settings, field, "")
    settings.save()
    if keyring_available():
        try:
            keyring.delete_password(_SERVICE, account)
        except keyring.errors.KeyringError:
            pass


# -- Claude API key -------------------------------------------------------


def get_claude_api_key(settings: Settings) -> str:
    """The stored Claude API key: from the OS credential store if one is
    available and holds a value, otherwise the plaintext fallback field."""
    return _get_secret("claude_api_key", settings.ai_claude_api_key)


def set_claude_api_key(settings: Settings, api_key: str) -> bool:
    return _set_secret("claude_api_key", api_key, settings, "ai_claude_api_key")


def delete_claude_api_key(settings: Settings) -> None:
    """Remove the stored key from wherever it lives."""
    _delete_secret("claude_api_key", settings, "ai_claude_api_key")


# -- Spotify Client Secret --------------------------------------------------
# The Client ID is not sensitive on its own (it's visible in every request)
# and lives in plain Settings; only the Secret goes through here.


def get_spotify_client_secret(settings: Settings) -> str:
    return _get_secret("spotify_client_secret", settings.spotify_client_secret)


def set_spotify_client_secret(settings: Settings, secret: str) -> bool:
    return _set_secret("spotify_client_secret", secret, settings, "spotify_client_secret")


def delete_spotify_client_secret(settings: Settings) -> None:
    _delete_secret("spotify_client_secret", settings, "spotify_client_secret")

"""Storage for the Claude API key.

Prefers the OS credential store (the Windows Credential Locker, via
``keyring``) so the key never sits in plaintext on disk. Falls back to the
plaintext ``Settings.ai_claude_api_key`` field when no keyring backend is
available -- the app must still work without one, just with a visible
warning, which is the UI's responsibility (see ``config.py``).
"""

from __future__ import annotations

import keyring
import keyring.errors

from ..config import Settings

_SERVICE = "MusicStudio"
_ACCOUNT = "claude_api_key"


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


def get_claude_api_key(settings: Settings) -> str:
    """The stored Claude API key: from the OS credential store if one is
    available and holds a value, otherwise the plaintext fallback field."""
    if keyring_available():
        try:
            value = keyring.get_password(_SERVICE, _ACCOUNT)
        except keyring.errors.KeyringError:
            value = None
        if value:
            return value
    return settings.ai_claude_api_key


def set_claude_api_key(settings: Settings, api_key: str) -> bool:
    """Store ``api_key``, preferring the OS credential store.

    Returns True if it went into the OS store, False if it fell back to
    plaintext in ``settings`` -- the caller should warn the user when this
    happens. Either way, the field that ends up *not* authoritative is
    cleared, so the key is never left duplicated in both places.
    """
    if not api_key:
        delete_claude_api_key(settings)
        return keyring_available()

    if keyring_available():
        try:
            keyring.set_password(_SERVICE, _ACCOUNT, api_key)
        except keyring.errors.KeyringError:
            pass
        else:
            settings.ai_claude_api_key = ""
            settings.save()
            return True

    settings.ai_claude_api_key = api_key
    settings.save()
    return False


def delete_claude_api_key(settings: Settings) -> None:
    """Remove the stored key from wherever it lives."""
    settings.ai_claude_api_key = ""
    settings.save()
    if keyring_available():
        try:
            keyring.delete_password(_SERVICE, _ACCOUNT)
        except keyring.errors.KeyringError:
            pass

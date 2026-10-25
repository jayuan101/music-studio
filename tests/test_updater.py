"""Update checks tolerate service failures and malformed release metadata."""

from unittest.mock import MagicMock

import httpx
import pytest

from musicstudio.core import updater


def mock_response(monkeypatch, payload):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value.json.return_value = payload
    monkeypatch.setattr(updater.httpx, "Client", lambda **kwargs: client)
    return client


@pytest.mark.parametrize("payload", [
    [], None, {"tag_name": 123},
    {"tag_name": "v99.0.0", "assets": None},
    {"tag_name": "v99.0.0", "assets": [None]},
    {"tag_name": "v99.0.0", "assets": [{"name": "app.zip"}]},
    {"tag_name": "v99.0.0", "assets": [{"name": 123}]},
])
def test_malformed_release_does_not_crash(monkeypatch, payload):
    mock_response(monkeypatch, payload)
    assert updater.check_for_update() is None


def test_valid_new_release(monkeypatch):
    mock_response(monkeypatch, {
        "tag_name": "v99.0.0", "body": " Fixes ",
        "assets": [{"name": "MusicStudio-Windows.zip",
                    "browser_download_url": "https://example.com/app.zip", "size": 42}],
    })
    info = updater.check_for_update()
    assert info.version == "99.0.0"
    assert info.notes == "Fixes"
    assert info.size == 42


def test_offline_check_does_not_crash(monkeypatch):
    client = mock_response(monkeypatch, {})
    client.get.side_effect = httpx.ConnectError("offline")
    assert updater.check_for_update() is None


@pytest.mark.parametrize("size", [None, "large", -1, True])
def test_invalid_optional_fields_are_safe(monkeypatch, size):
    mock_response(monkeypatch, {
        "tag_name": "v99.0.0", "body": {"unexpected": "object"},
        "assets": [{"name": "app.zip",
                    "browser_download_url": "https://example.com/app.zip", "size": size}],
    })
    info = updater.check_for_update()
    assert info.notes == ""
    assert info.size == 0


def test_current_release_is_not_offered(monkeypatch):
    mock_response(monkeypatch, {"tag_name": updater.__version__, "assets": []})
    assert updater.check_for_update() is None

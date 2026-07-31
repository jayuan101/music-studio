"""Spotify search: the Client Credentials token flow, track search, and the
"no credentials configured" no-op path. HTTP is mocked throughout.
"""

from __future__ import annotations

import httpx
import pytest

from musicstudio.config import Settings
from musicstudio.core import secrets, spotify


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class FakeClient:
    """Stands in for httpx.Client, routing GET/POST by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _dispatch(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response() if callable(response) else response
        return FakeResponse(status_code=404)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Deterministic: credentials always come from the plaintext Settings
    field, regardless of what the host machine's keyring holds."""
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)


@pytest.fixture(autouse=True)
def clear_cache():
    spotify.clear_token_cache()
    yield
    spotify.clear_token_cache()


def install_client(monkeypatch, routes) -> FakeClient:
    client = FakeClient(routes)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return client


def configured_settings(**overrides) -> Settings:
    fields = {
        "spotify_enabled": True,
        "spotify_client_id": "test-client-id",
        "spotify_client_secret": "test-client-secret",
    }
    fields.update(overrides)
    return Settings(**fields)


TOKEN_HIT = FakeResponse(json_data={"access_token": "fake-token-123", "expires_in": 3600})

SEARCH_HIT = FakeResponse(
    json_data={
        "tracks": {
            "items": [
                {
                    "name": "Cheques",
                    "artists": [{"name": "Shubh"}],
                    "album": {
                        "name": "Still Rollin",
                        "release_date": "2023-06-27",
                        "images": [{"url": "https://i.scdn.co/image/big.jpg"}],
                    },
                }
            ]
        }
    }
)
SEARCH_EMPTY = FakeResponse(json_data={"tracks": {"items": []}})


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


def test_not_configured_when_disabled():
    settings = configured_settings(spotify_enabled=False)
    assert not spotify.is_configured(settings)


def test_not_configured_without_client_id():
    settings = configured_settings(spotify_client_id="")
    assert not spotify.is_configured(settings)


def test_not_configured_without_client_secret():
    settings = configured_settings(spotify_client_secret="")
    assert not spotify.is_configured(settings)


def test_configured_with_everything_present():
    assert spotify.is_configured(configured_settings())


# ---------------------------------------------------------------------------
# Token fetching and caching
# ---------------------------------------------------------------------------


def test_get_token_succeeds_and_authenticates_with_client_credentials(monkeypatch):
    client = install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT})
    token = spotify._get_token(configured_settings())

    assert token == "fake-token-123"
    method, url, kwargs = client.calls[0]
    assert method == "POST"
    assert kwargs["data"] == {"grant_type": "client_credentials"}
    assert kwargs["auth"] == ("test-client-id", "test-client-secret")


def test_get_token_is_cached_across_calls(monkeypatch):
    client = install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT})
    settings = configured_settings()

    spotify._get_token(settings)
    spotify._get_token(settings)

    assert len(client.calls) == 1, "second call should reuse the cached token, not re-authenticate"


def test_get_token_refetches_after_expiry(monkeypatch):
    client = install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT})
    settings = configured_settings()
    spotify._get_token(settings)

    # Jump time forward past the cached expiry.
    import time

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 7200)
    spotify._get_token(settings)

    assert len(client.calls) == 2


def test_get_token_none_without_credentials():
    assert spotify._get_token(Settings(spotify_client_id="", spotify_client_secret="")) is None


def test_get_token_none_on_http_error(monkeypatch):
    install_client(monkeypatch, {"accounts.spotify.com": FakeResponse(status_code=400)})
    assert spotify._get_token(configured_settings()) is None


# ---------------------------------------------------------------------------
# find_track
# ---------------------------------------------------------------------------


def test_find_track_returns_none_when_not_configured():
    settings = configured_settings(spotify_enabled=False)
    assert spotify.find_track("Shubh", "Cheques", settings=settings) is None


def test_find_track_returns_none_for_a_blank_query(monkeypatch):
    install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT, "api.spotify.com": SEARCH_HIT})
    assert spotify.find_track("", "", "", settings=configured_settings()) is None


def test_find_track_parses_the_top_result(monkeypatch):
    install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT, "api.spotify.com": SEARCH_HIT})

    match = spotify.find_track("Shubh", "Cheques", settings=configured_settings())

    assert match is not None
    assert match.title == "Cheques"
    assert match.artist == "Shubh"
    assert match.album == "Still Rollin"
    assert match.year == "2023"
    assert match.image_url == "https://i.scdn.co/image/big.jpg"


def test_find_track_none_when_search_is_empty(monkeypatch):
    install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT, "api.spotify.com": SEARCH_EMPTY})
    assert spotify.find_track("Nobody", "Nothing", settings=configured_settings()) is None


def test_find_track_none_when_token_fetch_fails(monkeypatch):
    install_client(monkeypatch, {"accounts.spotify.com": FakeResponse(status_code=401)})
    assert spotify.find_track("Shubh", "Cheques", settings=configured_settings()) is None


def test_find_track_handles_a_result_with_no_images(monkeypatch):
    no_image_hit = FakeResponse(
        json_data={
            "tracks": {
                "items": [
                    {
                        "name": "Instrumental",
                        "artists": [{"name": "Someone"}],
                        "album": {"name": "Album", "release_date": "2020-01-01", "images": []},
                    }
                ]
            }
        }
    )
    install_client(monkeypatch, {"accounts.spotify.com": TOKEN_HIT, "api.spotify.com": no_image_hit})

    match = spotify.find_track("Someone", "Instrumental", settings=configured_settings())

    assert match is not None
    assert match.image_url == ""


# ---------------------------------------------------------------------------
# fetch_image
# ---------------------------------------------------------------------------


def test_fetch_image_returns_bytes(monkeypatch):
    install_client(monkeypatch, {"i.scdn.co": FakeResponse(content=b"\x89PNGfakebytes")})
    assert spotify.fetch_image("https://i.scdn.co/image/big.jpg") == b"\x89PNGfakebytes"


def test_fetch_image_none_for_blank_url():
    assert spotify.fetch_image("") is None


def test_fetch_image_none_on_http_error(monkeypatch):
    install_client(monkeypatch, {"i.scdn.co": FakeResponse(status_code=404)})
    assert spotify.fetch_image("https://i.scdn.co/image/missing.jpg") is None

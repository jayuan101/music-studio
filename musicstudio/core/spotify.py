"""Cover art and metadata via the Spotify Web API.

Spotify's catalogue and cover-art quality are excellent -- generally better
matched than iTunes for anything outside mainstream English-language pop --
but unlike MusicBrainz and iTunes, its API requires credentials: a free
developer app's Client ID and Secret (the Client Credentials flow, which is
app-level access for searching and needs no user login or redirect, just the
two values from https://developer.spotify.com/dashboard).

Configure both in Preferences. Without them, every function here returns
None immediately rather than raising, so a lookup silently falls through to
the next provider -- the same "missing credentials looks like a cache miss,
not a crash" shape as the rest of this app's optional integrations.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import Settings, get_settings
from . import secrets

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"

#: Cached per client ID so repeated lookups in one session don't
#: re-authenticate every time; Spotify's own tokens last about an hour.
_token_cache: dict[str, tuple[str, float]] = {}


def is_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.spotify_enabled
        and settings.spotify_client_id
        and secrets.get_spotify_client_secret(settings)
    )


def clear_token_cache() -> None:
    """Forget cached access tokens -- used when credentials change, so a
    stale token from the old app is never sent under the new one."""
    _token_cache.clear()


def _get_token(settings: Settings) -> str | None:
    client_id = settings.spotify_client_id
    client_secret = secrets.get_spotify_client_secret(settings)
    if not client_id or not client_secret:
        return None

    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    token = data.get("access_token")
    if not token:
        return None
    # Refresh a minute early rather than risk a request landing right as it expires.
    _token_cache[client_id] = (token, time.monotonic() + float(data.get("expires_in", 3600)) - 60)
    return token


def _search_tracks(query: str, settings: Settings, *, limit: int = 5) -> list[dict]:
    token = _get_token(settings)
    if not token:
        return []
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                SEARCH_URL,
                params={"q": query, "type": "track", "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    return (data.get("tracks") or {}).get("items") or []


@dataclass
class SpotifyMatch:
    """One track result, with whatever a caller might want: tags or art."""

    title: str
    artist: str
    album: str
    year: str
    image_url: str


def find_track(
    artist: str, title: str = "", album: str = "", *, settings: Settings | None = None
) -> SpotifyMatch | None:
    """Search Spotify for the best-matching track.

    Spotify already ranks its own results by relevance, so the top hit is
    used directly rather than re-scoring -- unlike iTunes, which returns no
    match confidence of its own and needs :func:`artwork._itunes_score`.
    """
    settings = settings or get_settings()
    if not is_configured(settings):
        return None

    term = " ".join(part for part in (artist, title, album) if part).strip()
    if not term:
        return None

    items = _search_tracks(term, settings)
    if not items:
        return None

    best = items[0]
    album_info = best.get("album") or {}
    images = album_info.get("images") or []
    release_date = album_info.get("release_date", "")

    return SpotifyMatch(
        title=best.get("name", ""),
        artist=", ".join(a.get("name", "") for a in best.get("artists", []) if a.get("name")),
        album=album_info.get("name", ""),
        year=release_date[:4] if release_date else "",
        image_url=images[0]["url"] if images else "",
    )


def fetch_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError:
        return None

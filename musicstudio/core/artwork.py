"""Finding cover art online and keeping it up to date.

Two providers, neither requiring an API key or signup:

* **MusicBrainz + Cover Art Archive** -- an open music database. Searching it
  gives a release MBID, which the Cover Art Archive serves artwork for. Most
  accurate, because the match is to a specific release rather than a text guess.
* **iTunes Search API** -- broad, fast, and reliably high resolution. Used when
  MusicBrainz has no art, which is common for recent pop and for non-Western
  releases.

MusicBrainz asks that clients identify themselves and stay under one request
per second. :class:`RateLimiter` enforces that for real, across threads, because
ignoring it gets clients blocked.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import __version__
from ..config import ARTWORK_CACHE_DIR, Settings, get_settings
from .tags import Artwork, TagSet

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
COVERART_API = "https://coverartarchive.org"
ITUNES_API = "https://itunes.apple.com/search"

#: MusicBrainz's published rate limit for anonymous clients.
MUSICBRAINZ_MIN_INTERVAL = 1.0
#: Anything smaller than this is a placeholder or a spacer image, not cover art.
MIN_USABLE_BYTES = 1024


class ArtworkError(RuntimeError):
    """Raised when a lookup fails in a way worth reporting."""


@dataclass(frozen=True)
class ArtworkCandidate:
    """One cover image found online, before it is embedded."""

    data: bytes
    source: str            # "Cover Art Archive" / "iTunes"
    url: str
    width: int = 0
    height: int = 0
    #: How confident the match is, 0..1. Used to rank candidates.
    score: float = 0.0
    release_title: str = ""
    release_artist: str = ""

    @property
    def size_label(self) -> str:
        return f"{self.width}x{self.height}" if self.width else f"{len(self.data) // 1024} KB"

    def to_artwork(self) -> Artwork:
        art = Artwork.from_bytes(self.data)
        art.description = "Cover"
        return art


class RateLimiter:
    """Enforce a minimum interval between calls, safely across threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_call = time.monotonic()


_musicbrainz_limiter = RateLimiter(MUSICBRAINZ_MIN_INTERVAL)


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _cache_key(artist: str, album: str, size: int) -> str:
    raw = f"{artist.strip().lower()}|{album.strip().lower()}|{size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_path(artist: str, album: str, size: int) -> Path:
    return ARTWORK_CACHE_DIR / f"{_cache_key(artist, album, size)}.bin"


def _cache_miss_path(artist: str, album: str, size: int) -> Path:
    """Marker recording that a lookup found nothing.

    Without this, a library full of untagged files re-queries both providers on
    every single scan -- slow for the user and rude to free services.
    """
    return ARTWORK_CACHE_DIR / f"{_cache_key(artist, album, size)}.miss"


def read_cache(artist: str, album: str, size: int) -> bytes | None:
    path = _cache_path(artist, album, size)
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def is_cached_miss(artist: str, album: str, size: int, max_age_days: float = 30.0) -> bool:
    path = _cache_miss_path(artist, album, size)
    if not path.is_file():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < max_age_days


def write_cache(artist: str, album: str, size: int, data: bytes | None) -> None:
    ARTWORK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if data:
        try:
            _cache_path(artist, album, size).write_bytes(data)
        except OSError:
            pass
    else:
        try:
            _cache_miss_path(artist, album, size).touch()
        except OSError:
            pass


def clear_cache() -> int:
    """Delete every cached image and miss marker. Returns files removed."""
    if not ARTWORK_CACHE_DIR.is_dir():
        return 0
    removed = 0
    for path in ARTWORK_CACHE_DIR.iterdir():
        if path.suffix in (".bin", ".miss"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _user_agent(settings: Settings) -> str:
    return settings.musicbrainz_user_agent or f"MusicStudio/{__version__}"


def _client(settings: Settings, timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _user_agent(settings)},
    )


# ---------------------------------------------------------------------------
# MusicBrainz + Cover Art Archive
# ---------------------------------------------------------------------------


def _build_musicbrainz_query(artist: str, album: str) -> str:
    """Build a Lucene query, quoting values so punctuation cannot break it."""

    def escape(value: str) -> str:
        for char in '+-&|!(){}[]^"~*?:\\/':
            value = value.replace(char, f"\\{char}")
        return value

    parts = []
    if album:
        parts.append(f'release:"{escape(album)}"')
    if artist:
        parts.append(f'artist:"{escape(artist)}"')
    return " AND ".join(parts)


def search_musicbrainz(
    artist: str,
    album: str,
    *,
    settings: Settings | None = None,
    limit: int = 5,
) -> list[dict]:
    """Find candidate releases. Returns raw release dicts, best match first."""
    settings = settings or get_settings()
    if not album and not artist:
        return []

    query = _build_musicbrainz_query(artist, album)
    if not query:
        return []

    _musicbrainz_limiter.wait()
    try:
        with _client(settings) as client:
            response = client.get(
                f"{MUSICBRAINZ_API}/release",
                params={"query": query, "fmt": "json", "limit": limit},
            )
            response.raise_for_status()
            return response.json().get("releases", [])
    except (httpx.HTTPError, ValueError):
        return []


def fetch_coverart_archive(
    release_mbid: str,
    size: int = 1200,
    *,
    settings: Settings | None = None,
) -> bytes | None:
    """Download the front cover for a release from the Cover Art Archive."""
    settings = settings or get_settings()
    # The archive only renders these thumbnail sizes; anything else 404s.
    thumbnail = min((250, 500, 1200), key=lambda s: abs(s - size))
    urls = [
        f"{COVERART_API}/release/{release_mbid}/front-{thumbnail}",
        f"{COVERART_API}/release/{release_mbid}/front",
    ]
    try:
        with _client(settings) as client:
            for url in urls:
                try:
                    response = client.get(url)
                except httpx.HTTPError:
                    continue
                if response.status_code == 200 and len(response.content) >= MIN_USABLE_BYTES:
                    return response.content
    except httpx.HTTPError:
        return None
    return None


def lookup_musicbrainz(
    artist: str,
    album: str,
    size: int = 1200,
    *,
    settings: Settings | None = None,
) -> ArtworkCandidate | None:
    """Search MusicBrainz, then pull art for the best-scoring release with any."""
    settings = settings or get_settings()
    releases = search_musicbrainz(artist, album, settings=settings)
    for release in releases:
        mbid = release.get("id")
        if not mbid:
            continue
        data = fetch_coverart_archive(mbid, size, settings=settings)
        if not data:
            continue
        art = Artwork.from_bytes(data)
        credit = release.get("artist-credit") or []
        release_artist = credit[0].get("name", "") if credit else ""
        return ArtworkCandidate(
            data=data,
            source="Cover Art Archive",
            url=f"{COVERART_API}/release/{mbid}/front",
            width=art.width,
            height=art.height,
            # MusicBrainz scores matches 0-100; normalise to 0..1.
            score=min(1.0, float(release.get("score", 0)) / 100.0),
            release_title=release.get("title", ""),
            release_artist=release_artist,
        )
    return None


# ---------------------------------------------------------------------------
# iTunes
# ---------------------------------------------------------------------------


def _upgrade_itunes_url(url: str, size: int) -> str:
    """Rewrite an iTunes artwork URL to request full resolution.

    The API always returns a 100x100 thumbnail URL, but the same path serves
    any size if you substitute the dimensions -- that is how you get 1200px art
    out of an endpoint that appears to only offer postage stamps.
    """
    for token in ("100x100bb", "100x100"):
        if token in url:
            return url.replace(token, f"{size}x{size}bb")
    return url


def lookup_itunes(
    artist: str,
    album: str,
    size: int = 1200,
    *,
    settings: Settings | None = None,
    title: str = "",
) -> ArtworkCandidate | None:
    """Search the iTunes catalogue for album art."""
    settings = settings or get_settings()
    term = " ".join(part for part in (artist, album or title) if part).strip()
    if not term:
        return None

    try:
        with _client(settings, timeout=15.0) as client:
            response = client.get(
                ITUNES_API,
                params={
                    "term": term,
                    "entity": "album" if album else "song",
                    "limit": 5,
                    "media": "music",
                },
            )
            response.raise_for_status()
            results = response.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return None

    for result in results:
        thumbnail_url = result.get("artworkUrl100") or result.get("artworkUrl60")
        if not thumbnail_url:
            continue
        full_url = _upgrade_itunes_url(thumbnail_url, size)
        try:
            with _client(settings) as client:
                response = client.get(full_url)
                if response.status_code != 200 or len(response.content) < MIN_USABLE_BYTES:
                    # Fall back to the original thumbnail rather than nothing.
                    response = client.get(thumbnail_url)
                    if response.status_code != 200:
                        continue
                data = response.content
        except httpx.HTTPError:
            continue

        art = Artwork.from_bytes(data)
        return ArtworkCandidate(
            data=data,
            source="iTunes",
            url=full_url,
            width=art.width,
            height=art.height,
            score=_itunes_score(result, artist, album),
            release_title=result.get("collectionName", ""),
            release_artist=result.get("artistName", ""),
        )
    return None


def _itunes_score(result: dict, artist: str, album: str) -> float:
    """Rough confidence, since iTunes returns no match score of its own."""
    score = 0.5
    result_artist = (result.get("artistName") or "").lower()
    result_album = (result.get("collectionName") or "").lower()
    if artist and result_artist and artist.lower() in result_artist:
        score += 0.25
    if album and result_album and album.lower() in result_album:
        score += 0.25
    return min(1.0, score)


# ---------------------------------------------------------------------------
# Combined lookup
# ---------------------------------------------------------------------------


def find_artwork(
    artist: str,
    album: str,
    *,
    title: str = "",
    size: int | None = None,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> ArtworkCandidate | None:
    """Look up cover art, trying every enabled provider in order of accuracy.

    Results and misses are both cached on disk, so a repeated library scan
    costs nothing and does not hammer free services.
    """
    settings = settings or get_settings()
    size = size or settings.artwork_preferred_size

    if not artist and not album and not title:
        return None

    cache_artist, cache_album = artist, album or title
    if use_cache:
        cached = read_cache(cache_artist, cache_album, size)
        if cached:
            art = Artwork.from_bytes(cached)
            return ArtworkCandidate(
                data=cached, source="cache", url="", width=art.width, height=art.height, score=1.0
            )
        if is_cached_miss(cache_artist, cache_album, size):
            return None

    candidate = None
    if settings.artwork_use_musicbrainz and album:
        candidate = lookup_musicbrainz(artist, album, size, settings=settings)
    if candidate is None and settings.artwork_use_itunes:
        candidate = lookup_itunes(artist, album, size, settings=settings, title=title)

    if use_cache:
        write_cache(cache_artist, cache_album, size, candidate.data if candidate else None)
    return candidate


def find_all_candidates(
    artist: str,
    album: str,
    *,
    title: str = "",
    size: int | None = None,
    settings: Settings | None = None,
) -> list[ArtworkCandidate]:
    """Every provider's best result, for the 'choose an image' picker.

    Unlike :func:`find_artwork` this deliberately queries all providers and
    skips the cache, because the user is asking to see the options.
    """
    settings = settings or get_settings()
    size = size or settings.artwork_preferred_size
    candidates: list[ArtworkCandidate] = []

    if settings.artwork_use_musicbrainz and album:
        found = lookup_musicbrainz(artist, album, size, settings=settings)
        if found:
            candidates.append(found)
    if settings.artwork_use_itunes:
        found = lookup_itunes(artist, album, size, settings=settings, title=title)
        if found:
            candidates.append(found)

    # Prefer confident matches, then bigger images.
    candidates.sort(key=lambda c: (c.score, c.width * c.height), reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Applying artwork to files
# ---------------------------------------------------------------------------


def needs_artwork(tags: TagSet, settings: Settings | None = None) -> bool:
    """Whether this file should be given new art.

    True when there is none, or when what is embedded is below the minimum
    resolution the user set -- that second case is what "keep art up to date"
    means in practice, since a lot of libraries carry 200px thumbnails from a
    decade ago.
    """
    settings = settings or get_settings()
    if not tags.has_artwork():
        return True
    art = tags.artwork
    assert art is not None
    if art.width and art.width < settings.artwork_min_size:
        return True
    if art.height and art.height < settings.artwork_min_size:
        return True
    # Unknown dimensions: judge by file size as a rough proxy.
    if not art.width and len(art.data) < 40 * 1024:
        return True
    return False


@dataclass
class ArtworkUpdate:
    """Outcome of trying to give one file cover art."""

    path: Path
    updated: bool
    reason: str
    candidate: ArtworkCandidate | None = None


def update_file_artwork(
    path: str | Path,
    *,
    settings: Settings | None = None,
    force: bool = False,
    context=None,
) -> ArtworkUpdate:
    """Fetch and embed cover art for one file.

    Skips files that already have art good enough, unless ``force``.
    """
    from . import tags as tags_module

    settings = settings or get_settings()
    path = Path(path)

    try:
        existing = tags_module.read(path)
    except (tags_module.TagError, OSError) as exc:
        return ArtworkUpdate(path, False, f"Could not read tags: {exc}")

    if not force and not needs_artwork(existing, settings):
        art = existing.artwork
        return ArtworkUpdate(path, False, f"Already has {art.size_label} art" if art else "Has art")

    artist = existing.effective_albumartist
    album = existing.album
    if not artist and not album and not existing.title:
        return ArtworkUpdate(path, False, "No artist or album tag to search with")

    if context is not None:
        context.progress(None, f"Looking up art for {album or existing.title}…")

    candidate = find_artwork(
        artist, album, title=existing.title, settings=settings, use_cache=not force
    )
    if candidate is None:
        return ArtworkUpdate(path, False, "No cover art found")

    try:
        tags_module.write(path, existing, artwork=candidate.to_artwork())
    except tags_module.TagError as exc:
        return ArtworkUpdate(path, False, f"Could not embed art: {exc}", candidate)

    return ArtworkUpdate(
        path, True, f"Added {candidate.size_label} from {candidate.source}", candidate
    )


def update_library_artwork(
    paths: list[Path],
    *,
    settings: Settings | None = None,
    force: bool = False,
    context=None,
) -> list[ArtworkUpdate]:
    """Bulk 'update all artwork' over a set of files.

    One file failing never stops the batch -- a library scan that aborts on the
    first unreadable file is useless.
    """
    settings = settings or get_settings()
    results: list[ArtworkUpdate] = []
    total = len(paths)

    for index, path in enumerate(paths):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Artwork: {Path(path).name}")
        try:
            results.append(update_file_artwork(path, settings=settings, force=force))
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            results.append(ArtworkUpdate(Path(path), False, f"Failed: {exc}"))

    if context is not None:
        context.progress(1.0, f"Updated {sum(1 for r in results if r.updated)} of {total}")
    return results

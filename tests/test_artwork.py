"""Cover art lookup.

HTTP is mocked throughout: these tests verify the provider chain, caching and
rate limiting, not that MusicBrainz is reachable.
"""

from __future__ import annotations

import time

import httpx
import pytest

from musicstudio.config import Settings, get_settings
from musicstudio.core import artwork
from musicstudio.core import spotify as spotify_module
from musicstudio.core import tags as T


@pytest.fixture(autouse=True)
def artwork_dirs():
    from musicstudio import config

    config.ARTWORK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # The module captured the directory at import time; point it at the
    # per-test location so caching does not leak between tests.
    original = artwork.ARTWORK_CACHE_DIR
    artwork.ARTWORK_CACHE_DIR = config.ARTWORK_CACHE_DIR
    yield
    artwork.ARTWORK_CACHE_DIR = original


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class FakeStream:
    """A streamed FakeResponse, used as ``with client.stream(...) as r``."""

    def __init__(self, response, chunk_size=8192):
        self._response = response
        self._chunk_size = chunk_size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def status_code(self):
        return self._response.status_code

    @property
    def headers(self):
        return getattr(self._response, "headers", {})

    def raise_for_status(self):
        self._response.raise_for_status()

    def iter_bytes(self):
        content = self._response.content
        for start in range(0, len(content), self._chunk_size):
            yield content[start : start + self._chunk_size]


class FakeClient:
    """Stands in for httpx.Client, routing by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _match(self, url):
        for fragment, response in self.routes.items():
            if fragment in url:
                return response() if callable(response) else response
        return FakeResponse(status_code=404)

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        return self._match(url)

    def stream(self, _method, url, **kwargs):
        self.calls.append((url, None))
        return FakeStream(self._match(url))


def install_client(monkeypatch, routes) -> FakeClient:
    client = FakeClient(routes)
    monkeypatch.setattr(artwork, "_client", lambda *a, **k: client)
    return client


MB_HIT = FakeResponse(
    json_data={
        "releases": [
            {"id": "mbid-1", "score": 95, "title": "Neon Cartography",
             "artist-credit": [{"name": "The Rearview"}]}
        ]
    }
)
MB_EMPTY = FakeResponse(json_data={"releases": []})


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


def test_musicbrainz_hit_returns_cover_art_archive_image(monkeypatch, cover_png):
    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })
    found = artwork.find_artwork("The Rearview", "Neon Cartography", use_cache=False)
    assert found is not None
    assert found.source == "Cover Art Archive"
    assert found.data == cover_png
    assert found.score == pytest.approx(0.95)


def test_falls_back_to_itunes_when_musicbrainz_has_no_art(monkeypatch, cover_png):
    """The fallback is the whole reason iTunes is wired in."""
    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com/search": FakeResponse(
            json_data={"results": [{
                "artworkUrl100": "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg",
                "artistName": "The Rearview", "collectionName": "Neon Cartography",
            }]}
        ),
        "mzstatic.com": FakeResponse(content=cover_png),
    })
    found = artwork.find_artwork("The Rearview", "Neon Cartography", use_cache=False)
    assert found is not None
    assert found.source == "iTunes"
    assert found.data == cover_png


def test_itunes_query_includes_title_alongside_album(monkeypatch, cover_png):
    """Title used to be dropped whenever an album tag was present, only ever
    substituting for a missing album -- it must now be searched alongside
    artist and album, using the more precise "song" entity."""
    client = install_client(monkeypatch, {
        "itunes.apple.com/search": FakeResponse(
            json_data={"results": [{
                "artworkUrl100": "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg",
                "artistName": "The Rearview", "collectionName": "Neon Cartography",
                "trackName": "Skyline Drift",
            }]}
        ),
        "mzstatic.com": FakeResponse(content=cover_png),
    })
    found = artwork.lookup_itunes("The Rearview", "Neon Cartography", title="Skyline Drift")
    assert found is not None

    _, search_params = next(call for call in client.calls if call[1] and "term" in call[1])
    assert "Skyline Drift" in search_params["term"]
    assert "Neon Cartography" in search_params["term"]
    assert search_params["entity"] == "song"


def test_falls_back_to_spotify_when_musicbrainz_and_itunes_have_no_art(monkeypatch, cover_png):
    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com/search": FakeResponse(json_data={"results": []}),
    })
    monkeypatch.setattr(
        spotify_module,
        "find_track",
        lambda *a, **k: spotify_module.SpotifyMatch(
            title="Cheques", artist="Shubh", album="Still Rollin", year="2023",
            image_url="https://i.scdn.co/image/big.jpg",
        ),
    )
    monkeypatch.setattr(spotify_module, "fetch_image", lambda url: cover_png)

    found = artwork.find_artwork(
        "Shubh", "Still Rollin", title="Cheques", use_cache=False,
        settings=Settings(spotify_enabled=True, spotify_client_id="id", spotify_client_secret="secret"),
    )

    assert found is not None
    assert found.source == "Spotify"
    assert found.data == cover_png
    assert found.release_artist == "Shubh"


def test_falls_back_to_youtube_thumbnail_as_last_resort(monkeypatch, cover_png):
    from musicstudio.core import download as download_module

    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com/search": FakeResponse(json_data={"results": []}),
        "i.ytimg.com": FakeResponse(content=cover_png),
    })
    monkeypatch.setattr(spotify_module, "find_track", lambda *a, **k: None)
    monkeypatch.setattr(
        download_module,
        "search",
        lambda query, **k: [
            download_module.SearchResult(
                title="Some Video", uploader="Some Channel",
                url="https://youtube.com/watch?v=abc",
                thumbnail="https://i.ytimg.com/vi/abc/hq.jpg",
            )
        ],
    )

    found = artwork.find_artwork(
        "Some Artist", "", title="Some Song", use_cache=False,
        settings=Settings(artwork_use_youtube_thumbnail=True),
    )

    assert found is not None
    assert found.source == "YouTube thumbnail"
    assert found.data == cover_png
    assert found.score < 0.5, "must rank below every real cover-art provider"


def test_youtube_thumbnail_disabled_by_default_setting_is_skipped(monkeypatch, cover_png):
    from musicstudio.core import download as download_module

    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com/search": FakeResponse(json_data={"results": []}),
    })
    monkeypatch.setattr(spotify_module, "find_track", lambda *a, **k: None)
    search_calls = []
    monkeypatch.setattr(
        download_module, "search", lambda query, **k: search_calls.append(query) or []
    )

    found = artwork.find_artwork(
        "Some Artist", "", title="Some Song", use_cache=False,
        settings=Settings(artwork_use_youtube_thumbnail=False),
    )

    assert found is None
    assert not search_calls, "YouTube must not even be queried when the setting is off"


def test_itunes_score_rewards_a_matching_title():
    # Album deliberately does not match, so the artist+album score alone
    # (0.75) is below the 1.0 cap -- otherwise both scores would saturate at
    # the cap and the comparison below would be meaningless.
    result = {
        "artistName": "The Rearview", "collectionName": "Something Else Entirely",
        "trackName": "Skyline Drift",
    }
    with_title = artwork._itunes_score(result, "The Rearview", "Neon Cartography", "Skyline Drift")
    without_title = artwork._itunes_score(result, "The Rearview", "Neon Cartography")
    assert with_title > without_title


def test_itunes_url_is_upgraded_to_full_resolution():
    upgraded = artwork._upgrade_itunes_url(
        "https://is1.mzstatic.com/image/thumb/abc/100x100bb.jpg", 1200
    )
    assert "1200x1200bb" in upgraded
    assert "100x100bb" not in upgraded


def test_itunes_url_without_the_token_is_left_alone():
    url = "https://example.com/cover.jpg"
    assert artwork._upgrade_itunes_url(url, 1200) == url


def test_returns_none_when_every_provider_misses(monkeypatch):
    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com": FakeResponse(json_data={"results": []}),
    })
    assert artwork.find_artwork("Nobody", "Nothing", use_cache=False) is None


def test_network_failure_is_swallowed(monkeypatch):
    class ExplodingClient(FakeClient):
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(artwork, "_client", lambda *a, **k: ExplodingClient({}))
    assert artwork.find_artwork("A", "B", use_cache=False) is None


def test_tiny_responses_are_rejected_as_placeholders(monkeypatch):
    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=b"tiny"),
        "itunes.apple.com": FakeResponse(json_data={"results": []}),
    })
    assert artwork.find_artwork("A", "B", use_cache=False) is None


def test_musicbrainz_is_skipped_without_an_album(monkeypatch, cover_png):
    """Searching the release database with no release name is pointless."""
    client = install_client(monkeypatch, {
        "itunes.apple.com/search": FakeResponse(
            json_data={"results": [{"artworkUrl100": "https://x/100x100bb.jpg"}]}
        ),
        "https://x/": FakeResponse(content=cover_png),
    })
    artwork.find_artwork("Some Artist", "", title="A Song", use_cache=False)
    assert not any("musicbrainz" in url for url, _ in client.calls)


def test_lucene_special_characters_are_escaped():
    query = artwork._build_musicbrainz_query("AC/DC", "Who Made Who?")
    assert "\\/" in query
    assert "\\?" in query


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_second_lookup_is_served_from_cache(monkeypatch, cover_png):
    client = install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })
    first = artwork.find_artwork("The Rearview", "Neon Cartography")
    assert first.source == "Cover Art Archive"

    calls_after_first = len(client.calls)
    second = artwork.find_artwork("The Rearview", "Neon Cartography")
    assert second.source == "cache"
    assert second.data == cover_png
    assert len(client.calls) == calls_after_first  # no new requests


def test_misses_are_cached_so_scans_do_not_requery(monkeypatch):
    client = install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com": FakeResponse(json_data={"results": []}),
    })
    assert artwork.find_artwork("Nobody", "Nothing") is None
    calls = len(client.calls)
    assert artwork.find_artwork("Nobody", "Nothing") is None
    assert len(client.calls) == calls


def test_clear_cache_removes_entries(monkeypatch, cover_png):
    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })
    artwork.find_artwork("A", "B")
    assert artwork.clear_cache() > 0
    assert artwork.read_cache("A", "B", get_settings().artwork_preferred_size) is None


def test_ignore_cached_miss_requeries_instead_of_trusting_a_stale_miss(monkeypatch, cover_png):
    """A deliberate retry (ignore_cached_miss=True) must not be blocked by an
    earlier automatic lookup's cached "nothing found" answer."""
    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com": FakeResponse(json_data={"results": []}),
    })
    assert artwork.find_artwork("Nobody", "Nothing") is None  # records a miss

    client = install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })
    found = artwork.find_artwork("Nobody", "Nothing", ignore_cached_miss=True)
    assert found is not None
    assert found.source == "Cover Art Archive"
    assert any("musicbrainz" in url for url, _ in client.calls)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_spaces_calls_out():
    """MusicBrainz blocks clients that exceed one request per second."""
    limiter = artwork.RateLimiter(0.15)
    started = time.monotonic()
    for _ in range(3):
        limiter.wait()
    assert time.monotonic() - started >= 0.28


# ---------------------------------------------------------------------------
# needs_artwork policy
# ---------------------------------------------------------------------------


def test_missing_art_needs_updating():
    assert artwork.needs_artwork(T.TagSet())


def test_small_art_is_upgraded():
    """'Keep artwork up to date' means replacing old low-resolution thumbnails."""
    small = T.Artwork(b"x" * 5000, width=200, height=200)
    assert artwork.needs_artwork(T.TagSet(artwork=small))


def test_large_art_is_left_alone():
    large = T.Artwork(b"x" * 200_000, width=1200, height=1200)
    assert not artwork.needs_artwork(T.TagSet(artwork=large))


def test_unknown_dimensions_judged_by_file_size():
    assert artwork.needs_artwork(T.TagSet(artwork=T.Artwork(b"x" * 1000)))
    assert not artwork.needs_artwork(T.TagSet(artwork=T.Artwork(b"x" * 200_000)))


# ---------------------------------------------------------------------------
# Applying to files
# ---------------------------------------------------------------------------


def test_update_file_embeds_found_art(monkeypatch, tone_flac, cover_png):
    T.write(tone_flac, T.TagSet(artist="The Rearview", album="Neon Cartography"))
    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })

    result = artwork.update_file_artwork(tone_flac)
    assert result.updated
    assert T.read(tone_flac).artwork.data == cover_png


def test_update_skips_files_that_already_have_good_art(monkeypatch, tone_flac, cover_png):
    big = T.Artwork(cover_png, width=1200, height=1200)
    T.write(tone_flac, T.TagSet(artist="A", album="B"), artwork=big)
    result = artwork.update_file_artwork(tone_flac)
    assert not result.updated
    assert "Already has" in result.reason


def test_update_retries_even_after_an_earlier_cached_miss(monkeypatch, tone_flac, cover_png):
    """The Library page's "Update artwork" button is a deliberate retry -- an
    earlier failed lookup for this artist/album must not silently block it."""
    T.write(tone_flac, T.TagSet(artist="The Rearview", album="Neon Cartography"))
    install_client(monkeypatch, {
        "musicbrainz.org": MB_EMPTY,
        "itunes.apple.com": FakeResponse(json_data={"results": []}),
    })
    first = artwork.update_file_artwork(tone_flac)
    assert not first.updated
    assert "No cover art found" in first.reason

    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })
    second = artwork.update_file_artwork(tone_flac)
    assert second.updated
    assert T.read(tone_flac).artwork.data == cover_png


def test_update_needs_something_to_search_with(tone_flac):
    T.write(tone_flac, T.TagSet())
    result = artwork.update_file_artwork(tone_flac)
    assert not result.updated
    assert "No artist or album" in result.reason


def test_batch_continues_past_a_failing_file(monkeypatch, tone_flac, tmp_path, cover_png):
    T.write(tone_flac, T.TagSet(artist="The Rearview", album="Neon Cartography"))
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"not audio at all")

    install_client(monkeypatch, {
        "musicbrainz.org": MB_HIT,
        "coverartarchive.org": FakeResponse(content=cover_png),
    })

    results = artwork.update_library_artwork([broken, tone_flac])
    assert len(results) == 2
    assert sum(1 for r in results if r.updated) == 1


# ---------------------------------------------------------------------------
# Cover art from an arbitrary image URL
# ---------------------------------------------------------------------------


def test_looks_like_image_recognises_every_format_qt_renders(cover_png):
    assert artwork.looks_like_image(cover_png) == "PNG"
    assert artwork.looks_like_image(b"\xff\xd8\xff\xe0rest") == "JPEG"
    assert artwork.looks_like_image(b"GIF89a...") == "GIF"
    assert artwork.looks_like_image(b"BM- - -") == "BMP"
    # WebP's marker sits after the RIFF header, not at byte 0.
    assert artwork.looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "WEBP"
    assert artwork.looks_like_image(b"<!doctype html><html>") is None


def test_fetches_an_image_from_any_domain(monkeypatch, cover_png):
    """No allowlist: a link to a random host is fetched like any other."""
    install_client(monkeypatch, {
        "cdn.example-label.net": FakeResponse(content=cover_png),
    })
    assert artwork.fetch_image_url("https://cdn.example-label.net/art/cover.png") == cover_png


def test_a_link_to_a_page_rather_than_an_image_is_rejected(monkeypatch):
    """The common mistake: copying the page's address, not the picture's."""
    install_client(monkeypatch, {
        "example.com": FakeResponse(content=b"<!doctype html><html>a page</html>"),
    })
    with pytest.raises(artwork.ArtworkError, match="not a direct image"):
        artwork.fetch_image_url("https://example.com/album/nevermind")


def test_a_non_http_link_is_rejected_without_a_request(monkeypatch):
    client = install_client(monkeypatch, {})
    with pytest.raises(artwork.ArtworkError, match="http"):
        artwork.fetch_image_url("file:///C:/secrets.txt")
    assert client.calls == []


def test_an_empty_link_is_rejected(monkeypatch):
    install_client(monkeypatch, {})
    with pytest.raises(artwork.ArtworkError, match="No image link"):
        artwork.fetch_image_url("   ")


def test_a_server_error_is_reported_with_its_status(monkeypatch):
    install_client(monkeypatch, {
        "hotlink-protected.example": FakeResponse(status_code=403),
    })
    with pytest.raises(artwork.ArtworkError, match="refused"):
        artwork.fetch_image_url("https://hotlink-protected.example/cover.jpg")


def test_an_oversized_image_is_refused_rather_than_buffered(monkeypatch):
    """A link that turns out to be a video must not stream into memory."""
    oversized = b"\xff\xd8\xff" + b"\x00" * (artwork.MAX_REMOTE_IMAGE_BYTES + 1)
    install_client(monkeypatch, {
        "example.com": FakeResponse(content=oversized),
    })
    with pytest.raises(artwork.ArtworkError, match="too large"):
        artwork.fetch_image_url("https://example.com/not-really-a-cover.jpg")

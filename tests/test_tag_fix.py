"""Filling in missing tags: filename parsing, and the online lookup chain
(iTunes -> Spotify -> MusicBrainz). HTTP is mocked throughout.

core/tag_fix.py had no dedicated test file before this; these tests focus on
guess_from_online's provider chain (the part touched when Spotify was added)
plus baseline coverage of the filename parser and the blanks-only merge.
"""

from __future__ import annotations

import httpx
import pytest

from musicstudio.config import Settings
from musicstudio.core import spotify as spotify_module
from musicstudio.core import tag_fix
from musicstudio.core import tags as T


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class FakeClient:
    """Stands in for httpx.Client, routing GET by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response() if callable(response) else response
        return FakeResponse(status_code=404)


def install_client(monkeypatch, routes) -> FakeClient:
    client = FakeClient(routes)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return client


ITUNES_HIT = FakeResponse(json_data={
    "results": [{
        "trackName": "Cheques", "artistName": "Shubh", "collectionName": "Still Rollin",
        "primaryGenreName": "Hip-Hop", "releaseDate": "2023-06-27T00:00:00Z",
    }]
})
ITUNES_EMPTY = FakeResponse(json_data={"results": []})
MB_HIT = FakeResponse(json_data={
    "releases": [{"title": "Still Rollin", "artist-credit": [{"name": "Shubh"}], "date": "2023-06-27"}]
})
MB_EMPTY = FakeResponse(json_data={"releases": []})


# ---------------------------------------------------------------------------
# guess_from_filename
# ---------------------------------------------------------------------------


def test_guess_from_filename_splits_artist_and_title():
    guess = tag_fix.guess_from_filename("Shubh - Cheques.flac")
    assert guess.artist == "Shubh"
    assert guess.title == "Cheques"


def test_guess_from_filename_strips_promotional_noise():
    guess = tag_fix.guess_from_filename("Kid Ink - Be Real (Official Audio).mp3")
    assert guess.artist == "Kid Ink"
    assert guess.title == "Be Real"


def test_guess_from_filename_with_no_separator_is_title_only():
    guess = tag_fix.guess_from_filename("Cheques.flac")
    assert guess.artist == ""
    assert guess.title == "Cheques"


# ---------------------------------------------------------------------------
# guess_from_online: provider order and fallthrough
# ---------------------------------------------------------------------------


def test_blank_tags_never_make_a_network_call(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: calls.append(1) or FakeClient({}))
    result = tag_fix.guess_from_online(T.TagSet())
    assert result.to_dict() == T.TagSet().to_dict()
    assert not calls


def test_itunes_hit_is_used_and_short_circuits(monkeypatch):
    install_client(monkeypatch, {"itunes.apple.com": ITUNES_HIT})
    spotify_calls = []
    monkeypatch.setattr(
        spotify_module, "find_track", lambda *a, **k: spotify_calls.append(1) or None
    )

    result = tag_fix.guess_from_online(T.TagSet(artist="Shubh", title="Cheques"))

    assert result.album == "Still Rollin"
    assert result.genre == "Hip-Hop"
    assert result.date == "2023"
    assert not spotify_calls, "Spotify must not be queried once iTunes already answered"


def test_falls_back_to_spotify_when_itunes_is_empty(monkeypatch):
    install_client(monkeypatch, {"itunes.apple.com": ITUNES_EMPTY})
    monkeypatch.setattr(
        spotify_module,
        "find_track",
        lambda *a, **k: spotify_module.SpotifyMatch(
            title="Cheques", artist="Shubh", album="Still Rollin", year="2023", image_url=""
        ),
    )
    mb_calls = []
    monkeypatch.setattr(
        tag_fix, "search_musicbrainz", lambda *a, **k: mb_calls.append(1) or []
    )

    result = tag_fix.guess_from_online(T.TagSet(artist="Shubh", title="Cheques"))

    assert result.album == "Still Rollin"
    assert result.artist == "Shubh"
    assert not mb_calls, "MusicBrainz must not be queried once Spotify already answered"


def test_falls_back_to_musicbrainz_when_itunes_and_spotify_are_empty(monkeypatch):
    install_client(monkeypatch, {"itunes.apple.com": ITUNES_EMPTY, "musicbrainz.org": MB_HIT})
    monkeypatch.setattr(spotify_module, "find_track", lambda *a, **k: None)

    result = tag_fix.guess_from_online(T.TagSet(artist="Shubh", album="Still Rollin"))

    assert result.album == "Still Rollin"
    assert result.artist == "Shubh"
    assert result.date == "2023"


def test_all_providers_empty_returns_an_empty_tagset(monkeypatch):
    install_client(monkeypatch, {"itunes.apple.com": ITUNES_EMPTY, "musicbrainz.org": MB_EMPTY})
    monkeypatch.setattr(spotify_module, "find_track", lambda *a, **k: None)

    result = tag_fix.guess_from_online(T.TagSet(artist="Nobody", title="Nothing"))

    assert result.to_dict() == T.TagSet().to_dict()


def test_spotify_tags_has_no_genre_field_populated(monkeypatch):
    """Spotify's genre lives on the artist, not the track -- guess_from_online
    should not have to make a second API call per lookup just for that."""
    monkeypatch.setattr(
        spotify_module,
        "find_track",
        lambda *a, **k: spotify_module.SpotifyMatch(
            title="Cheques", artist="Shubh", album="Still Rollin", year="2023", image_url=""
        ),
    )
    result = tag_fix._spotify_tags("Shubh", "Cheques", "", Settings())
    assert result is not None
    assert result.genre == ""


# ---------------------------------------------------------------------------
# fix_file_tags: blanks-only merge against a real file
# ---------------------------------------------------------------------------


def test_fix_file_tags_never_overwrites_an_existing_value(tone_flac, monkeypatch):
    T.write(tone_flac, T.TagSet(title="Original Title", artist="Original Artist"))
    install_client(monkeypatch, {"itunes.apple.com": ITUNES_HIT})
    monkeypatch.setattr(spotify_module, "find_track", lambda *a, **k: None)

    result = tag_fix.fix_file_tags(tone_flac)

    assert result.title == "Original Title"
    assert result.artist == "Original Artist"
    # Blank fields still get filled from the (mocked) online hit.
    assert result.album == "Still Rollin"


def test_fix_file_tags_fills_blanks_from_filename_first(tmp_path, monkeypatch):
    from tests.conftest import make_tone

    path = make_tone(tmp_path / "Shubh - Cheques.flac")
    T.write(path, T.TagSet())  # no tags at all

    def fail_if_called(*a, **k):
        raise AssertionError("must not need the network when the filename already answers")

    monkeypatch.setattr(httpx, "Client", fail_if_called)

    result = tag_fix.fix_file_tags(path, use_online=False)

    assert result.artist == "Shubh"
    assert result.title == "Cheques"

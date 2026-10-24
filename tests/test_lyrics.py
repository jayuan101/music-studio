"""Synced lyrics: LRC parsing, the LRCLIB lookup, caching, and writing to files."""

from __future__ import annotations

import httpx
import pytest

from musicstudio.core import convert, formats, lyrics
from musicstudio.core import tags as T
from musicstudio.core.convert import ConvertRequest
from musicstudio.db import Library, scan_into_library

from .conftest import requires_ffmpeg
from .test_tag_fix import FakeClient, FakeResponse

LRC = """[ar:Some Artist]
[ti:Some Song]
[00:12.00]First line
[00:15.50]Second line
[01:02.5]Third line
"""


class _NoWait:
    def wait(self):
        pass


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(lyrics, "_limiter", _NoWait())


def install(monkeypatch, routes) -> FakeClient:
    client = FakeClient(routes)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return client


# -- parsing ------------------------------------------------------------------

def test_parse_lrc_basics():
    assert lyrics.parse_lrc(LRC) == [(12.0, "First line"), (15.5, "Second line"), (62.5, "Third line")]


def test_parse_lrc_repeated_stamps_fractions_and_long_minutes():
    text = "[00:30.5][02:10.25]Chorus\n[100:00.123]Way later\n[00:05]No fraction\n"
    assert lyrics.parse_lrc(text) == [
        (5.0, "No fraction"), (30.5, "Chorus"), (130.25, "Chorus"), (6000.123, "Way later"),
    ]


def test_parse_lrc_offset_and_word_stamps_and_junk():
    text = "[offset:+500]\n[00:10.00]<00:10.00>Hello <00:10.40>world\nno stamp here\n[00:20.00]\n"
    assert lyrics.parse_lrc(text) == [(9.5, "Hello world"), (19.5, "")]


def test_parse_lrc_never_goes_negative():
    assert lyrics.parse_lrc("[offset:+5000]\n[00:01.00]early")[0][0] == 0.0


def test_is_synced_needs_two_stamps():
    assert lyrics.is_synced(LRC)
    assert not lyrics.is_synced("[00:10.00]just one")
    assert not lyrics.is_synced("plain words\nno stamps")
    assert not lyrics.is_synced("")


def test_line_lookup_by_position():
    synced = lyrics.SyncedLyrics.from_text(LRC)
    assert synced.index_at(0) == -1 and synced.text_at(5) == ""
    assert synced.text_at(12.0) == "First line"
    assert synced.text_at(15.49) == "First line"
    assert synced.text_at(15.5) == "Second line"
    assert synced.text_at(999) == "Third line"


# -- LRCLIB -------------------------------------------------------------------

EXACT = FakeResponse(json_data={"syncedLyrics": LRC, "plainLyrics": "a\nb"})


def test_fetch_exact_match(monkeypatch):
    client = install(monkeypatch, {"/get": EXACT})
    found = lyrics.fetch("Artist", "Song", "Album", 200.4)
    assert found.synced == LRC.strip() and found.plain == "a\nb"
    url, params = client.calls[0]
    assert url.endswith("/get")
    assert params["duration"] == 200 and params["album_name"] == "Album"


def test_fetch_falls_back_to_search_and_picks_the_closest_synced(monkeypatch):
    search = FakeResponse(json_data=[
        {"duration": 260, "syncedLyrics": "[00:01.00]x\n[00:02.00]y", "plainLyrics": "x"},
        {"duration": 201, "syncedLyrics": None, "plainLyrics": "plain only"},
        {"duration": 202.5, "syncedLyrics": LRC, "plainLyrics": "p"},
        {"duration": 199, "instrumental": True, "syncedLyrics": None, "plainLyrics": None},
    ])
    install(monkeypatch, {"/get": FakeResponse(status_code=404), "/search": search})
    found = lyrics.fetch("Artist", "Song", "Album", 200.0)
    assert found.synced == LRC.strip()  # the 260 s version is a different recording


def test_fetch_without_album_goes_straight_to_search(monkeypatch):
    client = install(monkeypatch, {"/search": FakeResponse(json_data=[{"plainLyrics": "words"}])})
    found = lyrics.fetch("Artist", "Song")
    assert found.plain == "words" and found.synced is None
    assert [u.rsplit("/", 1)[1] for u, _ in client.calls] == ["search"]


def test_fetch_no_lyrics_is_none_not_an_error(monkeypatch):
    install(monkeypatch, {"/get": FakeResponse(status_code=404),
                          "/search": FakeResponse(json_data=[])})
    assert lyrics.fetch("Artist", "Song", "Album", 200) is None
    assert lyrics.fetch("", "Song") is None


def test_fetch_failures_raise(monkeypatch):
    install(monkeypatch, {"/search": FakeResponse(status_code=500)})
    with pytest.raises(lyrics.LyricsError):
        lyrics.fetch("Artist", "Song")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "Client", boom)
    with pytest.raises(lyrics.LyricsError):
        lyrics.fetch("Artist", "Song")


# -- cache --------------------------------------------------------------------

def test_cache_remembers_hits_and_misses_but_not_failures(monkeypatch):
    client = install(monkeypatch, {"/search": FakeResponse(json_data=[{"syncedLyrics": LRC}])})
    first = lyrics.lookup_cached("Artist", "Song", "", 200)
    assert first.synced == LRC.strip()
    calls = len(client.calls)
    assert lyrics.lookup_cached("Artist", "Song", "", 200).synced == LRC.strip()
    assert len(client.calls) == calls  # served from the cache

    install(monkeypatch, {"/search": FakeResponse(json_data=[])})
    assert lyrics.lookup_cached("Nobody", "Nothing", "", 100) is None
    hit, result = lyrics.load_cached("Nobody", "Nothing", 100)
    assert hit and result is None

    monkeypatch.setattr(lyrics, "MISS_TTL_SECONDS", -1)  # misses expire
    assert lyrics.load_cached("Nobody", "Nothing", 100) == (False, None)

    install(monkeypatch, {"/search": FakeResponse(status_code=503)})
    with pytest.raises(lyrics.LyricsError):
        lyrics.lookup_cached("Down", "Song", "", 100)
    assert lyrics.load_cached("Down", "Song", 100) == (False, None)


# -- writing to files ---------------------------------------------------------

@requires_ffmpeg
@pytest.mark.parametrize("profile_id", ["flac", "mp3", "aac", "opus", "wma", "wavpack"])
def test_write_fields_lyrics_touches_only_lyrics(tone_flac, tmp_path, cover_png, profile_id):
    profile = formats.get_profile(profile_id)
    path = tmp_path / f"x{profile.extension}"
    convert.convert(ConvertRequest(tone_flac, path, profile, overwrite=True))
    T.write(path, T.TagSet(title="Keep", artist="Me", comment="hi"),
            artwork=T.Artwork.from_bytes(cover_png))
    before = T.read(path)

    T.write_fields(path, lyrics=LRC)
    after = T.read(path)
    assert after.lyrics.strip() == LRC.strip()
    assert lyrics.is_synced(after.lyrics)
    assert (after.title, after.artist, after.comment) == ("Keep", "Me", "hi")
    assert after.artwork.data == before.artwork.data

    T.write_fields(path, lyrics="")
    assert T.read(path).lyrics == ""
    assert T.read(path).title == "Keep"


@requires_ffmpeg
def test_download_library(tmp_path, tone_flac, monkeypatch):
    folder = tmp_path / "music"
    folder.mkdir()
    paths = {}
    for name in ("synced", "plain", "none", "has"):
        path = folder / f"{name}.flac"
        path.write_bytes(tone_flac.read_bytes())
        T.write(path, T.TagSet(title=name, artist="A", album="LP"))
        paths[name] = path
    T.write_fields(paths["has"], lyrics=LRC)
    library = Library(tmp_path / "t.db")
    scan_into_library(library, [folder])

    answers = {
        "synced": lyrics.LyricsResult(synced=LRC, plain="p"),
        "plain": lyrics.LyricsResult(plain="just words"),
        "none": None,
    }
    monkeypatch.setattr(lyrics, "fetch", lambda artist, title, album, duration:
                        answers[title])

    result = lyrics.download_library(list(paths.values()), library=library)
    assert [p.name for p in result.synced] == ["synced.flac"]
    assert [p.name for p in result.plain_only] == ["plain.flac"]
    assert [p.name for p in result.missing] == ["none.flac"]
    assert [p.name for p in result.skipped] == ["has.flac"]  # already synced
    assert not result.failed
    assert lyrics.is_synced(T.read(paths["synced"]).lyrics)
    assert T.read(paths["plain"]).lyrics == "just words"

    # A lookup that fails is reported, not swallowed, and does not stop the rest.
    def flaky(artist, title, album, duration):
        raise lyrics.LyricsError("offline")

    monkeypatch.setattr(lyrics, "fetch", flaky)
    again = lyrics.download_library([paths["none"]], library=library)
    # The earlier miss was cached, so this needs no lookup (and the broken
    # fetch above is never reached).
    assert [p.name for p in again.missing] == ["none.flac"] and not again.failed
    other = folder / "other.flac"
    other.write_bytes(tone_flac.read_bytes())
    T.write(other, T.TagSet(title="other", artist="A", album="LP"))
    scan_into_library(library, [folder])
    offline = lyrics.download_library([other], library=library)
    assert offline.failed and "offline" in offline.failed[0][1]

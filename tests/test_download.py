"""URL download handling.

yt-dlp itself is not exercised here -- these cover URL validation, the quality
guidance shown before downloading, and how yt-dlp's output is turned into a
tagged file.
"""

from __future__ import annotations

import pytest

from musicstudio.core import download, formats
from musicstudio.core.download import UrlInfo


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "http://example.com/track.mp3",
        "https://soundcloud.com/artist/track",
        "  https://youtu.be/abc  ",
    ],
)
def test_accepts_real_urls(url):
    assert download.is_supported_url(url)


@pytest.mark.parametrize("value", ["", "not a url", "youtube.com/watch", "C:\\music\\a.mp3", "ftp://x/y"])
def test_rejects_non_urls(value):
    assert not download.is_supported_url(value)


def test_filenames_are_stripped_of_characters_windows_rejects():
    cleaned = download._sanitise_filename('A: "Song" <2026> / Part*1?')
    for char in '<>:"/\\|?*':
        assert char not in cleaned
    assert cleaned


def test_filenames_are_length_capped():
    assert len(download._sanitise_filename("x" * 500)) <= 120


def test_empty_title_still_yields_a_usable_name():
    assert download._sanitise_filename("...") == "track"


def test_glob_escape_protects_titles_with_brackets():
    escaped = download.glob_escape("Song [Remix] (2026)")
    assert "[[]" in escaped or "[[]" in escaped.replace("[]", "[]")
    assert "Remix" in escaped


# ---------------------------------------------------------------------------
# Quality guidance
# ---------------------------------------------------------------------------


def test_lossless_target_warns_that_it_cannot_help():
    info = UrlInfo(title="A Song", best_audio_codec="opus", best_audio_bitrate=160)
    note = download.quality_note_for(info, formats.FLAC)
    assert note is not None
    assert "OPUS" in note
    assert "160 kbps" in note
    assert "without recovering any detail" in note


def test_warning_is_generic_before_the_link_is_checked():
    """Unchecked links have no codec yet; the text must still read properly."""
    note = download.quality_note_for(UrlInfo(title=""), formats.FLAC)
    assert note is not None
    assert "COMPRESSED" not in note  # not a bare placeholder word
    assert "already compressed" in note


def test_lossy_target_produces_no_warning():
    info = UrlInfo(title="A Song", best_audio_codec="opus")
    assert download.quality_note_for(info, formats.OPUS) is None


def test_no_warning_without_a_target_format():
    assert download.quality_note_for(UrlInfo(title="x"), None) is None


# ---------------------------------------------------------------------------
# Extraction result handling
# ---------------------------------------------------------------------------


def test_best_audio_format_ignores_video_streams():
    info = {
        "formats": [
            {"acodec": "opus", "vcodec": "none", "abr": 160},
            {"acodec": "aac", "vcodec": "none", "abr": 128},
            {"acodec": "aac", "vcodec": "h264", "abr": 320},   # muxed, ignored
            {"acodec": "none", "vcodec": "h264"},
        ]
    }
    codec, bitrate = download._best_audio_format(info)
    assert codec == "opus"
    assert bitrate == 160


def test_best_audio_format_on_an_empty_list():
    assert download._best_audio_format({}) == ("", 0)


def test_resolve_path_prefers_what_the_postprocessor_wrote(tmp_path):
    actual = tmp_path / "Song.opus"
    actual.write_bytes(b"audio")
    entry = {"requested_downloads": [{"filepath": str(tmp_path / "Song.webm")}], "title": "Song"}
    assert download._resolve_downloaded_path(entry, tmp_path) == actual


def test_resolve_path_ignores_sidecar_thumbnails(tmp_path):
    (tmp_path / "Song.jpg").write_bytes(b"image")
    audio = tmp_path / "Song.m4a"
    audio.write_bytes(b"audio")
    entry = {"title": "Song", "_filename": str(tmp_path / "Song.webm")}
    assert download._resolve_downloaded_path(entry, tmp_path) == audio


def test_resolve_path_returns_none_when_nothing_matches(tmp_path):
    assert download._resolve_downloaded_path({"title": "Missing"}, tmp_path) is None


# ---------------------------------------------------------------------------
# Metadata derived from the source
# ---------------------------------------------------------------------------


def test_artist_and_title_split_out_of_a_video_title(tone_flac):
    """Music videos are usually titled 'Artist - Title'."""
    from musicstudio.core import tags as T
    from musicstudio.config import get_settings

    track = download.DownloadedTrack(path=tone_flac, title="The Rearview - Midnight Drive")
    request = download.DownloadRequest(url="https://x", output_dir=tone_flac.parent)
    entry = {"title": "The Rearview - Midnight Drive", "webpage_url": "https://x"}

    download._apply_download_metadata(track, entry, request, get_settings())

    written = T.read(tone_flac)
    assert written.artist == "The Rearview"
    assert written.title == "Midnight Drive"
    assert written.source_url == "https://x"


def test_explicit_track_fields_beat_title_splitting(tone_flac):
    from musicstudio.core import tags as T
    from musicstudio.config import get_settings

    track = download.DownloadedTrack(path=tone_flac, title="whatever")
    request = download.DownloadRequest(url="https://x", output_dir=tone_flac.parent)
    entry = {
        "title": "Some - Video - Title",
        "track": "Real Title",
        "artist": "Real Artist",
        "album": "Real Album",
        "release_year": 2024,
    }

    download._apply_download_metadata(track, entry, request, get_settings())

    written = T.read(tone_flac)
    assert written.title == "Real Title"
    assert written.artist == "Real Artist"
    assert written.album == "Real Album"
    assert written.date == "2024"


def test_sidecar_thumbnail_is_embedded_then_removed(tone_flac, cover_png):
    from musicstudio.core import tags as T
    from musicstudio.config import get_settings

    thumbnail = tone_flac.with_suffix(".jpg")
    thumbnail.write_bytes(cover_png)

    track = download.DownloadedTrack(path=tone_flac, title="Song")
    request = download.DownloadRequest(
        url="https://x", output_dir=tone_flac.parent, embed_thumbnail=True
    )
    download._apply_download_metadata(track, {"title": "Song"}, request, get_settings())

    assert T.read(tone_flac).artwork.data == cover_png
    assert not thumbnail.exists()  # cleaned up, not left beside the audio


def test_url_info_duration_label():
    assert UrlInfo(title="x", duration=95).duration_label == "1:35"
    assert UrlInfo(title="x", duration=3725).duration_label == "1:02:05"
    assert UrlInfo(title="x", duration=0).duration_label == ""


# ---------------------------------------------------------------------------
# search() -- dispatch across SEARCH_SOURCES
#
# _search_source() is the yt-dlp boundary (not exercised here, same as the
# rest of this module); these cover search()'s own logic for fanning a query
# out across sources and tolerating one of them failing.
# ---------------------------------------------------------------------------


def test_search_queries_every_source_by_default(monkeypatch):
    calls = []

    def fake_search_source(source, prefix, query, limit):
        calls.append(source)
        return [download.SearchResult(title=f"{source} hit", source=source)]

    monkeypatch.setattr(download, "_search_source", fake_search_source)

    results = download.search("some song")

    assert set(calls) == set(download.SEARCH_SOURCES)
    assert {r.source for r in results} == set(download.SEARCH_SOURCES)


def test_search_can_be_limited_to_specific_sources(monkeypatch):
    calls = []
    monkeypatch.setattr(
        download,
        "_search_source",
        lambda source, prefix, query, limit: calls.append(source) or [],
    )

    download.search("some song", sources=["SoundCloud"])

    assert calls == ["SoundCloud"]


def test_search_survives_one_source_failing(monkeypatch):
    def fake_search_source(source, prefix, query, limit):
        if source == "YouTube":
            raise download.DownloadError("network hiccup")
        return [download.SearchResult(title="Still here", source=source)]

    monkeypatch.setattr(download, "_search_source", fake_search_source)

    results = download.search("some song")

    assert len(results) == 1
    assert results[0].source == "SoundCloud"


def test_search_raises_only_when_every_source_fails(monkeypatch):
    monkeypatch.setattr(
        download,
        "_search_source",
        lambda source, prefix, query, limit: (_ for _ in ()).throw(
            download.DownloadError("down")
        ),
    )

    with pytest.raises(download.DownloadError):
        download.search("some song")


def test_search_returns_empty_list_for_a_blank_query(monkeypatch):
    monkeypatch.setattr(
        download,
        "_search_source",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    assert download.search("   ") == []


# ---------------------------------------------------------------------------
# _best_thumbnail
#
# A real bug: flat search extraction (used by search(), to stay fast over a
# whole results page) never populates yt-dlp's singular "thumbnail" field --
# only the "thumbnails" list, each with its own width, in no guaranteed
# order. Found live while wiring the YouTube-thumbnail artwork fallback up
# to a real search, where it silently returned no thumbnail for every result.
# ---------------------------------------------------------------------------


def test_prefers_the_direct_thumbnail_field_when_present():
    entry = {
        "thumbnail": "https://i.ytimg.com/vi/abc/direct.jpg",
        "thumbnails": [{"url": "https://i.ytimg.com/vi/abc/small.jpg", "width": 120}],
    }
    assert download._best_thumbnail(entry) == "https://i.ytimg.com/vi/abc/direct.jpg"


def test_falls_back_to_the_widest_entry_in_the_thumbnails_list():
    entry = {
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/abc/medium.jpg", "width": 360},
            {"url": "https://i.ytimg.com/vi/abc/large.jpg", "width": 720},
            {"url": "https://i.ytimg.com/vi/abc/small.jpg", "width": 120},
        ]
    }
    assert download._best_thumbnail(entry) == "https://i.ytimg.com/vi/abc/large.jpg"


def test_widest_entry_wins_regardless_of_list_order():
    entry = {
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/abc/large.jpg", "width": 720},
            {"url": "https://i.ytimg.com/vi/abc/small.jpg", "width": 120},
        ]
    }
    assert download._best_thumbnail(entry) == "https://i.ytimg.com/vi/abc/large.jpg"


def test_empty_when_neither_field_is_present():
    assert download._best_thumbnail({}) == ""


def test_empty_thumbnails_list_is_handled():
    assert download._best_thumbnail({"thumbnails": []}) == ""


def test_a_thumbnail_entry_missing_width_does_not_crash():
    """A malformed entry (no width key at all) must not blow up the max()
    comparison -- it should just lose to anything with a real width."""
    entry = {
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/abc/no-width.jpg"},
            {"url": "https://i.ytimg.com/vi/abc/large.jpg", "width": 720},
        ]
    }
    assert download._best_thumbnail(entry) == "https://i.ytimg.com/vi/abc/large.jpg"

"""Regression coverage for asynchronous lyrics during playback changes."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication

from musicstudio.config import get_settings
from musicstudio.core import lyrics
from musicstudio.ui.widgets.lyrics_fetch import LyricsFetcher
from musicstudio.ui.widgets.now_playing import NowPlayingBar


LRC = "[00:01]First line\n[00:02]Second line"


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.mark.parametrize("cancel_latest", [False, True])
def test_only_latest_pending_lookup_runs(qapp, monkeypatch, cancel_latest):
    started, release = Event(), Event()
    calls, delivered = [], []

    def lookup(artist, title, album, duration):
        calls.append(title)
        if title == "first":
            started.set()
            assert release.wait(5)
        return lyrics.LyricsResult(synced=LRC)

    monkeypatch.setattr(lyrics, "lookup_cached", lookup)
    fetcher = LyricsFetcher()
    fetcher.ready.connect(lambda token, result: delivered.append(token))
    fetcher.request("artist", "first", "", 10)
    try:
        assert started.wait(5)
        fetcher.request("artist", "skipped", "", 10)
        latest = fetcher.request("artist", "latest", "", 10)
        if cancel_latest:
            fetcher.cancel()
    finally:
        release.set()
        assert fetcher._pool.waitForDone(5000)
    qapp.processEvents()
    assert calls == (["first"] if cancel_latest else ["first", "latest"])
    assert delivered == ([] if cancel_latest else [latest])


def test_track_change_rejects_already_delivered_old_token():
    bar = SimpleNamespace(
        _lyrics_token=7, _lyrics_fetcher=Mock(), _set_lyrics=Mock(),
        player=Mock(), title_label=Mock(), artist_label=Mock(),
        _clear_art=Mock(), _update_controls=Mock(),
    )
    NowPlayingBar._on_current_changed(bar, None)
    bar._set_lyrics.reset_mock()
    NowPlayingBar._on_lyrics_ready(bar, 7, lyrics.LyricsResult(synced=LRC))
    bar._set_lyrics.assert_not_called()


@pytest.mark.parametrize("embedded", [False, True])
def test_disable_online_rejects_inflight_result_and_keeps_embedded(embedded):
    get_settings().lyrics_online_enabled = False
    bar = SimpleNamespace(
        _lyrics_token=7, _lyrics_fetcher=Mock(), _set_lyrics=Mock(),
        _lyrics=lyrics.SyncedLyrics.from_text(LRC) if embedded else None,
        _current_info=SimpleNamespace(lyrics=LRC if embedded else ""),
    )
    NowPlayingBar.refresh_lyrics(bar)
    bar._lyrics_fetcher.cancel.assert_called_once()
    if embedded:
        bar._set_lyrics.assert_not_called()
    else:
        bar._set_lyrics.assert_called_once_with(None)
    bar._set_lyrics.reset_mock()
    NowPlayingBar._on_lyrics_ready(bar, 7, lyrics.LyricsResult(synced=LRC))
    bar._set_lyrics.assert_not_called()

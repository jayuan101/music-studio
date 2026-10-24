"""Playlists in the library database: manual, smart, and M3U export."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from musicstudio.core import m3u
from musicstudio.core.playlist_rules import compile_rules, describe_rules
from musicstudio.core.probe import AudioInfo
from musicstudio.core.tags import TagSet
from musicstudio.db import Library


def _info(path: Path, *, codec="flac", duration=200.0, bitrate=900_000, bit_depth=16) -> AudioInfo:
    return AudioInfo(
        path=path, container=codec, codec=codec, codec_long_name=codec, duration=duration,
        sample_rate=44100, channels=2, channel_layout="stereo", bit_depth=bit_depth,
        bitrate=bitrate, size_bytes=1000, tags={},
    )


def _add(library: Library, folder: Path, name: str, **kwargs) -> Path:
    path = folder / name
    path.write_bytes(b"x")
    tag_fields = {k: kwargs.pop(k) for k in ("title", "artist", "album", "genre", "date")
                  if k in kwargs}
    library.upsert(path, _info(path, **kwargs), TagSet(**tag_fields))
    return path.resolve()


@pytest.fixture
def library(tmp_path) -> Library:
    return Library(tmp_path / "test.db")


@pytest.fixture
def songs(library, tmp_path) -> list[Path]:
    folder = tmp_path / "music"
    folder.mkdir()
    return [
        _add(library, folder, "a.flac", title="Alpha", artist="Ann", genre="Jazz", date="1999"),
        _add(library, folder, "b.mp3", title="Bravo", artist="Bob", genre="Rock", date="2010",
             codec="mp3", bitrate=128_000, bit_depth=0, duration=100.0),
        _add(library, folder, "c.flac", title="Charlie", artist="Cat", genre="Jazz fusion",
             date="2021-05-01"),
    ]


def _titles(tracks) -> list[str]:
    return [t.title for t in tracks]


# -- manual playlists -------------------------------------------------------

def test_add_keeps_order_and_skips_unknown_paths(library, songs, tmp_path):
    pid = library.create_playlist("Mix")
    added = library.add_to_playlist(pid, [songs[2], tmp_path / "nope.flac", songs[0]])
    assert added == 2
    assert _titles(library.playlist_tracks(pid)) == ["Charlie", "Alpha"]


def test_move_and_remove(library, songs):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, songs)
    library.move_in_playlist(pid, [2], 0)
    assert _titles(library.playlist_tracks(pid)) == ["Charlie", "Alpha", "Bravo"]
    # Drop "Charlie" after the last row.
    library.move_in_playlist(pid, [0], 3)
    assert _titles(library.playlist_tracks(pid)) == ["Alpha", "Bravo", "Charlie"]
    # Two rows together keep their relative order.
    library.move_in_playlist(pid, [0, 1], 3)
    assert _titles(library.playlist_tracks(pid)) == ["Charlie", "Alpha", "Bravo"]
    library.remove_from_playlist(pid, [1])
    assert _titles(library.playlist_tracks(pid)) == ["Charlie", "Bravo"]


def test_removing_a_track_from_the_library_drops_it_from_playlists(library, songs):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, songs)
    library.remove(songs[1])
    assert _titles(library.playlist_tracks(pid)) == ["Alpha", "Charlie"]


def test_rescan_keeps_playlist_entries(library, songs):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, [songs[0]])
    library.upsert(songs[0], _info(songs[0]), TagSet(title="Alpha (remaster)"))
    assert _titles(library.playlist_tracks(pid)) == ["Alpha (remaster)"]


def test_delete_playlist_leaves_songs(library, songs):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, songs)
    library.delete_playlist(pid)
    assert library.playlists() == []
    assert library.count() == 3


def test_playlists_survive_reopen(library, songs, tmp_path):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, [songs[1]])
    library.close()
    reopened = Library(tmp_path / "test.db")
    assert [p.name for p in reopened.playlists()] == ["Mix"]
    assert _titles(reopened.playlist_tracks(pid)) == ["Bravo"]


def test_duplicate_cleanup_moves_playlist_entries_to_the_kept_row(library, songs):
    """_canonicalize_paths drops a stale alias row; its playlist entries must
    follow the row that is kept, not be cascaded away."""
    pid = library.create_playlist("Mix")
    conn = library._connect()
    with conn:
        conn.execute(
            "INSERT INTO tracks(path, filename) VALUES (?, ?)",
            (str(songs[0].parent / "sub" / ".." / songs[0].name), songs[0].name),
        )
        alias_id = conn.execute("SELECT max(id) FROM tracks").fetchone()[0]
        conn.execute(
            "INSERT INTO playlist_items(playlist_id, track_id, position) VALUES (?, ?, 0)",
            (pid, alias_id),
        )
    from musicstudio.db import _canonicalize_paths

    with conn:
        _canonicalize_paths(conn)
    assert [t.path for t in library.playlist_tracks(pid)] == [songs[0]]


# -- smart playlists --------------------------------------------------------

def _smart(library, rules) -> list[str]:
    return _titles(library.query_rules(rules))


def test_text_rules(library, songs):
    rule = {"rules": [{"field": "genre", "op": "contains", "value": "jazz"}], "order_by": "title"}
    assert _smart(library, rule) == ["Alpha", "Charlie"]
    rule["rules"][0]["op"] = "is"
    assert _smart(library, rule) == ["Alpha"]
    rule["rules"][0] = {"field": "artist", "op": "starts_with", "value": "b"}
    assert _smart(library, rule) == ["Bravo"]


def test_like_wildcards_in_values_are_literal(library, songs):
    rule = {"rules": [{"field": "title", "op": "contains", "value": "%"}]}
    assert _smart(library, rule) == []


def test_number_boolean_and_any(library, songs):
    rules = {
        "match": "any",
        "rules": [
            {"field": "year", "op": "lt", "value": 2000},
            {"field": "bitrate", "op": "lt", "value": 200},
        ],
        "order_by": "title",
    }
    assert _smart(library, rules) == ["Alpha", "Bravo"]
    assert _smart(library, {"rules": [{"field": "year", "op": "between", "value": [2022, 2005]}],
                            "order_by": "title"}) == ["Bravo", "Charlie"]
    assert _smart(library, {"rules": [{"field": "is_lossless", "op": "is_false"}]}) == ["Bravo"]


def test_added_in_last_days(library, songs):
    conn = library._connect()
    with conn:
        conn.execute("UPDATE tracks SET added_at = ? WHERE title = 'Alpha'",
                     (time.time() - 40 * 86400,))
    rules = {"rules": [{"field": "added_at", "op": "in_last_days", "value": 30}],
             "order_by": "title"}
    assert _smart(library, rules) == ["Bravo", "Charlie"]


def test_order_and_limit(library, songs):
    assert _smart(library, {"order_by": "-title", "limit": 2}) == ["Charlie", "Bravo"]


def test_smart_playlist_round_trip(library, songs):
    rules = {"rules": [{"field": "genre", "op": "contains", "value": "jazz"}], "order_by": "title"}
    pid = library.create_playlist("Jazz", "smart", rules)
    assert library.get_playlist(pid).is_smart
    assert _titles(library.playlist_tracks(pid)) == ["Alpha", "Charlie"]


@pytest.mark.parametrize(
    "rules",
    [
        {"rules": [{"field": "path; DROP TABLE tracks", "op": "is", "value": "x"}]},
        {"rules": [{"field": "title", "op": "lt", "value": 1}]},
        {"order_by": "title; DROP TABLE tracks"},
        {"match": "some"},
        {"rules": [{"field": "year", "op": "between", "value": 3}]},
    ],
)
def test_bad_rules_are_rejected(library, rules):
    with pytest.raises(ValueError):
        compile_rules(rules)
    with pytest.raises(ValueError):
        library.create_playlist("Bad", "smart", rules)


def test_values_never_reach_the_sql_text():
    sql, params = compile_rules({"rules": [{"field": "title", "op": "is", "value": "'; --"}]})
    assert "'; --" not in sql
    assert "'; --" in params


def test_describe_rules():
    text = describe_rules({
        "match": "all",
        "rules": [
            {"field": "genre", "op": "contains", "value": "jazz"},
            {"field": "added_at", "op": "in_last_days", "value": 7},
        ],
    })
    assert text == "genre contains 'jazz' and date added in the last 7 days"


# -- M3U --------------------------------------------------------------------

def test_m3u_relative_paths_and_extinf(library, songs, tmp_path):
    pid = library.create_playlist("Mix")
    library.add_to_playlist(pid, [songs[1], songs[0]])
    target = tmp_path / "out.m3u8"
    assert m3u.write_m3u(target, library.playlist_tracks(pid)) == 2
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "#EXTM3U",
        "#EXTINF:100,Bob - Bravo",
        str(Path("music") / "b.mp3"),
        "#EXTINF:200,Ann - Alpha",
        str(Path("music") / "a.flac"),
    ]
    assert b"\r\n" in target.read_bytes()


def test_m3u_absolute_and_unicode(library, tmp_path):
    folder = tmp_path / "música"
    folder.mkdir()
    path = _add(library, folder, "señor.flac", title="Señor", artist="Björk")
    target = tmp_path / "lists" / "x.m3u8"
    m3u.write_m3u(target, [library.get(path)], relative=False)
    text = target.read_text(encoding="utf-8")
    assert "#EXTINF:200,Björk - Señor" in text
    assert str(path) in text

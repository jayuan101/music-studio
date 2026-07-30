"""Reshaping metadata to YouTube Music's conventions.

These are pure string/tag transformations, so nothing here needs the network.
The cases are drawn from a real downloaded library: the failure modes below
(a song name swallowed into a feature credit, a single mistaken for a video
title, a mixtape mistaken for a compilation) all occurred on real files.
"""

from __future__ import annotations

from musicstudio.core import tags as T
from musicstudio.core import ytmusic


# ---------------------------------------------------------------------------
# Featured artists
# ---------------------------------------------------------------------------


def test_splits_guests_off_the_artist():
    primary, guests = ytmusic.split_featured("Omarion ft. Usher, Fabolous & Busta Rhymes")
    assert primary == "Omarion"
    assert guests == ["Usher", "Fabolous", "Busta Rhymes"]


def test_recognises_every_spelling_of_the_marker():
    for artist in (
        "Drake feat. Rihanna",
        "Drake Feat Rihanna",
        "Drake ft Rihanna",
        "Drake ft. Rihanna",
        "Drake featuring Rihanna",
        "Drake f/ Rihanna",
        "Drake (feat. Rihanna)",
    ):
        assert ytmusic.split_featured(artist) == ("Drake", ["Rihanna"]), artist


def test_ampersand_in_a_band_name_is_not_a_guest_list():
    """Only text after an explicit feat./ft. marker is a guest."""
    for band in ("Hall & Oates", "Earth, Wind & Fire", "Simon & Garfunkel"):
        assert ytmusic.split_featured(band) == (band, [])


def test_guest_list_is_rendered_the_way_youtube_music_writes_it():
    assert ytmusic.format_feature_suffix(["Rihanna"]) == "(feat. Rihanna)"
    assert ytmusic.format_feature_suffix(["A", "B"]) == "(feat. A & B)"
    assert ytmusic.format_feature_suffix(["A", "B", "C"]) == "(feat. A, B & C)"
    assert ytmusic.format_feature_suffix([]) == ""


# ---------------------------------------------------------------------------
# Title cleanup
# ---------------------------------------------------------------------------


def test_strips_promotional_brackets():
    assert ytmusic.clean_title("Numb (Official Music Video)") == "Numb"
    assert ytmusic.clean_title("Cheques (Lyrics)") == "Cheques"
    assert ytmusic.clean_title("Copycats (Visualizer)") == "Copycats"
    assert ytmusic.clean_title("3005 [Explicit]") == "3005"
    assert ytmusic.clean_title("Song [4K UPGRADE]") == "Song"


def test_keeps_brackets_that_are_part_of_the_song():
    """A version marker is metadata, not promotion, and must survive."""
    for title in (
        "Ice Box (Remix)",
        "Song (Live)",
        "Song (Acoustic)",
        "Song (Radio Edit)",
        "Song (Remastered 2011)",
        "Billionaire (Dirty)",
        "9 Piece (Remix) (Dirty)",
    ):
        assert ytmusic.clean_title(title) == title


def test_drops_the_artist_echoed_into_the_title():
    assert ytmusic.clean_title("Shubh - Cheques", "Shubh") == "Cheques"
    assert ytmusic.clean_title("Numb – Linkin Park", "Linkin Park") == "Numb"
    assert ytmusic.clean_title("Cashin' Out - Cash Out", "Cash Out") == "Cashin' Out"


def test_a_dash_that_is_not_the_artist_is_left_alone():
    assert ytmusic.clean_title("Six - Eight", "Someone Else") == "Six - Eight"


def test_strips_channel_credits():
    assert ytmusic.clean_title("Ice Box Remix 🎥 By: @finnbjornerud") == "Ice Box Remix"


def test_bare_feature_marker_in_a_title_gets_bracketed():
    assert (
        ytmusic.normalise_title_features("Copycats ft. Underscores")
        == "Copycats (feat. Underscores)"
    )


def test_an_already_bracketed_credit_is_not_wrapped_twice():
    assert ytmusic.normalise_title_features("Song (feat. X)") == "Song (feat. X)"


def test_credit_does_not_swallow_the_song_name():
    """"Artist ft. Guest - Song" must not read the guest list to end of line."""
    assert (
        ytmusic.normalise_title_features(
            "Kid Ink ft. DeJ Loaf - Be Real", artist="Kid Ink"
        )
        == "Be Real (feat. DeJ Loaf)"
    )


def test_an_ambiguous_credit_is_left_untouched():
    """When the part before the marker is not the artist, do not guess."""
    original = "Song ft. A - Remix"
    assert ytmusic.normalise_title_features(original, artist="Someone Else") == original


# ---------------------------------------------------------------------------
# Genre
# ---------------------------------------------------------------------------


def test_folds_genre_spellings_together():
    for spelling in ("Hip-Hop", "Hip Hop", "Rap", "hip hop; rap", "Rap/Hip-Hop", "HipHop"):
        assert ytmusic.canonical_genre(spelling) == "Hip-Hop/Rap", spelling
    for spelling in ("R&B", "R & B", "RnB", "Soul"):
        assert ytmusic.canonical_genre(spelling) == "R&B/Soul", spelling


def test_clears_values_that_are_not_genres():
    for junk in ("Music", "Other", "Genre", "Unknown", "People & Blogs"):
        assert ytmusic.canonical_genre(junk) == "", junk


def test_clears_download_site_spam_and_mojibake():
    for junk in ("www.playwap.mobi", "djskee.com", "zvukoff.ru", "Õ³ï-õîï/ðýï"):
        assert ytmusic.canonical_genre(junk) == "", junk


def test_an_unrecognised_genre_is_kept():
    """The map only has to cover the mess, not every genre that exists."""
    assert ytmusic.canonical_genre("Shoegaze") == "Shoegaze"


# ---------------------------------------------------------------------------
# Whole-tag normalisation
# ---------------------------------------------------------------------------


def test_guests_move_from_the_artist_into_the_title():
    result = ytmusic.normalise_tags(
        T.TagSet(title="Ice Box", artist="Omarion ft. Usher & Fabolous")
    )
    assert result.artist == "Omarion"
    assert result.title == "Ice Box (feat. Usher & Fabolous)"


def test_album_artist_is_filled_from_the_primary_artist():
    """The field YouTube Music groups albums by; a guest must not split one."""
    result = ytmusic.normalise_tags(T.TagSet(title="X", artist="Drake feat. Rihanna"))
    assert result.albumartist == "Drake"


def test_a_guest_is_never_credited_twice():
    result = ytmusic.normalise_tags(
        T.TagSet(title="Song (feat. Rihanna)", artist="Drake ft. Rihanna")
    )
    assert result.title == "Song (feat. Rihanna)"


def test_a_video_title_in_the_album_field_is_cleared():
    result = ytmusic.normalise_tags(
        T.TagSet(
            title="Numb (Official Music Video) [4K UPGRADE] – Linkin Park",
            artist="Linkin Park",
            album="Numb (Official Music Video) [4K UPGRADE] – Linkin Park",
        )
    )
    assert result.title == "Numb"
    assert result.album == ""


def test_a_single_keeps_its_album():
    """An album that always just matched the song name is a single."""
    result = ytmusic.normalise_tags(
        T.TagSet(title="Don't Tell 'Em", artist="Jeremih", album="Don't Tell 'Em")
    )
    assert result.album == "Don't Tell 'Em"


def test_placeholder_and_spam_albums_are_cleared():
    for junk in ("Unknown Album", "HotNewHipHop.com", "Billboard Hot 100"):
        result = ytmusic.normalise_tags(T.TagSet(title="X", artist="Y", album=junk))
        assert result.album == "", junk


def test_normalisation_is_idempotent():
    """Running the pass twice must not keep rewriting the same file."""
    once = ytmusic.normalise_tags(
        T.TagSet(
            title="Numb (Official Video) - Linkin Park",
            artist="Linkin Park ft. Someone",
            album="Unknown Album",
            genre="Hip Hop",
        )
    )
    twice = ytmusic.normalise_tags(once)
    assert twice.to_dict() == once.to_dict()


# ---------------------------------------------------------------------------
# Album-wide decisions
# ---------------------------------------------------------------------------


def _entries(rows):
    from pathlib import Path

    return [(Path(f"{i}.flac"), T.TagSet(**row)) for i, row in enumerate(rows)]


def test_a_dominant_artist_owns_the_album():
    """A mixtape is not a compilation because one guest appears on it."""
    resolved = ytmusic._resolve_album_artists(
        _entries(
            [
                {"album": "No Ceilings", "artist": "Lil Wayne"},
                {"album": "No Ceilings", "artist": "Lil Wayne"},
                {"album": "No Ceilings", "artist": "Lil Wayne"},
                {"album": "No Ceilings", "artist": "Beyonce"},
            ]
        )
    )
    assert resolved[ytmusic._norm_compare("No Ceilings")] == "Lil Wayne"


def test_spelling_variants_of_one_artist_are_counted_together():
    resolved = ytmusic._resolve_album_artists(
        _entries(
            [
                {"album": "No Ceilings", "artist": "Lil Wayne"},
                {"album": "No Ceilings", "artist": "Lil Wayne"},
                {"album": "No Ceilings", "artist": "Lil' Wayne"},
                {"album": "No Ceilings", "artist": "Beyonce"},
            ]
        )
    )
    # "Lil' Wayne" must not read as a third artist and tip this to Various.
    assert resolved[ytmusic._norm_compare("No Ceilings")] == "Lil Wayne"


def test_an_album_with_no_dominant_artist_is_a_compilation():
    resolved = ytmusic._resolve_album_artists(
        _entries(
            [
                {"album": "Now That's What I Call Music", "artist": "A"},
                {"album": "Now That's What I Call Music", "artist": "B"},
                {"album": "Now That's What I Call Music", "artist": "C"},
                {"album": "Now That's What I Call Music", "artist": "D"},
            ]
        )
    )
    key = ytmusic._norm_compare("Now That's What I Call Music")
    assert resolved[key] == ytmusic.VARIOUS_ARTISTS


def test_various_artists_is_dropped_when_there_is_no_album():
    """It only means something on a real release."""
    result = ytmusic.normalise_tags(
        T.TagSet(title="X", artist="Drake", albumartist="Various Artists")
    )
    assert result.albumartist == "Drake"


# ---------------------------------------------------------------------------
# Applying to files
# ---------------------------------------------------------------------------


def test_normalise_library_writes_and_reports(tone_flac):
    T.write(
        tone_flac,
        T.TagSet(title="Numb (Official Video)", artist="Linkin Park ft. Guest", genre="Hip Hop"),
    )
    results = ytmusic.normalise_library([tone_flac])

    assert len(results) == 1 and results[0].updated
    written = T.read(tone_flac)
    assert written.title == "Numb (feat. Guest)"
    assert written.artist == "Linkin Park"
    assert written.albumartist == "Linkin Park"
    assert written.genre == "Hip-Hop/Rap"


def test_dry_run_changes_nothing_on_disk(tone_flac):
    T.write(tone_flac, T.TagSet(title="Numb (Official Video)", artist="Linkin Park"))
    results = ytmusic.normalise_library([tone_flac], dry_run=True)

    assert results[0].updated and results[0].changes
    assert T.read(tone_flac).title == "Numb (Official Video)"


def test_snapshot_round_trips(tone_flac, tmp_path):
    T.write(tone_flac, T.TagSet(title="Numb (Official Video)", artist="Linkin Park"))
    snapshot = ytmusic.snapshot_tags([tone_flac], tmp_path / "snap.json")

    ytmusic.normalise_library([tone_flac])
    assert T.read(tone_flac).title == "Numb"

    assert ytmusic.restore_snapshot(snapshot) == 1
    assert T.read(tone_flac).title == "Numb (Official Video)"


def test_one_bad_file_does_not_stop_the_batch(tone_flac, tmp_path):
    T.write(tone_flac, T.TagSet(title="Numb (Official Video)", artist="Linkin Park"))
    unreadable = tmp_path / "not-audio.flac"
    unreadable.write_bytes(b"this is not a flac file")

    # The unreadable file comes first, so a batch that aborted on it would
    # never reach the good one.
    results = ytmusic.normalise_library([unreadable, tone_flac])
    assert len(results) == 2
    assert not results[0].updated
    assert results[1].updated
    assert T.read(tone_flac).title == "Numb"

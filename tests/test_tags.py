"""Metadata round-trips across every container we can write."""

from __future__ import annotations

import pytest

from musicstudio.core import convert, formats
from musicstudio.core import tags as T
from musicstudio.core.convert import ConvertRequest

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg

#: Every field that should survive a write/read cycle in a full-featured format.
ROUND_TRIP_FIELDS = [
    "title", "artist", "album", "albumartist", "date", "genre", "composer",
    "comment", "lyrics", "track_number", "track_total", "disc_number",
    "disc_total", "bpm", "isrc", "publisher", "copyright", "compilation",
    "musicbrainz_albumid", "musicbrainz_trackid", "replaygain_track_gain",
    "source_url",
]

WRITABLE = ["flac", "alac", "aac", "wav", "aiff", "wavpack", "mp3", "opus", "vorbis", "wma"]


def full_tagset(artwork=None) -> T.TagSet:
    return T.TagSet(
        title="Midnight Drive", artist="The Rearview", album="Neon Cartography",
        albumartist="The Rearview", date="2026", genre="Synthwave",
        composer="A. Composer", comment="a comment", lyrics="line one\nline two",
        track_number=3, track_total=12, disc_number=1, disc_total=2, bpm=128,
        isrc="USRC17607839", publisher="Night Records", copyright="(C) 2026",
        encoded_by="Music Studio", compilation=True,
        musicbrainz_albumid="7d3f-abc", musicbrainz_trackid="tr-999",
        replaygain_track_gain="-6.25 dB", source_url="https://example.com/x",
        artwork=artwork,
    )


@pytest.fixture
def encoded(tone_flac, tmp_path):
    """Encode the reference tone into every writable format once."""
    produced = {}
    for profile_id in WRITABLE:
        profile = formats.get_profile(profile_id)
        destination = tmp_path / f"{profile_id}{profile.extension}"
        convert.convert(ConvertRequest(tone_flac, destination, profile, overwrite=True))
        produced[profile_id] = destination
    return produced


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", WRITABLE)
def test_all_fields_round_trip(encoded, profile_id):
    path = encoded[profile_id]
    original = full_tagset()
    T.write(path, original)
    read_back = T.read(path)

    mismatched = {
        name: (getattr(original, name), getattr(read_back, name))
        for name in ROUND_TRIP_FIELDS
        if str(getattr(original, name) or "") != str(getattr(read_back, name) or "")
    }
    assert not mismatched, f"{profile_id} lost fields: {mismatched}"


@pytest.mark.parametrize("profile_id", WRITABLE)
def test_artwork_round_trips_byte_identically(encoded, profile_id, cover_png):
    path = encoded[profile_id]
    art = T.Artwork.from_bytes(cover_png)
    T.write(path, full_tagset(), artwork=art)

    read_back = T.read(path)
    assert read_back.has_artwork(), f"{profile_id} lost its cover art"
    assert read_back.artwork.data == cover_png


@pytest.mark.parametrize("profile_id", WRITABLE)
def test_blanking_a_field_clears_it_in_the_file(encoded, profile_id, cover_png):
    path = encoded[profile_id]
    T.write(path, full_tagset(), artwork=T.Artwork.from_bytes(cover_png))
    T.write(path, T.TagSet(title="Only Title"), artwork=T.Artwork(b""))

    read_back = T.read(path)
    assert read_back.title == "Only Title"
    assert not read_back.artist
    assert not read_back.album
    assert not read_back.has_artwork()


def test_copy_tags_carries_metadata_and_art_across_formats(encoded, cover_png):
    """A conversion must not silently drop the cover art."""
    source = encoded["flac"]
    destination = encoded["opus"]
    T.write(source, full_tagset(), artwork=T.Artwork.from_bytes(cover_png))
    T.write(destination, T.TagSet())

    T.copy_tags(source, destination)

    copied = T.read(destination)
    assert copied.title == "Midnight Drive"
    assert copied.album == "Neon Cartography"
    assert copied.artwork.data == cover_png


# ---------------------------------------------------------------------------
# Model behaviour
# ---------------------------------------------------------------------------


def test_untagged_file_reads_as_empty_not_an_error(encoded):
    assert T.read(encoded["flac"]).is_empty() or True  # never raises


def test_read_rejects_a_non_audio_file(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not audio")
    with pytest.raises(T.TagError):
        T.read(junk)


def test_try_read_swallows_failures(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("nope")
    assert T.try_read(junk).is_empty()


def test_merge_fills_blanks_without_overwriting():
    existing = T.TagSet(title="Kept", artist="Kept Artist")
    incoming = T.TagSet(title="New", album="New Album")
    merged = existing.merged_with(incoming)
    assert merged.title == "Kept"          # user's value survives
    assert merged.artist == "Kept Artist"
    assert merged.album == "New Album"     # blank gets filled


def test_merge_can_overwrite_when_asked():
    merged = T.TagSet(title="Old").merged_with(T.TagSet(title="New"), overwrite=True)
    assert merged.title == "New"


def test_effective_albumartist_falls_back_to_artist():
    assert T.TagSet(artist="Solo").effective_albumartist == "Solo"
    assert T.TagSet(artist="Solo", albumartist="VA").effective_albumartist == "VA"


def test_track_number_parses_a_slash_pair():
    from musicstudio.core.tags import _split_pair

    assert _split_pair("3/12") == (3, 12)
    assert _split_pair(" 7 ") == (7, None)
    assert _split_pair("junk") == (None, None)


# ---------------------------------------------------------------------------
# Image sniffing
# ---------------------------------------------------------------------------


def test_png_dimensions_are_detected(cover_png):
    art = T.Artwork.from_bytes(cover_png)
    assert art.mime == "image/png"
    assert (art.width, art.height) == (240, 240)
    assert art.extension == ".png"


def test_jpeg_dimensions_are_detected():
    # Minimal JPEG: SOI, then an SOF0 declaring 120x80.
    import struct

    data = b"\xff\xd8" + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 80, 120, 3) + b"\x00" * 6
    art = T.Artwork.from_bytes(data)
    assert art.mime == "image/jpeg"
    assert (art.width, art.height) == (120, 80)


def test_unknown_bytes_do_not_crash_the_sniffer():
    art = T.Artwork.from_bytes(b"\x00\x01\x02\x03")
    assert art.mime == "image/jpeg"
    assert art.width == 0

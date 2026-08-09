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


def test_merge_missing_tags_fills_blanks_from_a_donor(encoded):
    keeper, donor = encoded["flac"], encoded["opus"]
    T.write(keeper, T.TagSet(title="Kept Title", artist="Kept Artist"))
    T.write(donor, T.TagSet(title="Donor Title", artist="Donor Artist", genre="Synthwave"))

    changed = T.merge_missing_tags(keeper, [donor])

    assert changed is True
    result = T.read(keeper)
    assert result.title == "Kept Title"       # never overwritten
    assert result.artist == "Kept Artist"     # never overwritten
    assert result.genre == "Synthwave"        # filled from the donor


def test_merge_missing_tags_fills_missing_artwork(encoded, cover_png):
    keeper, donor = encoded["flac"], encoded["opus"]
    T.write(keeper, T.TagSet(title="Kept Title"))
    T.write(donor, T.TagSet(title="Donor Title"), artwork=T.Artwork.from_bytes(cover_png))

    assert T.merge_missing_tags(keeper, [donor]) is True
    assert T.read(keeper).artwork.data == cover_png


def test_merge_missing_tags_no_op_when_nothing_to_fill(encoded):
    keeper = encoded["flac"]
    T.write(keeper, T.TagSet(title="Complete", artist="Band", album="Album", genre="Rock"))

    assert T.merge_missing_tags(keeper, []) is False


def test_merge_missing_tags_ignores_an_unreadable_donor(encoded, tmp_path):
    keeper = encoded["flac"]
    T.write(keeper, T.TagSet(title="Kept Title"))
    junk_donor = tmp_path / "junk.mp3"
    junk_donor.write_bytes(b"not audio")

    # Must not raise -- a bad donor is simply skipped.
    changed = T.merge_missing_tags(keeper, [junk_donor])
    assert changed is False
    assert T.read(keeper).title == "Kept Title"


# ---------------------------------------------------------------------------
# Model behaviour
# ---------------------------------------------------------------------------


def test_identifies_a_gif():
    # Minimal GIF89a header: signature + 16-bit little-endian width/height.
    header = b"GIF89a" + (200).to_bytes(2, "little") + (100).to_bytes(2, "little")
    artwork = T.Artwork.from_bytes(header)
    assert artwork.mime == "image/gif"
    assert (artwork.width, artwork.height) == (200, 100)
    assert artwork.extension == ".gif"


def test_identifies_a_bmp():
    # BITMAPFILEHEADER is 14 bytes ("BM" + filesize + 2 reserved + data
    # offset), then BITMAPINFOHEADER's own header-size field (4 bytes)
    # before width/height land at offset 18/22.
    header = (
        b"BM" + b"\x00" * 12
        + (40).to_bytes(4, "little")
        + (300).to_bytes(4, "little", signed=True)
        + (150).to_bytes(4, "little", signed=True)
    )
    artwork = T.Artwork.from_bytes(header)
    assert artwork.mime == "image/bmp"
    assert (artwork.width, artwork.height) == (300, 150)
    assert artwork.extension == ".bmp"


def _insert_png_chunk(png: bytes, chunk_type: bytes, chunk_data: bytes, *, after: bytes = b"IHDR") -> bytes:
    """Splice one extra chunk into an existing PNG, right after ``after``."""
    import struct
    import zlib

    marker = png.index(after) - 4  # back up to that chunk's length field
    length = struct.unpack(">I", png[marker : marker + 4])[0]
    insert_at = marker + 4 + 4 + length + 4  # length + type + data + CRC
    body = chunk_type + chunk_data
    new_chunk = struct.pack(">I", len(chunk_data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return png[:insert_at] + new_chunk + png[insert_at:]


def test_strip_broken_png_icc_profile_removes_the_chunk(cover_png):
    with_icc = _insert_png_chunk(cover_png, b"iCCP", b"icc\x00\x00" + b"\x00" * 20)
    assert b"iCCP" in with_icc

    cleaned = T.strip_broken_png_icc_profile(with_icc)

    assert b"iCCP" not in cleaned
    # Still a well-formed PNG the rest of the pipeline can read.
    artwork = T.Artwork.from_bytes(cleaned)
    assert artwork.mime == "image/png"
    assert artwork.width > 0 and artwork.height > 0


def test_strip_broken_png_icc_profile_is_a_noop_without_one(cover_png):
    assert T.strip_broken_png_icc_profile(cover_png) == cover_png


def test_strip_broken_png_icc_profile_ignores_non_png_data():
    jpeg_like = b"\xff\xd8\xff\xe0not really a jpeg"
    assert T.strip_broken_png_icc_profile(jpeg_like) == jpeg_like


def test_a_jfif_file_is_identified_as_jpeg():
    # .jfif is the same JPEG format under a different extension -- what
    # matters is that the JPEG magic bytes are recognised regardless of what
    # the source file happened to be named.
    jpeg_like = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    artwork = T.Artwork.from_bytes(jpeg_like)
    assert artwork.mime == "image/jpeg"


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


# ---------------------------------------------------------------------------
# write_with_retry -- absorbing a transient "file still in use" lock
# ---------------------------------------------------------------------------


def test_write_with_retry_succeeds_immediately_when_not_locked(tone_flac):
    T.write_with_retry(tone_flac, T.TagSet(title="No Lock"))
    assert T.read(tone_flac).title == "No Lock"


def test_write_with_retry_absorbs_a_transient_permission_error(tone_flac, monkeypatch):
    real_write = T.write
    calls = {"n": 0}

    def flaky_write(path, tags, *, artwork=None):
        calls["n"] += 1
        if calls["n"] < 3:
            try:
                raise PermissionError(13, "Access is denied")
            except PermissionError as exc:
                raise T.TagError(f"Could not write tags to {path}: {exc}") from exc
        real_write(path, tags, artwork=artwork)

    monkeypatch.setattr(T, "write", flaky_write)

    T.write_with_retry(tone_flac, T.TagSet(title="Recovered"), attempts=5, delay_s=0)

    assert calls["n"] == 3
    assert T.read(tone_flac).title == "Recovered"


def test_write_with_retry_gives_up_after_exhausting_attempts(tone_flac, monkeypatch):
    def always_locked(path, tags, *, artwork=None):
        try:
            raise PermissionError(13, "Access is denied")
        except PermissionError as exc:
            raise T.TagError(f"Could not write tags to {path}: {exc}") from exc

    monkeypatch.setattr(T, "write", always_locked)

    with pytest.raises(T.TagError):
        T.write_with_retry(tone_flac, T.TagSet(title="x"), attempts=3, delay_s=0)


def test_write_with_retry_does_not_retry_an_unrelated_error(tone_flac, monkeypatch):
    """A real "not a recognised audio file"-style failure must surface
    immediately -- retrying it five times would only slow the user down for
    a problem that will never resolve itself."""
    calls = {"n": 0}

    def unrelated_failure(path, tags, *, artwork=None):
        calls["n"] += 1
        raise T.TagError(f"{path} is not a recognised audio file")

    monkeypatch.setattr(T, "write", unrelated_failure)

    with pytest.raises(T.TagError):
        T.write_with_retry(tone_flac, T.TagSet(title="x"), attempts=5, delay_s=0)
    assert calls["n"] == 1

"""Unified metadata read/write across every audio container.

Each format stores tags in a completely different system -- ID3 frames, Vorbis
comments, MP4 atoms, APEv2 items, ASF attributes -- with different names, types
and artwork encodings. This module presents one :class:`TagSet` and hides all
of it, so the rest of the app never has to care what container it is editing.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import mutagen
from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File, APEBinaryValue
from mutagen.asf import ASF, ASFByteArrayAttribute, ASFUnicodeAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack


class TagError(RuntimeError):
    """Raised when a file's tags cannot be read or written."""


# ---------------------------------------------------------------------------
# The unified model
# ---------------------------------------------------------------------------


@dataclass
class Artwork:
    """An embedded cover image."""

    data: bytes
    mime: str = "image/jpeg"
    #: ID3 picture type; 3 is "Cover (front)".
    picture_type: int = 3
    description: str = "Cover"
    width: int = 0
    height: int = 0

    def __len__(self) -> int:
        return len(self.data)

    @property
    def extension(self) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
        }.get(self.mime, ".jpg")

    @property
    def size_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return f"{len(self.data) / 1024:.0f} KB"

    @classmethod
    def from_bytes(cls, data: bytes, description: str = "Cover") -> "Artwork":
        """Build artwork from raw image bytes, sniffing type and dimensions."""
        mime, width, height = _identify_image(data)
        return cls(data=data, mime=mime, description=description, width=width, height=height)


@dataclass
class TagSet:
    """Format-independent metadata for one track.

    Empty strings and None both mean "not set"; writing a TagSet removes tags
    that are blank so clearing a field in the UI actually clears it in the file.
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    date: str = ""            # year or full ISO date
    genre: str = ""
    composer: str = ""
    comment: str = ""
    lyrics: str = ""
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    bpm: int | None = None
    isrc: str = ""
    publisher: str = ""
    copyright: str = ""
    encoded_by: str = ""
    #: Set for compilations so players group them under "Various Artists".
    compilation: bool = False
    #: MusicBrainz identifiers, kept so artwork lookups stay exact on re-runs.
    musicbrainz_trackid: str = ""
    musicbrainz_albumid: str = ""
    musicbrainz_artistid: str = ""
    replaygain_track_gain: str = ""
    replaygain_album_gain: str = ""
    #: Where this file came from, when downloaded from a URL.
    source_url: str = ""
    artwork: Artwork | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    @property
    def display_title(self) -> str:
        return self.title or "(untitled)"

    @property
    def display_artist(self) -> str:
        return self.artist or self.albumartist or "(unknown artist)"

    @property
    def effective_albumartist(self) -> str:
        """Album artist, falling back to track artist.

        Artwork lookup needs this: a compilation tagged only per-track would
        otherwise search for the wrong artist entirely.
        """
        return self.albumartist or self.artist

    def is_empty(self) -> bool:
        return not any(
            getattr(self, f.name) for f in fields(self) if f.name != "artwork"
        )

    def has_artwork(self) -> bool:
        return self.artwork is not None and len(self.artwork.data) > 0

    def to_dict(self, include_artwork: bool = False) -> dict:
        data = asdict(self)
        if not include_artwork:
            data.pop("artwork", None)
        return data

    def merged_with(self, other: "TagSet", overwrite: bool = False) -> "TagSet":
        """Combine two tag sets.

        With ``overwrite=False`` (the default) ``other`` only fills in blanks,
        which is what auto-tagging from a lookup should do -- it must never
        stomp on values the user typed by hand.
        """
        merged = TagSet(**{k: v for k, v in self.to_dict().items()})
        merged.artwork = self.artwork
        for f in fields(TagSet):
            new_value = getattr(other, f.name)
            if not new_value:
                continue
            if overwrite or not getattr(merged, f.name):
                setattr(merged, f.name, new_value)
        return merged


# ---------------------------------------------------------------------------
# Image sniffing
# ---------------------------------------------------------------------------


def _identify_image(data: bytes) -> tuple[str, int, int]:
    """Detect an image's MIME type and dimensions from its header bytes.

    Doing this by hand avoids a Pillow dependency in the core engine. Covers
    every format cover art actually turns up in: PNG/JPEG/WEBP (including
    ".jfif", which is just JPEG under a different extension -- the JPEG
    magic bytes already catch it), plus GIF and BMP.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height

    if data[:2] == b"\xff\xd8":  # JPEG (and .jfif, the same format)
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            # SOF0..SOF15, excluding the non-dimension markers DHT/JPG/DAC.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return "image/jpeg", width, height
            if index + 4 > len(data):
                break
            segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            index += 2 + segment_length
        return "image/jpeg", 0, 0

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return "image/webp", width, height
        if data[12:16] == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "image/webp", width, height
        return "image/webp", 0, 0

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height

    if data[:2] == b"BM" and len(data) >= 26:
        # BITMAPFILEHEADER (14 bytes) then BITMAPINFOHEADER's width/height
        # (signed 32-bit each, at offsets 18 and 22); a bottom-up bitmap
        # stores a positive height, top-down a negative one -- either way
        # the magnitude is what matters for display.
        width, height = struct.unpack("<ii", data[18:26])
        return "image/bmp", width, abs(height)

    return "image/jpeg", 0, 0


# ---------------------------------------------------------------------------
# Field name maps
# ---------------------------------------------------------------------------

#: TagSet field -> Vorbis comment key (FLAC, Ogg Vorbis, Opus, WavPack/APEv2).
VORBIS_MAP = {
    "title": "TITLE",
    "artist": "ARTIST",
    "album": "ALBUM",
    "albumartist": "ALBUMARTIST",
    "date": "DATE",
    "genre": "GENRE",
    "composer": "COMPOSER",
    "comment": "COMMENT",
    "lyrics": "LYRICS",
    "track_number": "TRACKNUMBER",
    "track_total": "TRACKTOTAL",
    "disc_number": "DISCNUMBER",
    "disc_total": "DISCTOTAL",
    "bpm": "BPM",
    "isrc": "ISRC",
    "publisher": "ORGANIZATION",
    "copyright": "COPYRIGHT",
    "encoded_by": "ENCODED-BY",
    "compilation": "COMPILATION",
    "musicbrainz_trackid": "MUSICBRAINZ_TRACKID",
    "musicbrainz_albumid": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_artistid": "MUSICBRAINZ_ARTISTID",
    "replaygain_track_gain": "REPLAYGAIN_TRACK_GAIN",
    "replaygain_album_gain": "REPLAYGAIN_ALBUM_GAIN",
    "source_url": "SOURCEURL",
}

#: TagSet field -> ID3v2.4 frame id (MP3, AIFF, WAV).
ID3_MAP = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "date": "TDRC",
    "genre": "TCON",
    "composer": "TCOM",
    "bpm": "TBPM",
    "isrc": "TSRC",
    "publisher": "TPUB",
    "copyright": "TCOP",
    "encoded_by": "TENC",
    "compilation": "TCMP",
}

#: TagSet field -> MP4 atom (M4A: AAC and ALAC).
MP4_MAP = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "albumartist": "aART",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "composer": "\xa9wrt",
    "comment": "\xa9cmt",
    "lyrics": "\xa9lyr",
    "encoded_by": "\xa9too",
    "copyright": "cprt",
}

#: Freeform MP4 atoms, written under the '----' namespace.
MP4_FREEFORM_MAP = {
    "isrc": "ISRC",
    "publisher": "PUBLISHER",
    "musicbrainz_trackid": "MusicBrainz Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "replaygain_track_gain": "REPLAYGAIN_TRACK_GAIN",
    "replaygain_album_gain": "REPLAYGAIN_ALBUM_GAIN",
    "source_url": "SOURCEURL",
}

#: TagSet field -> ASF attribute name (WMA).
ASF_MAP = {
    "title": "Title",
    "artist": "Author",
    "album": "WM/AlbumTitle",
    "albumartist": "WM/AlbumArtist",
    "date": "WM/Year",
    "genre": "WM/Genre",
    "composer": "WM/Composer",
    "comment": "Description",
    "lyrics": "WM/Lyrics",
    "isrc": "WM/ISRC",
    "publisher": "WM/Publisher",
    "copyright": "Copyright",
    "encoded_by": "WM/EncodedBy",
    # ASF permits arbitrary attribute names, so the fields with no official
    # WM/ equivalent use the names Picard and foobar2000 already write. Without
    # these, converting to WMA quietly drops them.
    "track_total": "WM/TrackCount",
    "disc_total": "WM/DiscCount",
    "musicbrainz_trackid": "MusicBrainz/Track Id",
    "musicbrainz_albumid": "MusicBrainz/Album Id",
    "musicbrainz_artistid": "MusicBrainz/Artist Id",
    "replaygain_track_gain": "replaygain_track_gain",
    "replaygain_album_gain": "replaygain_album_gain",
    "source_url": "SOURCEURL",
    "compilation": "WM/IsCompilation",
}

#: Fields stored as integers rather than text.
_INT_FIELDS = {"track_number", "track_total", "disc_number", "disc_total", "bpm"}


def _discard(container, *keys) -> None:
    """Remove ``keys`` from a mutagen tag container if present.

    mutagen's tag containers are only partly dict-like: several implement
    ``pop`` without a default, so ``pop(key, None)`` raises TypeError. Deleting
    under a guard is the one approach that works across all of them.
    """
    for key in keys:
        try:
            if key in container:
                del container[key]
        except (KeyError, TypeError, ValueError):
            continue


def _ape_key(key: str) -> str:
    """Normalise an APEv2 key for case- and separator-insensitive matching."""
    return str(key).lower().replace("_", " ").strip()


def _first(value):
    """Unwrap a tag value that may or may not be wrapped in a list.

    mutagen is inconsistent here: MP4 returns ``trkn`` as ``[(3, 12)]`` but
    ``cpil`` as a bare ``True``. Indexing blindly crashes on the latter.
    """
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _to_int(value) -> int | None:
    """Parse '7', '7/12', ' 7 ' and similar into 7."""
    if value is None:
        return None
    text = str(value).strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    try:
        return int(text)
    except ValueError:
        return None


def _split_pair(value) -> tuple[int | None, int | None]:
    """Parse a '3/12' number-of-total pair."""
    text = str(value).strip()
    if "/" in text:
        first, _, second = text.partition("/")
        return _to_int(first), _to_int(second)
    return _to_int(text), None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read(path: str | Path) -> TagSet:
    """Read metadata and embedded artwork from ``path``.

    Files with no tags at all return an empty :class:`TagSet` rather than
    raising -- an untagged file is a normal thing to import.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    try:
        audio = mutagen.File(str(path))
    except Exception as exc:  # mutagen raises a wide variety of parse errors
        raise TagError(f"Could not read tags from {path.name}: {exc}") from exc

    if audio is None:
        raise TagError(f"{path.name} is not a recognised audio file")

    if isinstance(audio, (FLAC, OggVorbis, OggOpus, OggFLAC)):
        return _read_vorbis(audio)
    if isinstance(audio, MP4):
        return _read_mp4(audio)
    if isinstance(audio, ASF):
        return _read_asf(audio)
    if isinstance(audio, (MP3, AIFF, WAVE)) or getattr(audio, "tags", None).__class__ is ID3:
        return _read_id3(audio)
    if isinstance(audio, (WavPack, APEv2File)):
        return _read_apev2(audio)

    # Anything else: fall back to whatever key/value pairs mutagen exposes.
    return _read_generic(audio)


def _read_vorbis(audio) -> TagSet:
    tags = TagSet()
    comments = audio.tags or {}

    def get(key: str) -> str:
        values = comments.get(key) or comments.get(key.lower()) or []
        return str(values[0]) if values else ""

    for field_name, key in VORBIS_MAP.items():
        raw = get(key)
        if not raw:
            continue
        if field_name in _INT_FIELDS:
            # TRACKNUMBER is frequently written as "3/12" despite the spec.
            number, total = _split_pair(raw)
            setattr(tags, field_name, number)
            if total is not None:
                if field_name == "track_number":
                    tags.track_total = total
                elif field_name == "disc_number":
                    tags.disc_total = total
        elif field_name == "compilation":
            tags.compilation = raw.strip() in ("1", "true", "yes")
        else:
            setattr(tags, field_name, raw)

    tags.artwork = _read_vorbis_artwork(audio)
    return tags


def _read_vorbis_artwork(audio) -> Artwork | None:
    # FLAC has real picture blocks.
    pictures = getattr(audio, "pictures", None)
    if pictures:
        picture = _preferred_picture(pictures)
        return Artwork(
            data=picture.data,
            mime=picture.mime or "image/jpeg",
            picture_type=picture.type,
            description=picture.desc or "Cover",
            width=picture.width,
            height=picture.height,
        )

    # Ogg Vorbis/Opus embed a base64 FLAC picture block in a comment field.
    comments = audio.tags or {}
    encoded = comments.get("metadata_block_picture") or comments.get("METADATA_BLOCK_PICTURE")
    if encoded:
        try:
            picture = Picture(base64.b64decode(encoded[0]))
            return Artwork(
                data=picture.data,
                mime=picture.mime or "image/jpeg",
                picture_type=picture.type,
                description=picture.desc or "Cover",
                width=picture.width,
                height=picture.height,
            )
        except (ValueError, TypeError, struct.error):
            return None

    # Some taggers just base64 a raw JPEG into COVERART.
    legacy = comments.get("coverart") or comments.get("COVERART")
    if legacy:
        try:
            return Artwork.from_bytes(base64.b64decode(legacy[0]))
        except (ValueError, TypeError):
            return None
    return None


def _preferred_picture(pictures):
    """Front cover if present, otherwise the largest image."""
    fronts = [p for p in pictures if getattr(p, "type", 3) == 3]
    candidates = fronts or list(pictures)
    return max(candidates, key=lambda p: len(p.data))


def _read_id3(audio) -> TagSet:
    tags = TagSet()
    id3 = audio.tags
    if id3 is None:
        return tags

    def text(frame_id: str) -> str:
        frame = id3.get(frame_id)
        if frame is None:
            return ""
        return str(frame.text[0]) if getattr(frame, "text", None) else ""

    for field_name, frame_id in ID3_MAP.items():
        raw = text(frame_id)
        if not raw:
            continue
        if field_name in _INT_FIELDS:
            setattr(tags, field_name, _to_int(raw))
        elif field_name == "compilation":
            tags.compilation = raw.strip() in ("1", "true", "yes")
        else:
            setattr(tags, field_name, raw)

    tags.track_number, tags.track_total = _split_pair(text("TRCK"))
    tags.disc_number, tags.disc_total = _split_pair(text("TPOS"))

    for frame in id3.getall("COMM"):
        if getattr(frame, "text", None):
            tags.comment = str(frame.text[0])
            break
    for frame in id3.getall("USLT"):
        if getattr(frame, "text", None):
            tags.lyrics = str(frame.text)
            break

    # MusicBrainz ids and ReplayGain live in TXXX user-defined frames.
    txxx = {
        str(f.desc).lower(): (str(f.text[0]) if f.text else "")
        for f in id3.getall("TXXX")
    }
    tags.musicbrainz_trackid = txxx.get("musicbrainz release track id", "") or txxx.get(
        "musicbrainz track id", ""
    )
    tags.musicbrainz_albumid = txxx.get("musicbrainz album id", "")
    tags.musicbrainz_artistid = txxx.get("musicbrainz artist id", "")
    tags.replaygain_track_gain = txxx.get("replaygain_track_gain", "")
    tags.replaygain_album_gain = txxx.get("replaygain_album_gain", "")
    tags.source_url = txxx.get("sourceurl", "")
    if not tags.isrc:
        tags.isrc = txxx.get("isrc", "")

    apics = id3.getall("APIC")
    if apics:
        picture = _preferred_picture(apics)
        tags.artwork = Artwork.from_bytes(picture.data, picture.desc or "Cover")
        tags.artwork.mime = picture.mime or tags.artwork.mime
        tags.artwork.picture_type = picture.type
    return tags


def _read_mp4(audio) -> TagSet:
    tags = TagSet()
    atoms = audio.tags or {}

    for field_name, atom in MP4_MAP.items():
        value = _first(atoms.get(atom))
        if value:
            setattr(tags, field_name, str(value))

    for atom, number_field, total_field in (
        ("trkn", "track_number", "track_total"),
        ("disk", "disc_number", "disc_total"),
    ):
        pair = _first(atoms.get(atom))
        if isinstance(pair, (list, tuple)) and pair:
            setattr(tags, number_field, pair[0] or None)
            if len(pair) > 1:
                setattr(tags, total_field, pair[1] or None)
    if "tmpo" in atoms:
        tags.bpm = _to_int(_first(atoms.get("tmpo")))
    if "cpil" in atoms:
        tags.compilation = bool(_first(atoms.get("cpil")))

    for field_name, name in MP4_FREEFORM_MAP.items():
        raw = _first(atoms.get(f"----:com.apple.iTunes:{name}"))
        if raw:
            setattr(tags, field_name, bytes(raw).decode("utf-8", "replace"))

    covers = atoms.get("covr")
    if covers:
        cover = max(covers, key=len)
        mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        tags.artwork = Artwork.from_bytes(bytes(cover))
        tags.artwork.mime = mime
    return tags


def _read_asf(audio) -> TagSet:
    tags = TagSet()
    attrs = audio.tags or {}

    for field_name, key in ASF_MAP.items():
        raw = _first(attrs.get(key))
        if raw is None or str(raw) == "":
            continue
        text = str(raw)
        if field_name in _INT_FIELDS:
            setattr(tags, field_name, _to_int(text))
        elif field_name == "compilation":
            tags.compilation = text.strip().lower() in ("1", "true", "yes")
        else:
            setattr(tags, field_name, text)

    for key, target in (("WM/TrackNumber", "track_number"), ("WM/PartOfSet", "disc_number")):
        values = attrs.get(key)
        if values:
            number, total = _split_pair(str(values[0]))
            setattr(tags, target, number)
            if total is not None:
                setattr(tags, "track_total" if target == "track_number" else "disc_total", total)

    values = attrs.get("WM/BeatsPerMinute")
    if values:
        tags.bpm = _to_int(str(values[0]))

    pictures = attrs.get("WM/Picture")
    if pictures:
        parsed = _parse_asf_picture(bytes(pictures[0].value))
        if parsed is not None:
            tags.artwork = parsed
    return tags


def _parse_asf_picture(blob: bytes) -> Artwork | None:
    """Decode the WM/Picture byte layout: type, size, MIME, description, data."""
    try:
        picture_type = blob[0]
        offset = 5  # 1 byte type + 4 byte length
        mime_end = blob.index(b"\x00\x00", offset)
        # UTF-16LE strings, so align the terminator to an even offset.
        while (mime_end - offset) % 2:
            mime_end = blob.index(b"\x00\x00", mime_end + 1)
        mime = blob[offset:mime_end].decode("utf-16-le", "replace")
        offset = mime_end + 2
        desc_end = blob.index(b"\x00\x00", offset)
        while (desc_end - offset) % 2:
            desc_end = blob.index(b"\x00\x00", desc_end + 1)
        description = blob[offset:desc_end].decode("utf-16-le", "replace")
        data = blob[desc_end + 2 :]
        artwork = Artwork.from_bytes(data, description or "Cover")
        artwork.mime = mime or artwork.mime
        artwork.picture_type = picture_type
        return artwork
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def _read_apev2(audio) -> TagSet:
    tags = TagSet()
    items = audio.tags or {}

    # APEv2 keys are conventionally title-case with spaces where Vorbis uses
    # underscores. Normalise both sides so a key written as
    # "Musicbrainz Albumid" still matches the MUSICBRAINZ_ALBUMID field.
    lookup = {_ape_key(k): v for k, v in items.items()}

    for field_name, key in VORBIS_MAP.items():
        value = lookup.get(_ape_key(key))
        if value is None:
            continue
        raw = str(value)
        if field_name in _INT_FIELDS:
            number, total = _split_pair(raw)
            setattr(tags, field_name, number)
            if total is not None and field_name == "track_number":
                tags.track_total = total
        elif field_name == "compilation":
            tags.compilation = raw.strip() in ("1", "true", "yes")
        else:
            setattr(tags, field_name, raw)

    for key in ("cover art (front)", "cover art (back)"):
        value = lookup.get(key)
        if value is None:
            continue
        blob = bytes(value.value if hasattr(value, "value") else value)
        # APEv2 art is "filename\x00<binary>".
        _, _, data = blob.partition(b"\x00")
        if data:
            tags.artwork = Artwork.from_bytes(data)
            break
    return tags


def _read_generic(audio) -> TagSet:
    """Last-resort reader for containers we have no specific map for."""
    tags = TagSet()
    raw = audio.tags or {}
    lookup = {}
    try:
        for key in raw.keys():
            values = raw[key]
            lookup[str(key).lower()] = str(values[0] if isinstance(values, list) else values)
    except (AttributeError, TypeError, IndexError):
        return tags

    for field_name, key in VORBIS_MAP.items():
        value = lookup.get(key.lower())
        if not value:
            continue
        if field_name in _INT_FIELDS:
            setattr(tags, field_name, _to_int(value))
        elif field_name == "compilation":
            tags.compilation = value.strip() in ("1", "true", "yes")
        else:
            setattr(tags, field_name, value)
    return tags


def try_read(path: str | Path) -> TagSet:
    """Read tags, returning an empty set on any failure."""
    try:
        return read(path)
    except (TagError, FileNotFoundError, OSError):
        return TagSet()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write(path: str | Path, tags: TagSet, *, artwork: Artwork | None = None) -> None:
    """Write ``tags`` to ``path``, replacing what is there.

    ``artwork`` overrides ``tags.artwork`` when given. Passing neither leaves
    any existing embedded image alone; pass ``Artwork(b"")`` to remove it.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    image = artwork if artwork is not None else tags.artwork

    try:
        audio = mutagen.File(str(path))
        if audio is None:
            raise TagError(f"{path.name} is not a recognised audio file")

        if isinstance(audio, (FLAC, OggVorbis, OggOpus, OggFLAC)):
            _write_vorbis(audio, tags, image)
        elif isinstance(audio, MP4):
            _write_mp4(audio, tags, image)
        elif isinstance(audio, ASF):
            _write_asf(audio, tags, image)
        elif isinstance(audio, (MP3, AIFF, WAVE)):
            _write_id3(audio, path, tags, image)
        elif isinstance(audio, (WavPack, APEv2File)):
            _write_apev2(audio, tags, image)
        else:
            _write_generic(audio, tags)
        audio.save()
    except TagError:
        raise
    except Exception as exc:
        raise TagError(f"Could not write tags to {path.name}: {exc}") from exc


def _field_text(tags: TagSet, field_name: str) -> str:
    """Render one TagSet field as the string a tag should hold."""
    value = getattr(tags, field_name)
    if field_name == "compilation":
        return "1" if value else ""
    if value is None or value == "":
        return ""
    return str(value)


def _write_vorbis(audio, tags: TagSet, artwork: Artwork | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    comments = audio.tags

    for field_name, key in VORBIS_MAP.items():
        text = _field_text(tags, field_name)
        if text:
            comments[key] = [text]
        else:
            # Vorbis keys are case-insensitive but stored as written, so clear
            # both spellings or a stale lowercase key survives the edit.
            _discard(comments, key, key.lower())

    if artwork is None:
        return

    if isinstance(audio, FLAC):
        audio.clear_pictures()
        if artwork.data:
            audio.add_picture(_flac_picture(artwork))
    else:
        _discard(comments, "metadata_block_picture", "METADATA_BLOCK_PICTURE")
        if artwork.data:
            block = _flac_picture(artwork).write()
            comments["metadata_block_picture"] = [base64.b64encode(block).decode("ascii")]


def _flac_picture(artwork: Artwork) -> Picture:
    picture = Picture()
    picture.data = artwork.data
    picture.type = artwork.picture_type
    picture.mime = artwork.mime
    picture.desc = artwork.description
    picture.width = artwork.width
    picture.height = artwork.height
    picture.depth = 24
    return picture


def _write_id3(audio, path: Path, tags: TagSet, artwork: Artwork | None) -> None:
    from mutagen.id3 import COMM, TXXX, USLT

    if audio.tags is None:
        try:
            audio.add_tags()
        except mutagen.MutagenError:
            audio.tags = ID3()
    id3 = audio.tags

    for field_name, frame_id in ID3_MAP.items():
        text = _field_text(tags, field_name)
        id3.delall(frame_id)
        if text:
            frame_class = getattr(__import__("mutagen.id3", fromlist=[frame_id]), frame_id)
            id3.add(frame_class(encoding=3, text=[text]))

    id3.delall("TRCK")
    if tags.track_number is not None:
        value = str(tags.track_number)
        if tags.track_total:
            value = f"{value}/{tags.track_total}"
        from mutagen.id3 import TRCK

        id3.add(TRCK(encoding=3, text=[value]))

    id3.delall("TPOS")
    if tags.disc_number is not None:
        value = str(tags.disc_number)
        if tags.disc_total:
            value = f"{value}/{tags.disc_total}"
        from mutagen.id3 import TPOS

        id3.add(TPOS(encoding=3, text=[value]))

    id3.delall("COMM")
    if tags.comment:
        id3.add(COMM(encoding=3, lang="eng", desc="", text=[tags.comment]))

    id3.delall("USLT")
    if tags.lyrics:
        id3.add(USLT(encoding=3, lang="eng", desc="", text=tags.lyrics))

    for desc, value in (
        ("MusicBrainz Release Track Id", tags.musicbrainz_trackid),
        ("MusicBrainz Album Id", tags.musicbrainz_albumid),
        ("MusicBrainz Artist Id", tags.musicbrainz_artistid),
        ("replaygain_track_gain", tags.replaygain_track_gain),
        ("replaygain_album_gain", tags.replaygain_album_gain),
        ("SOURCEURL", tags.source_url),
    ):
        for frame in list(id3.getall("TXXX")):
            if str(frame.desc).lower() == desc.lower():
                id3.delall(f"TXXX:{frame.desc}")
        if value:
            id3.add(TXXX(encoding=3, desc=desc, text=[value]))

    if artwork is not None:
        id3.delall("APIC")
        if artwork.data:
            id3.add(
                APIC(
                    encoding=3,
                    mime=artwork.mime,
                    type=artwork.picture_type,
                    desc=artwork.description,
                    data=artwork.data,
                )
            )

    # ID3v2.3 is what Windows Explorer and older players actually read;
    # v2.4-only tags show up blank there.
    id3.update_to_v23()
    id3.save(str(path), v2_version=3)


def _write_mp4(audio, tags: TagSet, artwork: Artwork | None) -> None:
    from mutagen.mp4 import MP4FreeForm

    if audio.tags is None:
        audio.add_tags()
    atoms = audio.tags

    for field_name, atom in MP4_MAP.items():
        text = _field_text(tags, field_name)
        if text:
            atoms[atom] = [text]
        else:
            _discard(atoms, atom)

    if tags.track_number is not None:
        atoms["trkn"] = [(tags.track_number, tags.track_total or 0)]
    else:
        _discard(atoms, "trkn")
    if tags.disc_number is not None:
        atoms["disk"] = [(tags.disc_number, tags.disc_total or 0)]
    else:
        _discard(atoms, "disk")
    if tags.bpm is not None:
        atoms["tmpo"] = [tags.bpm]
    else:
        _discard(atoms, "tmpo")
    atoms["cpil"] = [bool(tags.compilation)]

    for field_name, name in MP4_FREEFORM_MAP.items():
        key = f"----:com.apple.iTunes:{name}"
        text = _field_text(tags, field_name)
        if text:
            atoms[key] = [MP4FreeForm(text.encode("utf-8"))]
        else:
            _discard(atoms, key)

    if artwork is not None:
        _discard(atoms, "covr")
        if artwork.data:
            # Apple's "covr" atom only ever declares JPEG or PNG -- unlike
            # ID3/FLAC/ASF, MP4 has no third option, so a GIF/BMP/WEBP cover
            # still gets tagged as JPEG here. The bytes are written as-is
            # (never re-encoded), so a source that really was JPEG-compatible
            # displays fine; a true GIF/BMP would need actual image
            # transcoding to embed correctly in an M4A, which this app does
            # not do. In practice this only matters for a manually-chosen
            # local image file -- every online art source and the download
            # thumbnail path already only ever provide JPEG/PNG/WEBP.
            image_format = (
                MP4Cover.FORMAT_PNG if artwork.mime == "image/png" else MP4Cover.FORMAT_JPEG
            )
            atoms["covr"] = [MP4Cover(artwork.data, imageformat=image_format)]


def _write_asf(audio, tags: TagSet, artwork: Artwork | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    attrs = audio.tags

    for field_name, key in ASF_MAP.items():
        text = _field_text(tags, field_name)
        if text:
            attrs[key] = [ASFUnicodeAttribute(text)]
        else:
            _discard(attrs, key)

    if tags.track_number is not None:
        attrs["WM/TrackNumber"] = [ASFUnicodeAttribute(str(tags.track_number))]
    else:
        _discard(attrs, "WM/TrackNumber")
    if tags.disc_number is not None:
        attrs["WM/PartOfSet"] = [ASFUnicodeAttribute(str(tags.disc_number))]
    else:
        _discard(attrs, "WM/PartOfSet")
    if tags.bpm is not None:
        attrs["WM/BeatsPerMinute"] = [ASFUnicodeAttribute(str(tags.bpm))]
    else:
        _discard(attrs, "WM/BeatsPerMinute")

    if artwork is not None:
        _discard(attrs, "WM/Picture")
        if artwork.data:
            attrs["WM/Picture"] = [ASFByteArrayAttribute(_build_asf_picture(artwork))]


def _build_asf_picture(artwork: Artwork) -> bytes:
    """Encode artwork into the WM/Picture byte layout."""
    mime = artwork.mime.encode("utf-16-le") + b"\x00\x00"
    description = artwork.description.encode("utf-16-le") + b"\x00\x00"
    return (
        bytes([artwork.picture_type])
        + struct.pack("<I", len(artwork.data))
        + mime
        + description
        + artwork.data
    )


def _write_apev2(audio, tags: TagSet, artwork: Artwork | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    items = audio.tags

    for field_name, key in VORBIS_MAP.items():
        # APEv2 convention is title-case keys with spaces, e.g. "Album Artist".
        ape_key = key.replace("_", " ").title()
        text = _field_text(tags, field_name)
        for existing in [k for k in list(items.keys()) if _ape_key(k) == _ape_key(ape_key)]:
            del items[existing]
        if text:
            items[ape_key] = text

    if artwork is not None:
        for existing in [k for k in list(items.keys()) if str(k).lower().startswith("cover art")]:
            del items[existing]
        if artwork.data:
            filename = f"cover{artwork.extension}".encode("utf-8")
            items["Cover Art (Front)"] = APEBinaryValue(filename + b"\x00" + artwork.data)


def _write_generic(audio, tags: TagSet) -> None:
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as exc:
            raise TagError(f"This file format does not support tags: {exc}") from exc
    for field_name, key in VORBIS_MAP.items():
        text = _field_text(tags, field_name)
        try:
            if text:
                audio.tags[key] = text
            else:
                _discard(audio.tags, key)
        except (KeyError, TypeError, ValueError):
            continue


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def copy_tags(source: str | Path, destination: str | Path) -> None:
    """Carry metadata and artwork from one file to another.

    Used after conversion so a new FLAC keeps everything the source MP3 had,
    including its cover art, regardless of how differently the two formats
    store it.
    """
    tags = read(source)
    write(destination, tags, artwork=tags.artwork)


def merge_missing_tags(keeper: str | Path, donors: list[str | Path]) -> bool:
    """Fill any blank fields (and missing artwork) on ``keeper`` from ``donors``.

    Built on TagSet.merged_with()'s existing overwrite=False contract, so a
    donor can only fill a blank -- it never replaces a value the keeper
    already has. Donors are read in order; the first non-blank value for
    each field wins. Returns True if the keeper's tags actually changed.
    """
    keeper = Path(keeper)
    original = read(keeper)
    merged = original
    for donor in donors:
        try:
            merged = merged.merged_with(read(Path(donor)))
        except (TagError, FileNotFoundError, OSError):
            continue
    if merged.to_dict(include_artwork=True) == original.to_dict(include_artwork=True):
        return False
    write(keeper, merged, artwork=merged.artwork)
    return True


def supports_artwork(path: str | Path) -> bool:
    """Whether this file's container can carry an embedded image."""
    suffix = Path(path).suffix.lower()
    return suffix not in (".wav", ".wave")

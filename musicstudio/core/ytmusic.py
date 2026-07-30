"""Normalising metadata to YouTube Music's conventions.

YouTube Music groups an uploaded library by **album artist**, not by track
artist. A library where that field is blank -- which is what downloading
from YouTube leaves you with -- fragments into one "album" per featured
artist. Its catalogue also keeps song titles clean (``Numb``, not
``Numb (Official Music Video) [4K UPGRADE]``) and puts guests in the title
as ``(feat. X)`` rather than in the artist field.

This module reshapes tags to match, and unlike :mod:`musicstudio.core.tag_fix`
-- which only ever fills blanks -- it deliberately *rewrites* existing
values, because "clean up this title" cannot be done any other way. Callers
are expected to snapshot first; :func:`snapshot_tags` does that.

Nothing here touches the network: it is pure string normalisation over tags
already on disk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import tags as tags_module
from .tags import TagSet

# ---------------------------------------------------------------------------
# Featured artists
# ---------------------------------------------------------------------------

#: "feat.", "ft", "featuring", "f/" -- optionally already wrapped in brackets.
#: A bare "f" only counts when written "f/", the way it is actually used;
#: allowing "f" on its own would split any title containing a stray letter f.
_FEATURE_SPLIT = re.compile(
    r"\s*[\(\[]?\s*(?:\b(?:feat|ft|featuring)\b\.?|\bf/)\s*",
    re.IGNORECASE,
)

#: A trailing "(feat. ...)" already present on a title.
_FEATURE_IN_TITLE = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring)\b\.?\s*[^)\]]*[\)\]]",
    re.IGNORECASE,
)

#: Separators between multiple guests: "A, B & C".
_GUEST_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\+|/|;)\s*", re.IGNORECASE)


def split_featured(artist: str) -> tuple[str, list[str]]:
    """Split ``"Omarion ft. Usher, Fabolous & Busta Rhymes"`` into
    ``("Omarion", ["Usher", "Fabolous", "Busta Rhymes"])``.

    An artist with no feature marker comes back unchanged with no guests --
    importantly, a name that merely *contains* an ampersand ("Hall & Oates",
    "Earth, Wind & Fire") is left whole, because only text after an explicit
    feat./ft. marker is treated as a guest list.
    """
    if not artist:
        return "", []
    parts = _FEATURE_SPLIT.split(artist, maxsplit=1)
    primary = parts[0].strip(" ([-–—,&")
    if len(parts) == 1:
        return primary, []
    guests = [
        guest.strip(" )]([-–—")
        for guest in _GUEST_SPLIT.split(parts[1].strip(" )]"))
        if guest.strip(" )]([-–—")
    ]
    return primary, guests


def format_feature_suffix(guests: list[str]) -> str:
    """Render guests the way YouTube Music writes them: ``(feat. A, B & C)``."""
    if not guests:
        return ""
    if len(guests) == 1:
        listed = guests[0]
    else:
        listed = f"{', '.join(guests[:-1])} & {guests[-1]}"
    return f"(feat. {listed})"


# ---------------------------------------------------------------------------
# Title cleanup
# ---------------------------------------------------------------------------

#: Words that, on their own, make a bracketed segment promotional rather than
#: part of the song's actual name. A segment is only dropped when *every*
#: word in it is one of these, so "(Remix)", "(Live)", "(Acoustic)",
#: "(Radio Edit)" and "(Remastered 2011)" all survive untouched.
_NOISE_WORDS = {
    "official", "officiel", "music", "video", "videos", "vid",
    "lyric", "lyrics", "lyrical", "lyricvideo",
    "audio", "sound", "visualizer", "visualiser", "visual",
    "hd", "hq", "uhd", "4k", "8k", "1080p", "720p", "quality",
    "explicit", "upgrade", "remastered4k", "full", "complete",
    "with", "and", "the", "in", "new", "free", "download",
}

_BRACKETED = re.compile(r"\s*[\(\[\{]([^)\]\}]*)[\)\]\}]")

#: Trailing junk that starts at an emoji -- "🎥 By: @somebody".
_EMOJI_TAIL = re.compile(
    r"\s*[\U0001F000-\U0001FAFF☀-➿️].*$"
)

#: Channel-handle credits, with or without a leading emoji.
_HANDLE_TAIL = re.compile(r"\s*(?:by\s*:?\s*)?@[\w.\-]+\s*$", re.IGNORECASE)

#: Separators a YouTube title uses between artist and song.
_TITLE_SEPARATORS = (" // ", " - ", " – ", " — ", " | ")


def _drop_noise_segments(text: str) -> str:
    """Remove bracketed segments that are purely promotional."""

    def replace(match: re.Match) -> str:
        inner = match.group(1)
        words = re.findall(r"[a-z0-9]+", inner.lower())
        if not words:
            return ""
        return "" if all(word in _NOISE_WORDS for word in words) else match.group(0)

    return _BRACKETED.sub(replace, text)


def _strip_artist_echo(title: str, artist: str) -> str:
    """Drop a leading or trailing copy of the artist name.

    YouTube titles are routinely "Artist - Song" or "Song – Artist"; once the
    artist is its own tag, repeating it in the title is noise.
    """
    if not artist or not title:
        return title
    lowered_artist = artist.strip().lower()
    for separator in _TITLE_SEPARATORS:
        if separator in title:
            head, _, tail = title.partition(separator)
            if head.strip().lower() == lowered_artist and tail.strip():
                return tail.strip()
            # Only the final segment is considered a trailing echo, so a title
            # that legitimately contains a dash keeps its middle intact.
            head_all, _, last = title.rpartition(separator)
            if last.strip().lower() == lowered_artist and head_all.strip():
                return head_all.strip()
    return title


def _split_at_separator(text: str) -> tuple[str, str]:
    """Cut ``text`` at the first title separator, returning both halves."""
    for separator in _TITLE_SEPARATORS:
        if separator in text:
            head, _, tail = text.partition(separator)
            return head.strip(), tail.strip()
    return text.strip(), ""


def normalise_title_features(title: str, artist: str = "") -> str:
    """Rewrite a bare ``ft. X`` inside a title as YouTube Music's ``(feat. X)``.

    A title that already brackets its credit is left exactly as it is, so
    this never double-wraps.

    The credit is bounded at the next title separator, because YouTube titles
    are often written "Artist ft. Guest - Song". Reading to the end of the
    string there would swallow the actual song name into the credit. When
    the part before the marker turns out to *be* the artist, the part after
    the separator is the real title; when it is something else, the title is
    too ambiguous to rewrite and is returned untouched.
    """
    if not title or _FEATURE_IN_TITLE.search(title):
        return title
    parts = _FEATURE_SPLIT.split(title, maxsplit=1)
    if len(parts) == 1:
        return title

    base = parts[0].strip(" ([-–—,&")
    guest_text, remainder = _split_at_separator(parts[1].strip(" )]"))
    guests = [
        guest.strip(" )]([-–—")
        for guest in _GUEST_SPLIT.split(guest_text)
        if guest.strip(" )]([-–—")
    ]
    if not guests:
        return title

    if remainder:
        if artist and base.lower() == artist.strip().lower():
            base = remainder      # "Artist ft. Guest - Song"
        else:
            return title          # too ambiguous to rewrite safely
    if not base:
        return title
    return f"{base} {format_feature_suffix(guests)}".strip()


def clean_title(title: str, artist: str = "") -> str:
    """Strip promotional noise, channel credits and a duplicated artist name."""
    if not title:
        return ""
    cleaned = _EMOJI_TAIL.sub("", title)
    cleaned = _HANDLE_TAIL.sub("", cleaned)
    cleaned = _drop_noise_segments(cleaned)
    cleaned = _strip_artist_echo(cleaned.strip(), artist)
    cleaned = _drop_noise_segments(cleaned)
    # Collapse the whitespace and dangling punctuation the removals leave.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" -–—|/,&")
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Genre
# ---------------------------------------------------------------------------

#: Every spelling seen in the wild, folded onto one canonical bucket. Keyed
#: on the lowercased, whitespace-collapsed value.
_GENRE_ALIASES = {
    # Hip-hop arrives under a dozen spellings, which splits one genre into a
    # dozen shelves in YouTube Music's browser.
    "hip-hop/rap": "Hip-Hop/Rap", "hip-hop": "Hip-Hop/Rap", "hip hop": "Hip-Hop/Rap",
    "hiphop": "Hip-Hop/Rap", "rap": "Hip-Hop/Rap", "hip hop/rap": "Hip-Hop/Rap",
    "hip hop / rap": "Hip-Hop/Rap", "hip-hop; rap": "Hip-Hop/Rap",
    "hip hop; rap": "Hip-Hop/Rap", "rap/hip-hop": "Hip-Hop/Rap",
    "rap/hip hop": "Hip-Hop/Rap", "rap/hiphop": "Hip-Hop/Rap",
    "rap/hip": "Hip-Hop/Rap", "rap & hip-hop": "Hip-Hop/Rap",
    "hip-hop & rap": "Hip-Hop/Rap", "hip-hop and rap": "Hip-Hop/Rap",
    "dirty south": "Hip-Hop/Rap", "urban": "Hip-Hop/Rap", "trap": "Hip-Hop/Rap",
    "gangsta rap": "Hip-Hop/Rap", "underground rap": "Hip-Hop/Rap",
    "rap/hip-hop/r&b": "Hip-Hop/Rap",
    # R&B
    "r&b": "R&B/Soul", "r & b": "R&B/Soul", "rnb": "R&B/Soul",
    "r&b/soul": "R&B/Soul", "soul": "R&B/Soul", "r&b & soul": "R&B/Soul",
    "contemporary r&b": "R&B/Soul", "neo-soul": "R&B/Soul",
    # Rock
    "rock": "Rock", "hard rock": "Rock", "classic rock": "Rock",
    "alternative rock": "Rock", "indie rock": "Rock", "punk": "Rock",
    "alternative": "Alternative", "indie": "Alternative",
    # Electronic
    "electronic": "Electronic", "electronica": "Electronic", "edm": "Electronic",
    "dance": "Electronic", "house": "Electronic", "techno": "Electronic",
    "dubstep": "Electronic", "drum & bass": "Electronic",
    "танцевальная/электронная музыка": "Electronic",
    # Reggae and the Caribbean
    "reggae": "Reggae", "dancehall": "Reggae", "modern dancehall": "Reggae",
    "reggaeton": "Reggae", "soca": "Reggae",
    # Everything else that only needs its capitalisation settled
    "pop": "Pop", "j-pop": "Pop", "k-pop": "Pop", "pop latino": "Latin",
    "latin": "Latin", "urbano latino": "Latin",
    "country": "Country", "bluegrass": "Country",
    "blues": "Blues", "jazz": "Jazz", "big band": "Jazz",
    "classical": "Classical", "christmas: classical": "Classical",
    "soundtrack": "Soundtrack", "score": "Soundtrack", "anime": "Soundtrack",
    "metal": "Metal", "heavy metal": "Metal",
    "gospel": "Christian", "christian": "Christian",
    "new age": "New Age", "world": "World", "worldwide": "World",
    "bollywood": "Bollywood", "industrial": "Industrial",
    "singer/songwriter": "Singer/Songwriter", "folk": "Folk",
    "karaoke": "Karaoke", "tribute": "Karaoke",
}

#: Values that are not genres at all -- YouTube's own category names, spam
#: left by download sites, record labels, and placeholder text.
_GENRE_JUNK = {
    "music", "other", "others", "genre", "unknown", "misc", "miscellaneous",
    "people & blogs", "entertainment", "hot 100", "grand hustle", "n/a",
    "none", "various", "audio", "другое",
}

#: Spam domains dumped into the genre tag by download sites.
_GENRE_SPAM = re.compile(r"(?:https?://|www\.|\.com|\.net|\.ru|\.mobi|\.org|\d{6,})", re.I)

#: Placeholders and chart/blog names that turn up in the album tag but are
#: not releases. Left in place they invent an "album" that collects unrelated
#: tracks, which is exactly what YouTube Music then shows.
_ALBUM_JUNK = {
    "unknown album", "unknown", "untitled", "various", "various artists",
    "misc", "miscellaneous", "n/a", "none", "album", "single", "music",
    "billboard hot 100", "hot 100", "top 100", "charts", "mixtape",
}


def canonical_album(album: str) -> str:
    """Blank an album value that is a placeholder or download-site spam."""
    if not album:
        return ""
    collapsed = re.sub(r"\s+", " ", album).strip()
    if not collapsed:
        return ""
    if collapsed.lower() in _ALBUM_JUNK or _GENRE_SPAM.search(collapsed):
        return ""
    return collapsed


def canonical_genre(genre: str) -> str:
    """Fold a genre onto one canonical spelling, or blank it when it is junk.

    An unrecognised but plausible genre is kept as-is rather than discarded --
    the map only has to cover the messy cases, not every genre in existence.
    """
    if not genre:
        return ""
    collapsed = re.sub(r"\s+", " ", genre).strip()
    if not collapsed:
        return ""
    lowered = collapsed.lower()
    if lowered in _GENRE_ALIASES:
        return _GENRE_ALIASES[lowered]
    if lowered in _GENRE_JUNK or _GENRE_SPAM.search(collapsed):
        return ""
    # Mojibake: a value carrying no ASCII letters at all is not a genre
    # anyone typed, it is a mis-decoded byte string.
    if not re.search(r"[A-Za-z]", collapsed):
        return ""
    return collapsed


# ---------------------------------------------------------------------------
# Whole-tag normalisation
# ---------------------------------------------------------------------------


def normalise_tags(tags: TagSet, *, album_artist: str | None = None) -> TagSet:
    """Return ``tags`` reshaped to YouTube Music's conventions.

    ``album_artist`` overrides the derived value, which is how a caller that
    can see the whole album (see :func:`normalise_library`) marks a
    compilation as "Various Artists".
    """
    updated = TagSet(**tags.to_dict())
    updated.artwork = tags.artwork

    primary, guests = split_featured(tags.artist)
    title = clean_title(tags.title, primary or tags.artist)
    # A credit already written into the title ("Copycats ft. Underscores")
    # gets the bracketed form before any artist-field guests are considered,
    # so the check below sees it and does not credit the same names twice.
    title = normalise_title_features(title, primary or tags.artist)

    # Guests move out of the artist field and into the title, which is how
    # YouTube Music's own catalogue reads. A title that already credits them
    # is left alone rather than credited twice.
    if guests and not _FEATURE_IN_TITLE.search(title):
        suffix = format_feature_suffix(guests)
        title = f"{title} {suffix}".strip() if title else suffix

    updated.title = title
    updated.artist = primary or tags.artist

    album = clean_title(tags.album, primary or tags.artist)
    # Downloads routinely land with the whole video title copied into the
    # album field. That is not an album, and it makes every track its own
    # release. It is only treated as an artifact when cleaning actually had
    # to strip something -- an album that always just matched the song name
    # is a single, and YouTube Music is right to show it as one.
    looked_like_a_video_title = album != (tags.album or "").strip()
    if album and looked_like_a_video_title and _norm_compare(album) == _norm_compare(title):
        album = ""
    updated.album = canonical_album(album)

    if album_artist is not None:
        updated.albumartist = album_artist
    elif not tags.albumartist:
        # The field YouTube Music groups by. Derived from the primary artist
        # so a featured guest cannot split an album in two.
        updated.albumartist = updated.artist
    else:
        # An album artist that is nothing but a credit -- "(feat. Birdman,
        # Jay Sean, Lil Wayne)" -- names nobody once the marker is stripped,
        # so the track's own artist is the only real answer.
        carried = split_featured(tags.albumartist)[0] or updated.artist
        # "Various Artists" is only meaningful on an actual album. Left on a
        # track with none, it files the song under a compilation that does
        # not exist.
        if not updated.album and carried.strip().lower() == VARIOUS_ARTISTS.lower():
            carried = updated.artist
        updated.albumartist = carried

    updated.genre = canonical_genre(tags.genre)
    return updated


def _norm_compare(value: str) -> str:
    """Casefold and strip punctuation, for comparing two titles for sameness."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


# ---------------------------------------------------------------------------
# Library-wide pass
# ---------------------------------------------------------------------------

#: An album needs at least this many tracks before "who is the album artist"
#: is worth asking as a group question at all.
_COMPILATION_MIN_TRACKS = 3
#: ...and one artist holding at least this share of the tracks owns the
#: album outright. A mixtape whose every track is by one rapper is not a
#: compilation just because one guest appears on it -- nor because the same
#: name is spelled two ways ("Lil Wayne" / "Lil' Wayne"), which is why the
#: tally is kept on the punctuation-stripped form.
_DOMINANT_SHARE = 0.5

VARIOUS_ARTISTS = "Various Artists"


@dataclass
class YtMusicResult:
    """What normalising one file changed."""

    path: Path
    updated: bool
    changes: list[str]
    reason: str = ""


def snapshot_tags(paths: list[Path], destination: Path) -> Path:
    """Write every file's current tags to JSON so the pass can be undone."""
    payload = {}
    for path in paths:
        tags = tags_module.try_read(path)
        payload[str(path)] = tags.to_dict()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return destination


def restore_snapshot(snapshot: Path) -> int:
    """Put back the tags recorded by :func:`snapshot_tags`. Returns files written."""
    payload = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    restored = 0
    for raw_path, values in payload.items():
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            existing = tags_module.try_read(path)
            tags = TagSet(**values)
            # Artwork is not in the snapshot (it would balloon the file), so
            # carry across whatever is embedded right now.
            tags_module.write(path, tags, artwork=existing.artwork)
            restored += 1
        except (tags_module.TagError, TypeError, OSError):
            continue
    return restored


def _resolve_album_artists(entries: list[tuple[Path, TagSet]]) -> dict[str, str]:
    """Decide one album artist per album, keyed on the normalised album name.

    An album dominated by a single artist takes that artist's name -- in the
    spelling used most often, so a stray "Lil' Wayne" does not become the
    album artist for a run of "Lil Wayne" tracks. Only an album with no
    dominant artist is called a compilation.

    Albums below :data:`_COMPILATION_MIN_TRACKS` are left out entirely: with
    one or two tracks in the library there is no group evidence, and each
    track's own artist is the better answer.
    """
    tally: dict[str, dict[str, int]] = {}
    for _, tags in entries:
        album = canonical_album(tags.album)
        if not album:
            continue
        primary = split_featured(tags.artist)[0] or tags.artist
        if not primary:
            continue
        tally.setdefault(_norm_compare(album), {})
        spellings = tally[_norm_compare(album)]
        spellings[primary] = spellings.get(primary, 0) + 1

    resolved: dict[str, str] = {}
    for album_key, spellings in tally.items():
        total = sum(spellings.values())
        if total < _COMPILATION_MIN_TRACKS:
            continue
        # Group the spellings of one artist together before judging dominance.
        by_identity: dict[str, list[tuple[str, int]]] = {}
        for name, count in spellings.items():
            by_identity.setdefault(_norm_compare(name), []).append((name, count))
        best_identity, best_spellings = max(
            by_identity.items(), key=lambda item: sum(c for _, c in item[1])
        )
        best_count = sum(count for _, count in best_spellings)
        if best_count / total >= _DOMINANT_SHARE:
            resolved[album_key] = max(best_spellings, key=lambda item: item[1])[0]
        else:
            resolved[album_key] = VARIOUS_ARTISTS
    return resolved


def normalise_library(
    paths: list[Path],
    *,
    context=None,
    dry_run: bool = False,
) -> list[YtMusicResult]:
    """Apply :func:`normalise_tags` across a set of files.

    Runs in two passes so albums can be judged as a whole: the first reads
    every file to find compilations, the second writes. One unreadable file
    never stops the batch.
    """
    entries: list[tuple[Path, TagSet]] = []
    total = len(paths)
    for index, raw_path in enumerate(paths):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(
                (index / total) * 0.3 if total else None, f"Reading {Path(raw_path).name}"
            )
        path = Path(raw_path)
        entries.append((path, tags_module.try_read(path)))

    album_artists = _resolve_album_artists(entries)

    results: list[YtMusicResult] = []
    for index, (path, existing) in enumerate(entries):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(
                0.3 + (index / total) * 0.7 if total else None, f"YouTube Music: {path.name}"
            )
        try:
            # An album's resolved artist is applied to every one of its
            # tracks, not only to compilations -- that way a wrong value
            # already sitting in the file gets corrected rather than kept.
            album_key = _norm_compare(canonical_album(existing.album))
            override = album_artists.get(album_key) if album_key else None
            updated = normalise_tags(existing, album_artist=override)

            changes = [
                f"{field}: {getattr(existing, field)!r} -> {getattr(updated, field)!r}"
                for field in ("title", "artist", "album", "albumartist", "genre")
                if getattr(existing, field) != getattr(updated, field)
            ]
            if not changes:
                results.append(YtMusicResult(path, False, [], "Already in shape"))
                continue
            if not dry_run:
                tags_module.write(path, updated, artwork=existing.artwork)
            results.append(YtMusicResult(path, True, changes))
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            results.append(YtMusicResult(path, False, [], f"Failed: {exc}"))

    if context is not None:
        context.progress(1.0, f"Normalised {sum(1 for r in results if r.updated)} of {total}")
    return results

"""Filling in missing tags automatically.

Two passes, in order, and never touching a field that already has a value:

1. **Filename parsing** -- many imported or downloaded files are named
   ``Artist - Title (Official Audio).ext``; splitting on the first separator
   and stripping common noise suffixes recovers title/artist for free, with
   no network call.
2. **Online lookup** -- reuses the same iTunes/MusicBrainz search this app
   already uses for cover art (see :mod:`musicstudio.core.artwork`), to fill
   in album, year and genre, and to correct/fill title/artist when the
   filename alone did not have them.

Every guess only fills blanks (:meth:`TagSet.merged_with` with
``overwrite=False``) -- an existing tag is never touched, no matter how
confident the guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import __version__
from ..config import Settings, get_settings
from . import tags as tags_module
from .artwork import ITUNES_API, search_musicbrainz
from .tags import TagSet

#: Trailing noise that download tools and video rippers commonly append,
#: e.g. "(Official Video)", "[Official Audio]", "(Lyrics)", "(HD)".
_NOISE_SUFFIX = re.compile(
    r"\s*[\(\[]\s*(official\s*)?(music\s*)?"
    r"(video|audio|lyrics?|visualizer|lyric\s*video|hd|4k|explicit|clean|remaster(ed)?)"
    r"[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)
_SEPARATORS = (" - ", " – ", " — ")


def looks_like_video_title(text: str) -> bool:
    """True when ``text`` carries a video-rip marker like "(Official Video)".

    Reuses the same pattern guess_from_filename() strips -- the one place
    this app already recognises the shape of a video-sourced name.
    """
    return bool(text) and bool(_NOISE_SUFFIX.search(text))


#: Same noise words as _NOISE_SUFFIX, but for when the junk made it into the
#: *tag* itself with no surrounding brackets at all (e.g. a title tag of
#: literally "Song Name Official Video"). Kept separate and narrower than a
#: bare match on every _NOISE_SUFFIX word -- "official" is required before
#: video/audio so a real title ending in a word like "HD" or "Clean" is never
#: touched, and "lyrics" is trusted bare since it is not a real word an actual
#: song title ends on in practice.
_BARE_OFFICIAL_SUFFIX = re.compile(
    r"\s+official\s+(music\s+)?(video|audio|lyric\s*video)\s*$", re.IGNORECASE
)
_BARE_LYRICS_SUFFIX = re.compile(r"\s+lyrics?(\s+video)?\s*$", re.IGNORECASE)

#: A YouTube channel handle sitting in the artist field, e.g. "HueyVEVO" --
#: the uploader's channel name, not a credited artist.
_CHANNEL_ARTIST_SUFFIX = re.compile(r"vevo$", re.IGNORECASE)


def clean_title_noise(title: str) -> str:
    """Strip a video/lyrics marker from ``title``, bracketed or bare.

    Unlike guess_from_filename() (which only ever sees this noise inside
    brackets, because that is how it typically shows up in a *filename*),
    a downloaded file's *tag* can carry the same junk with no brackets at
    all. Never returns an empty string -- if stripping would leave nothing,
    the original is kept rather than losing the title entirely.
    """
    if not title:
        return title
    cleaned = _NOISE_SUFFIX.sub(" ", title).strip()
    for pattern in (_BARE_OFFICIAL_SUFFIX, _BARE_LYRICS_SUFFIX):
        stripped = pattern.sub("", cleaned).strip()
        if stripped:
            cleaned = stripped
    return cleaned or title


def looks_like_channel_artist(artist: str, title: str = "") -> bool:
    """True when ``artist`` is a YouTube channel name rather than a real
    credit -- either a "...VEVO"-style handle, or literally a copy of the
    title (seen when a downloader had nothing better to put there)."""
    if not artist:
        return False
    if _CHANNEL_ARTIST_SUFFIX.search(artist.strip()):
        return True
    if title and artist.strip().lower() == title.strip().lower():
        return True
    return False


@dataclass
class JunkTagResult:
    """Outcome of cleaning one file's already-present (but wrong) tags."""

    path: Path
    updated: bool
    changes: list[str]
    reason: str = ""


def fix_junk_tags_library(
    paths: list[Path],
    *,
    context=None,
) -> list[JunkTagResult]:
    """Clean junk that survived into *existing* title/artist values.

    fix_library_tags() above only ever fills a *blank* field -- by design, it
    never touches a value that is already there, however wrong. That leaves
    two common kinds of YouTube-download residue completely untouched: noise
    words baked into the title tag itself ("Song Name Official Video", no
    brackets) and a channel handle sitting in the artist field ("HueyVEVO")
    instead of a real credit. Both are fixed here using only the filename as
    a second opinion -- e.g. "Huey - Pop, Lock & Drop It (Official Video).flac"
    already parses cleanly to artist "Huey" via guess_from_filename() -- so
    no network lookup is needed and the result stays deterministic.
    """
    results: list[JunkTagResult] = []
    total = len(paths)

    for index, path in enumerate(paths):
        path = Path(path)
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Cleaning tags: {path.name}")
        try:
            existing = tags_module.try_read(path)
            new_title = clean_title_noise(existing.title)
            new_artist = existing.artist
            changes = []

            if new_title != existing.title:
                changes.append(f'title: "{existing.title}" -> "{new_title}"')

            if looks_like_channel_artist(existing.artist, new_title):
                candidate = guess_from_filename(path).artist
                if candidate and not looks_like_channel_artist(candidate, new_title):
                    new_artist = candidate
                    changes.append(f'artist: "{existing.artist}" -> "{new_artist}"')

            if not changes:
                results.append(JunkTagResult(path, False, [], "Nothing to clean"))
                continue

            updated = existing.merged_with(TagSet(title=new_title, artist=new_artist), overwrite=True)
            tags_module.write(path, updated, artwork=existing.artwork)
            results.append(JunkTagResult(path, True, changes))
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            results.append(JunkTagResult(path, False, [], f"Failed: {exc}"))

    if context is not None:
        updated_count = sum(1 for r in results if r.updated)
        context.progress(1.0, f"Cleaned {updated_count} of {total}")
    return results


def guess_from_filename(path: str | Path) -> TagSet:
    """Recover title/artist from a filename like 'Artist - Title (Official Audio)'."""
    stem = Path(path).stem
    stem = _NOISE_SUFFIX.sub(" ", stem).strip()

    for sep in _SEPARATORS:
        if sep in stem:
            artist, _, title = stem.partition(sep)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return TagSet(artist=artist, title=title)

    # No recognisable separator: treat the cleaned-up whole name as the title.
    return TagSet(title=stem) if stem else TagSet()


def _itunes_tags(artist: str, title: str, album: str, settings: Settings) -> TagSet | None:
    """Search iTunes for a track and translate the result into a TagSet.

    A small, separate query from :func:`musicstudio.core.artwork.lookup_itunes`
    -- that one is shaped around fetching an image, not reading back
    trackName/collectionName/releaseDate/primaryGenreName, so it is simpler to
    ask directly than to bolt those fields onto ``ArtworkCandidate``.
    """
    term = " ".join(part for part in (artist, title, album) if part).strip()
    if not term:
        return None
    user_agent = settings.musicbrainz_user_agent or f"MusicStudio/{__version__}"
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": user_agent}) as client:
            response = client.get(
                ITUNES_API,
                params={"term": term, "entity": "song", "limit": 1, "media": "music"},
            )
            response.raise_for_status()
            results = response.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None

    result = results[0]
    release_date = result.get("releaseDate") or ""
    return TagSet(
        title=result.get("trackName", ""),
        artist=result.get("artistName", ""),
        album=result.get("collectionName", ""),
        genre=result.get("primaryGenreName", ""),
        date=release_date[:4] if len(release_date) >= 4 else "",
        track_number=result.get("trackNumber"),
        disc_number=result.get("discNumber"),
    )


def _musicbrainz_tags(artist: str, album: str, settings: Settings) -> TagSet | None:
    releases = search_musicbrainz(artist, album, settings=settings, limit=1)
    if not releases:
        return None
    release = releases[0]
    credit = release.get("artist-credit") or []
    release_artist = credit[0].get("name", "") if credit else ""
    date = release.get("date", "")
    return TagSet(
        album=release.get("title", ""),
        artist=release_artist,
        date=date[:4] if date else "",
    )


def _spotify_tags(artist: str, title: str, album: str, settings: Settings) -> TagSet | None:
    from . import spotify as spotify_module

    match = spotify_module.find_track(artist, title, album, settings=settings)
    if match is None:
        return None
    # No genre here: Spotify puts genre on the *artist*, not the track, which
    # would mean a second API call per lookup -- not worth it when iTunes
    # already covers genre whenever it has a hit at all.
    return TagSet(title=match.title, artist=match.artist, album=match.album, date=match.year)


def guess_from_online(tags: TagSet, *, settings: Settings | None = None) -> TagSet:
    """Fill in album/year/genre (and title/artist if still blank) online.

    Tries iTunes first -- it answers with genre and year in the same call.
    Spotify is next: better catalogue coverage than iTunes for a lot of
    non-mainstream music, but needs credentials and has no genre field.
    MusicBrainz is the last resort, release-only fallback.
    """
    settings = settings or get_settings()
    if not tags.artist and not tags.title and not tags.album:
        return TagSet()

    guess = _itunes_tags(tags.artist, tags.title, tags.album, settings)
    if guess is None or guess.is_empty():
        guess = _spotify_tags(tags.artist, tags.title, tags.album, settings)
    if guess is None or guess.is_empty():
        guess = _musicbrainz_tags(tags.artist, tags.album, settings)
    return guess or TagSet()


def fix_file_tags(
    path: str | Path,
    *,
    baseline: TagSet | None = None,
    settings: Settings | None = None,
    use_online: bool = True,
) -> TagSet:
    """Return the tags ``path`` should have.

    Starts from ``baseline`` (the caller's current view of the tags, e.g. an
    in-progress edit in the UI) or, if not given, whatever is already on
    disk -- then fills blanks from the filename, then, if fields of real
    interest are still missing, from an online lookup. Existing values are
    never overwritten.
    """
    settings = settings or get_settings()
    existing = baseline if baseline is not None else tags_module.try_read(path)

    merged = existing.merged_with(guess_from_filename(path), overwrite=False)
    if use_online and (not merged.album or not merged.date or not merged.genre):
        merged = merged.merged_with(guess_from_online(merged, settings=settings), overwrite=False)
    return merged


@dataclass
class TagFixResult:
    """Outcome of trying to fill in one file's missing tags."""

    path: Path
    updated: bool
    reason: str


def fix_library_tags(
    paths: list[Path],
    *,
    settings: Settings | None = None,
    context=None,
) -> list[TagFixResult]:
    """Bulk 'fix all metadata' over a set of files.

    One file failing never stops the batch. Only files where the fix
    actually changed something are written back to disk.
    """
    settings = settings or get_settings()
    results: list[TagFixResult] = []
    total = len(paths)

    for index, path in enumerate(paths):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Fixing tags: {Path(path).name}")
        try:
            existing = tags_module.try_read(path)
            fixed = fix_file_tags(path, baseline=existing, settings=settings)
            if fixed.to_dict() == existing.to_dict():
                results.append(TagFixResult(Path(path), False, "Nothing missing"))
                continue
            tags_module.write(path, fixed)
            results.append(TagFixResult(Path(path), True, "Filled in missing fields"))
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            results.append(TagFixResult(Path(path), False, f"Failed: {exc}"))

    if context is not None:
        updated = sum(1 for r in results if r.updated)
        context.progress(1.0, f"Fixed {updated} of {total}")
    return results

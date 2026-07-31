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

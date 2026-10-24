"""Synced (time-stamped) lyrics: parse LRC text, look songs up on LRCLIB.

Lyrics are kept as ordinary LRC text -- ``[01:23.45]some words`` per line --
in the file's existing lyrics field (ID3 USLT, Vorbis LYRICS, MP4 ``\\xa9lyr``,
ASF WM/Lyrics). That needs no new tag plumbing, works in every container
this app writes, and players that understand synced lyrics recognise the
timestamps; a sidecar ``.lrc`` file would be lost on any move, rename or
conversion.

LRCLIB (https://lrclib.net) is a free, community-run lyrics database that
needs no key. It asks callers to send an identifying User-Agent, which the
shared artwork HTTP client already does.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .. import config
from . import artwork
from . import tags as tags_module

LRCLIB_API = "https://lrclib.net/api"
#: Be polite to a free service.
_limiter = artwork.RateLimiter(0.25)
#: How long a "no lyrics found" answer is trusted before asking again.
MISS_TTL_SECONDS = 7 * 24 * 3600
#: LRCLIB matches by duration; its own docs allow a couple of seconds' drift.
DURATION_TOLERANCE_S = 3.0

_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE | re.MULTILINE)
#: Word-level timestamps from "enhanced" LRC: <01:23.45>
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")


class LyricsError(Exception):
    """The lookup itself failed (network down, service error) -- as opposed to
    the song simply having no lyrics, which is not an error."""


@dataclass(frozen=True)
class SyncedLyrics:
    """Time-stamped lines, sorted by time."""

    lines: tuple[tuple[float, str], ...]
    _times: tuple[float, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def from_text(cls, text: str) -> "SyncedLyrics | None":
        """Parse LRC text; None unless it has at least two timestamped lines
        (one stamp is more likely a stray bracket than a synced lyric)."""
        lines = parse_lrc(text)
        if len(lines) < 2:
            return None
        return cls(tuple(lines), tuple(t for t, _ in lines))

    def index_at(self, seconds: float) -> int:
        """Index of the line being sung at ``seconds``; -1 before the first."""
        return bisect.bisect_right(self._times, seconds) - 1

    def text_at(self, seconds: float) -> str:
        index = self.index_at(seconds)
        return self.lines[index][1] if index >= 0 else ""


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """LRC text -> sorted (seconds, line) pairs.

    Handles several timestamps on one line (a repeated chorus), minutes past
    59, ``.x`` / ``.xx`` / ``.xxx`` fractions, the ``[offset:+ms]`` tag, and
    word-level ``<mm:ss.xx>`` stamps (dropped). Metadata tags such as
    ``[ar:Artist]`` and lines without a timestamp are ignored.
    """
    offset_match = _OFFSET.search(text or "")
    # A positive offset makes lyrics appear sooner (LRC convention).
    offset = int(offset_match.group(1)) / 1000 if offset_match else 0.0

    result: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        stamps = list(_TIMESTAMP.finditer(raw))
        if not stamps:
            continue
        words = _WORD_STAMP.sub("", raw[stamps[-1].end():]).strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            moment = int(minutes) * 60 + int(seconds)
            if fraction:
                moment += int(fraction) / 10 ** len(fraction)
            result.append((max(0.0, moment - offset), words))
    result.sort(key=lambda pair: pair[0])
    return result


def is_synced(text: str) -> bool:
    return SyncedLyrics.from_text(text) is not None


# ---------------------------------------------------------------------------
# LRCLIB
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LyricsResult:
    synced: str | None = None
    plain: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.synced or self.plain)

    @property
    def best(self) -> str | None:
        return self.synced or self.plain


def _result_from(entry: dict) -> LyricsResult | None:
    synced = (entry.get("syncedLyrics") or "").strip() or None
    plain = (entry.get("plainLyrics") or "").strip() or None
    if entry.get("instrumental") or not (synced or plain):
        return None
    return LyricsResult(synced, plain)


def fetch(artist: str, title: str, album: str = "", duration: float = 0.0) -> LyricsResult | None:
    """Look a song up on LRCLIB. None means it has no lyrics there; a failed
    lookup raises :class:`LyricsError` so callers can tell the two apart.

    Tries the exact lookup first (needs the album and length), then a search
    on artist and title, taking the closest-length result that has synced
    lyrics.
    """
    if not artist.strip() or not title.strip():
        return None
    settings = config.get_settings()
    try:
        with artwork._client(settings, timeout=15.0) as client:
            if album.strip() and duration:
                _limiter.wait()
                response = client.get(
                    f"{LRCLIB_API}/get",
                    params={
                        "artist_name": artist, "track_name": title,
                        "album_name": album, "duration": int(round(duration)),
                    },
                )
                if response.status_code == 200:
                    found = _result_from(response.json())
                    if found:
                        return found
                elif response.status_code != 404:
                    raise LyricsError(f"LRCLIB answered {response.status_code}")

            _limiter.wait()
            response = client.get(
                f"{LRCLIB_API}/search", params={"artist_name": artist, "track_name": title}
            )
            if response.status_code != 200:
                raise LyricsError(f"LRCLIB answered {response.status_code}")
            entries = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LyricsError(str(exc)) from exc

    candidates = []
    for entry in entries if isinstance(entries, list) else []:
        result = _result_from(entry)
        if result is None:
            continue
        listed = float(entry.get("duration") or 0)
        gap = abs(listed - duration) if duration and listed else 0.0
        if gap > DURATION_TOLERANCE_S:
            continue  # a different recording (live, remix, radio edit)
        # Synced beats plain; then the closest length.
        candidates.append((not result.synced, gap, result))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Cache (what the player shows without touching the song's file)
# ---------------------------------------------------------------------------


def _cache_file(artist: str, title: str, duration: float) -> Path:
    raw = f"{artist.strip().casefold()}|{title.strip().casefold()}|{int(round(duration))}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return config.CACHE_DIR / "lyrics" / f"{digest}.json"


def load_cached(artist: str, title: str, duration: float) -> tuple[bool, LyricsResult | None]:
    """(hit, result). A hit with result None is a remembered "no lyrics"."""
    path = _cache_file(artist, title, duration)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if data.get("miss"):
        if time.time() - float(data.get("at", 0)) > MISS_TTL_SECONDS:
            return False, None
        return True, None
    return True, LyricsResult(data.get("synced"), data.get("plain"))


def store_cached(artist: str, title: str, duration: float, result: LyricsResult | None) -> None:
    path = _cache_file(artist, title, duration)
    payload = (
        {"miss": True, "at": time.time()}
        if result is None
        else {"synced": result.synced, "plain": result.plain}
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written is only slower


def lookup_cached(artist: str, title: str, album: str, duration: float) -> LyricsResult | None:
    """Cache first, then LRCLIB (remembering the answer). Raises LyricsError
    when the lookup fails, and caches nothing in that case."""
    hit, result = load_cached(artist, title, duration)
    if hit:
        return result
    result = fetch(artist, title, album, duration)
    store_cached(artist, title, duration, result)
    return result


# ---------------------------------------------------------------------------
# Writing into files
# ---------------------------------------------------------------------------


@dataclass
class DownloadResult:
    synced: list[Path] = field(default_factory=list)
    plain_only: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def download_library(
    paths: list[Path],
    *,
    library,
    context=None,
    replace_existing: bool = False,
) -> DownloadResult:
    """Fetch lyrics for each of ``paths`` and write them into the files.

    Synced lyrics win; plain ones are written only when the file has no
    lyrics at all. A song that already has synced lyrics is left alone
    unless ``replace_existing``. One song failing never stops the batch.
    """
    from . import probe

    result = DownloadResult()
    total = len(paths)
    for index, path in enumerate(paths):
        path = Path(path)
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Lyrics for {path.name}")
        row = library.get(path)
        if row is None:
            result.skipped.append(path)
            continue
        try:
            existing = tags_module.try_read(path).lyrics or ""
            if is_synced(existing) and not replace_existing:
                result.skipped.append(path)
                continue
            found = lookup_cached(row.artist or row.albumartist, row.title, row.album,
                                  row.duration)
            if found is None:
                result.missing.append(path)
                continue
            if found.synced:
                tags_module.write_fields(path, lyrics=found.synced)
                result.synced.append(path)
            elif not existing.strip():
                tags_module.write_fields(path, lyrics=found.plain)
                result.plain_only.append(path)
            else:
                result.missing.append(path)  # only plain found; keep what is there
                continue
            info = probe.try_probe(path)
            if info is not None:
                library.upsert(path, info, tags_module.try_read(path))
        except LyricsError as exc:
            result.failed.append((path, f"lookup failed: {exc}"))
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            result.failed.append((path, str(exc)))

    if context is not None:
        context.progress(1.0, f"Lyrics: {len(result.synced)} synced")
    return result

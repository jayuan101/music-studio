"""Identify a song by how it sounds: Chromaprint fingerprint + AcoustID lookup.

For files whose tags and filename say nothing useful ("track01.flac", a
YouTube id), the audio itself can still name the song. ``fpcalc`` (part of
Chromaprint) turns two minutes of audio into a compact fingerprint, and the
free AcoustID service matches fingerprints to MusicBrainz recordings. What
comes back is a title, artist and album, which then feed the same online
lookup "Fix metadata" already uses for the rest (year, genre).

Two things must be present for this to do anything, and both are optional:

* ``fpcalc`` -- bundled next to ffmpeg (``vendor/chromaprint/``), or on PATH.
* An AcoustID application key in Preferences. The key is free
  (https://acoustid.org/new-application) and identifies this app, not the
  user, so it is stored as an ordinary setting rather than a secret.

Without either, :func:`identify` quietly returns None and callers carry on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_settings
from . import artwork, ffmpeg
from .tags import TagSet

ACOUSTID_API = "https://api.acoustid.org/v2/lookup"
#: Matches scoring below this are guesses. AcoustID's own docs treat ~0.8+ as
#: a confident match; a wrong identification is worse than none, so be strict.
MIN_SCORE = 0.85
#: Two minutes is what AcoustID fingerprints are meant to cover.
FINGERPRINT_SECONDS = 120
#: AcoustID allows about three requests a second.
_limiter = artwork.RateLimiter(0.34)

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_cached_path: Path | None = None


class FingerprintError(Exception):
    """fpcalc or the AcoustID lookup failed."""


@dataclass(frozen=True)
class Match:
    score: float
    title: str
    artist: str
    album: str = ""


# ---------------------------------------------------------------------------
# fpcalc
# ---------------------------------------------------------------------------


def _bundle_roots() -> Iterable[Path]:
    """Where a shipped fpcalc may live -- the same places ffmpeg's are."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / "chromaprint"
        yield Path(meipass)
    yield Path(__file__).resolve().parent.parent.parent / "vendor" / "chromaprint"
    if getattr(sys, "frozen", False):
        yield Path(sys.executable).parent / "chromaprint"
        yield Path(sys.executable).parent


def find_fpcalc() -> Path | None:
    global _cached_path
    if _cached_path is not None and _cached_path.is_file():
        return _cached_path
    for root in _bundle_roots():
        candidate = root / f"fpcalc{_EXE_SUFFIX}"
        if candidate.is_file():
            _cached_path = candidate
            return candidate
    on_path = shutil.which("fpcalc")
    _cached_path = Path(on_path) if on_path else None
    return _cached_path


def is_available() -> bool:
    return find_fpcalc() is not None


def fingerprint_file(path: Path, *, timeout: float = 90.0) -> tuple[float, str]:
    """(duration in seconds, fingerprint) for the audio in ``path``."""
    exe = find_fpcalc()
    if exe is None:
        raise FingerprintError("fpcalc (Chromaprint) is not installed")
    try:
        proc = subprocess.run(
            [str(exe), "-json", "-length", str(FINGERPRINT_SECONDS), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **ffmpeg._no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FingerprintError(f"fpcalc could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise FingerprintError(detail[-1] if detail else f"fpcalc exited with {proc.returncode}")
    return parse_fpcalc_output(proc.stdout)


def parse_fpcalc_output(text: str) -> tuple[float, str]:
    try:
        data = json.loads(text)
        duration, fingerprint = float(data["duration"]), str(data["fingerprint"])
    except (ValueError, KeyError, TypeError) as exc:
        raise FingerprintError("fpcalc printed something unexpected") from exc
    if not fingerprint:
        raise FingerprintError("fpcalc produced no fingerprint")
    return duration, fingerprint


# ---------------------------------------------------------------------------
# AcoustID
# ---------------------------------------------------------------------------


def _artist_credit(artists: list) -> str:
    parts = []
    for artist in artists or []:
        parts.append(str(artist.get("name", "")))
        parts.append(str(artist.get("joinphrase", "")))
    return "".join(parts).strip()


def _album_of(recording: dict) -> str:
    groups = [g for g in recording.get("releasegroups") or [] if g.get("title")]
    # A plain studio album beats a compilation, a single or a live release.
    for group in groups:
        if group.get("type") == "Album" and not group.get("secondarytypes"):
            return str(group["title"])
    return str(groups[0]["title"]) if groups else ""


def best_match(response: dict, *, min_score: float = MIN_SCORE) -> Match | None:
    """The most confident named recording in an AcoustID response, or None."""
    results = sorted(
        (r for r in response.get("results") or [] if float(r.get("score") or 0) >= min_score),
        key=lambda r: -float(r["score"]),
    )
    for result in results:
        for recording in result.get("recordings") or []:
            title = str(recording.get("title") or "").strip()
            artist = _artist_credit(recording.get("artists"))
            if title and artist:
                return Match(float(result["score"]), title, artist, _album_of(recording))
    return None


def lookup(duration: float, fingerprint: str, api_key: str) -> Match | None:
    """Ask AcoustID who this fingerprint is. None when nothing matches
    confidently; :class:`FingerprintError` when the lookup itself fails."""
    _limiter.wait()
    try:
        with artwork._client(get_settings(), timeout=20.0) as client:
            response = client.get(
                ACOUSTID_API,
                params={
                    "client": api_key,
                    "format": "json",
                    "meta": "recordings releasegroups",
                    "duration": int(round(duration)),
                    "fingerprint": fingerprint,
                },
            )
            if response.status_code != 200:
                raise FingerprintError(f"AcoustID answered {response.status_code}")
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FingerprintError(str(exc)) from exc
    if data.get("status") != "ok":
        raise FingerprintError((data.get("error") or {}).get("message", "AcoustID refused the request"))
    return best_match(data)


def is_configured() -> bool:
    """Whether identify() can do anything: fpcalc found and a key entered."""
    return is_available() and bool(get_settings().acoustid_api_key.strip())


def identify(path: Path) -> TagSet | None:
    """Title, artist and album for ``path`` from its sound, or None.

    The single seam the rest of the app (and the tests) goes through. Every
    failure -- no fpcalc, no key, offline, no confident match -- is None, the
    same "couldn't tell" the other online lookups give.
    """
    key = get_settings().acoustid_api_key.strip()
    if not key or not is_available():
        return None
    try:
        duration, fingerprint = fingerprint_file(Path(path))
        match = lookup(duration, fingerprint, key)
    except FingerprintError:
        return None
    if match is None:
        return None
    return TagSet(title=match.title, artist=match.artist, album=match.album)

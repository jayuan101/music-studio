"""Downloading audio from YouTube and any other site yt-dlp supports.

Two modes, and the difference matters for quality:

* **keep** -- take the best audio stream the site offers and leave it exactly
  as it arrived. No re-encoding, so no second generation of loss. The result is
  usually Opus or AAC in a WebM/M4A container.
* **convert** -- decode that stream and re-encode into a format you choose.
  Useful for compatibility, but it cannot improve on what was downloaded, and
  the caller is told so.

Streaming sites serve lossy audio. Converting a download to FLAC produces a
lossless copy *of an already-lossy signal*: bigger, not better. The app says so
rather than implying an upgrade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from . import convert as convert_module
from . import ffmpeg, formats, probe
from . import tags as tags_module
from .formats import FormatProfile


class DownloadError(RuntimeError):
    """Raised when a download cannot be completed."""


#: Sites whose audio is always lossy, so "convert to FLAC" cannot gain anything.
_LOSSY_SOURCE_HINT = (
    "Streaming sites deliver compressed audio. Converting it to a lossless "
    "format makes a much larger file without recovering any detail -- the "
    "detail was discarded before you downloaded it."
)


@dataclass
class DownloadRequest:
    """One URL to fetch."""

    url: str
    output_dir: Path
    #: "keep" leaves the original stream untouched; "convert" re-encodes.
    mode: str = "keep"
    #: Target format when mode == "convert".
    profile: FormatProfile | None = None
    #: Write the video thumbnail in as cover art.
    embed_thumbnail: bool = True
    #: Fill in title/artist from what the site reports.
    apply_metadata: bool = True
    #: Cap on playlist entries; 0 means no limit.
    playlist_limit: int = 0
    #: Look up proper cover art after downloading.
    fetch_artwork: bool = False


@dataclass
class DownloadedTrack:
    """One file produced by a download."""

    path: Path
    title: str
    uploader: str = ""
    duration: float = 0.0
    url: str = ""
    #: Codec of the stream as downloaded, before any conversion.
    source_codec: str = ""
    source_bitrate: int = 0
    converted: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class DownloadResult:
    tracks: list[DownloadedTrack] = field(default_factory=list)
    #: Playlist title when the URL pointed at one.
    playlist_title: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tracks)


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

_URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


def is_supported_url(url: str) -> bool:
    """Whether this looks like a URL we can hand to yt-dlp."""
    return bool(_URL_PATTERN.match(url.strip()))


def _sanitise_filename(name: str, max_length: int = 120) -> str:
    """Strip characters Windows refuses in filenames."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (cleaned[:max_length].rstrip() or "track")


# ---------------------------------------------------------------------------
# Probing without downloading
# ---------------------------------------------------------------------------


@dataclass
class UrlInfo:
    """What a URL contains, fetched without downloading the media."""

    title: str
    uploader: str = ""
    duration: float = 0.0
    is_playlist: bool = False
    entry_count: int = 1
    thumbnail: str = ""
    #: Best audio stream on offer: (codec, bitrate kbps).
    best_audio_codec: str = ""
    best_audio_bitrate: int = 0

    @property
    def duration_label(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def inspect_url(url: str, *, playlist_limit: int = 0) -> UrlInfo:
    """Read a URL's metadata without downloading it.

    Lets the UI show what is about to be fetched -- and, importantly, what
    quality the source actually offers -- before committing to the download.
    """
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
    }
    if playlist_limit:
        options["playlistend"] = playlist_limit

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt_dlp raises many extractor-specific errors
        raise DownloadError(f"Could not read that URL: {exc}") from exc

    if info is None:
        raise DownloadError("That URL returned no media")

    entries = info.get("entries")
    if entries is not None:
        entry_list = [e for e in entries if e]
        return UrlInfo(
            title=info.get("title") or "Playlist",
            uploader=info.get("uploader") or info.get("channel") or "",
            is_playlist=True,
            entry_count=len(entry_list),
            duration=sum(float(e.get("duration") or 0) for e in entry_list),
        )

    codec, bitrate = _best_audio_format(info)
    return UrlInfo(
        title=info.get("title") or "Unknown",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=float(info.get("duration") or 0),
        thumbnail=info.get("thumbnail") or "",
        best_audio_codec=codec,
        best_audio_bitrate=bitrate,
    )


def _best_audio_format(info: dict) -> tuple[str, int]:
    """The highest-bitrate audio-only stream in an extraction result."""
    best_codec, best_bitrate = "", 0
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none"):
            continue
        codec = fmt.get("acodec") or ""
        if codec in ("none", ""):
            continue
        bitrate = int(fmt.get("abr") or 0)
        if bitrate >= best_bitrate:
            best_codec, best_bitrate = codec, bitrate
    return best_codec, best_bitrate


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


def download(
    request: DownloadRequest,
    *,
    context=None,
    settings: Settings | None = None,
) -> DownloadResult:
    """Fetch audio from ``request.url``.

    Progress is reported through ``context`` when given. Downloading and any
    subsequent conversion are reported as separate phases so the progress bar
    does not appear to stall.
    """
    import yt_dlp

    settings = settings or get_settings()
    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = DownloadResult()
    downloaded_paths: list[tuple[Path, dict]] = []

    def progress_hook(status: dict) -> None:
        if context is not None and context.is_cancelled():
            raise DownloadError("Cancelled")
        if context is None:
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            done = status.get("downloaded_bytes") or 0
            fraction = (done / total) if total else None
            speed = status.get("speed") or 0
            speed_label = f" at {speed / 1_000_000:.1f} MB/s" if speed else ""
            name = Path(status.get("filename") or "").name
            # Downloading is roughly the first 70% of the work; conversion and
            # tagging make up the rest.
            context.progress(
                fraction * 0.7 if fraction is not None else None,
                f"Downloading {name[:50]}{speed_label}",
            )
        elif status.get("status") == "finished":
            context.progress(0.7, "Download complete, processing…")

    options: dict = {
        # Best audio-only stream, falling back to extracting from a muxed one.
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "ignoreerrors": True,      # one dead playlist entry must not kill the batch
        "retries": 5,
        "fragment_retries": 5,
        "windowsfilenames": True,  # keep names legal on the target platform
        "restrictfilenames": False,
        "writethumbnail": request.embed_thumbnail,
        "postprocessors": [],
    }

    if request.playlist_limit:
        options["playlistend"] = request.playlist_limit
    if ffmpeg.is_available():
        options["ffmpeg_location"] = str(ffmpeg.ffmpeg_path().parent)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(request.url, download=True)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"Download failed: {exc}") from exc

    if info is None:
        raise DownloadError("Nothing was downloaded from that URL")

    entries = info.get("entries")
    if entries is not None:
        result.playlist_title = info.get("title") or ""
        items = [e for e in entries if e]
    else:
        items = [info]

    for entry in items:
        path = _resolve_downloaded_path(entry, output_dir)
        if path is None:
            result.warnings.append(f"Could not locate the file for {entry.get('title', '?')}")
            continue
        downloaded_paths.append((path, entry))

    if not downloaded_paths:
        raise DownloadError("The download produced no audio files")

    total_items = len(downloaded_paths)
    for index, (path, entry) in enumerate(downloaded_paths):
        if context is not None:
            context.raise_if_cancelled()
            base = 0.7 + (index / total_items) * 0.3
            context.progress(base, f"Processing {path.name[:50]}")
        track = _process_download(path, entry, request, settings, context=context)
        result.tracks.append(track)

    if context is not None:
        context.progress(1.0, f"Finished {len(result.tracks)} track(s)")
    return result


def _resolve_downloaded_path(entry: dict, output_dir: Path) -> Path | None:
    """Find the file yt-dlp actually wrote for this entry.

    yt-dlp reports the intended name, but post-processors change the extension,
    so the recorded path often no longer exists by the time we look.
    """
    candidates: list[str] = []
    for downloaded in entry.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            if downloaded.get(key):
                candidates.append(downloaded[key])
    for key in ("filepath", "_filename", "filename"):
        if entry.get(key):
            candidates.append(entry[key])

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
        # Same stem, different extension after post-processing.
        matches = sorted(
            p for p in output_dir.glob(f"{glob_escape(path.stem)}.*")
            if p.suffix.lower() not in (".part", ".ytdl", ".jpg", ".png", ".webp")
        )
        if matches:
            return matches[0]

    title = entry.get("title")
    if title:
        matches = sorted(
            p for p in output_dir.glob(f"{glob_escape(_sanitise_filename(title))}.*")
            if p.suffix.lower() not in (".part", ".ytdl", ".jpg", ".png", ".webp")
        )
        if matches:
            return matches[0]
    return None


def glob_escape(value: str) -> str:
    """Escape glob metacharacters so titles with brackets still match."""
    return re.sub(r"([\[\]*?])", r"[\1]", value)


def _process_download(
    path: Path,
    entry: dict,
    request: DownloadRequest,
    settings: Settings,
    *,
    context=None,
) -> DownloadedTrack:
    """Convert, tag and art up one downloaded file."""
    info = probe.try_probe(path)
    track = DownloadedTrack(
        path=path,
        title=entry.get("title") or path.stem,
        uploader=entry.get("uploader") or entry.get("channel") or "",
        duration=float(entry.get("duration") or (info.duration if info else 0)),
        url=entry.get("webpage_url") or entry.get("original_url") or request.url,
        source_codec=info.codec if info else "",
        source_bitrate=info.bitrate_kbps if info else 0,
    )

    # -- Convert --------------------------------------------------------
    if request.mode == "convert" and request.profile is not None and info is not None:
        if request.profile.lossless and not info.is_lossless:
            track.notes.append(_LOSSY_SOURCE_HINT)

        destination = path.with_suffix(request.profile.extension)
        convert_request = convert_module.ConvertRequest(
            source=path,
            destination=destination,
            profile=request.profile,
            overwrite=destination != path,
        )
        try:
            outcome = convert_module.convert(convert_request, context=context, info=info)
        except ffmpeg.FFmpegError as exc:
            track.notes.append(f"Conversion failed, keeping the original: {exc.tail(3)}")
        else:
            if outcome.destination != path:
                try:
                    path.unlink()
                except OSError:
                    pass
            track.path = outcome.destination
            track.converted = True
            track.notes.extend(
                str(note) for note in outcome.notes if note.severity.value == "warning"
            )

    # -- Tag ------------------------------------------------------------
    if request.apply_metadata:
        _apply_download_metadata(track, entry, request, settings)

    return track


def _apply_download_metadata(
    track: DownloadedTrack,
    entry: dict,
    request: DownloadRequest,
    settings: Settings,
) -> None:
    """Write what the site told us into the file's tags."""
    existing = tags_module.try_read(track.path)

    # Record where the file came from. Fall back through the entry and the
    # original request so the tag is never blank just because one of yt-dlp's
    # url fields was missing.
    source_url = (
        track.url
        or entry.get("webpage_url")
        or entry.get("original_url")
        or request.url
    )
    track.url = source_url

    # yt-dlp often parses "Artist - Title" out of the video title for music
    # content; prefer those fields over the raw title when present.
    artist = entry.get("artist") or entry.get("creator") or entry.get("uploader") or ""
    title = entry.get("track") or entry.get("title") or track.title
    album = entry.get("album") or ""

    if not entry.get("track") and " - " in str(title) and not artist:
        # Fall back to splitting "Artist - Title" ourselves.
        left, _, right = str(title).partition(" - ")
        artist, title = left.strip(), right.strip()

    new_tags = tags_module.TagSet(
        title=str(title),
        artist=str(artist),
        album=str(album),
        albumartist=str(entry.get("album_artist") or artist),
        date=str(entry.get("release_year") or (entry.get("upload_date") or "")[:4] or ""),
        genre=str(entry.get("genre") or ""),
        source_url=source_url,
        encoded_by="Music Studio",
    )
    merged = existing.merged_with(new_tags)

    # A YouTube title is not a song title: it carries "(Official Video)",
    # repeats the artist, and leaves album artist blank -- which is the field
    # YouTube Music groups a library by. Normalising here means downloads land
    # already in shape instead of needing a cleanup pass later.
    if settings.ytmusic_format_downloads:
        from . import ytmusic as ytmusic_module

        merged = ytmusic_module.normalise_tags(merged)

    artwork_image = existing.artwork
    if request.embed_thumbnail and artwork_image is None:
        artwork_image = _load_sidecar_thumbnail(track.path)

    if request.fetch_artwork:
        from . import artwork as artwork_module

        candidate = artwork_module.find_artwork(
            merged.effective_albumartist, merged.album, title=merged.title, settings=settings
        )
        if candidate is not None:
            artwork_image = candidate.to_artwork()

    try:
        tags_module.write(track.path, merged, artwork=artwork_image)
    except tags_module.TagError as exc:
        track.notes.append(f"Could not write tags: {exc}")

    _cleanup_sidecar_thumbnails(track.path)


def _load_sidecar_thumbnail(path: Path) -> tags_module.Artwork | None:
    """Pick up the thumbnail image yt-dlp saved next to the audio."""
    for suffix in (".jpg", ".webp", ".png", ".jpeg"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            try:
                return tags_module.Artwork.from_bytes(candidate.read_bytes())
            except OSError:
                continue
    return None


def _cleanup_sidecar_thumbnails(path: Path) -> None:
    for suffix in (".jpg", ".webp", ".png", ".jpeg"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            try:
                candidate.unlink()
            except OSError:
                pass


def quality_note_for(url_info: UrlInfo, profile: FormatProfile | None) -> str | None:
    """Warn, before downloading, when the chosen format cannot help.

    Shown next to the format picker so the tradeoff is visible at the moment
    the choice is made, not after the file is on disk.
    """
    if profile is None:
        return None
    if profile.lossless:
        # The codec is only known once the link has been checked; before that,
        # say so generically rather than printing "provides COMPRESSED".
        if url_info.best_audio_codec:
            bitrate = (
                f" at ~{url_info.best_audio_bitrate} kbps" if url_info.best_audio_bitrate else ""
            )
            source = f"only provides {url_info.best_audio_codec.upper()}{bitrate}, which is"
        else:
            source = "delivers audio that is"
        return (
            f"This source {source} already compressed. Saving it as {profile.label} makes a "
            f"much larger file without recovering any detail — the detail was discarded "
            f"before you downloaded it. Keeping the original stream gives identical sound "
            f"in a fraction of the space."
        )
    return None

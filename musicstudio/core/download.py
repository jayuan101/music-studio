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
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from . import convert as convert_module
from . import ffmpeg, formats, probe
from . import tags as tags_module
from .formats import FormatProfile


class DownloadError(RuntimeError):
    """Raised when a download cannot be completed."""


#: Every extension a downloaded thumbnail can turn up with -- yt-dlp/sites
#: serve ".jfif" as often as ".jpg" (the same JPEG format, different
#: extension), plus the occasional GIF or BMP. Anything missing here is a
#: thumbnail that gets mistaken for the audio download itself, or a sidecar
#: image left behind uncleaned next to the track forever.
_THUMBNAIL_EXTENSIONS = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp")

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
    #: An already-fetched file to adopt into ``output_dir`` instead of
    #: re-downloading -- used to "keep" a preview the user has already
    #: auditioned, without hitting the network a second time.
    source: Path | None = None
    #: The yt-dlp metadata captured when ``source`` was first fetched, so tags
    #: can be rebuilt without re-querying the site.
    source_entry: dict = field(default_factory=dict)


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
    #: The raw yt-dlp metadata entry this track was produced from, kept so a
    #: later "Keep" can adopt the file without re-fetching it.
    raw_entry: dict = field(default_factory=dict, repr=False)


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


#: Sources this app can search, mapped to yt-dlp's search-prefix name. Each
#: one resolves "prefixN:query" against a specific site's own search, no API
#: key or login needed -- the same reason the rest of this module leans on
#: yt-dlp instead of a site-specific SDK per source.
SEARCH_SOURCES: dict[str, str] = {
    "YouTube": "ytsearch",
    "SoundCloud": "scsearch",
}


@dataclass
class SearchResult:
    """One hit from :func:`search`, light enough to list dozens at once."""

    title: str
    uploader: str = ""
    duration: float = 0.0
    url: str = ""
    thumbnail: str = ""
    #: Which source this came from, e.g. "YouTube" or "SoundCloud" -- shown
    #: in the results list so a search spanning several sites stays legible.
    source: str = ""

    @property
    def duration_label(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def search(
    query: str, *, limit: int = 20, sources: list[str] | None = None
) -> list[SearchResult]:
    """Search every source in :data:`SEARCH_SOURCES` for ``query``.

    "Search all sites" only means something if one flaky source can't take
    the others down with it: each source is queried independently, and a
    source that errors (a network hiccup, an extractor that broke) is
    dropped rather than failing the whole search, as long as at least one
    source still comes back with something.
    """
    query = query.strip()
    if not query:
        return []

    if is_supported_url(query):
        # A pasted link (e.g. a YouTube "Share" URL like youtu.be/...) is not
        # a search query -- fed to a site's text search it matches nothing,
        # which reads as "no song". Resolve it into one result instead, so
        # preview and download work from the search box too.
        return [_result_from_url(query)]

    chosen = list(sources) if sources is not None else list(SEARCH_SOURCES)
    results: list[SearchResult] = []
    errors: list[str] = []
    for source in chosen:
        prefix = SEARCH_SOURCES.get(source)
        if prefix is None:
            continue
        try:
            results.extend(_search_source(source, prefix, query, limit))
        except DownloadError as exc:
            errors.append(f"{source}: {exc}")

    if not results and errors:
        raise DownloadError("; ".join(errors))
    return results


def _result_from_url(url: str) -> SearchResult:
    """Turn a pasted link into a single search result.

    The entry keeps the original URL (short links included) as its target,
    so previewing or downloading it goes through the same yt-dlp path the
    Source box uses -- which already knows how to resolve share links.
    """
    info = inspect_url(url)
    title = info.title
    if info.is_playlist:
        title = f"{info.title} (playlist, {info.entry_count} tracks)"
    return SearchResult(
        title=title,
        uploader=info.uploader,
        duration=info.duration,
        url=url,
        thumbnail=info.thumbnail,
        source="Link",
    )


def _search_source(source: str, prefix: str, query: str, limit: int) -> list[SearchResult]:
    """Run one source's ``prefixN:query`` search via yt-dlp's flat extraction.

    Flat extraction keeps this fast: it reads the results page rather than
    resolving every hit's full format list up front, which a search over 20
    results would otherwise pay for one at a time.
    """
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"{prefix}{max(1, limit)}:{query}", download=False)
    except Exception as exc:
        raise DownloadError(f"Search failed: {exc}") from exc

    entries = [e for e in (info or {}).get("entries") or [] if e]
    results = []
    for entry in entries:
        video_id = entry.get("id")
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url and video_id and prefix == "ytsearch":
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not url:
            continue
        results.append(
            SearchResult(
                title=entry.get("title") or "Unknown",
                uploader=entry.get("uploader") or entry.get("channel") or "",
                duration=float(entry.get("duration") or 0),
                url=url,
                thumbnail=_best_thumbnail(entry),
                source=source,
            )
        )
    return results


def _best_thumbnail(entry: dict) -> str:
    """The highest-resolution thumbnail URL available for a search result.

    Flat extraction (used by search() to stay fast) never populates the
    singular "thumbnail" field -- only the "thumbnails" list, each with its
    own width/height, in no guaranteed order. Picking explicitly by width
    avoids relying on list order for something a future yt-dlp version could
    change.
    """
    direct = entry.get("thumbnail")
    if direct:
        return direct
    thumbnails = entry.get("thumbnails") or []
    if not thumbnails:
        return ""
    best = max(thumbnails, key=lambda t: t.get("width") or 0)
    return best.get("url", "")


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
    settings = settings or get_settings()
    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = DownloadResult()
    downloaded_paths: list[tuple[Path, dict]] = []

    if request.source is not None:
        dest = _adopt_preview_source(request.source, output_dir)
        downloaded_paths = [(dest, request.source_entry or {})]
    else:
        downloaded_paths, result.playlist_title, result.warnings = _fetch_from_url(
            request, output_dir, context=context
        )

    if not downloaded_paths:
        raise DownloadError("The download produced no audio files")

    total_items = len(downloaded_paths)
    for index, (path, entry) in enumerate(downloaded_paths):
        if context is not None:
            context.raise_if_cancelled()
            base = 0.7 + (index / total_items) * 0.3
            context.progress(base, f"Processing {path.name[:50]}")
        track = _process_download(path, entry, request, settings, context=context)
        track.raw_entry = entry
        result.tracks.append(track)

    if context is not None:
        context.progress(1.0, f"Finished {len(result.tracks)} track(s)")
    return result


def _adopt_preview_source(source: Path, output_dir: Path) -> Path:
    """Copy an already-fetched file (and any sidecar thumbnail) into
    ``output_dir`` instead of downloading it again.

    Copies rather than moves: ``source`` may still be open for playback in
    the Now Playing bar, and Windows will refuse to rename/delete a file
    that's open without share-delete. A copy needs only read access, which
    playback already has, so "Keep" can run while the preview keeps playing
    uninterrupted.
    """
    dest = _unique_destination(output_dir / source.name)
    shutil.copy2(source, dest)
    for suffix in _THUMBNAIL_EXTENSIONS:
        sidecar = source.with_suffix(suffix)
        if sidecar.is_file():
            try:
                shutil.copy2(sidecar, dest.with_suffix(suffix))
            except OSError:
                pass
    return dest


def _unique_destination(path: Path) -> Path:
    """``path``, or a "(2)", "(3)", … suffixed sibling if it already exists."""
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_stem(f"{path.stem} ({counter})")
        if not candidate.exists():
            return candidate
        counter += 1


def _fetch_from_url(
    request: DownloadRequest, output_dir: Path, *, context=None
) -> tuple[list[tuple[Path, dict]], str, list[str]]:
    """Run yt-dlp against ``request.url``.

    Returns ``(downloaded_paths, playlist_title, warnings)``.
    """
    import yt_dlp

    downloaded_paths: list[tuple[Path, dict]] = []
    playlist_title = ""
    warnings: list[str] = []

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
        playlist_title = info.get("title") or ""
        items = [e for e in entries if e]
    else:
        items = [info]

    for entry in items:
        path = _resolve_downloaded_path(entry, output_dir)
        if path is None:
            warnings.append(f"Could not locate the file for {entry.get('title', '?')}")
            continue
        downloaded_paths.append((path, entry))

    return downloaded_paths, playlist_title, warnings


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
            if p.suffix.lower() not in (".part", ".ytdl") + _THUMBNAIL_EXTENSIONS
        )
        if matches:
            return matches[0]

    title = entry.get("title")
    if title:
        matches = sorted(
            p for p in output_dir.glob(f"{glob_escape(_sanitise_filename(title))}.*")
            if p.suffix.lower() not in (".part", ".ytdl") + _THUMBNAIL_EXTENSIONS
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
    for suffix in _THUMBNAIL_EXTENSIONS:
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            try:
                return tags_module.Artwork.from_bytes(candidate.read_bytes())
            except OSError:
                continue
    return None


def _cleanup_sidecar_thumbnails(path: Path) -> None:
    for suffix in _THUMBNAIL_EXTENSIONS:
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

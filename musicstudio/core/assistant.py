"""Personal AI assistant: natural language driving the whole app.

Qt-free, like ``convert.py``/``edit.py`` -- this module is unit-testable
without a ``QApplication``. The UI layer (``ui/assistant_panel.py``) supplies
a confirmation callback and a text-streaming callback; how those get from a
background thread to the Qt main thread is the UI's concern, not this one's.

Two backends, one tool schema: a local Ollama model handles commands with no
network or API key by default, and the Claude API is used only when the user
turns on cloud escalation in Preferences. Every mutating tool call is gated
behind a confirmation the caller controls -- the assistant never touches a
file without the app agreeing to show what it's about to do first.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import httpx

from ..config import Settings
from ..db import Library, TrackFilter, find_audio_files, scan_into_library
from . import convert as convert_module
from . import download as download_module
from . import formats
from . import organise
from . import probe
from . import tags as tags_module
from .artwork import update_library_artwork
from .edit import ChannelMode, EditSpec, EqBand, GainMode, Region, SilenceMode
from .jobs import JobContext


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ActionPreview:
    """What a tool call is about to do, shown before (and to decide whether)
    it actually happens."""

    tool_name: str
    summary: str
    paths: tuple[Path, ...] = ()
    #: Warning strings -- typically QualityNote text from resolve_output().
    notes: tuple[str, ...] = ()
    details: str = ""

    @property
    def is_multi_file(self) -> bool:
        return len(self.paths) > 1


@dataclass(frozen=True)
class ToolOutcome:
    """What execute() produced. ``content`` is fed back to the model as the
    tool result, so it should be something a model can read and act on."""

    content: str
    is_error: bool = False
    #: Files this call produced or changed on disk, for the UI to re-index --
    #: structured rather than scraped back out of ``content``.
    paths: tuple[Path, ...] = ()


#: Returns True to proceed, False to decline. Called synchronously from
#: whatever thread the turn loop runs on -- the UI implementation is expected
#: to block that thread until the user answers, not to return immediately.
ConfirmCallback = Callable[[ActionPreview], bool]


@dataclass
class AssistantContext:
    """The app state a tool needs in order to do its work."""

    library: Library
    settings: Settings


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    #: True if this tool can change something on disk or in the library.
    #: Mutating tools are always confirmed; non-mutating ones never are.
    mutates: bool
    preview: Callable[[dict, AssistantContext], ActionPreview]
    execute: Callable[[dict, AssistantContext, JobContext], ToolOutcome]


@dataclass
class Message:
    """One turn of the conversation, in a backend-agnostic shape.

    Each backend converts this to and from its own wire format --
    ``_to_ollama_message``/``_to_claude_messages`` -- rather than the loop
    knowing about either.
    """

    role: str  # "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Set on role="tool" messages, matching the ToolCall.id it answers.
    tool_call_id: str | None = None
    is_error: bool = False


# ---------------------------------------------------------------------------
# JSON Schema helpers
# ---------------------------------------------------------------------------


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _string(description: str, enum: list[str] | None = None) -> dict:
    schema = {"type": "string", "description": description}
    if enum:
        schema["enum"] = enum
    return schema


def _number(description: str) -> dict:
    return {"type": "number", "description": description}


def _integer(description: str) -> dict:
    return {"type": "integer", "description": description}


def _boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _array(items: dict, description: str) -> dict:
    return {"type": "array", "items": items, "description": description}


_FORMAT_IDS = [p.id for p in formats.ALL_PROFILES]


# ---------------------------------------------------------------------------
# Tool: search_library
# ---------------------------------------------------------------------------

_SEARCH_LIBRARY_SCHEMA = _schema(
    {
        "is_lossless": _boolean(
            "True for only lossless files (FLAC/ALAC/WAV/etc), false for only lossy."
        ),
        "codec": _string("Exact codec name, e.g. 'flac', 'mp3', 'aac'."),
        "min_bitrate": _integer("Minimum bitrate in bits/second."),
        "max_bitrate": _integer("Maximum bitrate in bits/second."),
        "min_sample_rate": _integer("Minimum sample rate in Hz, e.g. 44100."),
        "has_artwork": _boolean(
            "True for tracks with embedded cover art, false for tracks missing it."
        ),
        "artist_contains": _string("Substring to match against the artist tag."),
        "album_contains": _string("Substring to match against the album tag."),
        "genre_contains": _string("Substring to match against the genre tag."),
        "title_contains": _string("Substring to match against the title tag."),
        "path_prefix": _string("Only files whose path starts with this."),
        "order_by": _string(
            "Sort field, optionally prefixed with '-' for descending, e.g. '-bitrate'."
        ),
        "limit": _integer("Maximum number of results (default 50)."),
    }
)


def _filters_from_args(args: dict) -> TrackFilter:
    return TrackFilter(
        is_lossless=args.get("is_lossless"),
        codec=args.get("codec"),
        min_bitrate=args.get("min_bitrate"),
        max_bitrate=args.get("max_bitrate"),
        min_sample_rate=args.get("min_sample_rate"),
        has_artwork=args.get("has_artwork"),
        artist_contains=args.get("artist_contains"),
        album_contains=args.get("album_contains"),
        genre_contains=args.get("genre_contains"),
        title_contains=args.get("title_contains"),
        path_prefix=args.get("path_prefix"),
        order_by=args.get("order_by") or "albumartist",
        limit=args.get("limit") or 50,
    )


def _describe_filters(filters: TrackFilter) -> str:
    parts: list[str] = []
    if filters.is_lossless is not None:
        parts.append("lossless" if filters.is_lossless else "lossy")
    if filters.codec:
        parts.append(filters.codec)
    if filters.min_bitrate:
        parts.append(f">={filters.min_bitrate // 1000}kbps")
    if filters.max_bitrate:
        parts.append(f"<={filters.max_bitrate // 1000}kbps")
    if filters.min_sample_rate:
        parts.append(f">={filters.min_sample_rate / 1000:g}kHz")
    if filters.has_artwork is not None:
        parts.append("with artwork" if filters.has_artwork else "missing artwork")
    for label, value in (
        ("artist", filters.artist_contains),
        ("album", filters.album_contains),
        ("genre", filters.genre_contains),
        ("title", filters.title_contains),
    ):
        if value:
            parts.append(f"{label}~'{value}'")
    if filters.path_prefix:
        parts.append(f"under {filters.path_prefix}")
    return ", ".join(parts) or "all tracks"


def _preview_search_library(args: dict, ctx: AssistantContext) -> ActionPreview:
    filters = _filters_from_args(args)
    return ActionPreview(
        tool_name="search_library", summary=f"Search the library: {_describe_filters(filters)}"
    )


def _execute_search_library(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    try:
        filters = _filters_from_args(args)
        rows = ctx.library.query_tracks(filters)
    except ValueError as exc:
        return ToolOutcome(content=str(exc), is_error=True)

    if not rows:
        return ToolOutcome(content="No tracks matched.")

    shown = rows[:50]
    lines = [f"{r.display_artist} - {r.display_title} [{r.quality_label}] {r.path}" for r in shown]
    if len(rows) > len(shown):
        lines.append(f"...and {len(rows) - len(shown)} more (not shown).")
    return ToolOutcome(content="\n".join(lines))


# ---------------------------------------------------------------------------
# Tool: get_track_info
# ---------------------------------------------------------------------------

_GET_TRACK_INFO_SCHEMA = _schema(
    {"path": _string("Absolute path to the audio file.")}, required=["path"]
)


def _preview_get_track_info(args: dict, ctx: AssistantContext) -> ActionPreview:
    return ActionPreview(
        tool_name="get_track_info", summary=f"Read info for {args.get('path', '?')}"
    )


def _execute_get_track_info(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    path = Path(args["path"])
    info = probe.try_probe(path)
    if info is None:
        return ToolOutcome(
            content=f"Could not read {path} -- it may not be a valid audio file.", is_error=True
        )
    tag_set = tags_module.try_read(path)
    lines = [
        info.describe(),
        f"Duration: {info.duration:.1f}s",
        f"Title: {tag_set.title or '(none)'}",
        f"Artist: {tag_set.artist or '(none)'}",
        f"Album: {tag_set.album or '(none)'}",
        f"Genre: {tag_set.genre or '(none)'}",
        f"Track/Disc: {tag_set.track_number or '?'}/{tag_set.disc_number or '?'}",
        f"Has artwork: {'yes' if tag_set.has_artwork() else 'no'}",
    ]
    return ToolOutcome(content="\n".join(lines))


# ---------------------------------------------------------------------------
# Tool: convert_track
# ---------------------------------------------------------------------------

_CONVERT_TRACK_SCHEMA = _schema(
    {
        "paths": _array({"type": "string"}, "Absolute paths to the audio files to convert."),
        "format": _string("Target format id.", enum=_FORMAT_IDS),
        "sample_rate": _integer("Override the output sample rate in Hz. Omit to keep the source's."),
        "bit_depth": _integer(
            "Override the output bit depth (lossless formats only). Omit to keep the source's."
        ),
        "bitrate": _integer("Constant bitrate in kbps for lossy formats. Omit for variable bitrate."),
        "output_dir": _string("Folder to write converted files into. Omit for the default output folder."),
        "overwrite": _boolean("Overwrite an existing file of the same name instead of adding a number."),
    },
    required=["paths", "format"],
)


def _convert_options(args: dict) -> dict:
    return {
        "sample_rate": args.get("sample_rate"),
        "bit_depth": args.get("bit_depth"),
        "bitrate": args.get("bitrate"),
        "vbr_quality": None,
    }


def _preview_convert_track(args: dict, ctx: AssistantContext) -> ActionPreview:
    paths = [Path(p) for p in args.get("paths", [])]
    profile = formats.get_profile(args["format"])
    options = _convert_options(args)
    lines: list[str] = []
    notes: list[str] = []
    for path in paths:
        info = probe.try_probe(path)
        if info is None:
            lines.append(f"{path}: could not be read, will be skipped")
            continue
        output = convert_module.resolve_output(
            info,
            profile,
            preserve_rate=ctx.settings.preserve_source_rate,
            preserve_depth=ctx.settings.preserve_source_depth,
            **options,
        )
        lines.append(f"{path.name}: {convert_module.describe_conversion(info, output)}")
        notes.extend(str(note) for note in output.notes if note.severity.value == "warning")
    return ActionPreview(
        tool_name="convert_track",
        summary=f"Convert {len(paths)} file(s) to {profile.label}",
        paths=tuple(paths),
        notes=tuple(notes),
        details="\n".join(lines),
    )


def _execute_convert_track(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    paths = [Path(p) for p in args.get("paths", [])]
    profile = formats.get_profile(args["format"])
    options = _convert_options(args)
    output_dir = Path(args["output_dir"]) if args.get("output_dir") else None
    overwrite = args.get("overwrite", ctx.settings.overwrite_existing)

    produced: list[Path] = []
    errors: list[str] = []
    for path in paths:
        job.raise_if_cancelled()
        try:
            info = probe.probe(path)
            destination = convert_module.suggest_destination(path, profile, output_dir)
            request = convert_module.ConvertRequest(
                source=path, destination=destination, profile=profile, overwrite=overwrite, **options
            )
            result = convert_module.convert(request, context=job, info=info)
            try:
                tags_module.copy_tags(path, result.destination)
            except tags_module.TagError:
                pass
            produced.append(result.destination)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            errors.append(f"{path}: {exc}")

    lines = [f"Converted -> {p}" for p in produced] + errors
    return ToolOutcome(
        content="\n".join(lines) or "Nothing was converted.",
        is_error=bool(errors and not produced),
        paths=tuple(produced),
    )


# ---------------------------------------------------------------------------
# Tool: edit_track
# ---------------------------------------------------------------------------

_EDIT_TRACK_SCHEMA = _schema(
    {
        "path": _string("Absolute path to the audio file to edit."),
        "trim_start": _number("Keep audio only from this many seconds in."),
        "trim_end": _number("Keep audio only up to this many seconds in. Omit to keep to the end."),
        "cuts": _array(
            {
                "type": "object",
                "properties": {"start": _number("seconds"), "end": _number("seconds")},
            },
            "Spans to remove, closing the gap. Each item has start/end in seconds.",
        ),
        "gain_db": _number("Volume change in decibels. Positive boosts, negative reduces. Can exceed 0 dB."),
        "gain_mode": _string(
            "How to handle gain that would exceed full scale.", enum=["limit", "compress", "raw"]
        ),
        "normalize": _boolean("Apply EBU R128 loudness normalisation."),
        "normalize_target_lufs": _number("Target loudness in LUFS when normalize is true (default -14)."),
        "dynamic_normalize": _boolean("Apply dynamic (frame-based) loudness normalisation."),
        "fade_in": _number("Fade-in duration in seconds."),
        "fade_out": _number("Fade-out duration in seconds."),
        "tempo": _number("Playback speed multiplier, pitch preserved. 1.0 is unchanged."),
        "pitch_semitones": _number("Pitch shift in semitones, tempo preserved."),
        "eq_bands": _array(
            {
                "type": "object",
                "properties": {
                    "frequency": _number("Hz"),
                    "gain_db": _number("dB"),
                    "q": _number("Q factor"),
                },
            },
            "Graphic EQ bands to apply.",
        ),
        "trim_silence": _string("Strip silence.", enum=["none", "leading", "trailing", "both"]),
        "channel_mode": _string("Channel routing.", enum=["keep", "mono", "stereo", "swap"]),
        "sample_rate": _integer("Resample to this rate in Hz."),
        "export_format": _string(
            "Output format id. Omit to keep the source's own format.", enum=_FORMAT_IDS
        ),
        "output_dir": _string("Folder to write the exported file into. Omit to write beside the source."),
        "overwrite": _boolean("Overwrite an existing file of the same name instead of adding a number."),
    },
    required=["path"],
)


def _editspec_from_args(args: dict) -> EditSpec:
    trim = None
    if args.get("trim_start") is not None or args.get("trim_end") is not None:
        trim = Region(args.get("trim_start") or 0.0, args.get("trim_end"))
    cuts = [Region(c["start"], c.get("end")) for c in args.get("cuts") or []]
    eq_bands = [
        EqBand(b["frequency"], b["gain_db"], b.get("q", 1.0)) for b in args.get("eq_bands") or []
    ]
    return EditSpec(
        trim=trim,
        cuts=cuts,
        gain_db=args.get("gain_db", 0.0),
        gain_mode=GainMode(args.get("gain_mode", "limit")),
        normalize=args.get("normalize", False),
        normalize_target_lufs=args.get("normalize_target_lufs", -14.0),
        dynamic_normalize=args.get("dynamic_normalize", False),
        fade_in=args.get("fade_in", 0.0),
        fade_out=args.get("fade_out", 0.0),
        tempo=args.get("tempo", 1.0),
        pitch_semitones=args.get("pitch_semitones", 0.0),
        eq_bands=eq_bands,
        trim_silence=SilenceMode(args.get("trim_silence", "none")),
        channel_mode=ChannelMode(args.get("channel_mode", "keep")),
        sample_rate=args.get("sample_rate"),
    )


def describe_edit_spec(spec: EditSpec, duration: float) -> str:
    """One-line summary of what an EditSpec will do -- used in previews and
    the chat transcript, mirroring what the editor panel shows a human."""
    if spec.is_empty:
        return "No edits -- would just change format/settings."
    parts: list[str] = []
    if spec.trim is not None:
        parts.append("trimmed")
    if spec.cuts:
        parts.append(f"{len(spec.cuts)} cut(s)")
    if abs(spec.gain_db) > 0.05:
        parts.append(f"{spec.gain_db:+.1f} dB ({spec.gain_mode.value})")
    if spec.normalize:
        parts.append(f"normalised to {spec.normalize_target_lufs:g} LUFS")
    if spec.dynamic_normalize:
        parts.append("dynamic normalise")
    if abs(spec.tempo - 1.0) > 0.001:
        parts.append(f"{spec.tempo:g}x speed")
    if abs(spec.pitch_semitones) > 0.001:
        parts.append(f"{spec.pitch_semitones:+g} semitones")
    if spec.eq_bands:
        parts.append(f"EQ ({len(spec.eq_bands)} bands)")
    if spec.trim_silence is not SilenceMode.NONE:
        parts.append("silence trimmed")
    if spec.channel_mode is not ChannelMode.KEEP:
        parts.append(spec.channel_mode.value)
    if spec.sample_rate:
        parts.append(f"{spec.sample_rate / 1000:g} kHz")
    return f"{', '.join(parts)} -> {spec.estimated_duration(duration):.1f}s"


def _resolve_edit_target_profile(path: Path, args: dict) -> formats.FormatProfile:
    if args.get("export_format"):
        return formats.get_profile(args["export_format"])
    return formats.profile_for_extension(path.suffix) or formats.FLAC


def _preview_edit_track(args: dict, ctx: AssistantContext) -> ActionPreview:
    path = Path(args["path"])
    info = probe.try_probe(path)
    if info is None:
        return ActionPreview(tool_name="edit_track", summary=f"{path}: could not be read", paths=(path,))

    spec = _editspec_from_args(args)
    profile = _resolve_edit_target_profile(path, args)
    output = convert_module.resolve_output(
        info,
        profile,
        preserve_rate=ctx.settings.preserve_source_rate,
        preserve_depth=ctx.settings.preserve_source_depth,
    )
    notes = tuple(str(n) for n in output.notes if n.severity.value == "warning")
    return ActionPreview(
        tool_name="edit_track",
        summary=f"Edit {path.name}: {describe_edit_spec(spec, info.duration)}",
        paths=(path,),
        notes=notes,
        details=convert_module.describe_conversion(info, output),
    )


def _execute_edit_track(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    path = Path(args["path"])
    try:
        info = probe.probe(path)
    except (FileNotFoundError, ValueError) as exc:
        return ToolOutcome(content=str(exc), is_error=True)

    spec = _editspec_from_args(args)
    profile = _resolve_edit_target_profile(path, args)
    output_dir = Path(args["output_dir"]) if args.get("output_dir") else None
    overwrite = args.get("overwrite", ctx.settings.overwrite_existing)
    destination = convert_module.suggest_destination(path, profile, output_dir)

    request = convert_module.ConvertRequest(
        source=path, destination=destination, profile=profile, edits=spec, overwrite=overwrite
    )
    try:
        result = convert_module.convert(request, context=job, info=info)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the model as a failed tool call
        return ToolOutcome(content=f"Edit failed: {exc}", is_error=True)

    try:
        tags_module.copy_tags(path, result.destination)
    except tags_module.TagError:
        pass

    return ToolOutcome(content=f"Exported -> {result.destination}", paths=(result.destination,))


# ---------------------------------------------------------------------------
# Tool: update_tags
# ---------------------------------------------------------------------------

_TAG_FIELD_NAMES = (
    "title", "artist", "album", "albumartist", "date", "genre", "composer",
    "comment", "track_number", "disc_number", "bpm",
)

_UPDATE_TAGS_SCHEMA = _schema(
    {
        "paths": _array({"type": "string"}, "Absolute paths to the audio files to tag."),
        "title": _string("New title. Only sensible for a single path."),
        "artist": _string("New artist."),
        "album": _string("New album."),
        "albumartist": _string("New album artist."),
        "date": _string("New year or date."),
        "genre": _string("New genre."),
        "composer": _string("New composer."),
        "comment": _string("New comment."),
        "track_number": _integer("New track number."),
        "disc_number": _integer("New disc number."),
        "bpm": _integer("New BPM."),
        "filename_pattern": _string(
            "Instead of (or alongside) the fields above, infer tags from each file's own "
            "name using this pattern, e.g. '{artist} - {title}' or "
            "'{albumartist}/{album}/{track} - {title}'."
        ),
        "artwork_path": _string("Path to a local image file to embed as cover art."),
        "remove_artwork": _boolean("Remove any embedded cover art instead of setting one."),
        "overwrite": _boolean("Replace values the file already has. Default false: only fills in blanks."),
    },
    required=["paths"],
)


def _incoming_tags_from_args(args: dict, path: Path) -> tags_module.TagSet:
    incoming = tags_module.TagSet()
    if args.get("filename_pattern"):
        incoming = organise.parse_filename_tags(path, args["filename_pattern"])
    explicit = tags_module.TagSet(**{k: args[k] for k in _TAG_FIELD_NAMES if k in args})
    # Explicit fields always win over a filename guess.
    return incoming.merged_with(explicit, overwrite=True)


def _preview_update_tags(args: dict, ctx: AssistantContext) -> ActionPreview:
    paths = [Path(p) for p in args.get("paths", [])]
    overwrite = args.get("overwrite", False)
    lines: list[str] = []
    for path in paths:
        existing = tags_module.try_read(path)
        incoming = _incoming_tags_from_args(args, path)
        merged = existing.merged_with(incoming, overwrite=overwrite)
        changed = [
            f"{f}: {getattr(existing, f)!r} -> {getattr(merged, f)!r}"
            for f in _TAG_FIELD_NAMES
            if getattr(existing, f) != getattr(merged, f)
        ]
        if args.get("artwork_path") or args.get("remove_artwork"):
            changed.append("artwork: " + ("removed" if args.get("remove_artwork") else "updated"))
        lines.append(f"{path.name}: " + (", ".join(changed) if changed else "no changes"))
    return ActionPreview(
        tool_name="update_tags",
        summary=f"Update tags on {len(paths)} file(s)",
        paths=tuple(paths),
        details="\n".join(lines),
    )


def _execute_update_tags(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    paths = [Path(p) for p in args.get("paths", [])]
    overwrite = args.get("overwrite", False)
    remove_artwork = args.get("remove_artwork", False)
    artwork_path = args.get("artwork_path")

    artwork_obj: tags_module.Artwork | None = None
    if remove_artwork:
        artwork_obj = tags_module.Artwork(b"")
    elif artwork_path:
        try:
            artwork_obj = tags_module.Artwork.from_bytes(Path(artwork_path).read_bytes())
        except OSError as exc:
            return ToolOutcome(content=f"Could not read artwork file: {exc}", is_error=True)

    updated: list[Path] = []
    errors: list[str] = []
    for path in paths:
        job.raise_if_cancelled()
        try:
            existing = tags_module.read(path)
            incoming = _incoming_tags_from_args(args, path)
            merged = existing.merged_with(incoming, overwrite=overwrite)
            tags_module.write(path, merged, artwork=artwork_obj)
            updated.append(path)
        except (tags_module.TagError, FileNotFoundError) as exc:
            errors.append(f"{path}: {exc}")

    lines = [f"Tagged -> {p}" for p in updated] + errors
    return ToolOutcome(
        content="\n".join(lines) or "No files were tagged.",
        is_error=bool(errors and not updated),
        paths=tuple(updated),
    )


# ---------------------------------------------------------------------------
# Tool: update_artwork
# ---------------------------------------------------------------------------

_UPDATE_ARTWORK_SCHEMA = _schema(
    {
        "paths": _array({"type": "string"}, "Absolute paths to look up and embed cover art for."),
        "force": _boolean("Re-fetch and replace even if the file already has adequate artwork."),
    },
    required=["paths"],
)


def _preview_update_artwork(args: dict, ctx: AssistantContext) -> ActionPreview:
    paths = [Path(p) for p in args.get("paths", [])]
    return ActionPreview(
        tool_name="update_artwork",
        summary=f"Look up and embed cover art for {len(paths)} file(s)",
        paths=tuple(paths),
    )


def _execute_update_artwork(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    paths = [Path(p) for p in args.get("paths", [])]
    results = update_library_artwork(paths, settings=ctx.settings, force=args.get("force", False), context=job)
    lines = [f"{r.path.name}: {r.reason}" for r in results]
    return ToolOutcome(
        content="\n".join(lines) or "Nothing to update.",
        paths=tuple(r.path for r in results if r.updated),
    )


# ---------------------------------------------------------------------------
# Tool: download_url
# ---------------------------------------------------------------------------

_DOWNLOAD_URL_SCHEMA = _schema(
    {
        "url": _string("The URL to download from (YouTube or any yt-dlp-supported site)."),
        "mode": _string(
            "'keep' saves the original stream untouched (best quality, no re-encode); "
            "'convert' re-encodes into the given format.",
            enum=["keep", "convert"],
        ),
        "format": _string("Target format id when mode is 'convert'.", enum=_FORMAT_IDS),
        "output_dir": _string("Folder to save into. Omit for the default output folder."),
        "playlist_limit": _integer("Maximum number of tracks from a playlist. Omit for no limit."),
        "fetch_artwork": _boolean(
            "Look up proper cover art after downloading, instead of just the video thumbnail."
        ),
    },
    required=["url"],
)


def _preview_download_url(args: dict, ctx: AssistantContext) -> ActionPreview:
    url = args.get("url", "")
    try:
        info = download_module.inspect_url(url)
    except download_module.DownloadError as exc:
        return ActionPreview(tool_name="download_url", summary=f"Could not read that link: {exc}")

    notes: list[str] = []
    if args.get("mode") == "convert" and args.get("format"):
        profile = formats.get_profile(args["format"])
        note = download_module.quality_note_for(info, profile)
        if note:
            notes.append(note)

    if info.is_playlist:
        summary = f'Download playlist "{info.title}" ({info.entry_count} tracks)'
    else:
        summary = f'Download "{info.title}"' + (f" by {info.uploader}" if info.uploader else "")
    return ActionPreview(tool_name="download_url", summary=summary, notes=tuple(notes))


def _execute_download_url(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    output_dir = Path(args["output_dir"]) if args.get("output_dir") else Path(ctx.settings.output_dir)
    mode = args.get("mode", ctx.settings.download_mode)
    profile = formats.get_profile(args["format"]) if args.get("format") else None
    request = download_module.DownloadRequest(
        url=args["url"],
        output_dir=output_dir,
        mode=mode,
        profile=profile,
        embed_thumbnail=ctx.settings.download_embed_thumbnail,
        playlist_limit=args.get("playlist_limit", ctx.settings.download_playlist_limit),
        fetch_artwork=args.get("fetch_artwork", False),
    )
    try:
        result = download_module.download(request, context=job, settings=ctx.settings)
    except download_module.DownloadError as exc:
        return ToolOutcome(content=str(exc), is_error=True)

    lines = [f"Downloaded -> {t.path}" for t in result.tracks]
    for track in result.tracks:
        lines.extend(f"  note: {n}" for n in track.notes)
    lines.extend(f"warning: {w}" for w in result.warnings)
    return ToolOutcome(
        content="\n".join(lines) or "Nothing was downloaded.",
        paths=tuple(t.path for t in result.tracks),
    )


# ---------------------------------------------------------------------------
# Tool: import_folder
# ---------------------------------------------------------------------------

_IMPORT_FOLDER_SCHEMA = _schema(
    {"paths": _array({"type": "string"}, "Folders or files to scan and add to the library.")},
    required=["paths"],
)


def _preview_import_folder(args: dict, ctx: AssistantContext) -> ActionPreview:
    paths = [Path(p) for p in args.get("paths", [])]
    found = find_audio_files(paths)
    return ActionPreview(
        tool_name="import_folder", summary=f"Import {len(found)} audio file(s) from {len(paths)} location(s)"
    )


def _execute_import_folder(args: dict, ctx: AssistantContext, job: JobContext) -> ToolOutcome:
    paths = [Path(p) for p in args.get("paths", [])]
    imported, skipped = scan_into_library(ctx.library, paths, context=job)
    return ToolOutcome(content=f"Indexed {imported} file(s), skipped {skipped}.")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def build_tools() -> dict[str, Tool]:
    """The complete set of tools the assistant can call."""
    return {
        "search_library": Tool(
            name="search_library",
            description=(
                "Search the music library with structured filters (lossless/lossy, "
                "bitrate, sample rate, missing artwork, text matches, path prefix)."
            ),
            parameters=_SEARCH_LIBRARY_SCHEMA,
            mutates=False,
            preview=_preview_search_library,
            execute=_execute_search_library,
        ),
        "get_track_info": Tool(
            name="get_track_info",
            description="Get technical info and tags for one audio file.",
            parameters=_GET_TRACK_INFO_SCHEMA,
            mutates=False,
            preview=_preview_get_track_info,
            execute=_execute_get_track_info,
        ),
        "convert_track": Tool(
            name="convert_track",
            description="Convert one or more audio files to a different format at the best quality that format allows.",
            parameters=_CONVERT_TRACK_SCHEMA,
            mutates=True,
            preview=_preview_convert_track,
            execute=_execute_convert_track,
        ),
        "edit_track": Tool(
            name="edit_track",
            description=(
                "Apply audio edits to one file -- trim, cut, gain/volume, normalisation, "
                "fades, tempo, pitch, EQ, silence trimming, channel routing, resampling -- "
                "and export the result, optionally in a different format."
            ),
            parameters=_EDIT_TRACK_SCHEMA,
            mutates=True,
            preview=_preview_edit_track,
            execute=_execute_edit_track,
        ),
        "update_tags": Tool(
            name="update_tags",
            description=(
                "Edit metadata (title, artist, album, etc.) and/or cover art on one or more "
                "files. Can also infer tags from each file's own filename using a pattern."
            ),
            parameters=_UPDATE_TAGS_SCHEMA,
            mutates=True,
            preview=_preview_update_tags,
            execute=_execute_update_tags,
        ),
        "update_artwork": Tool(
            name="update_artwork",
            description="Look up and embed cover art from MusicBrainz/iTunes for one or more files.",
            parameters=_UPDATE_ARTWORK_SCHEMA,
            mutates=True,
            preview=_preview_update_artwork,
            execute=_execute_update_artwork,
        ),
        "download_url": Tool(
            name="download_url",
            description=(
                "Download audio from a URL (YouTube or any yt-dlp-supported site), either "
                "keeping the original stream or converting it."
            ),
            parameters=_DOWNLOAD_URL_SCHEMA,
            mutates=True,
            preview=_preview_download_url,
            execute=_execute_download_url,
        ),
        "import_folder": Tool(
            name="import_folder",
            description="Scan folders or files and add them to the library index.",
            parameters=_IMPORT_FOLDER_SCHEMA,
            mutates=False,
            preview=_preview_import_folder,
            execute=_execute_import_folder,
        ),
    }


# ---------------------------------------------------------------------------
# Backend wire-format adapters
# ---------------------------------------------------------------------------


def to_claude_tool(tool: Tool) -> dict:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def to_ollama_tool(tool: Tool) -> dict:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


def _to_ollama_message(message: Message) -> dict:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    payload: dict = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": c.id, "function": {"name": c.name, "arguments": c.arguments}} for c in message.tool_calls
        ]
    return payload


def _to_claude_messages(messages: list[Message]) -> list[dict]:
    result: list[dict] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": message.content,
                            "is_error": message.is_error,
                        }
                    ],
                }
            )
        elif message.role == "assistant" and message.tool_calls:
            content: list[dict] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            result.append({"role": "assistant", "content": content})
        else:
            result.append({"role": message.role, "content": message.content})
    return result


def _parse_tool_arguments(raw) -> dict:
    """Ollama returns tool-call arguments as either a dict or a JSON string,
    depending on the model. Normalise both, repairing mildly malformed JSON
    (trailing commas, single quotes) before giving up and returning nothing --
    an empty dict fails a tool's own validation cleanly rather than crashing
    the turn loop.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = str(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r",\s*([}\]])", r"\1", text)  # trailing commas
    repaired = repaired.replace("'", '"')
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return {}


class AssistantBackend(Protocol):
    """What the turn loop needs from a model backend, regardless of which one."""

    def run_turn(
        self, messages: list[Message], tools: list[Tool], *, on_text: Callable[[str], None] | None = None
    ) -> Message:
        """Send the conversation, return the model's reply (text and/or tool calls)."""
        ...


# ---------------------------------------------------------------------------
# Local backend: Ollama
# ---------------------------------------------------------------------------


class OllamaBackend:
    """Talks to a local Ollama instance over HTTP.

    Ollama is never pip-installed or bundled -- it is a separate service the
    user installs and runs themselves (``ollama pull llama3.1``), reached at
    ``host`` (default ``http://localhost:11434``).
    """

    def __init__(self, host: str, model: str, *, timeout: float = 120.0) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self._timeout = timeout

    def is_reachable(self) -> bool:
        try:
            with httpx.Client(timeout=5.0) as client:
                return client.get(f"{self.host}/api/version").status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.host}/api/tags")
                response.raise_for_status()
                return [m["name"] for m in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    def probe_tool_calling(self, model: str | None = None) -> bool:
        """One-shot check that ``model`` can produce a well-formed tool call.

        Ollama's set of tool-calling-capable models changes over time and
        varies in JSON quality even within a family, so this is checked at
        runtime rather than against a static allowlist.
        """
        model = model or self.model
        dummy_tool = {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Respond to a connectivity check by calling this tool.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.host}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Call the ping tool now."}],
                        "tools": [dummy_tool],
                        "stream": False,
                    },
                )
                response.raise_for_status()
                calls = (response.json().get("message") or {}).get("tool_calls") or []
                return bool(calls)
        except (httpx.HTTPError, ValueError):
            return False

    def context_length(self, model: str | None = None) -> int | None:
        """The model's context window, so long conversations can be trimmed
        deliberately rather than silently truncated by Ollama itself."""
        model = model or self.model
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(f"{self.host}/api/show", json={"name": model})
                response.raise_for_status()
                info = response.json().get("model_info", {})
                for key, value in info.items():
                    if key.endswith("context_length"):
                        return int(value)
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return None

    def run_turn(
        self, messages: list[Message], tools: list[Tool], *, on_text: Callable[[str], None] | None = None
    ) -> Message:
        payload_messages = [_to_ollama_message(m) for m in messages]
        payload_tools = [to_ollama_tool(t) for t in tools]

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        with httpx.Client(timeout=self._timeout) as client:
            with client.stream(
                "POST",
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "messages": payload_messages,
                    "tools": payload_tools,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message") or {}
                    delta = message.get("content") or ""
                    if delta:
                        text_parts.append(delta)
                        if on_text:
                            on_text(delta)
                    for call in message.get("tool_calls") or []:
                        function = call.get("function", {})
                        tool_calls.append(
                            ToolCall(
                                id=call.get("id") or uuid.uuid4().hex,
                                name=function.get("name", ""),
                                arguments=_parse_tool_arguments(function.get("arguments")),
                            )
                        )
                    if chunk.get("done"):
                        break

        return Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# Cloud backend: Claude
# ---------------------------------------------------------------------------


class ClaudeBackend:
    """Talks to the Anthropic API. ``anthropic`` is imported lazily inside
    ``run_turn`` so the package need not even be installed when cloud
    escalation is turned off."""

    def __init__(self, api_key: str, model: str, *, max_tokens: int = 4096) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens

    def run_turn(
        self, messages: list[Message], tools: list[Tool], *, on_text: Callable[[str], None] | None = None
    ) -> Message:
        import anthropic  # noqa: PLC0415 -- deliberately lazy, see class docstring

        client = anthropic.Anthropic(api_key=self._api_key)
        payload_messages = _to_claude_messages(messages)
        payload_tools = [to_claude_tool(t) for t in tools]

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        with client.messages.stream(
            model=self.model, max_tokens=self._max_tokens, messages=payload_messages, tools=payload_tools
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    text_parts.append(event.delta.text)
                    if on_text:
                        on_text(event.delta.text)
            final = stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        return Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# The turn loop
# ---------------------------------------------------------------------------


class Assistant:
    """Drives one conversation: sends messages to a backend, executes the
    tool calls it asks for, gates mutating ones behind confirmation, and
    loops until the model replies with plain text instead of a tool call.
    """

    def __init__(
        self,
        backend: AssistantBackend,
        context: AssistantContext,
        *,
        confirm: ConfirmCallback | None = None,
        max_tool_rounds: int = 8,
    ) -> None:
        self.backend = backend
        self.context = context
        self.tools = build_tools()
        #: Defaults to always-approve, which is what a synchronous test
        #: harness wants; the UI always supplies a real gate.
        self._confirm = confirm or (lambda preview: True)
        self._max_tool_rounds = max_tool_rounds
        self.history: list[Message] = []
        #: Files changed by the tool calls in the most recent send(), for the
        #: UI to re-index -- reset at the start of every call.
        self.last_changed_paths: list[Path] = []

    def send(
        self,
        user_text: str,
        job: JobContext,
        *,
        on_text: Callable[[str], None] | None = None,
        on_narration: Callable[[str], None] | None = None,
    ) -> str:
        """Send one user message, run the tool-calling loop to completion,
        and return the assistant's final text reply."""
        self.history.append(Message(role="user", content=user_text))
        self.last_changed_paths = []
        reply = Message(role="assistant")

        for _ in range(self._max_tool_rounds):
            job.raise_if_cancelled()
            reply = self.backend.run_turn(self.history, list(self.tools.values()), on_text=on_text)
            self.history.append(reply)

            if not reply.tool_calls:
                return reply.content

            for call in reply.tool_calls:
                job.raise_if_cancelled()
                outcome = self._run_tool_call(call, job, on_narration)
                self.history.append(
                    Message(role="tool", content=outcome.content, tool_call_id=call.id, is_error=outcome.is_error)
                )

        return reply.content or (
            "I've made a number of tool calls without finishing, so I'm stopping here rather "
            "than looping indefinitely. Ask me to continue if you'd like."
        )

    def _run_tool_call(
        self, call: ToolCall, job: JobContext, on_narration: Callable[[str], None] | None
    ) -> ToolOutcome:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolOutcome(content=f"Unknown tool '{call.name}'.", is_error=True)

        try:
            preview = tool.preview(call.arguments, self.context)
        except Exception as exc:  # noqa: BLE001 -- a bad preview must not crash the turn
            return ToolOutcome(content=f"Could not prepare '{call.name}': {exc}", is_error=True)

        if on_narration:
            on_narration(preview.summary)

        if tool.mutates or preview.is_multi_file:
            if not self._confirm(preview):
                return ToolOutcome(content="The user declined this action.")

        try:
            outcome = tool.execute(call.arguments, self.context, job)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the model, not raised to the caller
            return ToolOutcome(content=f"'{call.name}' failed: {exc}", is_error=True)

        if outcome.paths:
            self.last_changed_paths.extend(outcome.paths)
        return outcome

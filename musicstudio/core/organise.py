"""Building filenames and folder layouts from tags.

Makes ``Settings.filename_template`` real. Deliberately conservative: this
produces *paths*, it never moves anything on its own. Reorganising somebody's
music library is not something to do as a side effect.
"""

from __future__ import annotations

import re
from pathlib import Path

from .tags import TagSet

#: Fields a template may reference, mapped to how they render.
#: ``track`` and ``disc`` are zero-padded because "2" sorts after "10".
TEMPLATE_FIELDS = {
    "title": lambda t: t.title,
    "artist": lambda t: t.artist,
    "album": lambda t: t.album,
    "albumartist": lambda t: t.effective_albumartist,
    "year": lambda t: (t.date or "")[:4],
    "date": lambda t: t.date,
    "genre": lambda t: t.genre,
    "composer": lambda t: t.composer,
    "track": lambda t: f"{t.track_number:02d}" if t.track_number else "",
    "disc": lambda t: str(t.disc_number) if t.disc_number else "",
}

#: Shown in the Preferences panel so the placeholders are discoverable.
TEMPLATE_HELP = "Available: " + ", ".join(f"{{{name}}}" for name in TEMPLATE_FIELDS)

DEFAULT_TEMPLATE = "{albumartist}/{album}/{track} - {title}"

#: Characters Windows refuses in a filename, plus control characters.
_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
#: Names Windows reserves regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class TemplateError(ValueError):
    """Raised when a template cannot produce a usable path."""


def sanitise_component(name: str, max_length: int = 120) -> str:
    """Make one path component safe on Windows.

    Slashes are *not* accepted here -- a value containing one would silently
    create a directory level the user did not ask for, so they are replaced.
    """
    cleaned = _ILLEGAL.sub("_", name).replace("/", "_").replace("\\", "_")
    # Windows silently strips trailing dots and spaces, which turns "Album ."
    # into a path that never matches what we wrote.
    cleaned = cleaned.strip().rstrip(". ")
    if cleaned.upper().split(".")[0] in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip() or "Unknown"


def render_template(template: str, tags: TagSet) -> str:
    """Substitute tag values into ``template``.

    Unknown placeholders are left alone rather than raising, and empty values
    collapse cleanly: a missing track number should not leave " - Title"
    hanging off the front of every filename.
    """
    if not template.strip():
        raise TemplateError("The filename template is empty")

    def replace(match: re.Match) -> str:
        name = match.group(1).lower()
        renderer = TEMPLATE_FIELDS.get(name)
        if renderer is None:
            return match.group(0)
        try:
            return str(renderer(tags) or "")
        except (TypeError, ValueError):
            return ""

    # A trailing ":..." format spec is accepted and ignored. Templates written
    # in Python's format syntax ("{track:02d}") are a natural thing to type,
    # and rendering them literally into filenames is worse than useless --
    # numeric fields are already padded sensibly by TEMPLATE_FIELDS.
    rendered = re.sub(r"\{(\w+)(?::[^}]*)?\}", replace, template)

    # Tidy up what empty fields left behind: doubled separators, and separators
    # stranded at the start or end of a path component.
    rendered = re.sub(r"\s*-\s*-\s*", " - ", rendered)
    parts = []
    for part in rendered.split("/"):
        part = re.sub(r"\s{2,}", " ", part).strip()
        part = part.strip("-").strip()
        parts.append(part)
    rendered = "/".join(p for p in parts if p)

    if not rendered.strip():
        raise TemplateError("The template produced an empty name for this file")
    return rendered


def render_path(
    template: str,
    tags: TagSet,
    extension: str,
    *,
    root: Path,
) -> Path:
    """Full destination path for one file, under ``root``.

    A template containing ``/`` produces subfolders. Every component is
    sanitised separately, and the result is confined to ``root`` -- a tag
    containing "../.." must not be able to write outside the output folder.
    """
    rendered = render_template(template, tags)
    components = [sanitise_component(part) for part in rendered.split("/") if part.strip()]
    if not components:
        raise TemplateError("The template produced no usable path")

    if not extension.startswith("."):
        extension = f".{extension}"

    # Append rather than Path.with_suffix(): with_suffix replaces everything
    # after the last dot, so "Mr. Blue Sky" would be written as "Mr.flac" and
    # "Track 1.5 Interlude" as "Track 1.flac". Titles with dots are common.
    components[-1] = f"{components[-1]}{extension}"
    destination = root.joinpath(*components)

    # Confinement check. sanitise_component already strips separators, so this
    # is belt and braces -- but tags come from files we did not write.
    root_resolved = root.resolve()
    try:
        destination.resolve().relative_to(root_resolved)
    except ValueError:
        raise TemplateError(
            "The tags on this file would place it outside the output folder"
        ) from None

    return destination


def preview_template(template: str, root: Path | None = None) -> str:
    """Render a template against a sample track, for the Preferences panel."""
    sample = TagSet(
        title="Midnight Drive",
        artist="The Rearview",
        albumartist="The Rearview",
        album="Neon Cartography",
        date="2026",
        genre="Synthwave",
        track_number=3,
        disc_number=1,
    )
    try:
        rendered = render_template(template, sample)
    except TemplateError as exc:
        return f"Invalid template: {exc}"
    components = [sanitise_component(p) for p in rendered.split("/") if p.strip()]
    if not components:
        return "(empty)"
    components[-1] = f"{components[-1]}.flac"
    return str(Path(*components))

"""Filename templating and path safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicstudio.core import organise
from musicstudio.core.organise import TemplateError, render_path, render_template, sanitise_component
from musicstudio.core.tags import TagSet


def full_tags() -> TagSet:
    return TagSet(
        title="Midnight Drive", artist="The Rearview", albumartist="The Rearview",
        album="Neon Cartography", date="2026-03-14", genre="Synthwave",
        track_number=3, disc_number=1,
    )


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_all_placeholders_substitute():
    rendered = render_template(
        "{albumartist}/{album}/{track} - {title} [{year}] {genre}", full_tags()
    )
    assert rendered == "The Rearview/Neon Cartography/03 - Midnight Drive [2026] Synthwave"


def test_track_number_is_zero_padded():
    """'2' would sort after '10' without padding."""
    assert render_template("{track}", TagSet(track_number=2, title="x")) == "02"


def test_year_is_taken_from_a_full_date():
    assert render_template("{year}", TagSet(date="2026-03-14", title="x")) == "2026"


def test_unknown_placeholder_is_left_alone():
    assert "{nonsense}" in render_template("{title} {nonsense}", full_tags())


def test_python_style_format_specs_are_accepted():
    """'{track:02d}' is a natural thing to type; rendering it literally into a
    filename would be worse than useless."""
    assert render_template("{track:02d} - {title}", full_tags()) == "03 - Midnight Drive"
    assert render_template("{title:>20}", full_tags()) == "Midnight Drive"


def test_the_shipped_default_template_substitutes_everything():
    """Regression: the default used {track:02d}, which the renderer ignored."""
    from musicstudio.config import Settings

    rendered = render_template(Settings().filename_template, full_tags())
    assert "{" not in rendered and "}" not in rendered


def test_missing_fields_collapse_instead_of_leaving_debris():
    """A track with no number must not produce ' - Title'."""
    rendered = render_template("{track} - {title}", TagSet(title="Only A Title"))
    assert rendered == "Only A Title"


def test_missing_middle_field_does_not_double_separators():
    rendered = render_template("{artist} - {album} - {title}", TagSet(artist="A", title="T"))
    assert " -  - " not in rendered
    assert rendered == "A - T"


def test_empty_path_components_are_dropped():
    rendered = render_template("{albumartist}/{album}/{title}", TagSet(title="Solo"))
    assert rendered == "Solo"


def test_empty_template_is_rejected():
    with pytest.raises(TemplateError):
        render_template("   ", full_tags())


def test_template_producing_nothing_is_rejected():
    with pytest.raises(TemplateError):
        render_template("{album}", TagSet(title="no album"))


# ---------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("char", list('<>:"|?*'))
def test_windows_illegal_characters_are_replaced(char):
    assert char not in sanitise_component(f"Song{char}Name")


def test_separators_never_survive_into_a_component():
    """A tag containing a slash must not invent a folder level."""
    cleaned = sanitise_component("AC/DC")
    assert "/" not in cleaned and "\\" not in cleaned


def test_trailing_dots_and_spaces_are_stripped():
    """Windows strips these itself, so the path we wrote would not match."""
    assert sanitise_component("Album Name. ") == "Album Name"


def test_reserved_device_names_are_escaped():
    assert sanitise_component("CON") != "CON"
    assert sanitise_component("NUL.mp3").startswith("_")


def test_blank_component_gets_a_fallback():
    assert sanitise_component("   ") == "Unknown"


def test_components_are_length_capped():
    assert len(sanitise_component("x" * 400)) <= 120


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_slashes_in_the_template_create_folders(tmp_path):
    path = render_path("{albumartist}/{album}/{track} - {title}", full_tags(), ".flac", root=tmp_path)
    assert path == tmp_path / "The Rearview" / "Neon Cartography" / "03 - Midnight Drive.flac"


def test_extension_is_normalised(tmp_path):
    with_dot = render_path("{title}", full_tags(), ".mp3", root=tmp_path)
    without = render_path("{title}", full_tags(), "mp3", root=tmp_path)
    assert with_dot == without
    assert with_dot.suffix == ".mp3"


def test_traversal_in_tags_cannot_escape_the_output_folder(tmp_path):
    """Tags come from files we did not write; treat them as hostile."""
    evil = TagSet(title="../../etc/passwd", album="..", albumartist="..")
    path = render_path("{albumartist}/{album}/{title}", evil, ".flac", root=tmp_path)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in path.parts


def test_absolute_path_in_a_tag_is_neutralised(tmp_path):
    evil = TagSet(title="/etc/shadow")
    path = render_path("{title}", evil, ".flac", root=tmp_path)
    assert path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "title",
    ["Track 1.5 Interlude", "Mr. Blue Sky", "Song feat. Someone", "No dots here"],
)
def test_title_with_a_dot_is_not_truncated(tmp_path, title):
    """Path.with_suffix() replaces everything after the last dot, which turns
    'Mr. Blue Sky' into 'Mr.flac'. Titles with dots are common."""
    path = render_path("{title}", TagSet(title=title), ".flac", root=tmp_path)
    assert path.suffix == ".flac"
    assert path.name == f"{title}.flac"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_renders_a_sample():
    preview = organise.preview_template("{albumartist}/{album}/{track} - {title}")
    assert "Neon Cartography" in preview
    assert preview.endswith(".flac")


def test_preview_reports_a_bad_template_instead_of_raising():
    assert "Invalid template" in organise.preview_template("   ")


def test_default_template_is_valid():
    assert "Neon Cartography" in organise.preview_template(organise.DEFAULT_TEMPLATE)

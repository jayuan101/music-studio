"""Personal AI assistant: tool registry, schema conversion, and turn loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicstudio.config import Settings
from musicstudio.core import assistant as A
from musicstudio.core.jobs import JobContext
from musicstudio.db import Library, scan_into_library

from .conftest import make_tone, requires_ffmpeg

pytestmark = requires_ffmpeg


def make_job() -> JobContext:
    return JobContext()


@pytest.fixture
def library(tmp_path) -> Library:
    return Library(tmp_path / "test.db")


@pytest.fixture
def ctx(library, tmp_path) -> A.AssistantContext:
    settings = Settings()
    settings.output_dir = str(tmp_path / "out")
    return A.AssistantContext(library=library, settings=settings)


# ---------------------------------------------------------------------------
# Tool registry / schema conversion
# ---------------------------------------------------------------------------


def test_build_tools_registers_all_eight():
    tools = A.build_tools()
    assert set(tools) == {
        "search_library", "get_track_info", "convert_track", "edit_track",
        "update_tags", "update_artwork", "download_url", "import_folder",
    }


def test_mutating_flags_match_the_plan():
    tools = A.build_tools()
    mutating = {name for name, t in tools.items() if t.mutates}
    assert mutating == {"convert_track", "edit_track", "update_tags", "update_artwork", "download_url"}


def test_to_claude_tool_shape():
    tool = A.build_tools()["search_library"]
    claude = A.to_claude_tool(tool)
    assert claude["name"] == "search_library"
    assert claude["input_schema"] is tool.parameters


def test_to_ollama_tool_shape():
    tool = A.build_tools()["search_library"]
    ollama = A.to_ollama_tool(tool)
    assert ollama["type"] == "function"
    assert ollama["function"]["name"] == "search_library"
    assert ollama["function"]["parameters"] is tool.parameters


# ---------------------------------------------------------------------------
# _parse_tool_arguments
# ---------------------------------------------------------------------------


def test_parse_tool_arguments_accepts_a_dict():
    assert A._parse_tool_arguments({"a": 1}) == {"a": 1}


def test_parse_tool_arguments_accepts_a_json_string():
    assert A._parse_tool_arguments('{"a": 1}') == {"a": 1}


def test_parse_tool_arguments_repairs_trailing_comma_and_single_quotes():
    assert A._parse_tool_arguments("{'a': 1,}") == {"a": 1}


def test_parse_tool_arguments_gives_up_gracefully_on_garbage():
    assert A._parse_tool_arguments("not json at all") == {}


def test_parse_tool_arguments_empty_input():
    assert A._parse_tool_arguments(None) == {}
    assert A._parse_tool_arguments("") == {}


# ---------------------------------------------------------------------------
# search_library / get_track_info -- real files, real library
# ---------------------------------------------------------------------------


def test_search_library_preview_and_execute(ctx, tmp_path):
    flac = make_tone(tmp_path / "song.flac", codec="flac")
    mp3 = make_tone(tmp_path / "song2.mp3", codec="libmp3lame", extra=["-b:a", "128k"])
    scan_into_library(ctx.library, [tmp_path])

    tool = A.build_tools()["search_library"]
    preview = tool.preview({"is_lossless": True}, ctx)
    assert "lossless" in preview.summary

    outcome = tool.execute({"is_lossless": True}, ctx, make_job())
    assert not outcome.is_error
    assert "song.flac" in outcome.content
    assert "song2.mp3" not in outcome.content


def test_search_library_rejects_bad_order_by(ctx):
    tool = A.build_tools()["search_library"]
    outcome = tool.execute({"order_by": "path; DROP TABLE tracks"}, ctx, make_job())
    assert outcome.is_error


def test_get_track_info_reports_tags(ctx, tmp_path):
    path = make_tone(tmp_path / "song.flac", codec="flac")
    from musicstudio.core import tags as tags_module
    tags = tags_module.TagSet(title="Midnight Drive", artist="The Rearview")
    tags_module.write(path, tags)

    tool = A.build_tools()["get_track_info"]
    outcome = tool.execute({"path": str(path)}, ctx, make_job())
    assert not outcome.is_error
    assert "Midnight Drive" in outcome.content
    assert "The Rearview" in outcome.content


def test_get_track_info_missing_file_is_a_clean_error(ctx, tmp_path):
    tool = A.build_tools()["get_track_info"]
    outcome = tool.execute({"path": str(tmp_path / "nope.flac")}, ctx, make_job())
    assert outcome.is_error


# ---------------------------------------------------------------------------
# convert_track / edit_track -- mutating tools, real ffmpeg
# ---------------------------------------------------------------------------


def test_convert_track_preview_then_execute(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    tool = A.build_tools()["convert_track"]
    args = {"paths": [str(source)], "format": "mp3", "output_dir": str(tmp_path / "out")}

    preview = tool.preview(args, ctx)
    assert preview.tool_name == "convert_track"
    assert preview.paths == (source,)

    outcome = tool.execute(args, ctx, make_job())
    assert not outcome.is_error
    produced = Path(outcome.content.split("-> ", 1)[1])
    assert produced.exists()
    assert produced.suffix == ".mp3"


def test_edit_track_gain_and_describe_edit_spec(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    tool = A.build_tools()["edit_track"]
    args = {"path": str(source), "gain_db": 6.0, "output_dir": str(tmp_path / "out")}

    preview = tool.preview(args, ctx)
    assert "6.0" in preview.summary or "+6.0" in preview.summary

    outcome = tool.execute(args, ctx, make_job())
    assert not outcome.is_error
    produced = Path(outcome.content.split("-> ", 1)[1])
    assert produced.exists()


def test_describe_edit_spec_reports_no_edits_for_an_empty_spec():
    spec = A.EditSpec()
    assert A.describe_edit_spec(spec, 10.0).startswith("No edits")


# ---------------------------------------------------------------------------
# update_tags -- filename-pattern inference plumbed through the real tool
# ---------------------------------------------------------------------------


def test_update_tags_from_filename_pattern(ctx, tmp_path):
    source = make_tone(tmp_path / "The Rearview - Midnight Drive.flac", codec="flac")
    tool = A.build_tools()["update_tags"]
    args = {"paths": [str(source)], "filename_pattern": "{artist} - {title}"}

    preview = tool.preview(args, ctx)
    assert "artist" in preview.details or "title" in preview.details or preview.details

    outcome = tool.execute(args, ctx, make_job())
    assert not outcome.is_error

    from musicstudio.core import tags as tags_module
    written = tags_module.read(source)
    assert written.artist == "The Rearview"
    assert written.title == "Midnight Drive"


def test_update_tags_explicit_field_wins_over_filename_guess(ctx, tmp_path):
    source = make_tone(tmp_path / "The Rearview - Midnight Drive.flac", codec="flac")
    tool = A.build_tools()["update_tags"]
    args = {
        "paths": [str(source)],
        "filename_pattern": "{artist} - {title}",
        "title": "Explicit Title",
    }
    tool.execute(args, ctx, make_job())

    from musicstudio.core import tags as tags_module
    written = tags_module.read(source)
    assert written.title == "Explicit Title"
    assert written.artist == "The Rearview"


# ---------------------------------------------------------------------------
# import_folder
# ---------------------------------------------------------------------------


def test_import_folder_indexes_files(ctx, tmp_path):
    make_tone(tmp_path / "a.flac", codec="flac")
    make_tone(tmp_path / "b.flac", codec="flac")
    tool = A.build_tools()["import_folder"]

    preview = tool.preview({"paths": [str(tmp_path)]}, ctx)
    assert "2" in preview.summary

    outcome = tool.execute({"paths": [str(tmp_path)]}, ctx, make_job())
    assert "Indexed 2" in outcome.content


# ---------------------------------------------------------------------------
# The turn loop, with a fake backend (no network)
# ---------------------------------------------------------------------------


class FakeBackend:
    """Scripted backend: replays a fixed sequence of replies, one per call
    to run_turn, so the turn loop can be tested without a real model."""

    def __init__(self, replies: list[A.Message]) -> None:
        self._replies = list(replies)
        self.calls: list[list[A.Message]] = []

    def run_turn(self, messages, tools, *, on_text=None):
        self.calls.append(list(messages))
        if on_text:
            on_text("")
        return self._replies.pop(0)


def test_send_returns_plain_text_reply(ctx):
    backend = FakeBackend([A.Message(role="assistant", content="Hello!")])
    asst = A.Assistant(backend, ctx)
    result = asst.send("hi", make_job())
    assert result == "Hello!"
    assert len(backend.calls) == 1


def test_send_runs_a_non_mutating_tool_call_without_confirmation(ctx, tmp_path):
    make_tone(tmp_path / "song.flac", codec="flac")
    scan_into_library(ctx.library, [tmp_path])

    call = A.ToolCall(id="1", name="search_library", arguments={"is_lossless": True})
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Found it."),
    ])
    confirmations: list[A.ActionPreview] = []
    asst = A.Assistant(backend, ctx, confirm=lambda p: confirmations.append(p) or True)

    result = asst.send("find lossless tracks", make_job())
    assert result == "Found it."
    assert confirmations == []  # non-mutating: never asked

    tool_message = asst.history[-2]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "1"
    assert "song.flac" in tool_message.content


def test_send_gates_a_mutating_tool_call_behind_confirmation(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    call = A.ToolCall(
        id="1", name="convert_track",
        arguments={"paths": [str(source)], "format": "mp3", "output_dir": str(tmp_path / "out")},
    )
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Converted."),
    ])
    seen: list[A.ActionPreview] = []

    def confirm(preview):
        seen.append(preview)
        return True

    asst = A.Assistant(backend, ctx, confirm=confirm)
    result = asst.send("convert it to mp3", make_job())

    assert result == "Converted."
    assert len(seen) == 1
    assert seen[0].tool_name == "convert_track"
    tool_message = asst.history[-2]
    assert "Converted ->" in tool_message.content
    assert Path(tool_message.content.split("-> ", 1)[1]).exists()


def test_send_declined_confirmation_does_not_execute(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    out_dir = tmp_path / "out"
    call = A.ToolCall(
        id="1", name="convert_track",
        arguments={"paths": [str(source)], "format": "mp3", "output_dir": str(out_dir)},
    )
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Okay, cancelled."),
    ])
    asst = A.Assistant(backend, ctx, confirm=lambda preview: False)
    result = asst.send("convert it to mp3", make_job())

    assert result == "Okay, cancelled."
    tool_message = asst.history[-2]
    assert "declined" in tool_message.content
    assert not tool_message.is_error
    assert not out_dir.exists() or not list(out_dir.glob("*.mp3"))


def test_send_reports_unknown_tool_without_crashing(ctx):
    call = A.ToolCall(id="1", name="not_a_real_tool", arguments={})
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Sorry about that."),
    ])
    asst = A.Assistant(backend, ctx)
    result = asst.send("do something impossible", make_job())
    assert result == "Sorry about that."
    tool_message = asst.history[-2]
    assert tool_message.is_error
    assert "Unknown tool" in tool_message.content


def test_send_stops_after_max_tool_rounds_instead_of_looping_forever(ctx):
    def infinite_call():
        return A.Message(
            role="assistant",
            tool_calls=[A.ToolCall(id="x", name="search_library", arguments={})],
        )

    backend = FakeBackend([infinite_call() for _ in range(3)])
    asst = A.Assistant(backend, ctx, max_tool_rounds=3)
    result = asst.send("loop forever", make_job())
    assert "stopping here" in result
    assert len(backend.calls) == 3


def test_convert_track_outcome_reports_produced_paths(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    tool = A.build_tools()["convert_track"]
    args = {"paths": [str(source)], "format": "mp3", "output_dir": str(tmp_path / "out")}
    outcome = tool.execute(args, ctx, make_job())
    assert len(outcome.paths) == 1
    assert outcome.paths[0].suffix == ".mp3"
    assert outcome.paths[0].exists()


def test_update_tags_outcome_reports_the_tagged_path(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    tool = A.build_tools()["update_tags"]
    outcome = tool.execute({"paths": [str(source)], "title": "New Title"}, ctx, make_job())
    assert outcome.paths == (source,)


def test_send_accumulates_changed_paths_across_a_turn(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    call = A.ToolCall(
        id="1", name="convert_track",
        arguments={"paths": [str(source)], "format": "mp3", "output_dir": str(tmp_path / "out")},
    )
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Done."),
    ])
    asst = A.Assistant(backend, ctx)
    asst.send("convert it", make_job())

    assert len(asst.last_changed_paths) == 1
    assert asst.last_changed_paths[0].suffix == ".mp3"


def test_send_resets_changed_paths_on_a_fresh_call(ctx):
    backend = FakeBackend([
        A.Message(role="assistant", content="Hello!"),
    ])
    asst = A.Assistant(backend, ctx)
    asst.last_changed_paths = [Path("/stale/leftover.flac")]
    asst.send("hi", make_job())
    assert asst.last_changed_paths == []


def test_confirm_defaults_to_always_approve_when_omitted(ctx, tmp_path):
    source = make_tone(tmp_path / "song.flac", codec="flac")
    call = A.ToolCall(
        id="1", name="convert_track",
        arguments={"paths": [str(source)], "format": "mp3", "output_dir": str(tmp_path / "out")},
    )
    backend = FakeBackend([
        A.Message(role="assistant", tool_calls=[call]),
        A.Message(role="assistant", content="Done."),
    ])
    asst = A.Assistant(backend, ctx)  # no confirm= passed
    result = asst.send("convert it", make_job())
    assert result == "Done."


# ---------------------------------------------------------------------------
# Message <-> wire format conversion
# ---------------------------------------------------------------------------


def test_to_ollama_message_round_trips_tool_call():
    call = A.ToolCall(id="1", name="search_library", arguments={"is_lossless": True})
    message = A.Message(role="assistant", content="", tool_calls=[call])
    payload = A._to_ollama_message(message)
    assert payload["tool_calls"][0]["function"]["name"] == "search_library"
    assert payload["tool_calls"][0]["function"]["arguments"] == {"is_lossless": True}


def test_to_ollama_message_tool_role():
    message = A.Message(role="tool", content="result text", tool_call_id="1")
    payload = A._to_ollama_message(message)
    assert payload == {"role": "tool", "tool_call_id": "1", "content": "result text"}


def test_to_claude_messages_tool_result_shape():
    message = A.Message(role="tool", content="result text", tool_call_id="1", is_error=True)
    payload = A._to_claude_messages([message])
    assert payload[0]["role"] == "user"
    block = payload[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "1"
    assert block["is_error"] is True


def test_to_claude_messages_assistant_tool_use_shape():
    call = A.ToolCall(id="1", name="search_library", arguments={"is_lossless": True})
    message = A.Message(role="assistant", content="Let me check.", tool_calls=[call])
    payload = A._to_claude_messages([message])
    blocks = payload[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1] == {"type": "tool_use", "id": "1", "name": "search_library", "input": {"is_lossless": True}}

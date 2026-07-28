"""Locating and running the bundled ffmpeg / ffprobe binaries.

Every encode, decode and analysis in the app funnels through :func:`run` or
:func:`run_with_progress` so there is exactly one place that knows how to find
the binaries, hide the console window on Windows, and translate ffmpeg's
progress stream into a 0..1 fraction.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


class FFmpegNotFound(RuntimeError):
    """Raised when neither the bundled nor a system ffmpeg can be located."""


class FFmpegError(RuntimeError):
    """A non-zero exit from ffmpeg, carrying its stderr for display."""

    def __init__(self, message: str, *, command: Sequence[str], stderr: str, returncode: int):
        super().__init__(message)
        self.command = list(command)
        self.stderr = stderr
        self.returncode = returncode

    def tail(self, lines: int = 12) -> str:
        """The last few stderr lines -- what actually explains the failure."""
        return "\n".join(self.stderr.strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_cache: dict[str, Path] = {}


def _bundle_roots() -> Iterable[Path]:
    """Directories that may hold the shipped binaries, most specific first."""
    # PyInstaller unpacks datas/binaries under sys._MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass)
        yield root / "ffmpeg"
        yield root
    # Running from source: music-studio/vendor/ffmpeg/
    pkg_root = Path(__file__).resolve().parent.parent.parent
    yield pkg_root / "vendor" / "ffmpeg"
    # Alongside a frozen executable.
    if getattr(sys, "frozen", False):
        yield Path(sys.executable).parent / "ffmpeg"
        yield Path(sys.executable).parent


def find_binary(name: str) -> Path:
    """Locate ``ffmpeg`` or ``ffprobe``.

    Bundled copies win over anything on PATH, so a user's stray old ffmpeg
    cannot change encoding behaviour behind our back.
    """
    if name in _cache:
        return _cache[name]

    filename = f"{name}{_EXE_SUFFIX}"
    for root in _bundle_roots():
        candidate = root / filename
        if candidate.is_file():
            _cache[name] = candidate
            return candidate

    on_path = shutil.which(name)
    if on_path:
        _cache[name] = Path(on_path)
        return _cache[name]

    raise FFmpegNotFound(
        f"Could not find {filename}. It should ship inside the application "
        f"folder (vendor/ffmpeg/), or be installed and on your PATH."
    )


def ffmpeg_path() -> Path:
    return find_binary("ffmpeg")


def ffprobe_path() -> Path:
    return find_binary("ffprobe")


def is_available() -> bool:
    try:
        ffmpeg_path()
        ffprobe_path()
    except FFmpegNotFound:
        return False
    return True


def clear_cache() -> None:
    """Forget resolved binary paths. Used by tests."""
    _cache.clear()
    _capabilities.clear()


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _no_window_kwargs() -> dict:
    """Keep a console window from flashing up on Windows for every subprocess."""
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
    return {}


def run(command: Sequence[str], *, timeout: float | None = None) -> str:
    """Run a command to completion and return stdout, raising on failure."""
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_no_window_kwargs(),
    )
    if proc.returncode != 0:
        raise FFmpegError(
            f"{Path(command[0]).name} exited with code {proc.returncode}",
            command=command,
            stderr=proc.stderr or "",
            returncode=proc.returncode,
        )
    return proc.stdout


@dataclass
class Progress:
    """A single progress report from a running ffmpeg process."""

    #: Seconds of audio processed so far.
    seconds_done: float
    #: Total duration in seconds, if known up front.
    total_seconds: float | None
    #: Encoding speed relative to realtime, e.g. 42.0 means 42x.
    speed: float | None = None

    @property
    def fraction(self) -> float | None:
        """Completion in 0..1, or None when the total duration is unknown."""
        if not self.total_seconds or self.total_seconds <= 0:
            return None
        return min(1.0, max(0.0, self.seconds_done / self.total_seconds))


ProgressCallback = Callable[[Progress], None]
CancelCheck = Callable[[], bool]


def _parse_progress_line(line: str, state: dict[str, float]) -> bool:
    """Fold one ``key=value`` progress line into ``state``.

    Returns True when the line closes a progress block, meaning the caller
    should emit an update.
    """
    key, _, value = line.strip().partition("=")
    value = value.strip()
    if key == "out_time_us" or key == "out_time_ms":
        # Both keys are microseconds in practice -- ffmpeg's out_time_ms is a
        # long-standing misnomer and reports microseconds too.
        try:
            state["seconds"] = int(value) / 1_000_000
        except ValueError:
            pass
    elif key == "speed":
        try:
            state["speed"] = float(value.rstrip("x"))
        except ValueError:
            pass
    elif key == "progress":
        return True
    return False


def run_with_progress(
    command: Sequence[str],
    *,
    total_seconds: float | None = None,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> str:
    """Run ffmpeg, streaming progress updates and honouring cancellation.

    ``command`` should already contain ``-progress pipe:1 -nostats``; use
    :func:`progress_args` to add them.

    Returns the collected stderr, which holds ffmpeg's analysis output (the
    loudnorm JSON summary, for instance).
    """
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_no_window_kwargs(),
    )

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        # stderr must be consumed concurrently or a chatty ffmpeg fills the
        # pipe buffer and deadlocks against our stdout read loop.
        assert proc.stderr is not None
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    state: dict[str, float] = {"seconds": 0.0, "speed": 0.0}
    cancelled = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_cancel is not None and should_cancel():
                cancelled = True
                proc.terminate()
                break
            if _parse_progress_line(line, state) and on_progress is not None:
                on_progress(
                    Progress(
                        seconds_done=state["seconds"],
                        total_seconds=total_seconds,
                        speed=state["speed"] or None,
                    )
                )
    finally:
        if cancelled:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        returncode = proc.wait()
        stderr_thread.join(timeout=5)

    stderr = "".join(stderr_chunks)
    if cancelled:
        raise CancelledError("Operation cancelled")
    if returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with code {returncode}",
            command=command,
            stderr=stderr,
            returncode=returncode,
        )
    return stderr


class CancelledError(RuntimeError):
    """Raised when a job is cancelled by the user."""


def progress_args() -> list[str]:
    """Flags that make ffmpeg emit a machine-readable progress stream."""
    return ["-progress", "pipe:1", "-nostats"]


def base_args(*, overwrite: bool = True) -> list[str]:
    """Standard leading flags for every ffmpeg invocation."""
    return [
        str(ffmpeg_path()),
        "-hide_banner",
        "-loglevel", "info",
        "-y" if overwrite else "-n",
    ]


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------

_capabilities: dict[str, set[str]] = {}


def _list_capability(kind: str) -> set[str]:
    """Parse ``ffmpeg -encoders`` / ``-filters`` into a set of names."""
    if kind in _capabilities:
        return _capabilities[kind]
    names: set[str] = set()
    try:
        output = run([str(ffmpeg_path()), "-hide_banner", f"-{kind}"], timeout=30)
    except (FFmpegError, FFmpegNotFound, subprocess.TimeoutExpired):
        _capabilities[kind] = names
        return names

    for line in output.splitlines():
        # Rows look like " A....D libopus  libopus Opus (codec opus)" -- the
        # name is the second whitespace-separated token after the flag column.
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(" -") and "=" not in parts[0]:
            flags = parts[0]
            if flags and all(c in ".ATSVAXCD" for c in flags) and len(flags) >= 2:
                names.add(parts[1])
    _capabilities[kind] = names
    return names


def has_encoder(name: str) -> bool:
    """Whether this ffmpeg build can encode with ``name``."""
    return name in _list_capability("encoders")


def has_filter(name: str) -> bool:
    """Whether this ffmpeg build provides the ``name`` audio filter."""
    return name in _list_capability("filters")


def best_aac_encoder() -> str:
    """Prefer libfdk_aac when the build has it -- it is audibly better at
    the same bitrate than ffmpeg's native encoder."""
    return "libfdk_aac" if has_encoder("libfdk_aac") else "aac"

"""Checking for and installing app updates from GitHub Releases.

The build workflow (.github/workflows/build.yml) attaches a Windows zip to a
real GitHub Release for every ``v*`` tag, so the public Releases API is
enough to find the latest build with no authentication needed.

Applying an update only makes sense for a packaged (frozen) install: a
source checkout has no "install directory" to replace. PyInstaller's onedir
bundle keeps every DLL in the running process open for as long as it's
alive, so the file swap can't happen in-process -- a detached helper script
waits for this process to exit, mirrors the new build over the install
directory, relaunches the app, then deletes itself and the download.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import __version__

GITHUB_REPO = "jayuan101/music-studio"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Directory containing the running MusicStudio.exe (frozen builds only)."""
    return Path(sys.executable).resolve().parent


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn 'v1.2.3-ci-verify' or '1.2.3' into (1, 2, 3).

    Any non-numeric prerelease suffix (after the first '-') is dropped, and
    an unparseable segment becomes 0, rather than raising -- a malformed tag
    should just compare as "not newer", not crash the update check.
    """
    text = text.lstrip("vV").split("-", 1)[0]
    parts = []
    for piece in text.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    download_url: str
    size: int


def check_for_update(*, timeout: float = 10.0) -> UpdateInfo | None:
    """Ask GitHub for the latest release; return it only if newer than this
    build. Returns None on any network error, a malformed response, or when
    already up to date -- a failed check should look like "no update", not
    a crash.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    tag = data.get("tag_name", "")
    if not tag or _parse_version(tag) <= _parse_version(__version__):
        return None

    asset = next(
        (a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".zip")),
        None,
    )
    if asset is None:
        return None

    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=(data.get("body") or "").strip(),
        download_url=asset["browser_download_url"],
        size=asset.get("size", 0),
    )


def download_update(info: UpdateInfo, *, context=None) -> Path:
    """Download the release zip to a fresh temp file, returning its path."""
    workdir = Path(tempfile.mkdtemp(prefix="musicstudio-update-"))
    dest = workdir / "update.zip"

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", info.download_url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0)) or info.size
            written = 0
            with open(dest, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
                    written += len(chunk)
                    if context is not None:
                        context.raise_if_cancelled()
                        context.progress(
                            written / total if total else None, "Downloading update…"
                        )
    return dest


def apply_update(zip_path: Path) -> None:
    """Extract the downloaded release and hand off to a helper script that
    swaps it in once this process exits, then relaunches the app.

    Does not itself exit the process -- the caller must do that (e.g. via
    QApplication.quit()) once this returns, or the helper will just wait.
    """
    if not is_frozen():
        raise RuntimeError("Updating only applies to the packaged app, not a source checkout")

    target_dir = install_dir()
    extract_dir = zip_path.parent / "extracted"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    exe_path = target_dir / Path(sys.executable).name
    helper = zip_path.parent / "apply_update.bat"
    log_path = zip_path.parent / "robocopy.log"
    steps_log = zip_path.parent / "apply_update.log"
    helper.write_text(
        "@echo off\r\n"
        f'set "PID={os.getpid()}"\r\n'
        f'set "STEPLOG={steps_log}"\r\n'
        f'echo Waiting for PID %PID% to exit... > "%STEPLOG%"\r\n'
        # Matches on IMAGENAME too, not just PID -- a bare PID filter would
        # wait forever if this PID happens to be reused by an unrelated,
        # longer-lived process before this check runs.
        ":wait\r\n"
        'tasklist /FI "PID eq %PID%" /FI "IMAGENAME eq MusicStudio.exe" 2^>nul | find "%PID%" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        # The process table drops the PID slightly before Windows finishes
        # releasing the exe/DLL file handles -- copying immediately can hit
        # a locked file (made worse by antivirus scanning the freshly
        # written new build). A short grace period avoids that race.
        "timeout /t 2 /nobreak >nul\r\n"
        f'echo Copying new build over "{target_dir}"... >> "%STEPLOG%"\r\n'
        f'robocopy "{extract_dir}" "{target_dir}" /MIR /R:10 /W:2 /LOG:"{log_path}" >nul\r\n'
        # Robocopy's own exit codes: 0-7 all mean "success" (some
        # combination of copied/extra/mismatched files); 8+ means at least
        # one file could not be copied even after the retries above. A
        # locked exe that never gets copied must not be masked by silently
        # launching whatever is left in target_dir.
        "set \"COPY_RESULT=%ERRORLEVEL%\"\r\n"
        'echo Robocopy exit code: %COPY_RESULT% >> "%STEPLOG%"\r\n'
        "if %COPY_RESULT% GEQ 8 (\r\n"
        '  echo Copy failed -- not launching. See robocopy.log next to this file. >> "%STEPLOG%"\r\n'
        "  goto :eof\r\n"
        ")\r\n"
        f'if not exist "{exe_path}" (\r\n'
        f'  echo "{exe_path}" is missing after copy -- not launching. >> "%STEPLOG%"\r\n'
        "  goto :eof\r\n"
        ")\r\n"
        f'echo Launching "{exe_path}"... >> "%STEPLOG%"\r\n'
        f'start "" "{exe_path}"\r\n'
        'echo Launch requested. >> "%STEPLOG%"\r\n'
        # Give Windows a moment to actually hand off before this script's
        # own directory (which it is currently executing from) is removed.
        "timeout /t 3 /nobreak >nul\r\n"
        f'rmdir /s /q "{zip_path.parent}"\r\n',
        encoding="utf-8",
    )
    subprocess.Popen(
        ["cmd", "/c", str(helper)],
        # DETACHED_PROCESS (no console at all) used to be combined with
        # CREATE_NO_WINDOW here, on the assumption that "more hidden" is
        # strictly better. It is not: cmd's own `start` builtin -- the line
        # that actually relaunches the app -- depends on the process having
        # *a* console object to work from, even a hidden one. Under a fully
        # console-less DETACHED_PROCESS parent, `start` can silently fail to
        # create the new process while the rest of the script (a plain
        # console tool like robocopy) runs fine -- which looked exactly like
        # "the update installed but the app never reopened". CREATE_NO_WINDOW
        # alone still hides the window and does not tie the helper's
        # lifetime to this one (Windows does not kill child processes when
        # a parent exits), so nothing is lost by dropping DETACHED_PROCESS.
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )

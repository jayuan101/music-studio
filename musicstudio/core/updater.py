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
    helper.write_text(
        "@echo off\r\n"
        f'set "PID={os.getpid()}"\r\n'
        ":wait\r\n"
        'tasklist /FI "PID eq %PID%" | find "%PID%" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'robocopy "{extract_dir}" "{target_dir}" /MIR /R:5 /W:1 >nul\r\n'
        f'start "" "{exe_path}"\r\n'
        f'rmdir /s /q "{zip_path.parent}"\r\n',
        encoding="utf-8",
    )
    subprocess.Popen(
        ["cmd", "/c", str(helper)],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )

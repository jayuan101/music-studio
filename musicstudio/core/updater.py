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

import sys
import tempfile
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

    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name", "")
    if not isinstance(tag, str) or not tag or _parse_version(tag) <= _parse_version(__version__):
        return None
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        return None

    asset = next(
        (a for a in assets
         if isinstance(a, dict)
         and isinstance(a.get("name"), str)
         and a["name"].lower().endswith(".zip")
         and isinstance(a.get("browser_download_url"), str)
         and a["browser_download_url"].startswith("https://")),
        None,
    )
    if asset is None:
        return None

    notes = data.get("body")
    size = asset.get("size", 0)
    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=notes.strip() if isinstance(notes, str) else "",
        download_url=asset["browser_download_url"],
        size=size if type(size) is int and size >= 0 else 0,
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
                    if context is not None:
                        context.raise_if_cancelled()
                    f.write(chunk)
                    written += len(chunk)
                    if context is not None:
                        context.raise_if_cancelled()
                        context.progress(
                            written / total if total else None, "Downloading update…"
                        )
            if total and written != total:
                raise ValueError("Update download is incomplete; please retry")
    return dest


def apply_update(zip_path: Path) -> None:
    """Validate, back up, install, and roll back if copying or startup fails."""
    if not is_frozen():
        raise RuntimeError("Updating only applies to the packaged app, not a source checkout")
    from .update_install import launch
    launch(zip_path, install_dir())

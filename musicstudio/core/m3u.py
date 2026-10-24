"""Write playlists as extended M3U (``.m3u8``) for other players and devices."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Protocol


class _Track(Protocol):
    path: Path
    duration: float
    display_artist: str
    display_title: str


def _entry_path(track_path: Path, playlist_dir: Path, relative: bool) -> str:
    """Relative to the playlist when possible, so a folder copied to another
    drive or a phone keeps working; absolute when the two are on different
    drives (Windows has no relative path between them)."""
    track_path = Path(track_path)
    if relative:
        try:
            return os.path.relpath(track_path, playlist_dir)
        except ValueError:
            pass
    return str(track_path)


def write_m3u(path: Path, tracks: Iterable[_Track], *, relative: bool = True) -> int:
    """Write ``tracks`` to ``path`` as UTF-8 extended M3U. Returns the count.

    UTF-8 regardless of the extension -- every current player reads it, and
    the legacy ANSI-code-page reading of plain ``.m3u`` mangles non-English
    titles.
    """
    path = Path(path)
    playlist_dir = path.resolve().parent
    lines = ["#EXTM3U"]
    count = 0
    for track in tracks:
        seconds = int(round(track.duration)) if track.duration else -1
        # A newline in a tag would end the EXTINF line early.
        label = f"{track.display_artist} - {track.display_title}".replace("\n", " ")
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(_entry_path(track.path, playlist_dir, relative))
        count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Bytes, not write_text: text mode would turn each \r\n into \r\r\n.
    tmp.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    os.replace(tmp, path)
    return count

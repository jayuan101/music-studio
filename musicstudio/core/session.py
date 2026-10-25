"""Atomic, best-effort persistence for the playback queue."""

import json
import os
from pathlib import Path

from .. import config


def save(state: dict) -> None:
    path = config.DATA_DIR / "playback-session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load() -> dict:
    try:
        path = config.DATA_DIR / "playback-session.json"
        if path.stat().st_size > 8_000_000:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

"""Application settings and on-disk locations.

Settings are stored via ``QSettings`` (registry on Windows) when Qt is available,
and fall back to a plain JSON file so the core engine and its tests can run
headless without importing Qt.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

from . import APP_NAME, APP_ORG

_dirs = PlatformDirs(appname=APP_NAME, appauthor=APP_ORG, roaming=True)

CONFIG_DIR = Path(_dirs.user_config_dir)
DATA_DIR = Path(_dirs.user_data_dir)
CACHE_DIR = Path(_dirs.user_cache_dir)

#: SQLite library index.
DB_PATH = DATA_DIR / "library.db"
#: Downloaded and converted output lands here unless the user picks somewhere else.
DEFAULT_OUTPUT_DIR = Path(_dirs.user_music_dir or Path.home() / "Music") / APP_NAME
#: Cover art fetched from the network is cached here, keyed by artist+album.
ARTWORK_CACHE_DIR = CACHE_DIR / "artwork"
#: Scratch space for intermediate renders and waveform peak files.
TEMP_DIR = CACHE_DIR / "tmp"

_SETTINGS_FILE = CONFIG_DIR / "settings.json"


def ensure_dirs() -> None:
    """Create every directory the app writes to. Safe to call repeatedly."""
    for path in (CONFIG_DIR, DATA_DIR, CACHE_DIR, ARTWORK_CACHE_DIR, TEMP_DIR, DEFAULT_OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    """User-facing preferences, with quality-first defaults."""

    # -- Output ---------------------------------------------------------
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    #: Filename pattern for converted/downloaded files. A "/" creates a
    #: subfolder. Track and disc numbers are zero-padded automatically, so no
    #: format spec is needed. See musicstudio.core.organise for the fields.
    filename_template: str = "{albumartist}/{album}/{track} - {title}"
    #: Never silently replace a file the user already has.
    overwrite_existing: bool = False

    # -- Quality policy -------------------------------------------------
    #: Keep the source's sample rate and bit depth unless explicitly changed.
    preserve_source_rate: bool = True
    preserve_source_depth: bool = True
    #: Warn (but never block) when an operation cannot possibly add quality.
    warn_on_lossy_to_lossless: bool = True
    warn_on_lossy_to_lossy: bool = True
    #: Dither when reducing bit depth. Off means truncation, which is worse.
    dither_on_downconvert: bool = True

    # -- Artwork --------------------------------------------------------
    artwork_enabled: bool = True
    #: Re-fetch art when the embedded image is smaller than this (pixels, square).
    artwork_min_size: int = 600
    #: Size requested from providers.
    artwork_preferred_size: int = 1200
    artwork_use_musicbrainz: bool = True
    artwork_use_itunes: bool = True
    #: MusicBrainz requires a contactable User-Agent; edit if you fork this.
    musicbrainz_user_agent: str = (
        "MusicStudio/1.0 (https://github.com/jayuan101/transcript-agent-releases)"
    )

    # -- Download -------------------------------------------------------
    #: "keep" downloads the best original stream untouched (no re-encode).
    #: "convert" re-encodes into `download_format`.
    download_mode: str = "keep"
    download_format: str = "flac"
    download_embed_thumbnail: bool = True
    download_playlist_limit: int = 0  # 0 = no limit

    # -- Editing --------------------------------------------------------
    #: True-peak ceiling used by the limiter when boosting past 0 dB.
    limiter_ceiling_db: float = -0.3
    #: Highest gain the UI slider offers, in dB. +30 dB is ~3000% volume.
    max_gain_db: float = 30.0
    #: Target loudness for EBU R128 normalization.
    loudnorm_target_lufs: float = -14.0

    # -- UI -------------------------------------------------------------
    theme: str = "dark"
    library_paths: list[str] = field(default_factory=list)

    # -- Personal AI ------------------------------------------------------
    #: Local model backend. Ollama runs as a separate process the user
    #: installs themselves -- never pip-installed or bundled, just an HTTP
    #: endpoint the assistant talks to.
    ai_ollama_host: str = "http://localhost:11434"
    #: Model name as shown by `ollama list`, e.g. "llama3.1". Empty until the
    #: user picks one in Preferences.
    ai_ollama_model: str = ""
    #: Escalate to the Claude API for commands the local model struggles
    #: with. Off by default: the local path needs no network and no key.
    ai_use_claude: bool = False
    ai_claude_model: str = "claude-sonnet-5"
    #: Fallback storage for the API key when core.secrets can't use the OS
    #: credential store (e.g. no `keyring` backend available). Plain text in
    #: settings.json when used this way -- core.secrets is what decides
    #: whether this field or the OS store is authoritative; never read this
    #: directly, go through core.secrets.get_claude_api_key().
    ai_claude_api_key: str = ""

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        if _SETTINGS_FILE.exists():
            try:
                return cls.from_dict(json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                # A corrupt settings file must never stop the app from starting.
                pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, _SETTINGS_FILE)  # atomic, so a crash cannot truncate settings


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings_cache() -> None:
    """Drop the cached singleton. Used by tests."""
    global _settings
    _settings = None

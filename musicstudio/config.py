"""Application settings and on-disk locations.

Settings are stored via ``QSettings`` (registry on Windows) when Qt is available,
and fall back to a plain JSON file so the core engine and its tests can run
headless without importing Qt.
"""

from __future__ import annotations

import json
import os
import shutil
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
#: Where a download is fetched to for "preview and decide" auditioning,
#: before the user clicks Keep. Never a permanent home for a file.
PREVIEW_CACHE_DIR = TEMP_DIR / "download_preview"

_SETTINGS_FILE = CONFIG_DIR / "settings.json"


def ensure_dirs() -> None:
    """Create every directory the app writes to. Safe to call repeatedly."""
    for path in (
        CONFIG_DIR,
        DATA_DIR,
        CACHE_DIR,
        ARTWORK_CACHE_DIR,
        TEMP_DIR,
        PREVIEW_CACHE_DIR,
        DEFAULT_OUTPUT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def clear_preview_cache() -> None:
    """Best-effort wipe of everything under ``PREVIEW_CACHE_DIR``.

    Called explicitly at app startup and shutdown -- not folded into
    ``ensure_dirs()``, which is documented safe-to-call-repeatedly and
    should never have a destructive side effect. A file can be briefly
    locked by playback or another process; skipping over failures here is
    fine because the next startup's wipe will catch anything left behind.
    """
    if not PREVIEW_CACHE_DIR.is_dir():
        return
    for child in PREVIEW_CACHE_DIR.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            pass


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
    #: Tried after MusicBrainz/iTunes come back empty (for both artwork and,
    #: in core/tag_fix.py, metadata) -- best catalogue and cover-art quality
    #: of any provider here, but needs credentials (see spotify_client_id
    #: below), unlike the two free/keyless ones above. One flag governs both
    #: uses, the same as iTunes/MusicBrainz aren't separately toggled per use.
    spotify_enabled: bool = False
    spotify_client_id: str = ""
    #: Fallback storage for the Client Secret when core.secrets can't use the
    #: OS credential store -- same arrangement as ai_claude_api_key below;
    #: never read this directly, go through core.secrets.get_spotify_client_secret().
    spotify_client_secret: str = ""
    #: Last resort: a YouTube video thumbnail, when nothing else has real
    #: cover art. No credentials needed, but it is a video frame, not an
    #: album cover, so quality and accuracy vary.
    artwork_use_youtube_thumbnail: bool = True

    # -- Download -------------------------------------------------------
    #: "keep" downloads the best original stream untouched (no re-encode).
    #: "convert" re-encodes into `download_format`. Defaults to converting so
    #: every download lands as a proper library format (FLAC) rather than
    #: whatever container the source happens to use (often Opus-in-WebM).
    download_mode: str = "convert"
    download_format: str = "flac"
    download_embed_thumbnail: bool = True
    download_playlist_limit: int = 0  # 0 = no limit

    # -- Metadata style -------------------------------------------------
    #: Reshape tags to YouTube Music's conventions on download: clean the
    #: title, move guests into it as "(feat. X)", and always fill album
    #: artist -- the field YouTube Music groups a library by.
    ytmusic_format_downloads: bool = True

    # -- Auto-trim --------------------------------------------------------
    #: Detect and remove leading/trailing silence or logo bumpers from
    #: tracks that look like they came from a music video. Off by default --
    #: this rewrites the audio file in place.
    auto_trim_enabled: bool = False
    #: Run the trim automatically right after each download finishes.
    auto_trim_new_tracks: bool = False
    auto_trim_silence_threshold_db: float = -50.0
    auto_trim_max_intro_s: float = 12.0
    auto_trim_max_outro_s: float = 12.0
    #: Also try to recognise spoken (non-music) intros/outros via local voice
    #: detection, not just silence. Experimental: a sung intro can sometimes
    #: be mistaken for speech. Off by default; still bounded by the caps above.
    auto_trim_detect_speech: bool = False

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

"""The catalogue of output formats and their maximum-quality settings.

Input is not restricted here -- ffmpeg decodes essentially every music format
in existence, so anything it can open can be imported. This module describes
only what we can *write*, and what the best possible settings are for each.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Extensions we offer to scan when importing a folder. Decoding is not limited
#: to this list; it exists so a library scan skips images and text files.
IMPORTABLE_EXTENSIONS = frozenset(
    {
        ".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
        ".wav", ".wave", ".aiff", ".aif", ".aifc", ".wma", ".wv", ".ape",
        ".alac", ".mka", ".mpc", ".tta", ".spx", ".ac3", ".dts", ".amr",
        ".au", ".ra", ".m4b", ".mp2", ".caf", ".w64", ".dsf", ".dff",
    }
)


@dataclass(frozen=True)
class FormatProfile:
    """How to encode one output format at the best quality it can manage."""

    id: str
    label: str
    extension: str
    #: ffmpeg encoder name. "aac" is special-cased to prefer libfdk_aac.
    encoder: str
    lossless: bool
    #: Explicit muxer when the extension alone would pick the wrong one.
    muxer: str | None = None

    # -- Lossy quality knobs -------------------------------------------
    #: Whether this encoder has a quality-based VBR mode (usually the best
    #: choice: it spends bits where the music needs them).
    supports_vbr: bool = False
    #: ffmpeg flag carrying the VBR quality value, e.g. "-q:a".
    vbr_flag: str = "-q:a"
    #: Best practical VBR setting. For libmp3lame 0 is V0 (~245 kbps average).
    default_vbr_quality: str = "0"
    #: Bitrate in kbps used for CBR mode, chosen at the transparency ceiling.
    default_bitrate: int = 320

    # -- Technical limits ----------------------------------------------
    #: Sample rates the encoder accepts. None means "anything the source has".
    supported_sample_rates: tuple[int, ...] | None = None
    #: Bit depths the format can store, best first. Empty for lossy formats,
    #: where bit depth is not a meaningful concept.
    supported_bit_depths: tuple[int, ...] = ()
    max_channels: int = 8
    #: Whether the container can carry embedded cover art.
    supports_artwork: bool = True
    description: str = ""
    #: Optional extra encoder flags appended verbatim.
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_lossy(self) -> bool:
        return not self.lossless


# ---------------------------------------------------------------------------
# Lossless targets
# ---------------------------------------------------------------------------

FLAC = FormatProfile(
    id="flac",
    label="FLAC",
    extension=".flac",
    encoder="flac",
    lossless=True,
    supported_bit_depths=(24, 16, 8),
    supported_sample_rates=None,
    description=(
        "Free Lossless Audio Codec. Bit-perfect, roughly half the size of WAV, "
        "and understood by nearly everything. The best default for archiving."
    ),
    # Level 8 is the densest the encoder offers. It is slower to encode but
    # decodes just as fast and is bit-identical to level 0 on playback.
    extra_args=("-compression_level", "8"),
)

ALAC = FormatProfile(
    id="alac",
    label="ALAC (Apple Lossless)",
    extension=".m4a",
    encoder="alac",
    lossless=True,
    muxer="ipod",
    supported_bit_depths=(24, 16),
    description=(
        "Apple Lossless. Same bit-perfect quality as FLAC, slightly larger, "
        "and the right choice for iTunes / Apple Music / iOS libraries."
    ),
)

WAV = FormatProfile(
    id="wav",
    label="WAV (uncompressed)",
    extension=".wav",
    encoder="pcm_s24le",
    lossless=True,
    supported_bit_depths=(24, 16, 32),
    supports_artwork=False,
    description=(
        "Raw uncompressed PCM. Maximum compatibility with audio editors, but "
        "two to three times the size of FLAC for identical audio, and its tag "
        "support is poor."
    ),
)

AIFF = FormatProfile(
    id="aiff",
    label="AIFF (uncompressed)",
    extension=".aiff",
    encoder="pcm_s24be",
    lossless=True,
    supported_bit_depths=(24, 16, 32),
    description=(
        "Uncompressed PCM in Apple's container. Same audio as WAV, with better "
        "metadata support."
    ),
)

WAVPACK = FormatProfile(
    id="wavpack",
    label="WavPack",
    extension=".wv",
    encoder="wavpack",
    lossless=True,
    supported_bit_depths=(24, 16, 32),
    description=(
        "Lossless with excellent compression and high-resolution support. "
        "Less widely supported by hardware players than FLAC."
    ),
    extra_args=("-compression_level", "3"),
)

# ---------------------------------------------------------------------------
# Lossy targets
# ---------------------------------------------------------------------------

MP3 = FormatProfile(
    id="mp3",
    label="MP3",
    extension=".mp3",
    encoder="libmp3lame",
    lossless=False,
    supports_vbr=True,
    vbr_flag="-q:a",
    default_vbr_quality="0",  # V0: highest LAME VBR setting, ~245 kbps
    default_bitrate=320,      # the format's ceiling
    supported_sample_rates=(48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000),
    description=(
        "Universally playable. V0 variable bitrate is the best MP3 quality "
        "worth having -- 320 kbps constant is bigger with no audible gain."
    ),
)

AAC = FormatProfile(
    id="aac",
    label="AAC (M4A)",
    extension=".m4a",
    encoder="aac",  # upgraded to libfdk_aac when the build supports it
    lossless=False,
    muxer="ipod",
    supports_vbr=False,
    default_bitrate=256,
    supported_sample_rates=(96000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000),
    description=(
        "Better than MP3 at the same bitrate, and the native format for Apple "
        "devices. 256 kbps is effectively transparent."
    ),
)

OPUS = FormatProfile(
    id="opus",
    label="Opus",
    extension=".opus",
    encoder="libopus",
    lossless=False,
    default_bitrate=192,
    # libopus always works at 48 kHz internally; feeding it anything else just
    # makes ffmpeg resample first.
    supported_sample_rates=(48000, 24000, 16000, 12000, 8000),
    description=(
        "The best-sounding lossy codec at any given size. 192 kbps is "
        "transparent for music. Not supported by some older hardware players."
    ),
    extra_args=("-vbr", "on", "-compression_level", "10", "-application", "audio"),
)

VORBIS = FormatProfile(
    id="vorbis",
    label="Ogg Vorbis",
    extension=".ogg",
    encoder="libvorbis",
    lossless=False,
    supports_vbr=True,
    vbr_flag="-q:a",
    default_vbr_quality="8",  # ~256 kbps
    default_bitrate=256,
    description=(
        "Open, patent-free lossy format. Quality 8 is roughly transparent. "
        "Opus beats it at every bitrate, but Vorbis has broader support."
    ),
)

WMA = FormatProfile(
    id="wma",
    label="Windows Media Audio",
    extension=".wma",
    encoder="wmav2",
    lossless=False,
    default_bitrate=320,
    supported_sample_rates=(48000, 44100, 32000, 22050, 16000, 11025, 8000),
    description=(
        "Legacy Windows format. Included for compatibility with old devices; "
        "there is no quality reason to choose it over AAC or Opus."
    ),
)


ALL_PROFILES: tuple[FormatProfile, ...] = (
    FLAC, ALAC, WAV, AIFF, WAVPACK,      # lossless first -- the quality default
    MP3, AAC, OPUS, VORBIS, WMA,
)

PROFILES_BY_ID: dict[str, FormatProfile] = {p.id: p for p in ALL_PROFILES}

LOSSLESS_PROFILES = tuple(p for p in ALL_PROFILES if p.lossless)
LOSSY_PROFILES = tuple(p for p in ALL_PROFILES if p.is_lossy)


def get_profile(profile_id: str) -> FormatProfile:
    """Look up a profile by id, raising a helpful error for unknown ids."""
    try:
        return PROFILES_BY_ID[profile_id.lower()]
    except KeyError:
        known = ", ".join(sorted(PROFILES_BY_ID))
        raise ValueError(f"Unknown output format {profile_id!r}. Available: {known}") from None


def profile_for_extension(extension: str) -> FormatProfile | None:
    """Best-guess profile from a file extension, for 'convert to same format'."""
    extension = extension.lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    for profile in ALL_PROFILES:
        if profile.extension == extension:
            return profile
    return {".ogg": VORBIS, ".oga": VORBIS, ".aif": AIFF, ".wave": WAV, ".aac": AAC}.get(extension)

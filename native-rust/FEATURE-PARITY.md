# Feature parity worklist

Full feature parity is required. These Rust apps remain previews until the missing behavior is implemented, Windows builds pass, and the installed app is exercised. A repository commit alone is not an app update.

Initial audit: 2026-11-06. Compared original README and source/module inventory with Rust source; this is a living checklist, not an exhaustive acceptance result. “Added” means source implementation, not installed or interactively verified.

| Feature | Current status | Original source reference |
|---|---|---|
| Playback, queue, repeat and shuffle | Partial: play-next, queue reorder/removal and shuffle-upcoming added; persistent shuffle toggle and periodic autosave pending. | `musicstudio/core/session.py` |
| Manual/smart playlists, drag reorder and M3U8 | Partial: manual playlists/export; smart rules and playlist reorder missing. | `musicstudio/core/playlist_rules.py; m3u.py` |
| Library scan, relink missing, health views and authenticity checks | Partial: reference import; missing files retained. Other tools missing. | `musicstudio/core/relink.py; authenticity.py; health_presets.py` |
| All conversion formats and source precision preservation | Added for export copies: FLAC, Apple Lossless, WAV, AIFF, WavPack, MP3, AAC, Opus, Vorbis, WMA. Implicit resampling, downmix and precision loss are refused. Every render plan passed against real FFmpeg 9.0, with lossless output decoding to the exact source samples. | `musicstudio/core/formats.py; convert.py` |
| Waveform, trim/cut, gain/limiter, normalise, fades, tempo/pitch, EQ, channel mapping | Partial: export copies have trim, gain, sample-peak limiter, dynamic normalization, pitch-preserving speed, bass/treble, fades and mono/stereo. Waveform, cut, pitch shift, two-pass EBU R128 and channel mapping missing. | `musicstudio/core/edit.py` |
| Independent editor preview and atomic save with undo | Missing; export copies only. | `musicstudio/core/edit.py; history.py` |
| Tags, provider metadata matching, artwork picker/cache/retry | Missing. | `musicstudio/core/tags.py; tag_fix.py; artwork.py` |
| YouTube Music tag conventions and library organisation | Missing. | `musicstudio/core/ytmusic.py; organise.py` |
| ReplayGain scan, tags and playback modes | Missing. | `musicstudio/core/replaygain.py` |
| Synced LRCLIB lyrics, AcoustID and fpcalc identification | Missing. | `musicstudio/core/lyrics.py; fingerprint.py` |
| YouTube search, yt-dlp downloads and import conversion | Missing. | `musicstudio/core/download.py` |
| Ollama/Claude assistant and reviewed actions | Missing. | `musicstudio/core/assistant.py` |
| Speech tools, Spotify integration and autotrim | Missing; source modules require detailed UI acceptance mapping. | `musicstudio/core/speech.py; spotify.py; autotrim.py` |
| Job progress/cancellation/retry, preferences and secret storage | Partial: single background import/export only. | `musicstudio/core/jobs.py; secrets.py` |
| App update download, installation and rollback | Partial: checked ZIP import only. | `musicstudio/core/updater.py; update_install.py` |

## Completion criteria for every row

- Match the original user workflow and failure/recovery behavior.
- Add appropriate regression tests; test Windows runtime behavior and needed hardware/providers.
- Push tested source to this app’s repository and build a matching executable.
- Install that exact verified build under this app’s D: folder and verify START.cmd opens it.
- Preserve the original app and user data until replacement acceptance.

## Installation and local builds

UPDATE.cmd installs the build pinned in `ci/latest-build.json`. That executable always comes from Windows CI (MSVC), and the checksum, source revision and run are verified before it is installed under `dist`. `scripts\verify-launch.ps1` then starts it through START.cmd and confirms the running file is that exact executable.

This PC has no Visual Studio or Windows SDK, so local development uses GNU Rust plus a MinGW kit in this project's `.tools` folder (`scripts\install-rust.ps1`). Local GNU builds are for testing and are not installed. `scripts\release.py` mirrors this folder into the `.repo` clone and writes the build records from GitHub run data.

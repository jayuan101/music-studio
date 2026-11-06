# Music Studio Rust preview

A native egui/eframe Windows application with Rust audio decoding/playback through Rodio. The original `D:\music-studio` project is unchanged. User data is stored under `D:\UserData\Apps\MusicStudio-Rust`.

Implemented:

- Add selected files or scan a folder in the background. Import records paths, keeps missing saved entries, and deduplicates Windows paths without copying or deleting source audio.
- Filename search, double-click playback, queue playback, pause/resume, previous/next, repeat off/queue/track, seek where supported, and volume.
- Named playlists saved from the queue and UTF-8 M3U8 export with absolute paths.
- Session snapshots for library, playlists, queue, volume, repeat, and playback position. Restored queues stay paused until Play is clicked. Save explicitly or close normally; periodic crash recovery is not implemented.
- Export the selected track as a new FLAC or MP3 V0 file, optionally trimming by start/end seconds. Uses the existing `D:\Projects\MusicStudio-Rust\tools\ffmpeg\ffmpeg.exe`, or an explicitly selected FFmpeg executable. No shell command interpolation, source edits, or output replacement.

The player supports the formats available in the included Symphonia decoder. Unsupported or unreadable files are counted during import. Import skips symlinks/reparse points, limits traversal to 100,000 entries and 20,000 audio tracks, and reports limit failures. Audio device access starts only when playback is requested.

Exports go to `exports`, playlist copies to `playlists`, FFmpeg error logs to `logs`, and session snapshots to `sessions`. Source sample rate/channel count are not explicitly changed by export arguments. Original Music Studio's full sample-format negotiation, resampling/dither policy, and every encoder preset are not yet ported. MP3 adds a lossy generation; FLAC cannot restore detail missing from a lossy source. A failed export may retain a partial output and error log for diagnosis. Exports time out after 30 minutes.

Still to port: tag/artwork editing, waveform display, shuffle/reordering, smart playlists, replay gain, lyrics, fingerprinting, library health/relinking, advanced effects, undo, other formats, downloads, AI assistant, and updates.

Use `START.cmd` after installing a verified workspace build. Windows UI and actual audio-output testing remain separate acceptance steps. `scripts/verify-music-export.py` exercises the existing FFmpeg executable with synthetic audio; it does not run the native GUI. Rust tests cover queue behavior, path deduplication, playlist format, export arguments, and PCM decoding without opening an audio device.

Primary playback reference: [Rodio 0.21.1](https://docs.rs/rodio/0.21.1/rodio/).

# MusicStudio-Rust

Standalone Rust Windows preview. This folder contains only this app, with its own Cargo workspace, build output, installer, launch button, and app-local support code. It does not depend on the other Rust project folders.

Library/queue playback, playlists/M3U8, session restore and FFmpeg export copies in ten formats with trim, gain, limiting, normalization, speed, EQ, fades and output-quality controls. Source files are never edited by export. Waveform, save-in-place, tags/artwork, ReplayGain, downloads and AI are not ported yet.

## Locations

- Source: `D:\Projects\MusicStudio-Rust`
- App data: `D:\UserData\Apps\MusicStudio-Rust`
- Builds: this project's `target` and `dist` folders
- Local Rust tools/cache: this project's `.tools` folder
- Temporary files and logs: this project's `.tmp` and `logs` folders
- Repository: https://github.com/jayuan101/music-studio
- Branch: `main`; source: `native-rust/`

The original Windows app and its data remain unchanged. This folder holds the app's source; `.repo` is its Git clone, kept in step by `scripts\release.py sync`.

## Use

Run `UPDATE.cmd` to download and install the verified build pinned in `ci/latest-build.json` using your signed-in GitHub CLI. Close the running app first, then open `START.cmd`. `INSTALL.cmd` installs a manually downloaded build ZIP with the same checks.

For local development, open `OPEN-PROJECT.cmd`, run `scripts\install-rust.ps1` once, then `scripts\local-test.ps1`. Windows CI builds the executable that gets installed; local builds are for testing.

Automated tests and a launch check do not replace trying each feature with real hardware and provider accounts. See `VALIDATION.json` for what has actually been checked and [FEATURE-PARITY.md](FEATURE-PARITY.md) for remaining work.

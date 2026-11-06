# MusicStudio-Rust

Standalone Rust Windows preview. This folder contains only this app, with its own Cargo workspace, build output, installer, launch button, and app-local support code. It does not depend on the other Rust project folders.

Library/queue playback, playlists/M3U8, session restore and FFmpeg export copies with optional trim. Advanced editing, tags/artwork, waveform, replay gain, downloads, AI and updates are NOT ported. Originals are never edited by the export workflow.

## Locations

- Source: `D:\Projects\MusicStudio-Rust`
- App data: `D:\UserData\Apps\MusicStudio-Rust`
- Builds: this project's `target` and `dist` folders
- Local Rust tools/cache: this project's `.tools` folder
- Temporary files and logs: this project's `.tmp` and `logs` folders
- Repository: https://github.com/jayuan101/music-studio
- Branch: `codex/rust-preview-20261106`; source: `native-rust/`

The original Windows app and its data remain unchanged. This source folder is synchronized through the connected GitHub API; it is not a local Git checkout.

## Use

Download **MusicStudio-Rust-windows** from the successful preview build in this app's draft PR, then run `INSTALL.cmd` and select that ZIP. `ci/latest-build.json` pins the verified ZIP's checksum and source revision after the build succeeds. Run `START.cmd` to launch the installed preview.

For a local build, open `OPEN-PROJECT.cmd`, run `scripts\install-rust.ps1` if needed, then `scripts\build.ps1`. Windows MSVC Build Tools/SDK are required; system-managed prerequisite files can live outside D:. The app and Rust project paths above stay on D:.

Windows compilation and automated tests do not replace interactive UI/audio/WebView2 testing. These are previews, not full production replacements. See `VALIDATION.json` for the current build status. No live provider credentials or user recordings are used by tests.

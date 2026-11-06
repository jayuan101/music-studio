# FFmpeg tools

The local `ffmpeg.exe` and `ffprobe.exe` were copied from the existing Music Studio vendor directory and SHA-256 checked against those originals. They are FFmpeg 9.0 binaries from the existing Gyan full build.

These third-party binaries are excluded from source control and the Rust preview CI artifact. Keep this tools folder when transferring the local app to another D: drive, or install a separately licensed FFmpeg distribution here. The Rust UI also lets you choose another `ffmpeg.exe` explicitly. No Python application or other Rust project is required at runtime.

Official project and licensing: https://ffmpeg.org/ and https://ffmpeg.org/legal.html.

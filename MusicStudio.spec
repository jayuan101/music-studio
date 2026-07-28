# PyInstaller build recipe for Music Studio.
#
# Built as a *onedir* bundle rather than onefile: it starts far faster (no
# unpacking to a temp folder on every launch), and single-file executables that
# unpack and execute a bundled ffmpeg.exe are a reliable way to get flagged by
# Windows antivirus heuristics.
#
# Build with:  pyinstaller MusicStudio.spec
#
# ffmpeg.exe and ffprobe.exe must be in vendor/ffmpeg/ before building; the CI
# workflow downloads them. musicstudio.core.ffmpeg resolves them at runtime.

import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH)
VENDOR_FFMPEG = PROJECT_ROOT / "vendor" / "ffmpeg"

binaries = []
if VENDOR_FFMPEG.is_dir():
    for name in ("ffmpeg", "ffprobe"):
        for candidate in (VENDOR_FFMPEG / f"{name}.exe", VENDOR_FFMPEG / name):
            if candidate.is_file():
                # Land in an "ffmpeg" subfolder, which _bundle_roots() checks first.
                binaries.append((str(candidate), "ffmpeg"))
                break
else:
    print("WARNING: vendor/ffmpeg not found -- the build will have no bundled ffmpeg.")

a = Analysis(
    ["run_musicstudio.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        # yt-dlp loads its 1700+ extractors dynamically, so static analysis
        # misses them entirely and every download would fail with a bare
        # "unsupported URL".
        "yt_dlp",
        "yt_dlp.extractor",
        "yt_dlp.extractor.lazy_extractors",
        "yt_dlp.compat",
        "yt_dlp.utils",
        # mutagen dispatches on file type at runtime.
        "mutagen",
        "mutagen.aac", "mutagen.aiff", "mutagen.apev2", "mutagen.asf",
        "mutagen.easyid3", "mutagen.flac", "mutagen.id3", "mutagen.mp3",
        "mutagen.mp4", "mutagen.monkeysaudio", "mutagen.musepack",
        "mutagen.oggflac", "mutagen.oggopus", "mutagen.oggspeex",
        "mutagen.oggtheora", "mutagen.oggvorbis", "mutagen.optimfrog",
        "mutagen.trueaudio", "mutagen.wave", "mutagen.wavpack",
        "httpx", "httpcore", "certifi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PySide6-Essentials still pulls in a lot we never touch. Dropping
        # these keeps the download to a sane size.
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtQuick3D",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtTest", "PySide6.QtDesigner",
        "PySide6.QtSql", "PySide6.QtHelp", "PySide6.QtMultimediaWidgets",
        "tkinter", "matplotlib", "numpy", "scipy", "PIL", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = PROJECT_ROOT / "assets" / "icon.ico"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus far too often
    console=False,      # a GUI app must not open a console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MusicStudio",
)

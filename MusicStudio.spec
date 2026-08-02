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
    datas=(
        # The window icon is looked up at runtime, so it has to travel with
        # the bundle -- the EXE's own icon is set separately, below.
        [(str(PROJECT_ROOT / "assets" / "icon.ico"), "assets")]
        if (PROJECT_ROOT / "assets" / "icon.ico").is_file()
        else []
    ),
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
        # Playback. Qt loads its multimedia backend plugin at runtime, so
        # static analysis alone would leave the app silent.
        "PySide6.QtMultimedia",
        # Personal AI assistant. anthropic is imported lazily by
        # core/assistant.py's ClaudeBackend, only when the user turns on
        # cloud escalation -- but it still has to be discoverable by
        # PyInstaller's static analysis to be bundled at all.
        "anthropic",
        # keyring picks its backend at runtime based on the OS; on Windows
        # that is the Credential Locker backend specifically.
        "keyring.backends.Windows",
        # core/library_ops.py imports this specific backend directly (see
        # the comment there for why: send2trash's default Windows path
        # depends on pywin32/COM, which is unreliable to freeze correctly).
        "send2trash",
        "send2trash.win",
        "send2trash.win.legacy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PySide6 pulls in a great deal we never touch -- and Addons, which we
        # depend on only for QtMultimedia, is by far the largest part. Dropping
        # the rest keeps the download to a sane size.
        #
        # NOTE: do NOT exclude PySide6.QtMultimedia. QtMultimediaWidgets is
        # only the video-surface widget, which this app has no use for.
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtWebView", "PySide6.QtWebChannel",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtQuick3D",
        "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtSerialBus", "PySide6.QtWebSockets", "PySide6.QtHttpServer",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets", "PySide6.QtQuickTest",
        "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
        "PySide6.QtSql", "PySide6.QtHelp", "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
        "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
        "PySide6.QtSpatialAudio", "PySide6.QtNetworkAuth", "PySide6.QtCanvasPainter",
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

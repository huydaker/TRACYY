# PyInstaller spec for the Tracyy application.
#
# V1 built from a hundred-line command line embedded in a .bat and a .command,
# which meant the Windows and macOS builds drifted apart and neither was
# reviewable. The build inputs live here instead; the platform scripts only
# create the virtualenv and call PyInstaller.
#
#   pyinstaller --noconfirm --clean packaging/tracyy.spec

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
IS_MACOS = sys.platform == "darwin"

datas = [
    (str(ROOT / "icon"), "icon"),
    (str(ROOT / "tracyy" / "gpu" / "waveform_surface.qml"), "tracyy/gpu"),
    (str(ROOT / "license_config.enc"), "."),
]

# Build-time keys, which the repository does not carry. Bundled only when the
# file is present, so a clone without the keys still builds — the licence layer
# then reports itself as unconfigured instead of failing the build. A release
# build needs it, or the two TRACYY_*_KEY variables set in the environment.
_secrets = ROOT / "license_secrets.json"
if _secrets.is_file():
    datas.append((str(_secrets), "."))
else:
    print(f"[tracyy.spec] {_secrets.name} không có — build này sẽ không mở được admin USB token.")
binaries = []
hiddenimports = [
    # Qt modules the GPU waveform surface loads at runtime, which the static
    # analysis cannot see through QQuickWidget.
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    # Imported by spawned worker processes, never by the main module graph.
    "tracyy.audio.streaming",
    "tracyy.workers.media_tasks",
    "tracyy.server.cue_viewer",
    # Loaded through a try/except, so it is invisible to the analysis.
    "tracyy.licensing.secure_loader",
    "segno",
]

# tracyy.audio and tracyy.core resolve their public names through string-keyed
# lazy tables (importlib.import_module on a name from a dict), so the module
# graph cannot see tracyy.audio.transport, .ltc_encoder, .resampler,
# .ringbuffer or tracyy.core.perf, .reactive, .ui_helpers at all. Listing them
# by hand only works until someone adds another entry, and the failure is
# invisible until a frozen build is actually launched — the app imported fine
# from source and died on "No module named tracyy.audio.transport". Collect the
# application's own package whole; it is small, and this removes the entire
# class of bug rather than the instance of it.
hiddenimports += collect_submodules("tracyy")

for package in ("PySide6", "sounddevice", "soundfile", "av", "soxr"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

datas += collect_data_files("certifi")

# Qt ships far more than a playback application needs. Excluding these keeps
# the bundle to roughly what V1 shipped despite the added dependencies.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtWebSockets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    "matplotlib",
    "tkinter",
    "pytest",
]

# `excludes` above prunes the MODULE GRAPH. It does not touch the files
# collect_all() already harvested, so the bundle shipped all 155 Qt frameworks
# — Qt3D, Charts, DataVisualization, WebEngine, Designer — no matter what that
# list said. On macOS that is also a hard build failure, not just weight:
# PyInstaller writes a framework's top-level symlink before the versioned
# binary it points at, and COLLECT dies opening the dangling path
# (QtDesigner.framework is the first one to hit it).
#
# Dropping the payload for the excluded families makes `excludes` mean what it
# says and removes the frameworks that break the copy.
UNUSED_QT_PREFIXES = (
    "Qt3D",
    "QtBluetooth",
    "QtCharts",
    "QtDataVisualization",
    "QtDesigner",
    "QtHelp",
    "QtNfc",
    "QtPositioning",
    "QtTest",
    "QtWebEngine",
    "QtWebSockets",
)


def _is_unused_qt(entry):
    source = str(entry[0])

    # PySide6 ships three developer GUI applications — Assistant, Designer and
    # Linguist. collect_all() harvests them whole, and a nested .app inside
    # Frameworks also fails codesign, because its executable is a symlink.
    if ".app/" in source:
        return True

    return any(f"/{prefix}" in source for prefix in UNUSED_QT_PREFIXES)


binaries = [entry for entry in binaries if not _is_unused_qt(entry)]
datas = [entry for entry in datas if not _is_unused_qt(entry)]

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Tracyy",
    console=False,
    icon=str(ROOT / "icon" / ("Tracyy.icns" if IS_MACOS else "Tracyy.ico")),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Tracyy",
)

if IS_MACOS:
    app = BUNDLE(
        collection,
        name="Tracyy.app",
        icon=str(ROOT / "icon" / "Tracyy.icns"),
        bundle_identifier="com.tracyy.app",
        info_plist={
            "CFBundleName": "Tracyy",
            "CFBundleDisplayName": "Tracyy",
            "CFBundleShortVersionString": "2.1.0",
            "CFBundleVersion": "2.1.0",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            # Timecode chase reads an audio input, which macOS gates.
            "NSMicrophoneUsageDescription": (
                "Tracyy reads LTC timecode from an audio input to chase an "
                "external clock."
            ),
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Tracyy Project",
                    "CFBundleTypeRole": "Editor",
                    "LSHandlerRank": "Owner",
                    "CFBundleTypeIconFile": "TracyyProject.icns",
                    "LSItemContentTypes": ["com.tracyy.project"],
                }
            ],
            "UTExportedTypeDeclarations": [
                {
                    "UTTypeIdentifier": "com.tracyy.project",
                    "UTTypeDescription": "Tracyy Project",
                    "UTTypeConformsTo": ["public.data"],
                    "UTTypeTagSpecification": {"public.filename-extension": ["Tracyy"]},
                }
            ],
        },
    )

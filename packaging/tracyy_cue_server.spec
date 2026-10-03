# PyInstaller spec for the standalone LAN cue viewer.
#
# The viewer runs as its own process so a crash in the web server can never
# take the show playback down with it. It needs no Qt and no audio stack, so
# it stays a small single file.
#
#   pyinstaller --noconfirm --clean packaging/tracyy_cue_server.spec

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(ROOT / "tracyy" / "server" / "cue_viewer.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "icon" / "logo.png"), "icon")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "shiboken6",
        "numpy",
        "av",
        "soundfile",
        "sounddevice",
        "soxr",
        "cryptography",
        "matplotlib",
        "tkinter",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="TracyyCueServer",
    console=False,
    strip=False,
    upx=False,
)

#!/bin/bash
# Tracyy V2 — macOS build.
#
# Produces dist/Tracyy.app plus the standalone cue-viewer executable inside
# the bundle's Resources. Run it from Finder or from a terminal; it always
# works relative to the Tracyy folder, not the shell's current directory.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "============================================================"
echo " TRACYY V2 — macOS BUILD"
echo "============================================================"

if [[ ! -f main.py ]]; then
    echo "[ERROR] Không tìm thấy main.py."
    exit 1
fi
if [[ ! -f license_config.enc ]]; then
    echo "[ERROR] Không tìm thấy license_config.enc."
    exit 1
fi
if [[ -f license_config.json ]]; then
    echo "[ERROR] Xoá license_config.json trước khi build (không được đóng gói)."
    exit 1
fi

PYTHON_BIN="${TRACYY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    # 3.11 first: it is what pyproject targets (requires-python >=3.11,
    # target-version py311) and what the show machine runs. Preferring 3.12
    # silently built against a Homebrew interpreter whose PySide6 cannot
    # resolve its own Qt plugin path, so the test step aborted before
    # PyInstaller ever ran. TRACYY_PYTHON still overrides.
    for candidate in python3.11 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "[ERROR] Không tìm thấy Python 3.11+.  brew install python@3.12"
    exit 1
fi

echo "[1/7] Môi trường build ($($PYTHON_BIN --version))..."
if [[ ! -x .venv-build/bin/python ]]; then
    "$PYTHON_BIN" -m venv .venv-build
fi
PYTHON="$PWD/.venv-build/bin/python"

echo "[2/7] Cài dependencies..."
"$PYTHON" -m pip install --upgrade --quiet pip setuptools wheel
"$PYTHON" -m pip install --quiet -r requirements-dev.txt

echo "[3/7] Kiểm tra mã nguồn..."
"$PYTHON" -m ruff check tracyy tools
"$PYTHON" -m pytest -q

echo "[4/7] Xoá build cũ..."
rm -rf build dist build_server dist_server license_cache.dat
find . -name __pycache__ -type d -prune -exec rm -rf {} +

echo "[5/7] Build Tracyy.app..."
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/tracyy.spec

echo "[6/7] Build TracyyCueServer..."
"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath dist_server --workpath build_server \
    packaging/tracyy_cue_server.spec
cp dist_server/TracyyCueServer "dist/Tracyy.app/Contents/MacOS/TracyyCueServer"
chmod +x "dist/Tracyy.app/Contents/MacOS/TracyyCueServer"
rm -rf dist_server build_server

echo "[7/7] Kiểm tra bundle..."
# Wheels and the icon folder carry extended attributes, and codesign refuses a
# bundle with "resource fork, Finder information, or similar detritus".
xattr -cr "dist/Tracyy.app"
codesign --force --deep --sign - "dist/Tracyy.app"
codesign --verify --deep --strict "dist/Tracyy.app"
plutil -p "dist/Tracyy.app/Contents/Info.plist" | grep -i microphone || true
lipo -archs "dist/Tracyy.app/Contents/MacOS/Tracyy" || true

echo
echo "============================================================"
echo " BUILD THÀNH CÔNG"
echo "============================================================"
echo "App: $PWD/dist/Tracyy.app"
echo
echo "Chạy thử với latency thấp (sound card có dây):"
echo "  TRACYY_AUDIO_LATENCY=0.015 dist/Tracyy.app/Contents/MacOS/Tracyy"
echo
echo "Ra Bluetooth/AirPods thì app tự nâng sàn theo thiết bị — không cần đổi gì."
echo "============================================================"
open dist

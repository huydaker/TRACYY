#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

APP_PATH="$(pwd)/dist/Tracyy.app"
STAGE_DIR="$(pwd)/dmg_stage"
OUTPUT_DMG="$(pwd)/Tracyy-Installer.dmg"

if [ ! -d "$APP_PATH" ]; then
    echo "[ERROR] Không tìm thấy dist/Tracyy.app."
    echo "Hãy chạy ./build_macos.command trước."
    exit 1
fi

rm -rf "$STAGE_DIR"
rm -f "$OUTPUT_DMG"

mkdir -p "$STAGE_DIR"
cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

hdiutil create \
    -volname "Tracyy Installer" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    "$OUTPUT_DMG"

rm -rf "$STAGE_DIR"

echo
echo "DMG đã tạo:"
echo "$OUTPUT_DMG"
open -R "$OUTPUT_DMG" || true

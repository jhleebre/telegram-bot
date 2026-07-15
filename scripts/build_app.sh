#!/bin/bash
#
# Build "Context Bot.app" — a macOS app bundle you can drag to the Dock.
# It launches the project's venv Python + run.py (no Terminal needed).
#
# Usage:
#   ./scripts/build_app.sh            # builds into the project folder
#   ./scripts/build_app.sh ~/Applications   # builds into a chosen folder
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENVPY="$PROJECT_DIR/.venv/bin/python"
DEST_DIR="${1:-$PROJECT_DIR}"
APP="$DEST_DIR/Context Bot.app"
BUILD="$PROJECT_DIR/build"

if [ ! -x "$VENVPY" ]; then
  echo "error: venv not found at $VENVPY" >&2
  echo "  create it first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "▶ Project : $PROJECT_DIR"
echo "▶ Output  : $APP"
mkdir -p "$BUILD"

# 1) Render the icon PNG (offscreen so it works over SSH / no display).
QT_QPA_PLATFORM=offscreen "$VENVPY" "$PROJECT_DIR/scripts/make_icon.py" "$BUILD/icon.png"

# 2) Build the .icns from the PNG.
ICONSET="$BUILD/ContextBot.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
gen() { sips -z "$1" "$1" "$BUILD/icon.png" --out "$ICONSET/$2" >/dev/null; }
gen 16   icon_16x16.png
gen 32   icon_16x16@2x.png
gen 32   icon_32x32.png
gen 64   icon_32x32@2x.png
gen 128  icon_128x128.png
gen 256  icon_128x128@2x.png
gen 256  icon_256x256.png
gen 512  icon_256x256@2x.png
gen 512  icon_512x512.png
gen 1024 icon_512x512@2x.png
iconutil -c icns "$ICONSET" -o "$BUILD/ContextBot.icns"

# 3) Assemble the bundle.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/ContextBot.icns" "$APP/Contents/Resources/ContextBot.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Context Bot</string>
    <key>CFBundleDisplayName</key>     <string>Context Bot</string>
    <key>CFBundleIdentifier</key>      <string>com.contextbot.app</string>
    <key>CFBundleVersion</key>         <string>0.1.0</string>
    <key>CFBundleShortVersionString</key> <string>0.1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>ContextBot</string>
    <key>CFBundleIconFile</key>        <string>ContextBot</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
</dict>
</plist>
PLIST

# The launcher: cd into the project (so .env is found) and run the app.
cat > "$APP/Contents/MacOS/ContextBot" <<LAUNCH
#!/bin/bash
cd "$PROJECT_DIR"
exec "$VENVPY" "$PROJECT_DIR/run.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/ContextBot"

# Refresh Finder's icon cache for the new bundle.
touch "$APP"

echo "✅ Built: $APP"
echo
echo "다음 단계:"
echo "  1) Finder에서 '$APP' 을 더블클릭해 실행되는지 확인"
echo "  2) 실행 중일 때 Dock 아이콘 우클릭 → 옵션 → 'Dock에 유지' 로 고정"
echo "     (또는 '$APP' 을 Dock 오른쪽 영역으로 드래그)"

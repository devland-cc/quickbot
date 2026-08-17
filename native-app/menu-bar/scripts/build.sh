#!/bin/bash
# Builds Quickbot.app and installs it into ~/Applications.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
APP="$BUILD/Quickbot.app"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Applications}"

echo "==> Cleaning previous build"
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Generating icons (lucide bot / bot-off)"
if command -v rsvg-convert >/dev/null 2>&1; then
  for icon in bot bot-off; do
    rsvg-convert -f pdf -w 36 -h 36 "$ROOT/icons/$icon.svg" -o "$ROOT/icons/$icon.pdf"
  done
else
  echo "    rsvg-convert not found; using the versioned PDFs"
fi
cp "$ROOT/icons/bot.pdf" "$ROOT/icons/bot-off.pdf" "$APP/Contents/Resources/"

echo "==> Compiling (Swift, arm64)"
swiftc -O \
  -target arm64-apple-macos13.0 \
  -framework Cocoa \
  -o "$APP/Contents/MacOS/Quickbot" \
  "$ROOT/Sources/Icons.swift" \
  "$ROOT/Sources/SwitchControl.swift" \
  "$ROOT/Sources/ServerController.swift" \
  "$ROOT/Sources/main.swift"

echo "==> Writing Info.plist"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Quickbot</string>
    <key>CFBundleDisplayName</key>
    <string>Quickbot</string>
    <key>CFBundleIdentifier</key>
    <string>com.quickbot.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>Quickbot</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo "==> Signing (ad-hoc)"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "    ad-hoc signing failed (not critical)"

echo "==> Installing into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Quit the old instance before replacing it
pkill -f "$INSTALL_DIR/Quickbot.app/Contents/MacOS/Quickbot" 2>/dev/null || true
sleep 0.5
rm -rf "$INSTALL_DIR/Quickbot.app"
cp -R "$APP" "$INSTALL_DIR/"

echo
echo "OK: $INSTALL_DIR/Quickbot.app"
echo "Open with: open \"$INSTALL_DIR/Quickbot.app\""

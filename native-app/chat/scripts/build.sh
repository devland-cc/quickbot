#!/bin/bash
# Builds Quickbot Chat.app and installs it into ~/Applications.
#
# Uses SwiftPM only — no Xcode required. The build pins the macOS 26.5 SDK
# when available because newer SDKs turn SwiftUI property wrappers into
# compiler macros whose plugins ship only with Xcode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/build/Quickbot Chat.app"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Applications}"

PINNED_SDK="/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"
if [ -d "$PINNED_SDK" ] && ! xcrun --find actool >/dev/null 2>&1; then
  export SDKROOT="$PINNED_SDK"
  echo "==> Building (SwiftPM, SDK pinned to MacOSX26.5)"
else
  echo "==> Building (SwiftPM)"
fi
swift build -c release --package-path "$ROOT"
BIN="$(swift build -c release --package-path "$ROOT" --show-bin-path)/QuickbotChat"

echo "==> Regenerating icons (if rsvg-convert is available)"
if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w 512 -h 512 "$ROOT/icons/logo-nobg.svg" -o "$ROOT/icons/logo-nobg.png"
  rm -rf "$ROOT/icons/AppIcon.iconset"
  mkdir -p "$ROOT/icons/AppIcon.iconset"
  for s in 16 32 128 256 512; do
    rsvg-convert -w "$s" -h "$s" "$ROOT/icons/appicon.svg" -o "$ROOT/icons/AppIcon.iconset/icon_${s}x${s}.png"
    d=$((s * 2))
    rsvg-convert -w "$d" -h "$d" "$ROOT/icons/appicon.svg" -o "$ROOT/icons/AppIcon.iconset/icon_${s}x${s}@2x.png"
  done
  iconutil -c icns "$ROOT/icons/AppIcon.iconset" -o "$ROOT/icons/AppIcon.icns"
  rm -rf "$ROOT/icons/AppIcon.iconset"
else
  echo "    rsvg-convert not found; using the versioned icons"
fi

echo "==> Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Quickbot Chat"
cp "$ROOT/icons/AppIcon.icns" "$ROOT/icons/logo-nobg.png" "$APP/Contents/Resources/"

# SwiftPM packages with resources (e.g. KeyboardShortcuts localizations)
# produce .bundle directories that Bundle.module expects to find inside
# the app's Resources; missing bundles crash at first access.
BIN_DIR="$(dirname "$BIN")"
for bundle in "$BIN_DIR"/*.bundle; do
  [ -e "$bundle" ] && cp -R "$bundle" "$APP/Contents/Resources/"
done

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Quickbot Chat</string>
    <key>CFBundleDisplayName</key>
    <string>Quickbot Chat</string>
    <key>CFBundleIdentifier</key>
    <string>com.quickbot.chat</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>Quickbot Chat</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>Quickbot Chat lets you talk to the model with your voice.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Quickbot Chat uses the microphone for voice conversations.</string>
    <key>NSAccessibilityUsageDescription</key>
    <string>Quickbot Chat can perform operations on selected text such as fixing grammar, extending text, and custom commands.</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsArbitraryLoads</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST

echo "==> Signing (ad-hoc)"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "    ad-hoc signing failed (not critical)"

echo "==> Installing into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Quit the old instance before replacing it
pkill -f "$INSTALL_DIR/Quickbot Chat.app/Contents/MacOS/Quickbot Chat" 2>/dev/null || true
sleep 0.5
rm -rf "$INSTALL_DIR/Quickbot Chat.app"
cp -R "$APP" "$INSTALL_DIR/"

echo
echo "OK: $INSTALL_DIR/Quickbot Chat.app"
echo "Open with: open \"$INSTALL_DIR/Quickbot Chat.app\""

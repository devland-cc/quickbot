#!/bin/bash
# Builds the distributable Quickbot release tarball.
#
# Produces build/release/quickbot-<version>.tar.gz containing:
#   Quickbot.app        (menu bar app, with the server component bundled
#                        in Contents/Resources/server)
#   Quickbot Chat.app
#
# The models are NOT in the tarball: `quickbot setup` downloads them from
# their original Hugging Face repositories on the user's machine.
#
# Usage: scripts/release.sh <version>   e.g. scripts/release.sh 0.1.0
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGING="$ROOT/build/release"

echo "==> Staging release $VERSION in $STAGING"
rm -rf "$STAGING"
mkdir -p "$STAGING"

INSTALL_DIR="$STAGING" "$ROOT/native-app/menu-bar/scripts/build.sh"
INSTALL_DIR="$STAGING" "$ROOT/native-app/chat/scripts/build.sh"

echo "==> Bundling the server component into Quickbot.app"
SERVER_DEST="$STAGING/Quickbot.app/Contents/Resources/server"
mkdir -p "$SERVER_DEST"
cp "$ROOT/server/serverctl" "$ROOT/server/serverctl.py" \
   "$ROOT/server/toolproxy.py" "$ROOT/server/websearch.py" \
   "$ROOT/server/requirements.lock" "$ROOT/server/README.md" "$SERVER_DEST/"

echo "==> Compiling webkit-fetch (web search helper)"
swiftc -O -framework WebKit \
  -o "$SERVER_DEST/webkit-fetch" "$ROOT/server/webkit-fetch.swift"

echo "==> Stamping version $VERSION"
for app in "Quickbot.app" "Quickbot Chat.app"; do
  plist="$STAGING/$app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$plist"
done

echo "==> Re-signing (ad-hoc)"
for app in "Quickbot.app" "Quickbot Chat.app"; do
  codesign --force --deep --sign - "$STAGING/$app"
done

echo "==> Creating tarball"
TARBALL="$STAGING/quickbot-$VERSION.tar.gz"
tar -czf "$TARBALL" -C "$STAGING" "Quickbot.app" "Quickbot Chat.app"

echo
echo "OK: $TARBALL"
shasum -a 256 "$TARBALL"

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

# Private Python runtime bundled with the app (python-build-standalone).
# The serverctl shim unpacks it into the user's data dir on first run, so
# the cask no longer installs Python on the user's machine.
PBS_TAG="20260814"
PBS_PY="3.12.14"
PBS_SHA256="4572133a5542f306b9bdb155da5800f9e38950cd0a98d469b832ce256fe299ea"
PBS_FILE="cpython-${PBS_PY}+${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_FILE}"

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

echo "==> Bundling the Python runtime (CPython $PBS_PY)"
CACHE="$ROOT/build/cache"
mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PBS_FILE" ]; then
  curl -fL -o "$CACHE/$PBS_FILE" "$PBS_URL"
fi
if ! echo "$PBS_SHA256  $CACHE/$PBS_FILE" | shasum -a 256 -c - >/dev/null; then
  rm -f "$CACHE/$PBS_FILE"
  echo "Checksum mismatch for $PBS_FILE (bad file removed; re-run)" >&2
  exit 1
fi
cp "$CACHE/$PBS_FILE" "$SERVER_DEST/python-runtime.tar.gz"
echo "$PBS_PY+$PBS_TAG" > "$SERVER_DEST/python-runtime.version"

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

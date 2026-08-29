#!/usr/bin/env bash
# Build the first-run setup window as a signed .app.
#
#   ./build.sh --sign "Developer ID Application: You (TEAMID)"
#   ./build.sh                      # unsigned: fine, it needs no permissions
#
# Unlike the MusicKit helper, nothing here depends on the signature: this
# process draws a window, prints JSON and exits. It is signed only so the
# containing app's signature stays valid, and so Gatekeeper sees one coherent
# bundle rather than an unsigned executable inside a signed app.
#
# It IS a real .app rather than a bare binary, because AppKit needs a bundle to
# put a window on screen properly -- an activation policy, a name in the menu
# bar, and focus when it opens.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Single source of truth: src/applemusic_mcp/ipc.py. A fork changes it in ONE
# place and every build artefact follows. Hardcoding it here was how the same
# string ended up in five files: the launchd label, the socket path and the TCC
# row all key on it, so a fork that misses one collides with the original.
# NB: the script has already cd'd to its own directory above, so the repo
# root is simply ../.. from here. Re-deriving it from ${BASH_SOURCE[0]} is
# wrong AFTER that cd: BASH_SOURCE is whatever the caller typed, so an
# invocation like ./swift/amcp-setup/build.sh from the repo root resolves
# against the new cwd and points somewhere that does not exist.
_ipc_bundle_id() {
  sed -nE 's/^BUNDLE_ID = "(.*)"/\1/p' "$1/src/applemusic_mcp/ipc.py" | head -1
}
BUNDLE_ID="${BUNDLE_ID:-$(_ipc_bundle_id "$(cd ../.. && pwd)").setup}"
APP="AMCPSetup.app"
SIGN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN="${2:?--sign needs an identity}"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Universal, for the same reason as the MusicKit helper: the app is now built
# per-architecture, and a single-arch nested helper would leave the Intel build
# with a setup wizard that cannot launch.
swiftc -O -target arm64-apple-macosx12.0  -o amcp-setup-arm64  main.swift
swiftc -O -target x86_64-apple-macosx12.0 -o amcp-setup-x86_64 main.swift
lipo -create -output amcp-setup amcp-setup-arm64 amcp-setup-x86_64
rm -f amcp-setup-arm64 amcp-setup-x86_64

rm -rf "$APP"
mkdir -p "${APP}/Contents/MacOS"
mv amcp-setup "${APP}/Contents/MacOS/AMCPSetup"

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleName</key><string>Unofficial Apple Music MCP</string>
    <key>CFBundleDisplayName</key><string>Unofficial Apple Music MCP</string>
    <key>CFBundleExecutable</key><string>AMCPSetup</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>CFBundleIconFile</key><string>AppleMusicMCP</string>
</dict>
</plist>
PLIST
plutil -lint "${APP}/Contents/Info.plist" >/dev/null

# The wizard runs with a .regular activation policy, so it gets its own Dock
# tile while it is on screen. Without the icon that tile is a blank page, which
# is the first thing a new user sees.
ICON="../../tools/icon/AppleMusicMCP.icns"
if [[ -f "$ICON" ]]; then
  mkdir -p "${APP}/Contents/Resources"
  cp "$ICON" "${APP}/Contents/Resources/AppleMusicMCP.icns"
fi

if [[ -n "$SIGN" ]]; then
  codesign --force --timestamp --options runtime --sign "$SIGN" "$APP"
  codesign --verify --strict "$APP"
  echo "built and signed: $(pwd)/$APP"
else
  echo "built (unsigned): $(pwd)/$APP"
fi

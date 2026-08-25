#!/usr/bin/env bash
# Build the MusicKit helper as a signed .app.
#
#   ./build.sh --sign "Developer ID Application: You (TEAMID)"
#   ./build.sh                      # unsigned: builds, but `add` will be denied
#
# Three things are required for MusicKit to authorise the call, and it took a
# while to establish which (see docs/PERMISSIONS.md):
#
#   1. A real .app bundle — an Info.plist carrying the bundle id and
#      NSAppleMusicUsageDescription. A bare Mach-O has no identity and is denied.
#   2. A Developer ID signature from a team whose App ID has MusicKit enabled.
#      That App ID is what Apple checks, SERVER-SIDE.
#   3. Nothing else. In particular NO entitlements plist and NO provisioning
#      profile: MusicKit is one of the capabilities (with In-App Purchase,
#      ShazamKit, WeatherKit) that inject no profile entitlement. Requesting
#      `com.apple.developer.musickit` makes it strictly worse — nothing grants
#      it, so the kernel SIGKILLs the process at launch with "restricted
#      entitlements ... validation failed". Verified: removing the profile
#      entirely still returns HTTP 202.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Single source of truth: src/applemusic_mcp/ipc.py. A fork changes it in ONE
# place and every build artefact follows. Hardcoding it here was how the same
# string ended up in five files: the launchd label, the socket path and the TCC
# row all key on it, so a fork that misses one collides with the original.
_ipc_bundle_id() {
  sed -nE 's/^BUNDLE_ID = "(.*)"/\1/p' "$1/src/applemusic_mcp/ipc.py" | head -1
}
BUNDLE_ID="${BUNDLE_ID:-$(_ipc_bundle_id "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)")}"
APP="AMCPMusicKit.app"
SIGN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN="${2:?--sign needs an identity}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

swiftc -O -target arm64-apple-macosx14.0 -o amcp-musickit main.swift

rm -rf "$APP"
mkdir -p "${APP}/Contents/MacOS"
mv amcp-musickit "${APP}/Contents/MacOS/AMCPMusicKit"

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleName</key><string>AMCPMusicKit</string>
    <key>CFBundleExecutable</key><string>AMCPMusicKit</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <!-- Shown in the Apple Music permission prompt. Required: without it the
         request is denied rather than shown. -->
    <key>NSAppleMusicUsageDescription</key>
    <string>Adds the songs you ask for to your Apple Music library.</string>
</dict>
</plist>
PLIST
plutil -lint "${APP}/Contents/Info.plist" >/dev/null

if [[ -n "$SIGN" ]]; then
  codesign --force --options runtime --sign "$SIGN" "$APP"
  echo "built and signed: $(pwd)/$APP"
  "${APP}/Contents/MacOS/AMCPMusicKit" status
else
  echo "built (unsigned): $(pwd)/$APP"
  echo "  'add' will be denied until signed with a Developer ID from a team"
  echo "  whose App ID has MusicKit enabled."
fi

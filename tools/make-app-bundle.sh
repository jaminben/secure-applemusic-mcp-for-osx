#!/usr/bin/env bash
# Build a minimal .app bundle + LaunchAgent so macOS TCC attributes the
# Automation-for-Music grant to THIS server rather than to your terminal.
#
# Why a LaunchAgent and not just the bundle: TCC responsibility is inherited
# from the spawning process, so a bundle exec'd as a child of your terminal is
# still attributed to the terminal. launchd-started processes are responsible
# for themselves. See docs/PERMISSIONS.md.
#
# Usage:
#   tools/make-app-bundle.sh [--sign "My Local Signing Cert"] [--out DIR]
#
# Verify it worked: trigger a Music operation and read the permission prompt.
# The app NAMED IN THE PROMPT is the identity receiving the grant. If it names
# your terminal, the scoping did not take.

set -euo pipefail

APP_NAME="SecureAppleMusicMCP"
BUNDLE_ID="io.github.jaminben.secure-applemusic-mcp"
OUT_DIR="${HOME}/Applications"
SIGN_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    --out)  OUT_DIR="${2:?--out needs a directory}";   shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SERVER_BIN="$(command -v secure-applemusic-mcp || true)"
if [[ -z "$SERVER_BIN" ]]; then
  echo "error: secure-applemusic-mcp not found on PATH." >&2
  echo "       Install it first (uv sync, or pip install -e .), then re-run." >&2
  exit 1
fi

APP="${OUT_DIR}/${APP_NAME}.app"
mkdir -p "${APP}/Contents/MacOS"

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleName</key><string>${APP_NAME}</string>
    <key>CFBundleExecutable</key><string>${APP_NAME}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <!-- No Dock icon, no menu bar: this is a background helper. -->
    <key>LSUIElement</key><true/>
    <!-- Shown verbatim in the Automation permission prompt. -->
    <key>NSAppleEventsUsageDescription</key>
    <string>Controls Music.app to manage your library, playlists, and playback.</string>
</dict>
</plist>
PLIST

cat > "${APP}/Contents/MacOS/${APP_NAME}" <<LAUNCHER
#!/bin/sh
# Exec the server in place so the bundle identity is the running process.
exec "${SERVER_BIN}" serve "\$@"
LAUNCHER
chmod +x "${APP}/Contents/MacOS/${APP_NAME}"

if [[ -n "$SIGN_ID" ]]; then
  codesign --force --deep --sign "$SIGN_ID" "$APP"
  echo "signed with: $SIGN_ID"
else
  cat <<'WARN'

WARNING: not signed. TCC keys its entry on the code-signing identity, so an
unsigned (or ad-hoc signed) bundle gets a new identity on every rebuild —
macOS will re-prompt, or silently deny using a stale entry. For anything
you intend to keep, create a self-signed certificate in Keychain Access and
re-run with:  --sign "My Local Signing Cert"

WARN
fi

echo "built: $APP"
cat <<PLIST

--- LaunchAgent -------------------------------------------------------------
Save as ~/Library/LaunchAgents/${BUNDLE_ID}.plist, then:
    launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/${BUNDLE_ID}.plist

<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array><string>${APP}/Contents/MacOS/${APP_NAME}</string></array>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
-----------------------------------------------------------------------------

NOTE: stdio MCP needs the CLIENT to own stdin/stdout, so a launchd-started
helper cannot serve the client directly. Finishing this requires the
shim/helper split described in docs/PERMISSIONS.md — a stdio shim the client
spawns (no permissions) forwarding to this helper over a unix socket. The
bundle and LaunchAgent above are the half that determines TCC identity.

To check the grant landed on the app and not your terminal:
    tccutil reset AppleEvents ${BUNDLE_ID}   # clear and re-prompt
    open "System Settings" -> Privacy & Security -> Automation
PLIST

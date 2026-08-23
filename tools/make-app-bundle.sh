#!/usr/bin/env bash
# Build (and optionally install) the .app bundle + LaunchAgent that give this
# server its OWN macOS permission identity.
#
# Why this exists: macOS attributes a TCC grant to the *responsible process*,
# which for a server your MCP client spawns is the client — so approving
# "Automation → Music" the naive way grants Music control to everything you run
# from that terminal. Responsibility is inherited from the spawning process, so
# a bundle exec'd by your terminal is still attributed to your terminal.
# launchd-started processes are responsible for themselves, which is why the
# server is split: a permissionless stdio shim your client spawns, talking over
# a 0600 unix socket to a resident helper that launchd starts from this bundle.
#
# Usage:
#   tools/make-app-bundle.sh [--sign "Identity"] [--out DIR] [--install] [--uninstall]
#
#   --install    also load the LaunchAgent and print the MCP client config
#   --uninstall  unload the agent and remove the bundle + plist
#
# Verify it worked: trigger a Music operation and read the permission prompt.
# The app NAMED IN THE PROMPT is the identity receiving the grant. If it names
# your terminal, the scoping did not take.

set -euo pipefail

APP_NAME="SecureAppleMusicMCP"
BUNDLE_ID="io.github.jaminben.secure-applemusic-mcp"   # keep in sync with ipc.py
OUT_DIR="${HOME}/Applications"
PLIST="${HOME}/Library/LaunchAgents/${BUNDLE_ID}.plist"
LOG_DIR="${HOME}/Library/Logs/${BUNDLE_ID}"
SIGN_ID=""
DO_INSTALL=0

# launchctl bootout returns before teardown finishes, so a bootout immediately
# followed by a bootstrap can fail with "service already loaded". Wait for the
# service to actually disappear (bounded) instead of racing it.
unload_and_wait() {
  launchctl bootout "gui/$(id -u)/${BUNDLE_ID}" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "gui/$(id -u)/${BUNDLE_ID}" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  echo "warning: ${BUNDLE_ID} is still registered with launchd after 10s" >&2
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign)      SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    --out)       OUT_DIR="${2:?--out needs a directory}";   shift 2 ;;
    --install)   DO_INSTALL=1; shift ;;
    --uninstall)
      unload_and_wait || true
      rm -f "$PLIST"
      rm -rf "${OUT_DIR}/${APP_NAME}.app"
      echo "Removed the LaunchAgent and bundle."
      echo "To revoke the permission too:  tccutil reset AppleEvents ${BUNDLE_ID}"
      exit 0 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: macOS only." >&2; exit 1
fi

SERVER_BIN="$(command -v secure-applemusic-mcp || true)"
if [[ -z "$SERVER_BIN" ]]; then
  echo "error: secure-applemusic-mcp not found on PATH." >&2
  echo "       Install it first (./install.sh, or uv sync / pip install .)." >&2
  exit 1
fi
# Resolve through symlinks so the bundle doesn't depend on a shim that may move.
SERVER_BIN="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$SERVER_BIN")"

APP="${OUT_DIR}/${APP_NAME}.app"
mkdir -p "${APP}/Contents/MacOS" "$LOG_DIR"
chmod 700 "$LOG_DIR"

cat > "${APP}/Contents/Info.plist" <<PLIST_EOF
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
PLIST_EOF

cat > "${APP}/Contents/MacOS/${APP_NAME}" <<LAUNCHER_EOF
#!/bin/sh
# exec (not a subshell) so the bundle's identity IS the running helper process.
exec "${SERVER_BIN}" helper "\$@"
LAUNCHER_EOF
chmod 755 "${APP}/Contents/MacOS/${APP_NAME}"

if [[ -n "$SIGN_ID" ]]; then
  codesign --force --deep --sign "$SIGN_ID" "$APP"
  echo "Signed with: $SIGN_ID"
  codesign -dv "$APP" 2>&1 | sed -n '1,3p'
else
  cat >&2 <<'WARN'

WARNING: bundle is not signed.

TCC keys its entry on the code-signing identity, so an unsigned (or ad-hoc
signed) bundle presents a new identity whenever the binary changes — macOS then
re-prompts, or silently denies using a stale entry. Fine for a first try; for
anything you intend to keep, create a self-signed certificate in Keychain
Access (Certificate Assistant -> Create a Certificate, type "Code Signing")
and re-run with:  --sign "My Local Signing Cert"

WARN
fi

cat > "$PLIST" <<AGENT_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array><string>${APP}/Contents/MacOS/${APP_NAME}</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>${LOG_DIR}/helper.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/helper.log</string>
</dict>
</plist>
AGENT_EOF
chmod 644 "$PLIST"

echo "Built:  $APP"
echo "Agent:  $PLIST"

if [[ "$DO_INSTALL" -eq 1 ]]; then
  unload_and_wait || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Loaded the LaunchAgent."
  for _ in $(seq 1 40); do
    [[ -S "${HOME}/Library/Application Support/${BUNDLE_ID}/helper.sock" ]] && break
    sleep 0.25
  done
  if [[ -S "${HOME}/Library/Application Support/${BUNDLE_ID}/helper.sock" ]]; then
    echo "Helper is listening."
  else
    echo "Helper did not come up. Check: ${LOG_DIR}/helper.log" >&2
    exit 1
  fi
fi

cat <<CONFIG

--- MCP client configuration ---------------------------------------------
Point your client at the SHIM, never at the helper. The shim holds no
permissions; the helper (started by launchd from the bundle above) owns the
Automation grant.

{
  "mcpServers": {
    "apple-music": {
      "command": "${SERVER_BIN}",
      "args": ["shim"]
    }
  }
}

Manage the helper:
    launchctl kickstart -k gui/$(id -u)/${BUNDLE_ID}    # restart
    launchctl bootout    gui/$(id -u)/${BUNDLE_ID}      # stop
    tail -f ${LOG_DIR}/helper.log                       # logs

Check the grant landed on the BUNDLE and not your terminal:
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
  You want a "${APP_NAME}" row there, with Music enabled. If instead your
  terminal has the Music row, the scoping did not take — see docs/PERMISSIONS.md.
    tccutil reset AppleEvents ${BUNDLE_ID}              # clear and re-prompt
--------------------------------------------------------------------------
CONFIG

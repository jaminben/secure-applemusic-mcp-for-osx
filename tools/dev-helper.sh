#!/usr/bin/env bash
# Development helper: runs the server straight from your working tree.
#
# The installed app vendors its own Python and its own copy of the code, so
# testing a change there costs a rebuild (~40s), a reinstall, a launchd restart
# and a client reconnect. This runs the SAME helper out of the source checkout
# instead, under its own bundle identity and its own socket, so it never
# collides with the installed app.
#
# The loop it gives you:
#
#     edit source  ->  tools/mcp-call --dev <tool> <args>     (~1.5s)
#
# No rebuild and no helper restart: the helper imports the server module inside
# the forked child, so every new connection picks up the current source. Each
# mcp-call is a new connection.
#
# Usage:
#   tools/dev-helper.sh start [--sign "Identity"]
#   tools/dev-helper.sh stop | restart | status | logs
#
# TCC: the dev bundle is a separate identity, so macOS asks for Automation
# permission for it once, separately from the installed app. That is the point —
# your terminal still gets nothing.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_ID="io.github.jaminben.amcp-dev"
APP_NAME="AppleMusicMCP-Dev"
APP="${HOME}/Applications/${APP_NAME}.app"
PLIST="${HOME}/Library/LaunchAgents/${DEV_ID}.plist"
LOG_DIR="${HOME}/Library/Logs/${DEV_ID}"
SOCKET="${HOME}/Library/Application Support/${DEV_ID}/helper.sock"
TARGET="gui/$(id -u)/${DEV_ID}"
SIGN_ID=""

CMD="${1:-status}"; shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

unload_and_wait() {
  launchctl bootout "$TARGET" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "$TARGET" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  return 1
}

case "$CMD" in
  stop)
    unload_and_wait || echo "warning: still registered" >&2
    echo "dev helper stopped"
    exit 0 ;;

  status)
    if launchctl print "$TARGET" >/dev/null 2>&1; then
      launchctl print "$TARGET" 2>/dev/null | grep -E "state =|pid =" | sed 's/^[[:space:]]*/  /'
      [[ -S "$SOCKET" ]] && echo "  socket = $SOCKET" || echo "  socket = MISSING"
    else
      echo "  not loaded (tools/dev-helper.sh start)"
    fi
    exit 0 ;;

  logs)
    exec tail -f "${LOG_DIR}/helper.log" ;;

  start|restart) ;;
  *) sed -n '2,25p' "$0"; exit 0 ;;
esac

SERVER_BIN="${REPO}/.venv/bin/secure-applemusic-mcp"
if [[ ! -x "$SERVER_BIN" ]]; then
  echo "error: $SERVER_BIN not found — run 'uv sync --all-extras --dev' first." >&2
  exit 1
fi

mkdir -p "${APP}/Contents/MacOS" "$LOG_DIR" "$(dirname "$SOCKET")"
chmod 700 "$LOG_DIR" "$(dirname "$SOCKET")"

# The launcher pins the dev socket so the dev helper can never bind the
# installed app's socket, whatever the environment says.
cat > "${APP}/Contents/MacOS/${APP_NAME}" <<LAUNCHER
#!/bin/sh
export APPLEMUSIC_MCP_SOCKET="${SOCKET}"
exec "${SERVER_BIN}" "\$@"
LAUNCHER
chmod 755 "${APP}/Contents/MacOS/${APP_NAME}"

cat > "${APP}/Contents/Info.plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>${DEV_ID}</string>
    <key>CFBundleName</key><string>${APP_NAME}</string>
    <key>CFBundleExecutable</key><string>${APP_NAME}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.0.0-dev</string>
    <key>LSUIElement</key><true/>
    <key>NSAppleEventsUsageDescription</key>
    <string>Apple Music MCP (development build) controls Music.app for testing.</string>
</dict>
</plist>
PLIST_EOF
plutil -lint "${APP}/Contents/Info.plist" >/dev/null

if [[ -n "$SIGN_ID" ]]; then
  codesign --force --deep --timestamp=none --sign "$SIGN_ID" "$APP" 2>/dev/null \
    && echo "signed dev bundle"
fi

cat > "$PLIST" <<AGENT_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${DEV_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP}/Contents/MacOS/${APP_NAME}</string>
        <string>helper</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>${LOG_DIR}/helper.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/helper.log</string>
</dict>
</plist>
AGENT_EOF
chmod 644 "$PLIST"

unload_and_wait || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

for _ in $(seq 1 40); do [[ -S "$SOCKET" ]] && break; sleep 0.25; done
if [[ ! -S "$SOCKET" ]]; then
  echo "error: dev helper did not come up. Log:" >&2
  tail -20 "${LOG_DIR}/helper.log" >&2 || true
  exit 1
fi

cat <<DONE
dev helper running
  source   ${REPO}
  socket   ${SOCKET}
  logs     ${LOG_DIR}/helper.log

  tools/mcp-call --dev playback action=now_playing
  tools/dev-helper.sh logs | stop | restart

Most edits are live on the NEXT call: the server module is imported inside the
forked child, so each mcp-call picks up current source.

Restart after editing a module the PARENT holds, because a fork inherits its
already-imported copy:

  helper.py, ipc.py, __init__.py   always held
  paths.py, update_check.py,       held once the daily update check has run,
  applescript.py                   so this varies by how long the helper is up

If a change does not seem to take effect, restart before debugging it — that
ambiguity costs more time than the restart does.
DONE

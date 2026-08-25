#!/usr/bin/env bash
#
# Return this Mac to the state a first-time user's machine is in, so the
# installer can be tested as they will experience it.
#
# Testing a first run is not something you get to do twice by accident. Once
# the LaunchAgent is installed, the config written and the Automation grant
# made, every subsequent launch is an UPGRADE — the wizard finds its work
# already done, macOS has nothing left to ask, and the run tells you nothing
# about what a stranger sees. Worse, it tells you that quietly: a granted
# permission produces no dialog, which is indistinguishable from a permission
# step that never ran. Undoing all of it by hand is a dozen commands across
# launchd, five client configs, TCC and three directories, and forgetting any
# one of them silently invalidates the test.
#
# What it removes:
#   * both LaunchAgents (main + dev helper), stopped then unregistered
#   * the installed apps in /Applications and ~/Applications
#   * our entry in every detected MCP client config (theirs are left alone)
#   * the TCC grants, so macOS asks again
#   * ~/.config/applemusic-mcp, ~/.cache/applemusic-mcp and the logs
#
# What it does NOT touch: dist/ (that is the thing under test), the repo, or
# any other app's MCP servers.
#
# CREDENTIALS ARE BACKED UP FIRST, ALWAYS. ~/.config holds the MusicKit
# private key, and Apple lets you download a .p8 exactly once — losing it means
# revoking the key and issuing a new one. The backup is verified (byte-compare
# plus an openssl parse) BEFORE anything is deleted, and the script aborts if
# that fails. --restore puts it back.
#
# Usage:
#   ./scripts/reset-install.sh              # back up, then wipe
#   ./scripts/reset-install.sh --dry-run    # print what would go, change nothing
#   ./scripts/reset-install.sh --keep-creds # wipe, but leave ~/.config alone
#   ./scripts/reset-install.sh --restore    # copy the newest backup back
#
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

BUNDLE_ID="io.github.jaminben.secure-applemusic-mcp"
SETUP_ID="${BUNDLE_ID}.setup"
DEV_ID="io.github.jaminben.amcp-dev"
CONFIG_DIR="${HOME}/.config/applemusic-mcp"
CACHE_DIR="${HOME}/.cache/applemusic-mcp"
BACKUP_ROOT="${HOME}/.local/share/applemusic-mcp-backups"
PY="${PYTHON:-${REPO}/.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

DRY=0; KEEP_CREDS=0; RESTORE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY=1; shift ;;
    --keep-creds) KEEP_CREDS=1; shift ;;
    --restore)    RESTORE=1; shift ;;
    -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { if [[ $DRY -eq 1 ]]; then say "    would: $*"; else "$@" >/dev/null 2>&1 || true; fi }

# --- restore -----------------------------------------------------------------
if [[ $RESTORE -eq 1 ]]; then
  latest="$(ls -1d "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -1 || true)"
  [[ -n "$latest" ]] || { echo "no backup under ${BACKUP_ROOT}" >&2; exit 1; }
  step "Restoring credentials from ${latest}"
  mkdir -p "$CONFIG_DIR"; chmod 700 "$CONFIG_DIR"
  for f in "$latest"*; do
    [[ -f "$f" ]] || continue
    case "$(basename "$f")" in
      claude_desktop_config.json) continue ;;   # not ours to put back
    esac
    cp -a "$f" "$CONFIG_DIR/" && say "    restored $(basename "$f")"
  done
  exit 0
fi

say "──────────────────────────────────────────────────────────"
say " reset-install · back to a first-run machine"
[[ $DRY -eq 1 ]] && say " DRY RUN — nothing will be changed"
say "──────────────────────────────────────────────────────────"

# --- 1. back up credentials, and VERIFY, before touching anything ------------
step "[1/6] Backing up credentials"
if [[ -d "$CONFIG_DIR" ]]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  DEST="${BACKUP_ROOT}/${STAMP}"
  if [[ $DRY -eq 1 ]]; then
    say "    would copy ${CONFIG_DIR} -> ${DEST}"
  else
    mkdir -p "$DEST"; chmod 700 "$DEST"
    cp -a "$CONFIG_DIR/." "$DEST/"
    # Verify before we are allowed to delete the originals.
    fail=0
    for f in "$CONFIG_DIR"/*; do
      [[ -f "$f" ]] || continue
      b="${DEST}/$(basename "$f")"
      if ! cmp -s "$f" "$b"; then say "    ✗ MISMATCH $(basename "$f")"; fail=1; fi
    done
    for key in "$DEST"/*.p8; do
      [[ -f "$key" ]] || continue
      if openssl pkey -in "$key" -noout >/dev/null 2>&1; then
        say "    ✓ $(basename "$key") backed up and parses as a valid key"
      else
        say "    ✗ $(basename "$key") did NOT parse — refusing to continue"; fail=1
      fi
    done
    if [[ $fail -eq 1 ]]; then
      echo >&2
      echo "Backup verification failed. Nothing has been deleted." >&2
      exit 1
    fi
    say "    backup: ${DEST}"
  fi
else
  say "    (no ${CONFIG_DIR} — nothing to back up)"
fi

# --- 2. stop and unregister the agents ---------------------------------------
step "[2/6] Stopping background helpers"
for id in "$BUNDLE_ID" "$DEV_ID"; do
  if launchctl print "gui/$(id -u)/${id}" >/dev/null 2>&1; then
    say "    booting out ${id}"
    run launchctl bootout "gui/$(id -u)/${id}"
  else
    say "    ${id}: not loaded"
  fi
done
for id in "$BUNDLE_ID" "$DEV_ID"; do
  run rm -f "${HOME}/Library/LaunchAgents/${id}.plist"
done
[[ $DRY -eq 1 ]] || say "    LaunchAgent plists removed"

# --- 3. the installed apps ---------------------------------------------------
step "[3/6] Removing installed apps"
# Both names: the bundle was AppleMusicMCP.app before it was renamed, and an
# older install left in /Applications would otherwise survive a "clean" wipe and
# keep answering as the same bundle id.
for app in "/Applications/UnofficialAppleMusicMCP.app" \
           "/Applications/AppleMusicMCP.app" \
           "${HOME}/Applications/AppleMusicMCP-Dev.app"; do
  if [[ -d "$app" ]]; then say "    removing ${app}"; run rm -rf "$app"
  else say "    ${app}: absent"; fi
done
say "    (dist/ left alone — that is what you are testing)"

# --- 4. our entry in every client config --------------------------------------
step "[4/6] Removing our MCP entry from client configs"
if [[ $DRY -eq 1 ]]; then
  say "    would remove unofficial-apple-music / apple-music / apple-music-dev"
else
  "$PY" - "$REPO" <<'PYEOF' || say "    (skipped: could not import the package)"
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from applemusic_mcp import clients

KEYS = ("unofficial-apple-music", "apple-music", "apple-music-dev")
for c in clients.known_clients():
    p = c.config
    if not p.exists():
        continue
    raw = p.read_text(encoding="utf-8")
    removed = []
    if c.fmt == "json":
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            print(f"    ! {c.name}: unparseable, left untouched"); continue
        servers = data.get(c.servers_key)
        if not isinstance(servers, dict):
            continue
        for k in KEYS:
            if k in servers:
                removed.append(k); servers.pop(k)
        if not removed:
            continue
        p.with_suffix(p.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}").write_text(raw)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        new = raw
        for k in KEYS:
            hdr = f"[{c.servers_key}.{k}]"
            if hdr in new:
                removed.append(k)
                new = clients._replace_toml_section(new, hdr, "")
        if not removed:
            continue
        p.with_suffix(p.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}").write_text(raw)
        p.write_text(new, encoding="utf-8")
    print(f"    {c.name}: removed {', '.join(removed)} (backed up)")
PYEOF
fi

# --- 5. TCC ------------------------------------------------------------------
step "[5/6] Resetting permissions"
for id in "$BUNDLE_ID" "$SETUP_ID" "$DEV_ID"; do
  run tccutil reset All "$id"
  say "    reset All for ${id}"
done
say "    note: MusicKit (Media & Apple Music) may survive this — it is not a"
say "          plain per-bundle TCC row. Check the wizard's Apple Music page."

# --- 6. state directories -----------------------------------------------------
step "[6/6] Removing state"
if [[ $KEEP_CREDS -eq 1 ]]; then
  say "    keeping ${CONFIG_DIR} (--keep-creds)"
else
  run rm -rf "$CONFIG_DIR"; say "    removed ${CONFIG_DIR}"
fi
run rm -rf "$CACHE_DIR"; say "    removed ${CACHE_DIR}"
for id in "$BUNDLE_ID" "$DEV_ID"; do
  run rm -rf "${HOME}/Library/Logs/${id}"
done
say "    removed logs (a fresh setup.log proves the run is new)"

# --- verify -------------------------------------------------------------------
if [[ $DRY -eq 0 ]]; then
  step "Verifying"
  bad=0
  check() { # name, "PRESENT test"
    if eval "$2"; then printf '    ✗ %-34s STILL PRESENT\n' "$1"; bad=1
    else printf '    ✓ %-34s clean\n' "$1"; fi
  }
  check "agents running"      "launchctl list 2>/dev/null | grep -qi 'amcp\|applemusic'"
  check "LaunchAgent plists"  "ls ${HOME}/Library/LaunchAgents 2>/dev/null | grep -qi 'amcp\|applemusic'"
  check "/Applications app"   "[[ -d /Applications/UnofficialAppleMusicMCP.app ]]"
  check "/Applications (old)"  "[[ -d /Applications/AppleMusicMCP.app ]]"
  check "dev app"             "[[ -d ${HOME}/Applications/AppleMusicMCP-Dev.app ]]"
  check "cache dir"           "[[ -d ${CACHE_DIR} ]]"
  check "setup.log"           "[[ -f ${HOME}/Library/Logs/${BUNDLE_ID}/setup.log ]]"
  if [[ $KEEP_CREDS -eq 0 ]]; then
    check "config dir"        "[[ -d ${CONFIG_DIR} ]]"
  fi
  echo
  if [[ $bad -eq 1 ]]; then
    say "Some state survived — see above."; exit 1
  fi
  say "Clean. Build with:"
  say "    tools/build-app.sh --sign \"Developer ID Application: ...\""
  say "then open dist/UnofficialAppleMusicMCP.app and read:"
  say "    ~/Library/Logs/${BUNDLE_ID}/setup.log"
  say
  say "Credentials restore with:  ./scripts/reset-install.sh --restore"
fi

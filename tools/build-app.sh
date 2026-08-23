#!/usr/bin/env bash
# Build a fully standalone AppleMusicMCP.app — vendored Python included.
#
# The result needs nothing preinstalled: no Python, no pip, no Homebrew, no
# Xcode tools. Someone downloads the zip, drags the app to /Applications,
# double-clicks it once, and it installs its own background helper, registers
# itself with Claude Desktop, and asks for the Music permission under its own
# identity.
#
# Usage:
#   tools/build-app.sh [--sign "Identity"] [--arch arm64|x86_64] [--out DIR] [--zip]
#
# Layout produced:
#   AppleMusicMCP.app/Contents/
#     Info.plist
#     MacOS/AppleMusicMCP          launcher: no args -> setup, "helper" -> helper
#     Resources/python/            relocatable CPython (python-build-standalone)
#     Resources/lib/               this package + its dependencies
#
# Relocatable by construction: the launcher resolves its own path and no venv
# is used, so nothing embeds an absolute path. Drag the app anywhere.

set -euo pipefail

APP_NAME="AppleMusicMCP"
BUNDLE_ID="io.github.jaminben.secure-applemusic-mcp"   # keep in sync with ipc.py
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO}/dist"
SIGN_ID=""
MAKE_ZIP=0
ARCH="$(uname -m)"
PYVER="3.12"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    --arch) ARCH="${2:?--arch needs arm64 or x86_64}"; shift 2 ;;
    --out)  OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
    --zip)  MAKE_ZIP=1; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || { echo "error: macOS only." >&2; exit 1; }

VERSION="$(sed -nE 's/^version = "(.*)"/\1/p' "${REPO}/pyproject.toml" | head -1)"
APP="${OUT_DIR}/${APP_NAME}.app"
RES="${APP}/Contents/Resources"

echo "==> Building ${APP_NAME} ${VERSION} for ${ARCH}"
rm -rf "$APP"
mkdir -p "${APP}/Contents/MacOS" "${RES}"

# --- 1. vendor a relocatable CPython ----------------------------------------
# python-build-standalone builds are self-contained and relocatable, which is
# what makes the app work without a system Python. uv already manages exactly
# these, so reuse its download cache rather than fetching separately.
command -v uv >/dev/null || { echo "error: uv is required to fetch the vendored Python." >&2; exit 1; }

case "$ARCH" in
  arm64|aarch64) UV_ARCH="aarch64" ;;
  x86_64)        UV_ARCH="x86_64" ;;
  *) echo "error: unsupported arch $ARCH" >&2; exit 1 ;;
esac

uv python install "cpython-${PYVER}-macos-${UV_ARCH}-none" >/dev/null 2>&1 || true
# Resolve the MANAGED install directly. `uv python find` happily returns a
# system/Homebrew interpreter, which is NOT relocatable — vendoring one would
# produce an app that works here and breaks on someone else's machine.
UV_PY_DIR="$(uv python dir 2>/dev/null)"
PY_ROOT="$(ls -d "${UV_PY_DIR}"/cpython-${PYVER}*-macos-${UV_ARCH}-none 2>/dev/null | sort -V | tail -1 || true)"
if [[ -z "$PY_ROOT" || ! -x "${PY_ROOT}/bin/python3" ]]; then
  echo "error: no uv-managed CPython ${PYVER} for ${UV_ARCH} in ${UV_PY_DIR}." >&2
  echo "       Run:  uv python install cpython-${PYVER}-macos-${UV_ARCH}-none" >&2
  exit 1
fi
case "$PY_ROOT" in
  /usr/*|/opt/homebrew/*|/Library/*)
    echo "error: refusing to vendor a non-relocatable system Python: $PY_ROOT" >&2
    exit 1 ;;
esac
echo "    vendoring $PY_ROOT"
mkdir -p "${RES}/python"
# -L: resolve symlinks so the copy stands alone even if the source is pruned.
(cd "$PY_ROOT" && tar cf - bin lib include 2>/dev/null) | (cd "${RES}/python" && tar xf -)
# Drop what a runtime never needs; keeps the download roughly a third smaller.
rm -rf "${RES}/python/lib/python${PYVER}/test" \
       "${RES}/python/lib/python${PYVER}/idlelib" \
       "${RES}/python/lib/python${PYVER}/tkinter" \
       "${RES}/python/lib/python${PYVER}/turtledemo" \
       "${RES}/python/include" 2>/dev/null || true
find "${RES}/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

VENDORED_PY="${RES}/python/bin/python${PYVER}"
[[ -x "$VENDORED_PY" ]] || VENDORED_PY="${RES}/python/bin/python3"
[[ -x "$VENDORED_PY" ]] || { echo "error: vendored python missing" >&2; exit 1; }

# --- 2. install the package into a plain directory (no venv, no abs paths) ---
echo "==> Installing the package and dependencies"
"$VENDORED_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENDORED_PY" -m pip install --quiet --target "${RES}/lib" "${REPO}" \
  || { echo "error: pip install failed" >&2; exit 1; }
find "${RES}/lib" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# Console-script stubs bake in an absolute interpreter path; the launcher uses
# `-m applemusic_mcp` instead, so they would only be a stale trap.
rm -rf "${RES}/lib/bin" 2>/dev/null || true

# --- 2b. the MusicKit helper -------------------------------------------------
# A signed Swift .app nested inside ours. It is the only way to add a catalog
# track without shipping an Apple Music credential: MusicKit signs the API call
# from the app's own identity. It must be a real .app (Info.plist with a bundle
# id and NSAppleMusicUsageDescription) signed with Developer ID — an entitlement
# plist is NOT how this is granted, and requesting one gets the process killed.
HELPER_SRC="${REPO}/swift/amcp-musickit/AMCPMusicKit.app"
if [[ -d "$HELPER_SRC" ]]; then
  mkdir -p "${APP}/Contents/Helpers"
  ditto "$HELPER_SRC" "${APP}/Contents/Helpers/AMCPMusicKit.app"
  echo "    bundled the MusicKit helper"
else
  echo "    note: no MusicKit helper (swift/amcp-musickit/build.sh) — catalog add will" >&2
  echo "          need a developer token instead" >&2
fi

# The first-run wizard. Optional: without it setup falls back to a chain of
# plain dialogs, which works but is markedly worse.
SETUP_SRC="${REPO}/swift/amcp-setup/AMCPSetup.app"
if [[ -d "$SETUP_SRC" ]]; then
  mkdir -p "${APP}/Contents/Helpers"
  ditto "$SETUP_SRC" "${APP}/Contents/Helpers/AMCPSetup.app"
  echo "    bundled the setup wizard"
else
  echo "    note: no setup wizard (swift/amcp-setup/build.sh) — first run will use" >&2
  echo "          plain dialogs instead" >&2
fi

ICON_SRC="${REPO}/tools/icon/AppleMusicMCP.icns"
if [[ -f "$ICON_SRC" ]]; then
  mkdir -p "${RES}"
  cp "$ICON_SRC" "${RES}/AppleMusicMCP.icns"
  echo "    bundled the app icon"
fi

# --- 3. launcher + Info.plist ------------------------------------------------
cat > "${APP}/Contents/MacOS/${APP_NAME}" <<'LAUNCHER'
#!/bin/sh
# Resolve our own bundle so the app is relocatable (drag it anywhere).
here="$(cd "$(dirname "$0")" && pwd)"
res="$(cd "${here}/../Resources" && pwd)"
py="${res}/python/bin/python3"
export PYTHONPATH="${res}/lib"
export PYTHONDONTWRITEBYTECODE=1
export APPLEMUSIC_APP_BUNDLE="$(cd "${here}/../.." && pwd)"

# No arguments = a user double-clicked us = run first-run setup.
# "helper" = launchd started us = be the resident helper.
if [ "$#" -eq 0 ]; then
  exec "$py" -m applemusic_mcp app-setup
fi
exec "$py" -m applemusic_mcp "$@"
LAUNCHER
chmod 755 "${APP}/Contents/MacOS/${APP_NAME}"

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleName</key><string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key><string>Apple Music MCP</string>
    <key>CFBundleExecutable</key><string>${APP_NAME}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>CFBundleIconFile</key><string>AppleMusicMCP</string>
    <!-- Background agent: no Dock icon. Setup still shows native dialogs. -->
    <key>LSUIElement</key><true/>
    <!-- Shown verbatim in the Automation permission prompt. -->
    <key>NSAppleEventsUsageDescription</key>
    <string>Apple Music MCP controls Music.app to manage your library, playlists, and playback.</string>
</dict>
</plist>
PLIST

plutil -lint "${APP}/Contents/Info.plist" >/dev/null

# --- 4. sign -----------------------------------------------------------------
if [[ -n "$SIGN_ID" ]]; then
  echo "==> Signing"

  # Notarization has two hard requirements a local self-signed build cannot
  # meet: a secure timestamp (needs Apple's timestamp server and a real cert)
  # and the hardened runtime. So a Developer ID identity gets both, and a
  # self-signed one gets neither and stays usable offline.
  if [[ "$SIGN_ID" == *"Developer ID"* ]]; then
    SIGN_OPTS=(--timestamp --options runtime)
    echo "    Developer ID -> hardened runtime + secure timestamp (notarizable)"
  else
    SIGN_OPTS=(--timestamp=none)
    echo "    local identity -> no hardened runtime or timestamp (NOT notarizable)"
  fi

  # Inside-out, and deliberately NOT --deep. --deep re-signs nested code with
  # the OUTER bundle's options, which silently replaces the MusicKit helper's
  # own signature; Apple deprecated it for exactly that reason. Every Mach-O is
  # signed explicitly instead, deepest first.
  #
  # Detected with file(1) rather than by extension: the vendored CPython ships
  # executables with no suffix and .so files that are not all in one place, and
  # a missed binary is a notarization rejection several minutes later.
  signed=0
  while IFS= read -r -d '' macho; do
    codesign --force "${SIGN_OPTS[@]}" --sign "$SIGN_ID" "$macho" 2>/dev/null \
      && signed=$((signed + 1))
  done < <(
    find "$RES" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0 2>/dev/null \
      | while IFS= read -r -d '' f; do
          file -b "$f" | grep -q "Mach-O" && printf '%s\0' "$f"
        done
  )
  echo "    signed ${signed} nested Mach-O files"

  # The MusicKit helper is its own bundle: signed as a unit, after its contents
  # and before the bundle that contains it.
  for nested in AMCPMusicKit AMCPSetup; do
    if [[ -d "${APP}/Contents/Helpers/${nested}.app" ]]; then
      codesign --force "${SIGN_OPTS[@]}" --sign "$SIGN_ID" \
        "${APP}/Contents/Helpers/${nested}.app"
    fi
  done

  codesign --force "${SIGN_OPTS[@]}" --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict "$APP" && echo "    signature verifies"
  if [[ "$SIGN_ID" == *"Developer ID"* ]]; then
    echo "    next: tools/notarize.sh \"$APP\""
  fi
else
  cat >&2 <<'WARN'

WARNING: unsigned build.

macOS keys the Automation permission on the code-signing identity, so an
unsigned app presents a new identity whenever its contents change: the grant
is re-prompted, or silently ignored. Gatekeeper will also quarantine a
downloaded unsigned app (right-click -> Open, once, to get past that).

For distribution, sign with a Developer ID and notarize. For personal use, a
self-signed "Code Signing" certificate from Keychain Access is enough:
    tools/build-app.sh --sign "My Local Signing Cert"

WARN
fi

SIZE="$(du -sh "$APP" | cut -f1)"
echo "==> Built ${APP} (${SIZE})"

if [[ "$MAKE_ZIP" -eq 1 ]]; then
  ZIP="${OUT_DIR}/${APP_NAME}-${VERSION}-macos-${ARCH}.zip"
  rm -f "$ZIP"
  # ditto preserves the bundle's symlinks, resource forks and signature.
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
  ( cd "$OUT_DIR" && shasum -a 256 "$(basename "$ZIP")" > "$(basename "$ZIP").sha256" )
  echo "==> Packaged ${ZIP}"
  cat "${ZIP}.sha256"
fi

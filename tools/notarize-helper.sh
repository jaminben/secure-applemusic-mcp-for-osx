#!/usr/bin/env bash
# Notarize and staple the MusicKit helper ON ITS OWN, for the PyPI wheel.
#
# The app bundle's notarization ticket does NOT travel with the nested helper.
# Un-stapled, the helper still passes Gatekeeper *online* — Apple's notary
# recognises the hash — but not offline, which is exactly the state a fresh
# machine is in. The wheel ships the helper with no outer bundle around it, so
# it needs its own ticket.
#
# Two macOS behaviours make this fiddlier than it looks, both learned the hard
# way:
#
#   1. `stapler staple` on a RELATIVE path builds a broken removal target and
#      fails with "Could not remove existing ticket ... No such file or
#      directory", error 73. Always pass an absolute path.
#   2. App Management protection stops this shell writing inside a signed .app
#      that the system already knows about — "Operation not permitted" — so
#      stapling in place fails even as the owner. Staple a copy in a temp
#      directory and swap it back.
#
# Usage: tools/notarize-helper.sh [--profile NAME]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${REPO}/swift/amcp-musickit/AMCPMusicKit.app"
PROFILE="amcp-notary"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a name}"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -d "$APP" ]] || { echo "error: no helper at $APP — run swift/amcp-musickit/build.sh --sign first" >&2; exit 1; }

echo "==> Submitting the helper to the notary"
ZIP="$(mktemp -d)/AMCPMusicKit.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait

echo "==> Stapling (via a temp copy; see the header for why in-place fails)"
WORK="$(mktemp -d)"
ditto "$APP" "${WORK}/AMCPMusicKit.app"
xcrun stapler staple "${WORK}/AMCPMusicKit.app"
rm -rf "$APP"
ditto "${WORK}/AMCPMusicKit.app" "$APP"
rm -rf "$WORK"

xcrun stapler validate "$APP"
echo "==> Helper notarized and stapled. Now build the wheel: uv build --wheel"

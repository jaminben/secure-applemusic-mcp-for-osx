#!/usr/bin/env bash
# Notarize a signed app bundle, staple the ticket into it, and produce a zip
# that installs with no Gatekeeper warning at all.
#
# What notarization actually buys you: without it, a downloaded app carries the
# quarantine bit and Gatekeeper refuses to open it -- the user has to
# right-click -> Open and agree to a scary dialog. Notarized and stapled, it
# just opens. That difference is the whole reason this fork ships an installer
# a non-technical person can use.
#
# What it does NOT buy you: notarization is an automated malware scan, not a
# review. It does not inspect what the app does, and it is not an endorsement.
#
# Prerequisites (one time):
#
#   1. An app-specific password for your Apple ID -- NOT your account password.
#      Create at https://account.apple.com -> Sign-In and Security ->
#      App-Specific Passwords.
#
#   2. Store it in the keychain so it never appears on a command line:
#
#        xcrun notarytool store-credentials amcp-notary \
#          --apple-id you@example.com \
#          --team-id <YOUR-TEAM-ID> \
#          --password <app-specific-password>
#
#      Your Team ID is in the signing certificate; this script prints the exact
#      command with it filled in if the credentials are missing.
#
# Then:  tools/build-app.sh --sign "Developer ID Application: ..."
#        tools/notarize.sh
#
# Usage: tools/notarize.sh [/path/to/AppleMusicMCP.app] [--profile NAME]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${REPO}/dist/AppleMusicMCP.app"
PROFILE="amcp-notary"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a name}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) APP="$1"; shift ;;
  esac
done

[[ -d "$APP" ]] || { echo "error: no app bundle at $APP (run tools/build-app.sh first)" >&2; exit 1; }
APP="$(cd "$APP" && pwd)"
NAME="$(basename "$APP" .app)"
WORK="$(dirname "$APP")"
ZIP="${WORK}/${NAME}-notarize.zip"
OUT="${WORK}/${NAME}.zip"

# --- preflight ---------------------------------------------------------------
# Every one of these is a rejection that otherwise arrives several minutes
# later, in a JSON log, phrased less clearly.
echo "==> Preflight"

info="$(codesign -dvvv "$APP" 2>&1)" || { echo "error: $APP is not signed" >&2; exit 1; }

grep -q "Authority=Developer ID Application" <<<"$info" || {
  echo "error: not signed with a Developer ID Application certificate." >&2
  echo "       Apple will not notarize a self-signed or Apple Development build." >&2
  echo "       Rebuild: tools/build-app.sh --sign \"Developer ID Application: ...\"" >&2
  exit 1
}

grep -q "flags=.*runtime" <<<"$info" || {
  echo "error: the hardened runtime is not enabled (codesign --options runtime)." >&2
  echo "       Rebuild with the current tools/build-app.sh, which sets it." >&2
  exit 1
}

grep -q "Timestamp=" <<<"$info" || {
  echo "error: no secure timestamp (signed with --timestamp=none)." >&2
  echo "       Rebuild with the current tools/build-app.sh." >&2
  exit 1
}

codesign --verify --deep --strict "$APP" || {
  echo "error: the signature does not verify; notarization would reject it." >&2
  exit 1
}
echo "    Developer ID, hardened runtime, secure timestamp, signature verifies"

# Read the Team ID off the certificate instead of hardcoding it: it identifies
# the developer account and does not belong in the repository (a test enforces
# that), and deriving it means this script works for anyone who forks it.
TEAM_ID="$(sed -n 's/.*Authority=Developer ID Application: .*(\([A-Z0-9]\{10\}\)).*/\1/p' <<<"$info" | head -1)"
: "${TEAM_ID:=<YOUR-TEAM-ID>}"

if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  cat >&2 <<MSG

error: no stored notary credentials under the profile "$PROFILE".

Create an app-specific password at https://account.apple.com (Sign-In and
Security -> App-Specific Passwords), then run:

  xcrun notarytool store-credentials $PROFILE \\
    --apple-id you@example.com \\
    --team-id $TEAM_ID \\
    --password <app-specific-password>

MSG
  exit 1
fi

# --- submit ------------------------------------------------------------------
# ditto, not zip(1): zip mangles symlinks and resource forks inside a bundle,
# and the signature stops verifying on the far side.
echo "==> Submitting (a few minutes; --wait blocks until Apple answers)"
rm -f "$ZIP"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

if ! xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait; then
  echo >&2
  echo "Notarization failed. The reason is in the log for the submission id above:" >&2
  echo "  xcrun notarytool log <submission-id> --keychain-profile $PROFILE" >&2
  rm -f "$ZIP"
  exit 1
fi
rm -f "$ZIP"

# --- staple ------------------------------------------------------------------
# Staple the ticket INTO the .app, then re-zip. You cannot staple a zip, and a
# zip made before stapling still needs the network to validate -- which is the
# usual reason a "notarized" app is still refused on someone else's machine.
echo "==> Stapling"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

echo "==> Verifying as Gatekeeper will"
spctl --assess --type exec -vv "$APP"

rm -f "$OUT"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"

cat <<DONE

notarized, stapled, and zipped
  app  $APP
  zip  $OUT  ($(du -h "$OUT" | cut -f1))

Ship the zip. It opens with no Gatekeeper prompt, offline, on a Mac that has
never seen it before.

Worth testing that claim rather than trusting it -- the quarantine bit is only
set on a real download, so a local copy always looks fine:
  xattr -w com.apple.quarantine "0083;00000000;Safari;" /tmp/copy-of.app
DONE

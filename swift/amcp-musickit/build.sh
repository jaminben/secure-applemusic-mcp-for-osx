#!/usr/bin/env bash
# Build (and optionally sign) the MusicKit helper.
#
#   ./build.sh                                  unsigned — `add` will fail with
#                                               "Permission denied", as expected
#   ./build.sh --sign "Developer ID Application: You (TEAMID)" \
#              --profile /path/to.provisionprofile
#
# The entitlement is a managed capability, so an unsigned or ad-hoc build cannot
# use it no matter what the plist says. See docs/PERMISSIONS.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
SIGN=""; PROFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) SIGN="${2:?}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
swiftc -O -target arm64-apple-macosx14.0 -o amcp-musickit main.swift
echo "built: $(pwd)/amcp-musickit"
if [[ -n "$SIGN" ]]; then
  codesign --force --options runtime --entitlements entitlements.plist \
           --sign "$SIGN" amcp-musickit
  codesign -d --entitlements - amcp-musickit 2>&1 | grep -q musickit \
    && echo "signed with the MusicKit entitlement"
  [[ -n "$PROFILE" ]] && echo "remember: copy the profile to the .app as Contents/embedded.provisionprofile"
else
  echo "unsigned — 'add' will report Permission denied until signed with a MusicKit profile"
fi

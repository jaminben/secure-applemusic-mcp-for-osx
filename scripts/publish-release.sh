#!/usr/bin/env bash
#
# Attach the notarized macOS app to its GitHub Release, so people can download it.
#
# Why this is a LOCAL step and not part of CI: release.yml creates the GitHub
# Release, but it runs on ubuntu-latest. It cannot build a .app, cannot sign one
# with a Developer ID that only exists in a Mac keychain, and cannot notarize.
# So CI produces a Release with notes and nothing to download. The artifact has
# to come from a Mac, which means from here.
#
# What it refuses to publish, and why each check earns its place:
#
#   * an unstapled bundle -- the single most damaging thing to ship. It looks
#     perfect locally, because the quarantine bit is only set on a real
#     download; every person who downloads it gets "Apple could not verify this
#     app is free of malware". Checked with `stapler validate`, not by trusting
#     that notarize.sh ran.
#   * a bundle Gatekeeper rejects, for any other reason.
#   * a checksum that does not match the file, since the README tells people to
#     verify it and a stale SHA256SUMS.txt fails them at step one.
#   * a version that disagrees with pyproject.toml.
#   * a dirty working tree -- an artifact built from uncommitted code
#     corresponds to no commit anyone can check out.
#
# Usage:
#   ./scripts/publish-release.sh              # upload to the release for v<version>
#   ./scripts/publish-release.sh --draft      # create it as a draft instead
#   ./scripts/publish-release.sh --dry-run    # verify everything, upload nothing
#   ./scripts/publish-release.sh --allow-dirty
#
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

DRY=0; DRAFT=0; ALLOW_DIRTY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY=1; shift ;;
    --draft)       DRAFT=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die()  { echo; echo "error: $*" >&2; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    ✓ %s\n' "$*"; }

VERSION="$(sed -nE 's/^version = "(.*)"/\1/p' pyproject.toml | head -1)"
ARCH="$(uname -m)"
TAG="v${VERSION}"
ZIP="dist/UnofficialAppleMusicMCP-${VERSION}-macos-${ARCH}.zip"
APP="dist/UnofficialAppleMusicMCP.app"

echo "──────────────────────────────────────────────────────────"
echo " publish-release · ${TAG} · ${ARCH}"
[[ $DRY -eq 1 ]] && echo " DRY RUN — nothing will be uploaded"
echo "──────────────────────────────────────────────────────────"

# --- 1. the things that make an upload safe ----------------------------------
step "[1/4] Verifying the artifact"

command -v gh >/dev/null 2>&1 || die "the GitHub CLI (gh) is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"
ok "gh authenticated as $(gh api user --jq .login 2>/dev/null || echo '?')"

[[ -f "$ZIP" ]] || die "no ${ZIP}
       Build and notarize it first:  SIGN_ID=\"Developer ID Application: ...\" make release"
ok "$(basename "$ZIP") ($(du -h "$ZIP" | cut -f1))"

[[ -d "$APP" ]] || die "no ${APP} beside the zip — cannot verify what was packaged"

# THE check. An unstapled bundle is indistinguishable from a good one locally.
if ! xcrun stapler validate "$APP" >/dev/null 2>&1; then
  die "${APP} has NO notarization ticket stapled to it.
       It will open fine here and fail on every machine that downloads it,
       because the quarantine bit is only set on a real download.
       Fix:  make notarize"
fi
ok "notarization ticket stapled (validates offline)"

assess="$(spctl --assess --type exec -vv "$APP" 2>&1 || true)"
grep -q "accepted" <<<"$assess" || die "Gatekeeper rejects the bundle:
${assess}"
grep -q "source=Notarized Developer ID" <<<"$assess" \
  || die "Gatekeeper accepts it, but not as a notarized Developer ID build:
${assess}"
ok "Gatekeeper: accepted, Notarized Developer ID"

# The bundle is built on a developer's machine, from a checkout in their home
# directory, using a vendored interpreter out of their cache. Several of those
# steps leave the path behind. Not a credential -- but it is a username and a
# directory layout published to strangers, and it is gratuitous.
step "[1b/4] Auditing the bundle for build-machine paths and secrets"

leaks="$(grep -rlI "$HOME" "$APP" 2>/dev/null || true)"
[[ -z "$leaks" ]] || die "the bundle contains your home directory:
$(echo "$leaks" | sed 's|^|       |')
       tools/build-app.sh scrubs these; rebuild rather than shipping it."
ok "no build-machine paths"

creds="$(find "$APP" \( -name '*.p8' -o -name '*.p12' -o -name '*.key' \
  -o -name '*.provisionprofile' -o -name '.env' -o -name 'developer_token.json' \
  -o -name 'music_user_token.json' -o -name 'audit_log.jsonl' \) 2>/dev/null || true)"
[[ -z "$creds" ]] || die "credential material inside the bundle:
$(echo "$creds" | sed 's|^|       |')"
ok "no credential files"

# certifi ships CA roots (public); a PRIVATE key is what must never appear.
privkeys="$(grep -rlI "BEGIN RSA PRIVATE KEY\|BEGIN EC PRIVATE KEY" "$APP" 2>/dev/null || true)"
[[ -z "$privkeys" ]] || die "private-key material inside the bundle:
$(echo "$privkeys" | sed 's|^|       |')"
ok "no private-key material"

# --- 2. does the artifact match the repo? ------------------------------------
step "[2/4] Verifying it matches this checkout"

python3 scripts/check_versions.py >/dev/null || die "version surfaces disagree — run scripts/check_versions.py"
ok "version surfaces agree (${VERSION})"

if [[ -f dist/SHA256SUMS.txt ]]; then
  ( cd dist && shasum -a 256 -c SHA256SUMS.txt >/dev/null 2>&1 ) \
    || die "SHA256SUMS.txt does not match the files in dist/.
       It was probably written before notarization restapled the bundle.
       Fix:  SIGN_ID=\"...\" make release"
  ok "SHA256SUMS.txt verifies"
else
  echo "    ! no dist/SHA256SUMS.txt — uploading without it"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  if [[ $ALLOW_DIRTY -eq 1 ]]; then
    echo "    ! working tree is dirty (--allow-dirty)"
  else
    die "working tree is dirty. This artifact would correspond to no commit
       anyone can check out. Commit first, or pass --allow-dirty."
  fi
else
  ok "working tree clean ($(git rev-parse --short HEAD))"
fi

# --- 3. the release ----------------------------------------------------------
step "[3/4] Locating the release"

ASSETS=("$ZIP")
[[ -f "${ZIP}.sha256"     ]] && ASSETS+=("${ZIP}.sha256")
[[ -f dist/SHA256SUMS.txt ]] && ASSETS+=("dist/SHA256SUMS.txt")

if gh release view "$TAG" >/dev/null 2>&1; then
  ok "release ${TAG} exists — will add/replace its assets"
  ACTION="upload"
else
  echo "    release ${TAG} does not exist yet; it will be created$([[ $DRAFT -eq 1 ]] && echo ' as a draft')"
  ACTION="create"
fi

# --- 4. upload ---------------------------------------------------------------
step "[4/4] Publishing"
if [[ $DRY -eq 1 ]]; then
  echo "    would ${ACTION} ${TAG} with:"
  printf '      %s\n' "${ASSETS[@]}"
  exit 0
fi

if [[ "$ACTION" == "create" ]]; then
  gh release create "$TAG" "${ASSETS[@]}" \
    --title "Unofficial Apple Music MCP ${TAG}" \
    --generate-notes \
    $([[ $DRAFT -eq 1 ]] && echo --draft) \
    --target "$(git rev-parse HEAD)"
else
  # --clobber so re-running after a rebuild replaces the asset instead of
  # failing on a name collision and leaving the old one in place.
  gh release upload "$TAG" "${ASSETS[@]}" --clobber
fi

URL="$(gh release view "$TAG" --json url --jq .url)"
echo
echo "Published:"
printf '    %s\n' "${ASSETS[@]}"
echo "  $URL"
echo
echo "Worth checking the claim rather than trusting it — download it from that"
echo "page on another Mac and open it. A local copy never carries the"
echo "quarantine bit, so it always looks fine here."

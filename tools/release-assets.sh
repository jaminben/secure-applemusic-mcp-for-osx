#!/usr/bin/env bash
# Build every release artifact, in the one order that produces working ones.
#
# Why this script exists: the steps have a required order that nothing enforced,
# and getting it wrong produces artifacts that look fine and fail on a stranger's
# Mac. Three traps, each of which cost a debugging round:
#
#   1. `build-app.sh --zip` zips BEFORE notarization, because notarization is a
#      separate step. Upload that zip and every download trips Gatekeeper. The
#      zip must be made from the STAPLED bundle, so this script never passes
#      --zip and does its own zipping afterwards.
#   2. Both architectures build to the same `dist/UnofficialAppleMusicMCP.app`,
#      so building one after the other silently leaves you with one app and two
#      zips. Each arch is moved to its own staging path before the next runs.
#   3. `xcrun stapler staple` needs an ABSOLUTE path (a relative one fails with
#      error 73 and a misleading "could not remove existing ticket"), and macOS
#      App Management refuses in-place writes into a signed .app — see
#      tools/notarize.sh and tools/notarize-helper.sh.
#
# Every release must upload BOTH the versioned and the unversioned name for each
# architecture. The unversioned ones are what the README, the landing page and
# every directory listing resolve through `releases/latest/download/…`; skip
# them and all those links keep serving the previous version, silently.
#
# Usage:
#   tools/release-assets.sh --sign "Developer ID Application: You (TEAMID)"
#   tools/release-assets.sh --sign "…" --upload      # also attach to the tag
#   tools/release-assets.sh --sign "…" --skip-wheel
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SIGN_ID=""
UPLOAD=0
SKIP_WHEEL=0
ALLOW_DIRTY=0
PROFILE="amcp-notary"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign)       SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    --profile)    PROFILE="${2:?--profile needs a name}"; shift 2 ;;
    --upload)     UPLOAD=1; shift ;;
    --skip-wheel) SKIP_WHEEL=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SIGN_ID" ]] || { echo "error: --sign is required; an unsigned build cannot be notarized." >&2; exit 1; }

VERSION="$(python3 -c "import re;print(re.search(r'^version = \"([^\"]+)\"', open('pyproject.toml').read(), re.M).group(1))")"
APP_NAME="UnofficialAppleMusicMCP"
OUT="${REPO}/dist"
echo "==> Releasing ${VERSION}"

python3 scripts/check_versions.py

# A clean tree, unless you say otherwise. The wheel that reaches every pip user
# is built HERE, not in CI — CI has no signing identity — so nothing downstream
# can tell whether it came from the tagged commit or from uncommitted edits on
# this Mac. That check existed in the publish script this replaced and was not
# carried over; the audit that caught it also noted the artifact has no
# cryptographic link to any commit, which makes this the only thing standing
# between "published from the tag" and "published from whatever was on disk".
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  if [[ "$ALLOW_DIRTY" -eq 1 ]]; then
    echo "!!  Working tree is DIRTY and --allow-dirty was passed."
    echo "!!  These artifacts will not correspond to any commit."
  else
    echo "error: the working tree has uncommitted changes." >&2
    echo "       The wheel is built here and published to PyPI, so it must come" >&2
    echo "       from a committed state. Commit, stash, or pass --allow-dirty" >&2
    echo "       for a local build you do not intend to ship." >&2
    git status --short >&2
    exit 1
  fi
fi

echo "==> Swift helpers (universal)"
./swift/amcp-musickit/build.sh --sign "$SIGN_ID" >/dev/null
./swift/amcp-setup/build.sh   --sign "$SIGN_ID" >/dev/null

for ARCH in arm64 x86_64; do
  echo "==> ${ARCH}: building"
  ./tools/build-app.sh --arch "$ARCH" --sign "$SIGN_ID" >/dev/null
  # Staged in a per-arch DIRECTORY so the bundle keeps its plain name — the
  # user drags "UnofficialAppleMusicMCP.app" to Applications, not
  # "UnofficialAppleMusicMCP-arm64.app". Move, not copy: the next architecture
  # builds to the same path and would otherwise overwrite this one.
  STAGE_DIR="${OUT}/stage-${ARCH}"
  STAGED="${STAGE_DIR}/${APP_NAME}.app"
  rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
  mv "${OUT}/${APP_NAME}.app" "$STAGED"

  echo "==> ${ARCH}: notarizing (a few minutes)"
  ./tools/notarize.sh "$STAGED" --profile "$PROFILE" >/dev/null

  echo "==> ${ARCH}: packaging from the stapled bundle"
  VER_ZIP="${OUT}/${APP_NAME}-${VERSION}-macos-${ARCH}.zip"
  LATEST_ZIP="${OUT}/${APP_NAME}-macos-${ARCH}.zip"
  rm -f "$VER_ZIP" "$LATEST_ZIP"
  ditto -c -k --sequesterRsrc --keepParent "$STAGED" "$VER_ZIP"
  cp "$VER_ZIP" "$LATEST_ZIP"

  # Prove the thing we are about to ship opens on a machine that has never seen
  # it. The quarantine bit is only set on a real download, so a local copy
  # always looks fine — set it deliberately and ask Gatekeeper.
  CHECK="$(mktemp -d)"
  ditto -x -k "$VER_ZIP" "$CHECK"
  # Fail if the bit cannot even be SET. Swallowing that would make the check
  # below pass on an unquarantined copy, which always passes — a green light
  # that proves nothing, which is worse than no check at all.
  if ! xattr -w com.apple.quarantine "0083;00000000;Safari;" "${CHECK}/${APP_NAME}.app" 2>/dev/null; then
    echo "error: could not set the quarantine bit, so the Gatekeeper check" >&2
    echo "       below would be meaningless. Check filesystem permissions." >&2
    exit 1
  fi
  if ! spctl -a -t exec "${CHECK}/${APP_NAME}.app" >/dev/null 2>&1; then
    echo "error: ${ARCH} zip is REJECTED by Gatekeeper when quarantined." >&2
    echo "       Do not ship it. Notarization or stapling did not take." >&2
    exit 1
  fi
  rm -rf "$CHECK"
  echo "    ${ARCH}: notarized, stapled, Gatekeeper-clean"
done

if [[ "$SKIP_WHEEL" -eq 0 ]]; then
  echo "==> Wheel (carries the signed helper; needs its own notarization)"
  ./tools/notarize-helper.sh --profile "$PROFILE" >/dev/null
  rm -f "${OUT}"/*.whl
  uv build --wheel >/dev/null
  WHEEL="$(ls "${OUT}"/*.whl)"

  # The helper is force-included by scripts/wheel_tag.py only when it has been
  # built, so that a fresh clone and CI can still install the package. The cost
  # of that flexibility is that a wheel CAN be built without it — and such a
  # wheel installs happily and silently has no MusicKit rail. Never ship one.
  if ! unzip -l "$WHEEL" | grep -q "AMCPMusicKit.app/Contents/MacOS/AMCPMusicKit"; then
    echo "error: the wheel does not contain the MusicKit helper." >&2
    echo "       It would install as a build with no catalog adds. Run" >&2
    echo "       swift/amcp-musickit/build.sh --sign ... and rebuild." >&2
    exit 1
  fi
  case "$WHEEL" in
    *macosx*) : ;;
    *) echo "error: $(basename "$WHEEL") is not platform-tagged; pip would" >&2
       echo "       install it on Linux. The helper was missing at build time." >&2
       exit 1 ;;
  esac
  echo "    $(basename "$WHEEL") (helper present, macOS-tagged)"
fi

echo "==> Checksums"
cd "$OUT"
# Build the list first: with --skip-wheel there is no *.whl, the glob goes
# unmatched, shasum exits non-zero and `set -e` kills the run before the
# checksums are written. Found by audit.
SUM_FILES=(${APP_NAME}-*.zip)
if compgen -G "*.whl" >/dev/null; then SUM_FILES+=(*.whl); fi
shasum -a 256 "${SUM_FILES[@]}" > SHA256SUMS.txt
for z in ${APP_NAME}-${VERSION}-macos-*.zip; do shasum -a 256 "$z" > "${z}.sha256"; done
cd "$REPO"

echo
echo "Artifacts in dist/:"
ls -1 "${OUT}"/${APP_NAME}-*.zip "${OUT}"/*.whl "${OUT}"/SHA256SUMS.txt 2>/dev/null | sed 's|.*/|  |'

if [[ "$UPLOAD" -eq 1 ]]; then
  echo
  echo "==> Uploading to v${VERSION}"
  # The wheel goes up too. It is the only way to install it until the PyPI
  # trusted publisher exists — `pip install <that release URL>` works today,
  # and the checksums cover it either way.
  UPLOADS=("${OUT}"/${APP_NAME}-*.zip "${OUT}"/*.zip.sha256 "${OUT}/SHA256SUMS.txt")
  if compgen -G "${OUT}/*.whl" >/dev/null; then UPLOADS+=("${OUT}"/*.whl); fi
  gh release upload "v${VERSION}" "${UPLOADS[@]}" \
    --repo jaminben/secure-applemusic-mcp-for-osx --clobber
  gh api "repos/jaminben/secure-applemusic-mcp-for-osx/releases/tags/v${VERSION}" \
    --jq '"    v\(.tag_name|ltrimstr("v")): \([.assets[].name]|join(", "))"'
else
  echo
  echo "Next: tools/release-assets.sh --sign \"…\" --upload   (or upload by hand)"
fi

if [[ "$SKIP_WHEEL" -eq 0 ]]; then
  echo
  echo "NOTE: the wheel is attached to the GitHub Release but NOT published to"
  echo "      PyPI — there is no trusted publisher for the project yet, so"
  echo "      \`pip install secure-applemusic-mcp-for-osx\` still finds nothing."
  echo "      See \"The PyPI wheel\" in RELEASING.md."
fi

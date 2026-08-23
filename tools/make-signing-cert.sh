#!/usr/bin/env bash
# Create a self-signed code-signing certificate for local builds.
#
# Why bother signing at all: macOS keys the Automation (Music) permission on the
# code-signing identity. An unsigned app presents a different identity every
# time its contents change, so the grant is re-prompted — or silently ignored
# against a stale entry. Signing with a STABLE identity makes the grant stick.
#
# Check what TCC keys on after signing:
#     codesign -d -r- /Applications/AppleMusicMCP.app
#     designated => identifier "io.github.jaminben..." and certificate root = H"<hash>"
# That certificate hash is why the cert must be kept, not regenerated.
#
# Usage:
#   tools/make-signing-cert.sh                # create (idempotent)
#   tools/make-signing-cert.sh --delete       # remove the keychain and cert
#
# Then:  SIGN_ID="Apple Music MCP Self-Signed" make app

set -euo pipefail

CERT_CN="Apple Music MCP Self-Signed"
KEYCHAIN="amcp-signing.keychain"
# This password guards a LOCAL, self-signed signing key in its own keychain —
# not an account credential. It is written down on purpose so builds are
# non-interactive; the key it protects can only vouch that two builds came from
# the same machine, and Apple trusts it nowhere.
KEYCHAIN_PW="amcp-local-signing"
DAYS=3650

if [[ "${1:-}" == "--delete" ]]; then
  security delete-keychain "$KEYCHAIN" 2>/dev/null && echo "Removed $KEYCHAIN" || echo "No $KEYCHAIN to remove"
  echo "Note: apps already signed with it keep working; rebuilds will need a new cert"
  echo "      and macOS will re-prompt for the Music permission."
  exit 0
fi

[[ "$(uname -s)" == "Darwin" ]] || { echo "error: macOS only." >&2; exit 1; }

if security find-certificate -c "$CERT_CN" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "Already present: \"$CERT_CN\" in $KEYCHAIN"
  echo "Build with:  SIGN_ID=\"$CERT_CN\" make app"
  exit 0
fi

# codesign resolves identities by name across the whole search list, so a
# same-named certificate in another keychain makes the name ambiguous and
# signing fails outright. Catch that here with a fixable message rather than
# letting the build die later.
for kc in $(security list-keychains -d user | tr -d '" '); do
  if security find-certificate -c "$CERT_CN" "$kc" >/dev/null 2>&1; then
    echo "error: a certificate named \"$CERT_CN\" already exists in:" >&2
    echo "         $kc" >&2
    echo "       codesign would not be able to tell them apart. Remove it with:" >&2
    echo "         security delete-identity -c \"$CERT_CN\" $kc" >&2
    exit 1
  fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT   # the private key must not linger outside the keychain

echo "==> Generating a self-signed code-signing certificate"
# extendedKeyUsage=codeSigning is required — without it codesign won't accept
# the identity no matter how the cert is trusted.
openssl req -x509 -newkey rsa:2048 -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
  -days "$DAYS" -nodes -subj "/CN=${CERT_CN}/O=local" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" \
  -addext "basicConstraints=critical,CA:false" 2>/dev/null

# macOS's PKCS#12 reader rejects OpenSSL 3's defaults ("MAC verification failed"),
# so export with the legacy MAC and cipher it understands.
openssl pkcs12 -export -out "$TMP/cert.p12" -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "$CERT_CN" -passout pass:tmp \
  -macalg sha1 -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES 2>/dev/null

echo "==> Creating a dedicated keychain"
# A separate keychain, rather than login.keychain, for two reasons: setting the
# key partition list on the login keychain needs your login password, and
# codesign would otherwise block on a GUI "wants to use your keychain" prompt
# that a scripted build cannot answer.
security create-keychain -p "$KEYCHAIN_PW" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"   # stay unlocked ~6h
security unlock-keychain -p "$KEYCHAIN_PW" "$KEYCHAIN"
security import "$TMP/cert.p12" -k "$KEYCHAIN" -P tmp -A -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PW" \
  "$KEYCHAIN" >/dev/null 2>&1

# Add to the search list so `codesign -s "$CERT_CN"` resolves it, without
# disturbing the existing entries.
CURRENT="$(security list-keychains -d user | tr -d '" ')"
if ! echo "$CURRENT" | grep -q "$KEYCHAIN"; then
  # shellcheck disable=SC2086
  security list-keychains -d user -s $CURRENT "$KEYCHAIN"
fi

echo "==> Verifying"
TESTBIN="$TMP/testbin"
cp /bin/echo "$TESTBIN"
if ! ERR="$(codesign --force --keychain "$KEYCHAIN" --sign "$CERT_CN" "$TESTBIN" 2>&1)"; then
  echo "error: test signing failed: $ERR" >&2
  exit 1
fi
echo "    signing works"
HASH="$(security find-identity -p codesigning "$KEYCHAIN" 2>/dev/null \
        | awk -v n="$CERT_CN" '$0 ~ n {print $2; exit}')"
[[ -n "$HASH" ]] && echo "    identity ${HASH}"

cat <<DONE

Created "$CERT_CN" in $KEYCHAIN.

  Build a signed app:   SIGN_ID="$CERT_CN" make app
  Remove it later:      tools/make-signing-cert.sh --delete

The certificate shows as untrusted in Keychain Access. That is expected and
does not matter here: it is not trying to prove anything to Apple, only to give
your builds a STABLE identity so the Music permission survives a rebuild.

It does NOT help anyone else: a self-signed app downloaded by someone else is
still unnotarized, so Gatekeeper will block it until they allow it in
System Settings → Privacy & Security. For that, use a Developer ID certificate
and notarize.
DONE

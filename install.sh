#!/usr/bin/env bash
# Install secure-applemusic-mcp into a private, self-contained virtualenv.
#
#   ./install.sh                 install, then print MCP client config
#   ./install.sh --scoped        also build + load the app bundle so the macOS
#                                permission is granted to THIS server rather
#                                than to your terminal (recommended)
#   ./install.sh --prefix DIR    install somewhere other than the default
#   ./install.sh --uninstall     remove everything this script installed
#
# Deliberately does not curl|bash from anywhere: it installs from the checkout
# you are standing in, so what runs is what you can read.

set -euo pipefail

PREFIX="${HOME}/.local/share/secure-applemusic-mcp"
BINDIR="${HOME}/.local/bin"
SCOPED=0
SIGN_ID=""
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scoped)    SCOPED=1; shift ;;
    --sign)      SIGN_ID="${2:?--sign needs an identity}"; shift 2 ;;
    --prefix)    PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --uninstall)
      "${SRC_DIR}/tools/make-app-bundle.sh" --uninstall 2>/dev/null || true
      rm -rf "$PREFIX"
      rm -f "${BINDIR}/secure-applemusic-mcp"
      echo "Removed the virtualenv and launcher."
      echo
      echo "Left in place on purpose (delete by hand if you want them gone):"
      echo "  ~/.config/applemusic-mcp   credentials, if you set up a developer token"
      echo "  ~/.cache/applemusic-mcp    audit log, snapshots, exports"
      exit 0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this build is macOS-only (it drives Music.app via Apple Events)." >&2
  exit 1
fi

PY="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "error: python3 not found." >&2; exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "error: Python 3.10+ required; found $("$PY" -V)" >&2; exit 1
fi

echo "==> Installing into $PREFIX"
mkdir -p "$PREFIX" "$BINDIR"
# The venv holds no secrets, but it does hold the code that will hold the
# Automation grant — don't leave it group/world-writable.
chmod 700 "$PREFIX"
"$PY" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC_DIR"

ln -sf "$PREFIX/venv/bin/secure-applemusic-mcp" "${BINDIR}/secure-applemusic-mcp"
echo "==> Installed: $("$PREFIX/venv/bin/secure-applemusic-mcp" --help >/dev/null 2>&1 && echo ok)"
"$PREFIX/venv/bin/python" -c "import applemusic_mcp; print('    version', applemusic_mcp.__version__)"

case ":$PATH:" in
  *":${BINDIR}:"*) ;;
  *) echo "    note: ${BINDIR} is not on your PATH (the config below uses a full path anyway)" ;;
esac

if [[ "$SCOPED" -eq 1 ]]; then
  echo
  echo "==> Building the app bundle so the permission is scoped to this server"
  PATH="${PREFIX}/venv/bin:${PATH}" "${SRC_DIR}/tools/make-app-bundle.sh" \
    --install ${SIGN_ID:+--sign "$SIGN_ID"}
  exit 0
fi

cat <<CONFIG

--- MCP client configuration ---------------------------------------------
{
  "mcpServers": {
    "apple-music": {
      "command": "${BINDIR}/secure-applemusic-mcp",
      "args": ["serve"]
    }
  }
}
--------------------------------------------------------------------------

First use will prompt: "<your terminal> wants to control Music.app".

  READ THIS BEFORE YOU CLICK ALLOW. That grant is attributed to whatever
  spawned this server — usually your terminal or agent app — so allowing it
  gives Music control to everything you run from there, not just this server.

  To scope the grant to this server alone, re-run:

      ./install.sh --scoped

  which installs a background helper under its own app bundle identity, and
  points your client at a permissionless shim instead. See docs/PERMISSIONS.md.

Never grant this Accessibility. It cannot use it; if something asks, that is a
bug worth reporting.
CONFIG

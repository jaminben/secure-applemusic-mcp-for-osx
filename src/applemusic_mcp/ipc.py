"""Unix-socket transport between the stdio shim and the resident helper.

Why this exists — it is a *permissions* mechanism, not a performance one.

macOS attributes a TCC grant (Automation → Music.app) to the **responsible
process**, which for a server spawned by an MCP client is normally the client:
your terminal, or the agent app. Approving that prompt therefore grants Music
control to everything you ever run from that terminal, which is the opposite of
scoping. Responsibility is inherited from the spawning process, so simply
putting the executable inside a ``.app`` does not change it.

launchd-started processes are responsible for themselves. So the server is split
in two:

    MCP client ──stdio──▶ shim  (no permissions, no Apple Events, pure bytes)
                            │ unix socket, 0600, inside a 0700 dir
                            ▼
                         helper (started by launchd from the .app bundle)
                            │ holds the Automation grant
                            ▼
                         Music.app

The shim needs no permission because it never talks to Music. Every Apple Event
happens under the helper's bundle identity, which gets its own row in System
Settings → Privacy & Security → Automation and can be revoked on its own.

Trust boundary: the socket is same-user only. That is the same boundary the
0600 token files already rely on — a process running as you can read those
files regardless — so the socket does not widen it. It is *not* a boundary
against other users: the containing directory is 0700 and the socket 0600 so
that other local accounts cannot reach it at all.
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

# THE bundle identifier. Every build script derives its copy from this line by
# sed, so this is the only place it is written down -- change it here and the
# app, both Swift helpers, the launchd label, the socket path and the TCC row
# all follow. Forking? Change this, and nothing else.
# (docs/PERMISSIONS.md quotes it for humans; a test keeps that honest.)
BUNDLE_ID = "io.github.jaminben.secure-applemusic-mcp"

_ENV_SOCKET = "APPLEMUSIC_MCP_SOCKET"

# A unix socket path goes into a fixed-size sockaddr_un.sun_path (104 bytes on
# macOS, including the NUL). A long $HOME can overflow it, and the failure mode
# is a confusing EINVAL rather than a clear error — so check and say so.
_SUN_PATH_MAX = 103


def runtime_dir() -> Path:
    """Directory holding the socket. Created 0700, verified on every call."""
    base = Path.home() / "Library" / "Application Support" / BUNDLE_ID
    base.mkdir(parents=True, exist_ok=True)
    # Re-assert the mode rather than trusting creation: the directory may
    # predate this version, or have been created by an older umask.
    os.chmod(base, 0o700)
    return base


def socket_path() -> Path:
    """Path to the helper's socket. ``APPLEMUSIC_MCP_SOCKET`` overrides (tests)."""
    env = os.environ.get(_ENV_SOCKET)
    if env:
        return Path(env).expanduser()
    return runtime_dir() / "helper.sock"


def check_path_length(path: Path) -> None:
    """Raise a readable error if the socket path won't fit in sockaddr_un."""
    encoded = str(path).encode()
    if len(encoded) > _SUN_PATH_MAX:
        raise OSError(
            f"Socket path is {len(encoded)} bytes; the macOS limit is {_SUN_PATH_MAX}. "
            f"Set {_ENV_SOCKET} to something shorter (e.g. /tmp/am-mcp.sock).\n  {path}"
        )


def create_listener(path: Path) -> socket.socket:
    """Bind a listening unix socket at ``path``, 0600, replacing a stale one.

    The mode is applied with a umask around ``bind`` so the socket is never
    briefly world-accessible: ``bind`` honours the umask, whereas a follow-up
    ``chmod`` would leave a window open.
    """
    check_path_length(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Best-effort on the directory: with the default path we own it and 0700 is
    # defence in depth, but an operator may point APPLEMUSIC_MCP_SOCKET at a
    # directory we don't own (/tmp). Failing to chmod someone else's directory
    # must not stop us — the 0600 socket below is the control that matters, and
    # macOS enforces permission checks on unix sockets.
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    # lstat, not stat: the checks below must describe the path itself, not
    # whatever it points at. `path.exists()`/`path.stat()` follow symlinks, so a
    # symlink to a socket would satisfy an "is it a socket?" test and we would
    # then be operating on someone else's chosen location.
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise OSError(
                f"Refusing to use a symlink as the socket path: {path}. "
                "Point APPLEMUSIC_MCP_SOCKET at a real path."
            )
        # Only ever unlink something that is actually a socket — never a
        # regular file that happens to share the name.
        if not stat.S_ISSOCK(st.st_mode):
            raise OSError(f"Refusing to replace non-socket file at {path}")
        path.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # -> 0600
    try:
        sock.bind(str(path))
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)  # belt and braces; some platforms ignore umask here
    # Verify rather than assume: this is the boundary keeping other local
    # accounts out, so a surprising mode should be a loud failure, not a
    # silently-permissive socket.
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        sock.close()
        path.unlink(missing_ok=True)
        raise OSError(f"Refusing to listen: socket at {path} is mode {oct(mode)}, expected 0o600")
    sock.listen(8)
    return sock


def connect(path: Path, timeout: float = 5.0) -> socket.socket:
    """Connect to the helper's socket, with an actionable error if it isn't up."""
    check_path_length(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ConnectionError(
            f"No Apple Music helper is listening at {path}.\n"
            "Start it with:  launchctl kickstart -k "
            f"gui/$(id -u)/{BUNDLE_ID}\n"
            "or install it with:  tools/make-app-bundle.sh --install"
        ) from exc
    sock.settimeout(None)  # blocking for the pump
    return sock

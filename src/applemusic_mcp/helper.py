"""Resident MCP server behind a unix socket. The half launchd starts.

This is the process that actually sends Apple Events, so this is the process
that must own the TCC grant. launchd starts it from the .app bundle, which
makes it responsible for itself — so the Automation prompt names the bundle and
the grant lands on the bundle rather than on whatever terminal happened to
spawn a server. See docs/PERMISSIONS.md.

One child process per client connection, mirroring how a client would otherwise
spawn a server per session: the MCP server assumes it owns stdin/stdout, and
per-session state (track cache, active engine) stays isolated. The child
inherits the parent's TCC responsibility, so it keeps the bundle identity.
"""

from __future__ import annotations

import io
import os
import signal
import socket
import sys

from . import ipc


def _serve_connection(conn: socket.socket) -> None:
    """Run the MCP server in THIS process with stdio wired to ``conn``.

    Only ever called in a forked child — it replaces fds 0 and 1 for the whole
    process and does not return.

    ``dup2`` alone is not enough. The MCP stdio transport wraps
    ``sys.stdin.buffer``, and that object was built at interpreter startup for
    whatever the HELPER's stdin was — if that was a regular file (launchd
    redirecting to a log, say) it cached ``seekable() == True``, and wrapping it
    over a socket fd then raises ESPIPE "Illegal seek". So rebuild the stream
    objects from the socket fd instead of reusing the inherited ones.
    """
    fd_r = os.dup(conn.fileno())
    fd_w = os.dup(conn.fileno())
    # Keep the real fds 0/1 pointing at the socket too, so anything reading them
    # directly (rather than via sys.stdin/stdout) still talks to the client.
    os.dup2(conn.fileno(), 0)
    os.dup2(conn.fileno(), 1)
    conn.close()

    sys.stdin = io.TextIOWrapper(
        io.BufferedReader(io.FileIO(fd_r, "rb", closefd=True)), encoding="utf-8"
    )
    sys.stdout = io.TextIOWrapper(
        io.BufferedWriter(io.FileIO(fd_w, "wb", closefd=True)),
        encoding="utf-8",
        write_through=True,
    )
    # stderr is left pointing at the helper's stderr, which launchd captures,
    # so diagnostics land in the log rather than in the JSON-RPC stream.
    from .server import main as server_main

    server_main()


def _reap(*_args) -> None:
    """Collect finished children so sessions don't accumulate as zombies."""
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
    except ChildProcessError:
        return


def main() -> int:
    path = ipc.socket_path()
    try:
        listener = ipc.create_listener(path)
    except OSError as exc:
        print(f"secure-applemusic-mcp helper: {exc}", file=sys.stderr, flush=True)
        return 1

    signal.signal(signal.SIGCHLD, _reap)
    stopping = False

    def _stop(*_args):
        nonlocal stopping
        stopping = True
        try:
            listener.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"helper listening on {path}", file=sys.stderr, flush=True)

    try:
        while not stopping:
            try:
                conn, _addr = listener.accept()
            except InterruptedError:
                continue  # a SIGCHLD landed mid-accept
            except OSError:
                if stopping:
                    break
                raise

            pid = os.fork()
            if pid == 0:
                # Child: drop the listener, become the session, never return.
                try:
                    listener.close()
                except OSError:
                    pass
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                try:
                    _serve_connection(conn)
                except Exception as exc:  # noqa: BLE001 - a session must not kill the helper
                    print(f"session error: {exc}", file=sys.stderr, flush=True)
                    os._exit(1)
                os._exit(0)
            conn.close()  # parent keeps only the listener
    finally:
        try:
            listener.close()
        except OSError:
            pass
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    return 0

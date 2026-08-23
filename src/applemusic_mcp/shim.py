"""stdio ⇄ unix-socket pump. The half your MCP client spawns.

This process deliberately does nothing else. It imports no Apple Events code,
holds no credentials, and needs no macOS permission — it copies bytes between
its own stdin/stdout and the helper's socket. That is the whole point: the
thing your client spawns (and whose TCC identity is therefore your terminal's)
must not be the thing that talks to Music.app.

Byte-transparent by design: it does not parse JSON-RPC, so it cannot corrupt a
message, and it will not need updating when the protocol changes.
"""

from __future__ import annotations

import os
import selectors
import socket
import sys

from . import ipc

_BUF = 65536


def _pump(sock: socket.socket) -> int:
    """Copy stdin→socket and socket→stdout until either side closes."""
    stdin_fd = sys.stdin.buffer.fileno()
    stdout = sys.stdout.buffer

    sel = selectors.DefaultSelector()
    sel.register(stdin_fd, selectors.EVENT_READ, "stdin")
    sel.register(sock, selectors.EVENT_READ, "sock")

    try:
        while True:
            for key, _mask in sel.select():
                if key.data == "stdin":
                    data = os.read(stdin_fd, _BUF)
                    if not data:
                        # Client closed stdin: half-close so the helper sees EOF
                        # and can shut its session down cleanly, then keep
                        # draining until it closes its side.
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            return 0
                        sel.unregister(stdin_fd)
                        continue
                    sock.sendall(data)
                else:
                    data = sock.recv(_BUF)
                    if not data:
                        return 0
                    stdout.write(data)
                    stdout.flush()
    except (BrokenPipeError, ConnectionResetError):
        return 0
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0
    finally:
        sel.close()
        try:
            sock.close()
        except OSError:
            pass


def main() -> int:
    """Connect to the helper and pump. Returns a process exit code."""
    path = ipc.socket_path()
    try:
        sock = ipc.connect(path)
    except (ConnectionError, OSError) as exc:
        # stdout is the JSON-RPC channel — diagnostics must go to stderr only,
        # or the client sees a protocol violation instead of the real problem.
        print(f"secure-applemusic-mcp shim: {exc}", file=sys.stderr, flush=True)
        return 1
    return _pump(sock)

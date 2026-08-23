"""The shim/helper transport — a permissions mechanism, so tested as one.

The socket exists so the process that sends Apple Events can be started by
launchd (and therefore own its own TCC identity) while the process the MCP
client spawns holds no permissions at all. Two properties matter and are
asserted here: the socket is never reachable by another local account, and the
shim never becomes a second thing that could talk to Music.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from applemusic_mcp import ipc, shim

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS transport")


@pytest.fixture
def sock_path(tmp_path, monkeypatch):
    # Keep well inside sockaddr_un's 103-byte limit.
    short = Path("/tmp") / f"am-t{os.getpid()}.sock"
    monkeypatch.setenv("APPLEMUSIC_MCP_SOCKET", str(short))
    yield short
    short.unlink(missing_ok=True)


# --- the socket is the boundary ------------------------------------------------


def test_listener_socket_is_0600(sock_path):
    s = ipc.create_listener(sock_path)
    try:
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        assert mode == 0o600, f"socket is {oct(mode)} — another local account could connect"
    finally:
        s.close()


def test_listener_rejects_a_world_readable_socket(sock_path, monkeypatch):
    """If the mode can't be enforced, refuse to listen rather than serve openly."""
    real_chmod = os.chmod

    def permissive(path, mode, *a, **k):
        # Simulate a filesystem that ignores the requested mode.
        return real_chmod(path, 0o666) if str(path) == str(sock_path) else real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", permissive)
    monkeypatch.setattr(os, "umask", lambda _m: 0o022)
    with pytest.raises(OSError, match="Refusing to listen"):
        ipc.create_listener(sock_path)


def test_listener_refuses_to_clobber_a_regular_file(sock_path):
    sock_path.write_text("not a socket")
    try:
        with pytest.raises(OSError, match="Refusing to replace non-socket"):
            ipc.create_listener(sock_path)
        assert sock_path.read_text() == "not a socket"
    finally:
        sock_path.unlink(missing_ok=True)


def test_listener_replaces_a_stale_socket(sock_path):
    first = ipc.create_listener(sock_path)
    first.close()  # leaves the socket file behind, as a crash would
    assert sock_path.exists()
    second = ipc.create_listener(sock_path)
    try:
        assert stat.S_ISSOCK(sock_path.stat().st_mode)
    finally:
        second.close()


def test_runtime_dir_is_private():
    d = ipc.runtime_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_overlong_path_is_a_clear_error(monkeypatch):
    long_path = Path("/tmp") / ("x" * 120)
    monkeypatch.setenv("APPLEMUSIC_MCP_SOCKET", str(long_path))
    with pytest.raises(OSError, match="macOS limit"):
        ipc.create_listener(ipc.socket_path())


def test_connect_without_helper_is_actionable(sock_path):
    with pytest.raises(ConnectionError, match="No Apple Music helper is listening"):
        ipc.connect(sock_path, timeout=1.0)


# --- the shim must stay inert ---------------------------------------------------


def test_shim_imports_nothing_that_talks_to_music():
    """The shim's whole security value is that it holds no capability.

    If it ever imports the AppleScript layer, the process the client spawns
    becomes a second thing that could drive Music.app — and the TCC scoping the
    split exists to achieve is silently undone.
    """
    tree = ast.parse(inspect.getsource(shim))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("applescript", "server", "subprocess", "requests"):
        assert banned not in imported, f"shim imports {banned}"
    assert "applescript" not in inspect.getsource(shim)


def test_shim_module_does_not_pull_in_the_server():
    """Importing the shim must not transitively import the Apple Events code."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import applemusic_mcp.shim; "
            "print('applemusic_mcp.applescript' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.strip() == "False", out.stderr


# --- end to end -----------------------------------------------------------------


def _helper_env(sock: Path, home: Path) -> dict:
    env = dict(os.environ)
    env["APPLEMUSIC_MCP_SOCKET"] = str(sock)
    env["APPLEMUSIC_MCP_HOME"] = str(home)
    return env


@pytest.mark.slow
def test_handshake_survives_the_shim_hop(sock_path, tmp_path):
    """A real JSON-RPC initialize must round-trip client → shim → helper → child.

    This is the regression test for a genuine bug found in development: dup2'ing
    the socket onto fd 0/1 is not enough, because the MCP stdio transport wraps
    the inherited ``sys.stdin.buffer`` whose seekability was cached at startup.
    A helper started with its stdin on a regular file (exactly what launchd
    does) then died with ESPIPE.
    """
    env = _helper_env(sock_path, tmp_path)
    helper = subprocess.Popen(
        [sys.executable, "-m", "applemusic_mcp", "helper"],
        env=env,
        stdin=subprocess.DEVNULL,
        # A regular file for stdout/stderr, reproducing the launchd arrangement
        # that triggered the ESPIPE bug.
        stdout=open(tmp_path / "helper.log", "w"),
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if sock_path.exists():
                break
            time.sleep(0.1)
        assert sock_path.exists(), (tmp_path / "helper.log").read_text()

        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        out = subprocess.run(
            [sys.executable, "-m", "applemusic_mcp", "shim"],
            input=request + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        first = out.stdout.strip().split("\n")[0]
        if not first:
            log = (tmp_path / "helper.log").read_text()
            raise AssertionError(f"no response.\nshim stderr: {out.stderr}\nhelper: {log}")
        msg = json.loads(first)
        assert msg["id"] == 1
        assert "serverInfo" in msg["result"]
    finally:
        helper.terminate()
        helper.wait(timeout=30)

    # The helper cleans up after itself so a later start isn't blocked by a
    # stale socket it would otherwise have to decide whether to unlink.
    assert not sock_path.exists()


def test_helper_child_inherits_no_listener(sock_path):
    """A session must not keep the listening socket open.

    If it did, killing the helper would leave a child holding the address and
    the next helper start would fail or, worse, two helpers would race.
    """
    src = inspect.getsource(__import__("applemusic_mcp.helper", fromlist=["helper"]))
    assert "listener.close()" in src
    tree = ast.parse(src)
    forks = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "fork"
    ]
    assert forks, "expected a fork-per-connection model"


def test_socket_is_unreachable_by_other_users(sock_path):
    """0600 on the socket is what keeps another local account out.

    We can't become another user in a test, so assert the mode that the kernel
    check keys on, plus that we can still connect as the owner.
    """
    listener = ipc.create_listener(sock_path)
    try:
        assert stat.S_IMODE(sock_path.stat().st_mode) & 0o077 == 0
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(sock_path))
        client.close()
    finally:
        listener.close()

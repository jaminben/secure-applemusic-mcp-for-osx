"""The bundle identifier must be written down exactly once.

It is not just a name. The launchd label, the unix socket path and the TCC row
that holds the Automation grant all key on it, so two builds sharing an
identifier fight over all three: a fork's helper answers on the original's
socket, its LaunchAgent replaces the original's, and macOS cannot tell their
permission grants apart.

It used to live in five files with "keep in sync" comments, which is a
convention, not a mechanism. Now every build script seds it out of ipc.py, and
these tests fail if anyone writes it down a second time.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IPC = ROOT / "src" / "applemusic_mcp" / "ipc.py"

# Every script that needs the identifier at build time.
BUILD_SCRIPTS = (
    "tools/build-app.sh",
    "tools/make-app-bundle.sh",
    "swift/amcp-setup/build.sh",
    "swift/amcp-musickit/build.sh",
)

# Where the literal is allowed to appear. ipc.py is the source; the docs quote
# it for humans, and the reset script must name the OLD one to clean it up.
ALLOWED = {
    "src/applemusic_mcp/ipc.py",
    "docs/PERMISSIONS.md",
    "README.md",
    "CHANGELOG.md",
    "CHANGELOG-upstream.md",
    "DISCLOSURE.md",
    "scripts/reset-install.sh",
    "tests/test_bundle_id_single_source.py",
}


def _canonical() -> str:
    m = re.search(r'^BUNDLE_ID = "([^"]+)"', IPC.read_text(encoding="utf-8"), re.M)
    assert m, "ipc.py no longer declares BUNDLE_ID the way the build scripts sed for"
    return m.group(1)


def test_ipc_declares_it():
    assert _canonical() == "io.github.jaminben.secure-applemusic-mcp"


@pytest.mark.parametrize("script", BUILD_SCRIPTS)
def test_build_scripts_derive_rather_than_hardcode(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "_ipc_bundle_id" in text, f"{script} does not derive the id from ipc.py"
    assert _canonical() not in text, (
        f"{script} hardcodes the bundle id. Derive it: a second copy is how the "
        "same string reached five files, and a fork that misses one collides "
        "with the original's socket, LaunchAgent and TCC grant."
    )


def _resolve(script: str, env: "dict | None" = None) -> subprocess.CompletedProcess:
    """Run a script's own derivation with a REAL BASH_SOURCE.

    BASH_SOURCE cannot be faked through `bash -c` -- assigning to it leaves it
    empty, `dirname ""` becomes `.`, and the snippet then reads a path relative
    to the test runner's cwd instead of the script's own directory. That looked
    like a broken derivation when the derivation was fine. So write the snippet
    to a real file beside the script and execute it.
    """
    path = ROOT / script
    body = path.read_text(encoding="utf-8")
    start = body.index("_ipc_bundle_id() {")
    end = body.index("\n", body.index('BUNDLE_ID="${BUNDLE_ID:-', start))
    probe = path.parent / ".bundle-id-probe.sh"
    probe.write_text(body[start:end] + '\nprintf "%s" "$BUNDLE_ID"\n', encoding="utf-8")
    try:
        return subprocess.run(
            ["bash", str(probe)], capture_output=True, text=True, timeout=30, env=env
        )
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.parametrize("script", BUILD_SCRIPTS)
def test_build_scripts_resolve_the_canonical_id(script):
    """Run each script's own derivation, in its own directory, for real."""
    out = _resolve(script)
    expected = _canonical() + (".setup" if "amcp-setup" in script else "")
    assert out.stdout == expected, f"{script} resolved {out.stdout!r}, expected {expected!r} ({out.stderr})"


@pytest.mark.parametrize("script", BUILD_SCRIPTS)
def test_an_env_override_wins(script):
    """A fork sets BUNDLE_ID once in the environment and every script follows."""
    out = _resolve(script, env={"PATH": "/usr/bin:/bin", "BUNDLE_ID": "com.example.fork"})
    assert out.stdout == "com.example.fork", out.stderr


def test_the_literal_appears_nowhere_else():
    """A grep is the only thing that actually enforces 'written down once'."""
    canonical = _canonical()
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel.startswith((".git/", ".venv/", "dist/", ".pytest_cache/", ".ruff_cache/")):
            continue
        # A built .app is an output, like dist/: its Info.plist is SUPPOSED to
        # carry the identifier, and it is regenerated from ipc.py every build.
        if ".app/" in rel or "__pycache__" in rel or path.suffix in {".pyc", ".icns", ".png", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if canonical in text:
            offenders.append(rel)
    assert not offenders, (
        "bundle id hardcoded outside ipc.py: " + ", ".join(sorted(offenders))
    )

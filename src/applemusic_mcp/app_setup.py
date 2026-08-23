"""First-run setup for the standalone AppleMusicMCP.app.

Runs when someone double-clicks the app. Three jobs, in this order:

1. Install a LaunchAgent so the helper is started by **launchd**. That is what
   gives the server its own TCC identity — see ``ipc`` and docs/PERMISSIONS.md.
2. Register with Claude Desktop by merging one entry into its config, pointed at
   the permissionless shim.
3. Trigger the Automation prompt *now*, while the app itself is the responsible
   process, so the dialog names this app and the grant lands on it — rather than
   surfacing mid-conversation attributed to whatever spawned the client.

Everything here is idempotent: running the app again repairs a half-finished
install rather than duplicating anything.

``subprocess`` is used for ``launchctl`` and ``osascript`` only, which the
capability invariants assert.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import ipc

APP_NAME = "AppleMusicMCP"
BUNDLE_ID = ipc.BUNDLE_ID

CLAUDE_CONFIG = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
)
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / BUNDLE_ID
SERVER_KEY = "apple-music"


# --- small native UI ---------------------------------------------------------


def _as_applescript_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    JSON and AppleScript agree on \" and \\ escaping, so json.dumps is the right
    tool — but only with ensure_ascii=False (see _dialog).
    """
    return json.dumps(str(value), ensure_ascii=False)


def _dialog(text: str, title: str = "Apple Music MCP", buttons=("OK",), default: int = 1) -> str:
    """Show a native dialog and return the button pressed (or "" if unavailable).

    Uses plain ``display dialog``, which runs in osascript's own context and
    needs no permission. Deliberately NOT ``tell application "System Events"``,
    which would require Accessibility — the one permission this build refuses
    to ask for.
    """
    btn_list = ", ".join(f'"{b}"' for b in buttons)
    script = (
        f"display dialog {json.dumps(text)} with title {json.dumps(title)} "
        f"buttons {{{btn_list}}} default button {default} with icon note"
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=600
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    # "button returned:OK"
    for part in (out.stdout or "").strip().split(", "):
        if part.startswith("button returned:"):
            return part.split(":", 1)[1]
    return ""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- 1. LaunchAgent ----------------------------------------------------------


def app_bundle_path() -> Path:
    """Path to the .app we're running from.

    ``sys.executable`` is the vendored interpreter at
    ``<App>/Contents/Resources/python/bin/python3``, so walk up to the bundle.
    """
    override = os.environ.get("APPLEMUSIC_APP_BUNDLE")
    if override:
        return Path(override)
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    # Not running from a bundle (dev checkout) — fall back to the repo root.
    return Path(__file__).resolve().parents[2]


def helper_executable() -> Path:
    return app_bundle_path() / "Contents" / "MacOS" / APP_NAME


def install_launch_agent() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": BUNDLE_ID,
        "ProgramArguments": [str(helper_executable()), "helper"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_DIR / "helper.log"),
        "StandardErrorPath": str(LOG_DIR / "helper.log"),
    }
    tmp = LAUNCH_AGENT.with_suffix(".plist.tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, LAUNCH_AGENT)
    os.chmod(LAUNCH_AGENT, 0o644)
    _log(f"wrote {LAUNCH_AGENT}")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=60)


def _service_target() -> str:
    return f"gui/{os.getuid()}/{BUNDLE_ID}"


def unload_agent(wait: float = 10.0) -> None:
    """Unload and WAIT — bootout is asynchronous, and bootstrapping over a
    still-registered service fails with 'service already loaded'."""
    _launchctl("bootout", _service_target())
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _launchctl("print", _service_target()).returncode != 0:
            return
        time.sleep(0.25)


def load_agent() -> bool:
    unload_agent()
    res = _launchctl("bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENT))
    if res.returncode != 0:
        _log(f"launchctl bootstrap failed: {res.stderr.strip()}")
        return False
    sock = ipc.socket_path()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sock.exists():
            return True
        time.sleep(0.25)
    _log("helper did not create its socket in time")
    return False


# --- 2. Claude Desktop -------------------------------------------------------


def claude_server_entry() -> dict:
    """The one entry we add. Points at the SHIM — never the helper."""
    return {"command": str(helper_executable()), "args": ["shim"]}


def configure_claude_desktop(config_path: Path | None = None) -> tuple[bool, str]:
    """Merge our entry into Claude Desktop's config.

    Merges rather than writes: the file usually contains the user's other MCP
    servers, and clobbering it would be a genuinely destructive act performed by
    an installer. A timestamped backup is kept, the write is atomic
    (temp + replace), and the file mode is preserved (Claude ships it 0600).
    """
    path = config_path or CLAUDE_CONFIG
    if not path.parent.exists():
        return False, (
            "Claude Desktop doesn't appear to be installed "
            f"(no {path.parent}). Skipped — you can add the server by hand later."
        )

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            # Never overwrite a config we could not parse: it is the user's, it
            # may hold other servers, and a broken merge would lose them.
            return False, (
                f"Couldn't read {path.name} ({exc}). Left it untouched — add the "
                "server manually (see the README)."
            )
        if not isinstance(data, dict):
            return False, f"{path.name} isn't a JSON object. Left it untouched."

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, f"{path.name} has a non-object 'mcpServers'. Left it untouched."

    already = servers.get(SERVER_KEY)
    entry = claude_server_entry()
    if already == entry:
        # Nothing to do. Returning before the backup matters: the app may be
        # launched many times, and a backup per launch would quietly fill the
        # directory with copies.
        return True, "Claude Desktop was already configured."

    if path.exists():
        backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        # copy2 copies contents then metadata, so the backup is briefly readable
        # at the umask default. Create it closed-first, then copy the bytes in.
        src_mode = path.stat().st_mode & 0o777
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, src_mode)
        with os.fdopen(fd, "wb") as out:
            out.write(path.read_bytes())
        _log(f"backed up {path.name} -> {backup.name}")

    servers[SERVER_KEY] = entry

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_suffix(".json.tmp")
    # Create the temp file with the FINAL mode, not the umask default. This file
    # holds a copy of the whole config, and other MCP servers routinely keep API
    # keys in their `env` blocks — a write-then-chmod leaves those world-readable
    # for the duration of the write. (Same gap auth._write_private closes for
    # token files.)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    verb = "Updated" if already else "Added"
    return True, f"{verb} the '{SERVER_KEY}' entry in Claude Desktop's config."


# --- 3. Permission -----------------------------------------------------------


def prime_permission() -> tuple[bool, str]:
    """Trigger the Automation prompt now, under THIS app's identity.

    Double-clicking means LaunchServices started us, so we are our own
    responsible process and the prompt names this app. Doing it here — rather
    than letting it surface on the first tool call, attributed to whatever
    spawned the MCP client — is the whole point of having a setup step.

    The script is a read: it asks Music for its name and starts nothing.
    """
    script = 'tell application "Music" to get name'
    try:
        res = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=120
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    if res.returncode == 0:
        return True, "Automation permission granted."
    err = (res.stderr or "").strip()
    if "-1743" in err or "Not authorized" in err:
        return False, (
            "Permission was declined. Re-run this app, or enable it in System Settings → "
            "Privacy & Security → Automation."
        )
    return False, err or "unknown error"


# --- orchestration -----------------------------------------------------------
#
# Each consequential step asks first, separately, and can be declined on its own.
# One blanket "Set Up" would not be meaningful consent for editing a config file
# the user did not write, or for triggering a system permission dialog. Declining
# a step is a normal outcome, not an error: the summary says what was skipped and
# how to do it later.


def _confirm(text: str, ok_label: str) -> bool:
    return _dialog(text, buttons=("Skip", ok_label), default=2) == ok_label


_INTRO = (
    "Apple Music MCP setup\n\n"
    "This lets Claude control the Music app on this Mac.\n\n"
    "There are three steps, and each one asks before it does anything:\n\n"
    "  1. Install a background helper (starts at login)\n"
    "  2. Add an entry to Claude Desktop's config\n"
    "  3. Ask macOS for permission to control Music\n\n"
    "You can skip any of them. Nothing has changed yet."
)

_STEP_HELPER = (
    "Step 1 of 3 — background helper\n\n"
    "Installs a LaunchAgent at:\n"
    "  ~/Library/LaunchAgents/{bundle}.plist\n\n"
    "It starts this app in the background at login. The helper is what actually "
    "talks to Music, and having launchd start it is what lets macOS grant the "
    "permission to THIS app rather than to your terminal.\n\n"
    "Remove it later by deleting that file, or by moving this app to the Trash "
    "and running:  launchctl bootout gui/$(id -u)/{bundle}"
)

_STEP_CLAUDE = (
    "Step 2 of 3 — Claude Desktop\n\n"
    "Adds one entry, '{key}', to:\n"
    "  {path}\n\n"
    "Your existing servers and settings are kept, and a timestamped backup of the "
    "file is written next to it first.\n\n"
    "Skip this if you would rather paste the config in yourself, or if you use a "
    "different MCP client."
)

_STEP_PERMISSION = (
    "Step 3 of 3 — permission to control Music\n\n"
    "macOS will now show its own dialog:\n"
    '  "AppleMusicMCP wants to control Music"\n\n'
    "Click OK there to allow it. Asking now — rather than in the middle of a "
    "conversation — is what makes the permission land on this app, so you can "
    "review or revoke it in System Settings → Privacy & Security → Automation.\n\n"
    "This never asks for Accessibility."
)


def main() -> int:
    if sys.platform != "darwin":
        _log("macOS only.")
        return 1

    if _dialog(_INTRO, buttons=("Quit", "Continue"), default=2) != "Continue":
        _log("cancelled by user before any changes")
        return 1

    steps: list[str] = []

    # 1. background helper
    if _confirm(_STEP_HELPER.format(bundle=BUNDLE_ID), "Install"):
        install_launch_agent()
        if load_agent():
            steps.append("✓ Background helper installed and running")
        else:
            steps.append(f"✗ Helper failed to start — see {LOG_DIR / 'helper.log'}")
    else:
        steps.append("• Helper skipped — run this app again to install it")

    # 2. Claude Desktop
    if _confirm(_STEP_CLAUDE.format(key=SERVER_KEY, path=CLAUDE_CONFIG), "Add Entry"):
        ok, msg = configure_claude_desktop()
        steps.append(("✓ " if ok else "✗ ") + msg)
    else:
        steps.append("• Claude Desktop config not touched")

    # 3. permission
    if _confirm(_STEP_PERMISSION, "Ask macOS"):
        ok, msg = prime_permission()
        steps.append(("✓ " if ok else "✗ ") + msg)
    else:
        steps.append("• Permission not requested — macOS will ask on first use instead")

    body = "\n".join(steps)
    tail = ""
    if any(s.startswith("✓") and "Claude" in s for s in steps):
        tail = "\n\nRestart Claude Desktop to pick up the new server."
    _dialog("Setup finished.\n\n" + body + tail, buttons=("Done",))
    _log(body)
    return 0

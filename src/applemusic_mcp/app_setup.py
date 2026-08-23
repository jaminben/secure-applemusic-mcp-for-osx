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
from typing import Optional

from . import clients, ipc, setup_ui

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
    # _as_applescript_string, NOT json.dumps: the default ensure_ascii=True
    # emits "✓", which is a SYNTAX ERROR in AppleScript, not a mangled
    # character. osascript then exits non-zero, _dialog returns "", and
    # _confirm reads that as "Skip" -- so a single em dash in a prompt silently
    # turns a consent step into a declined one. Every prompt below contains
    # one, and the summary always contains a check mark.
    btn_list = ", ".join(f"{_as_applescript_string(b)}" for b in buttons)
    script = (
        f"display dialog {_as_applescript_string(text)} "
        f"with title {_as_applescript_string(title)} "
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


def _choose(prompt: str, items: list[str], title: str = "Apple Music MCP") -> "Optional[list[str]]":
    """Native multi-select list. Returns the chosen labels, or None if cancelled.

    A checklist rather than one dialog per client: the consent that matters is
    "which of these files may I edit", and showing that as a single reviewable
    list is clearer than four sequential prompts the user learns to click
    through. Nothing is pre-selected -- the user picks, so an accidental Return
    installs nothing.
    """
    if not items:
        return []
    listing = ", ".join(_as_applescript_string(i) for i in items)
    script = (
        f"choose from list {{{listing}}} with title {_as_applescript_string(title)} "
        f"with prompt {_as_applescript_string(prompt)} "
        f"OK button name \"Add\" cancel button name \"Skip\" "
        f"with multiple selections allowed"
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=600
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = (out.stdout or "").strip()
    if out.returncode != 0 or not raw or raw == "false":
        return None
    # osascript joins the chosen items with ", ". Rather than trusting that
    # split, each token is matched against what we offered -- anything we did
    # not put in the list is discarded rather than acted on.
    offered = set(items)
    chosen = [tok for tok in raw.split(", ") if tok in offered]
    return chosen


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

    Kept as a named function because Claude Desktop is the client most people
    installing this will have; the work is now generic (see clients.py).
    """
    client = clients.find("claude-desktop")
    assert client is not None
    return clients.configure(client, claude_server_entry(), config_path)


def configure_detected_clients() -> list[str]:
    """Ask which installed MCP clients to configure, then configure those.

    Returns summary lines. Detection is deliberately separate from consent: we
    say what we found, and the user chooses. A client being installed is not a
    reason to edit its configuration.
    """
    found = clients.detected()
    if not found:
        return ["• No MCP clients detected — add the server by hand (see the README)"]

    labels = [c.label for c in found]
    caveats = "\n".join(f"  {c.label}: {c.caveat}" for c in found if c.caveat)
    prompt = (
        "Found these MCP clients on this Mac. Select the ones that should be "
        "able to control Music.\n\nEach file is backed up first, and your other "
        "servers are kept."
    )
    if caveats:
        prompt += "\n\nNote:\n" + caveats

    chosen = _choose(prompt, labels)
    if chosen is None:
        return ["• No client configs touched"]
    if not chosen:
        return ["• No clients selected"]

    lines = []
    configured = []
    for client in found:
        if client.label not in chosen:
            continue
        ok, msg = clients.configure(client, client.entry(str(helper_executable()), ["shim"]))
        lines.append(("✓ " if ok else "✗ ") + msg)
        if ok:
            configured.append(client)

    lines.extend(_offer_restart(configured))
    return lines


def _offer_restart(configured: list) -> list[str]:
    """Offer to quit and reopen the clients that are running.

    Every one of these reads its MCP config once, at startup. Editing the file
    under a running client is safe -- verified, not assumed: a Claude Desktop
    quit and a Claude Code session that predated the edit both preserved it --
    but the new server simply will not appear until the client restarts. So the
    edit is not the risk; leaving the user to wonder why nothing happened is.
    """
    running = [c for c in configured if c.restartable and clients.is_running(c)]
    stale = [c for c in configured if not c.restartable and c.caveat]

    out: list[str] = [f"• {c.label}: {c.caveat}" for c in stale]
    if not running:
        return out

    names = ", ".join(c.label for c in running)
    prompt = (
        f"{names} {'is' if len(running) == 1 else 'are'} running.\n\n"
        "MCP servers are read at startup, so Apple Music won't appear until "
        f"{'it restarts' if len(running) == 1 else 'they restart'}.\n\n"
        "Quit and reopen now? Anything unsaved should be saved first."
    )
    if _dialog(prompt, buttons=("Not Now", "Quit & Reopen"), default=1) != "Quit & Reopen":
        return out + [f"• {names}: restart to pick up the server"]

    for client in running:
        if not clients.quit_client(client):
            # Never escalate to a kill: an app that ignores SIGTERM has a
            # reason, and it is not worth the user's unsaved work.
            out.append(f"✗ {client.label} did not quit — restart it yourself")
            continue
        out.append(
            f"✓ {client.label} restarted"
            if clients.relaunch(client)
            else f"• {client.label} quit — reopen it to pick up the server"
        )
    return out


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

_STEP_CLIENTS = (
    "Step 2 of 3 — MCP clients\n\n"
    "Next you'll see the MCP clients found on this Mac, and you choose which "
    "ones get an '{key}' entry.\n\n"
    "For each one you pick: your existing servers and settings are kept, and a "
    "timestamped backup is written next to the file first.\n\n"
    "Skip this if you would rather paste the config in yourself."
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


def _client_options() -> list[dict]:
    options = []
    for client in clients.detected():
        option = {
            "id": client.key,
            "label": client.label,
            "checked": True,
            "detail": _tilde(client.config),
        }
        app = clients._app_path(client)
        if app is not None:
            option["iconPath"] = str(app)
        else:
            option["symbol"] = "terminal"
        if client.caveat:
            option["note"] = client.caveat
        elif clients.is_running(client):
            option["note"] = "Running — will be quit and reopened"
        options.append(option)
    return options


def _build_plan() -> dict:
    """The wizard: a splash saying what will happen, then one page per step.

    Order matters. The helper must exist before macOS can be asked to trust it,
    the permission must be granted before a client is pointed at something that
    cannot yet work, and the clients come last so the thing they are told about
    is already functioning.

    Each page says why the step is needed, what it grants, and — the part
    installers usually leave out — what it does not. A permission dialog with
    no stated limit is one the user has to take on faith.
    """
    return {
        "title": "Apple Music MCP Setup",
        "icon": setup_ui.icon_path(),
        "pages": [
            {
                "id": "splash",
                "title": "Apple Music MCP",
                "body": (
                    "This lets your AI assistants control the Music app on this "
                    "Mac — play something, search your library, build a playlist.\n\n"
                    "It asks for one macOS permission, and only one: the ability "
                    "to send commands to Music. Not to your files, not to your "
                    "browser, not to other apps.\n\n"
                    "Here is what setup will do. Nothing has changed yet."
                ),
                "bullets": [
                    {
                        "label": "1.  Install a background helper",
                        "detail": "So the permission belongs to this app, not to your terminal.",
                        "symbol": "gearshape.fill",
                    },
                    {
                        "label": "2.  Ask macOS for permission to control Music",
                        "detail": "One app. Revocable at any time in System Settings.",
                        "iconPath": "/System/Applications/Music.app",
                    },
                    {
                        "label": "3.  Add it to your AI assistants",
                        "detail": "You choose which ones. Each config is backed up first.",
                        "symbol": "app.badge.checkmark",
                    },
                ],
                "next": "Continue",
            },
            {
                "id": "helper",
                "title": "Background helper",
                "body": (
                    "WHY  macOS grants app-control permission to whichever program "
                    "starts the process. If your AI client launched the server, the "
                    "permission would land on your client — or on your terminal, "
                    "which would then hold it for everything you ever run there.\n\n"
                    "Letting launchd start this helper makes it answerable for "
                    "itself, so the permission lands on this app alone.\n\n"
                    "WHAT IT ADDS  One LaunchAgent file, which starts the helper "
                    "at login:\n"
                    "    ~/Library/LaunchAgents/{bundle}.plist\n\n"
                    "WHAT IT DOES NOT DO  It has no window and no menu bar item. "
                    "It listens on a private socket inside your home folder and "
                    "nothing else — no network port, nothing reachable from "
                    "outside this Mac.\n\n"
                    "TO REMOVE  Delete that file, or move this app to the Trash."
                ).format(bundle=BUNDLE_ID),
                "action": "Install",
            },
            {
                "id": "permission",
                "title": "Permission to control Music",
                "body": (
                    "WHY  Everything this does — play, pause, skip, search your "
                    "library, edit playlists — is done by sending commands to the "
                    "Music app, the same way an AppleScript would. macOS requires "
                    "your consent for that, per app.\n\n"
                    "WHAT YOU ARE GRANTING  Permission for this app to send "
                    "commands to Music. That is the entire scope: macOS tracks it "
                    "per target application, so it conveys nothing about any other "
                    "app on this Mac.\n\n"
                    "WHAT IT IS NOT  This is not Accessibility. Accessibility "
                    "would allow typing and clicking into any application, cannot "
                    "be limited to one app, and is what a keylogger needs. This "
                    "app never asks for it, and is built so it cannot use it.\n\n"
                    "It also grants no access to your files, your browser, your "
                    "messages or your keychain.\n\n"
                    "TO REVOKE  System Settings → Privacy & Security → Automation. "
                    "Turning it off stops this app; nothing else breaks."
                ),
                "action": "Ask macOS",
            },
            {
                "id": "clients",
                "title": "Add to your AI assistants",
                "body": (
                    "WHY  Each client needs one line of configuration telling it "
                    "how to start the server.\n\n"
                    "WHAT CHANGES  A single 'apple-music' entry per client. Your "
                    "other MCP servers and settings are untouched, and a "
                    "timestamped backup is written next to each file first.\n\n"
                    "WORTH KNOWING  The entry points at a shim that holds no "
                    "permission of its own — it just relays to the helper over "
                    "that private socket. So your AI client never inherits the "
                    "Music permission, and revoking it in System Settings still "
                    "works."
                ),
                "options": _client_options(),
                "action": "Add",
            },
            {
                "id": "summary",
                "title": "Setup finished",
                "body": (
                    "Any client that was already running needs a restart to pick "
                    "up the server.\n\n"
                    "You can review or revoke the Music permission at any time in "
                    "System Settings → Privacy & Security → Automation."
                ),
                "next": "Done",
            },
        ],
    }


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _run_step(page: str, selected: list) -> "tuple[bool, list[str]]":
    """Carry out one wizard page. Never raises: a failure is a reported line."""
    try:
        if page == "helper":
            install_launch_agent()
            if load_agent():
                return True, ["✓ Background helper installed and running"]
            return False, [f"✗ Helper failed to start — see {LOG_DIR / 'helper.log'}"]

        if page == "clients":
            lines, configured = [], []
            for client in clients.detected():
                if client.key not in selected:
                    continue
                ok, msg = clients.configure(
                    client, client.entry(str(helper_executable()), ["shim"])
                )
                lines.append(("✓ " if ok else "✗ ") + msg)
                if ok:
                    configured.append(client)
            if not lines:
                return True, ["• No clients selected"]
            lines.extend(_offer_restart(configured))
            return all(not ln.startswith("✗") for ln in lines), lines

        if page == "permission":
            ok, msg = prime_permission()
            return ok, [("✓ " if ok else "✗ ") + msg]
    except Exception as exc:  # noqa: BLE001 - a step must not kill the wizard
        _log(f"step {page} failed: {exc}")
        return False, [f"✗ {exc}"]
    return True, []


def _run_with_window() -> "Optional[int]":
    """The wizard. Returns an exit code, or None to fall back to dialogs."""
    outcome = setup_ui.run_wizard(_build_plan(), _run_step)
    if outcome is None:
        return None
    if not outcome:
        _log("cancelled by user")
        return 1
    return 0


def main() -> int:
    if sys.platform != "darwin":
        _log("macOS only.")
        return 1

    code = _run_with_window()
    if code is not None:
        return code

    # No window in this build: ask with plain dialogs instead. Never assume.
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

    # 2. MCP clients
    if _confirm(_STEP_CLIENTS.format(key=SERVER_KEY), "Choose Clients"):
        steps.extend(configure_detected_clients())
    else:
        steps.append("• No client configs touched")

    # 3. permission
    if _confirm(_STEP_PERMISSION, "Ask macOS"):
        ok, msg = prime_permission()
        steps.append(("✓ " if ok else "✗ ") + msg)
    else:
        steps.append("• Permission not requested — macOS will ask on first use instead")

    body = "\n".join(steps)
    tail = ""
    if any(s.startswith("✓") and "config" in s for s in steps):
        tail = "\n\nRestart your MCP client(s) to pick up the new server."
    _dialog("Setup finished.\n\n" + body + tail, buttons=("Done",))
    _log(body)
    return 0

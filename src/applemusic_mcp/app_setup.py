"""First-run setup for the standalone app.

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

from . import __version__, clients, ipc, musickit, setup_ui

APP_NAME = "AppleMusicMCP"
# Tips-and-tricks video channel, shown on the last page of setup. Empty means
# no link is shown at all -- a placeholder URL is worse than none.
YOUTUBE_URL = ""
BUNDLE_ID = ipc.BUNDLE_ID

CLAUDE_CONFIG = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
)
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / BUNDLE_ID
SERVER_KEY = "unofficial-apple-music"


# --- small native UI ---------------------------------------------------------


def _as_applescript_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    JSON and AppleScript agree on \" and \\ escaping, so json.dumps is the right
    tool — but only with ensure_ascii=False (see _dialog).
    """
    return json.dumps(str(value), ensure_ascii=False)


def _dialog(text: str, title: str = "Unofficial Apple Music MCP", buttons=("OK",), default: int = 1) -> str:
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


def _choose(prompt: str, items: list[str], title: str = "Unofficial Apple Music MCP") -> "Optional[list[str]]":
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


SETUP_LOG = LOG_DIR / "setup.log"


def _log(msg: str) -> None:
    """Record a setup step to stderr AND to a file.

    stderr alone is unrecoverable in the case that matters. Double-clicking
    means LaunchServices started us, so stderr goes nowhere the user can reach
    -- and asking them to re-run the binary from a terminal to see it is the
    wrong advice, because that makes the terminal the responsible process and
    the permission grant lands there instead of on the app. So the one install
    we most need to debug is the one that leaves no trace.

    Appended, not truncated: a second run after a failed first is exactly the
    history worth keeping. Never raises -- setup must not die over logging.
    """
    print(msg, file=sys.stderr, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with SETUP_LOG.open("a", encoding="utf-8") as fh:
            for line in str(msg).splitlines() or [""]:
                fh.write(f"{stamp}  {line}\n")
    except OSError:
        pass


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


# Properties AppleScript answers from the app bundle, WITHOUT sending an Apple
# Event -- so reading one never consults TCC and never prompts. Anything used to
# probe for permission must not be in here. Verified against osascript: each of
# these returns exit 0 with Music not running and no authorisation granted.
PRIMER_LOCAL_PROPS = ("name", "version", "running", "frontmost")


def prime_permission() -> tuple[bool, str]:
    """Trigger the Automation prompt now, under THIS app's identity.

    Double-clicking means LaunchServices started us, so we are our own
    responsible process and the prompt names this app. Doing it here — rather
    than letting it surface on the first tool call, attributed to whatever
    spawned the MCP client — is the whole point of having a setup step.

    The property matters. AppleScript answers an application's ``name``,
    ``version``, ``running`` and ``frontmost`` from the app bundle itself,
    without sending an Apple Event at all -- so ``get name`` returns "Music"
    and exits 0 whether or not permission exists. This function used to run
    exactly that, which meant it never triggered the prompt and always
    reported success: setup said "Automation permission granted" on a machine
    that had granted nothing, and the -1743 branch below was unreachable.

    ``player state`` is a real property of the running application, so reading
    it is a genuine Apple Event and does face TCC. The cost is that it launches
    Music if it is not already open, which ``get name`` avoided -- but a check
    that cannot fail is worth nothing, and this app exists to drive Music.
    """
    # Must be a property only the running app can answer. See PRIMER_SAFE_PROPS.
    script = 'tell application "Music" to get player state'
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
    "Unofficial Apple Music MCP setup\n\n"
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
            option["note"] = "Open now — will be restarted"
        options.append(option)
    return options


def _musickit_page() -> "Optional[dict]":
    """The Apple Music page, or None if this build cannot offer it.

    A different permission from controlling Music.app: that one reaches the
    Music app on this Mac, this one reaches the Apple Music service. They
    appear in different places in System Settings and are granted separately.

    Apple's guidance for permission copy is to say plainly what the app will do
    and why, in a sentence or two, without jargon -- so the page says what it is
    for and what it cannot do, and leaves the mechanics out.
    """
    if not musickit.is_available():
        return None                       # no signed helper in this build

    page = {
        "id": "musickit",
        "title": "Play anything on Apple Music",
        "body": (
            "If you subscribe to Apple Music, this opens up the whole catalog — "
            "not just what's already in your library. Ask for a song by name, "
            "or for something you've never heard.\n\n"
            "It needs your permission to use Apple Music."
        ),
        "examples": [
            "What was the best song of 2024 I've probably never heard?",
            "Play the album that defined 90s hip-hop",
            "Something like Khruangbin, but faster",
        ],
        "footer": (
            "Anything it adds for you goes into a playlist called "
            "\u201cAdded by Music MCP\u201d, so it's easy to find. It can't buy "
            "anything or change your subscription.\n\n"
            "Optional — everything else works without it."
        ),
        "next": "Continue",
    }
    if musickit.authorization_status() == "authorized":
        page["footer"] += "\n\nYou've already allowed this."
    else:
        page["action"] = "Continue"
        page["skip"] = "Not Now"
    return page


def _build_plan() -> dict:
    """The wizard: a splash saying what will happen, then one page per step.

    Copy follows Apple's conventions for asking permission: short sentences,
    plain words, say what it does and why before the system dialog appears, and
    name where to turn it off. No jargon the reader has to already know --
    nothing here says LaunchAgent, Apple Events or daemon.
    """
    optional = _musickit_page()

    return {
        "title": "Unofficial Apple Music MCP",
        "icon": setup_ui.icon_path(),
        "pages": [
            {
                "id": "splash",
                "title": "Control Apple Music with AI",
                "body": (
                    "Ask your AI assistant to put music on — in your own words."
                ),
                "examples": [
                    "Play something upbeat for a run",
                    "Add this to my dinner party playlist",
                    "Turn it down a bit",
                ],
                "footer": (
                    "Setting up takes a minute. Nothing changes until you say so."
                ),
                "bullets": [
                    {
                        "label": "Set up the server",
                        "detail": "Runs quietly on this Mac.",
                        "symbol": "gearshape.fill",
                    },
                    {
                        "label": "Connect your AI assistants",
                        "detail": "You pick which ones.",
                        "symbol": "app.badge.checkmark",
                    },
                    {
                        "label": "Allow control of Music",
                        "detail": "The one permission it needs.",
                        "iconPath": "/System/Applications/Music.app",
                    },
                    {
                        "label": "Play anything on Apple Music",
                        "detail": "Optional. For Apple Music subscribers.",
                        "symbol": "sparkles",
                    },
                ],
                "next": "Continue",
            },
            {
                "id": "helper",
                "title": "Set up the server",
                "body": (
                    "This sets up the MCP server to run locally on this Mac.\n\n"
                    "It starts when you log in and stays quietly in the "
                    "background. There's no window, and nothing on the internet "
                    "can reach it.\n\n"
                    "Changed your mind later? Just drag the app to the Trash."
                ),
                "action": "Set Up",
            },
            {
                "id": "clients",
                "title": "Connect your AI assistants",
                "body": (
                    "Choose which apps can control your music.\n\n"
                    "Each one keeps the settings it already has, and a backup "
                    "is saved first."
                ),
                "options": _client_options(),
                "action": "Connect",
            },
            {
                "id": "permission",
                "title": "Allow control of Music",
                "body": (
                    "macOS will ask whether Unofficial Apple Music MCP can "
                    "control Music. "
                    "Choose OK, and you'll be able to say things like:"
                ),
                "examples": [
                    "Play my Discover Weekly",
                    "Skip this one",
                    "Make a playlist of everything I loved this year",
                ],
                "footer": (
                    "That's the only permission it needs. It can't see your "
                    "files, your browser, or anything else on your Mac.\n\n"
                    "You can turn it off any time in System Settings > "
                    "Privacy & Security > Automation."
                ),
                "action": "Continue",
            },
            *([optional] if optional else []),
            {
                "id": "summary",
                "title": "You're all set",
                "body": (
                    "Go ahead and ask for something."
                ),
                "examples": ["Play something mellow while I work"],
                "footer": (
                    "If an app was already open, restart it so it picks up the "
                    "new connection.\n\n"
                    "You can change these permissions any time in System "
                    "Settings > Privacy & Security."
                ),
                "links": (
                    [{"label": "Tips and tricks on YouTube", "url": YOUTUBE_URL}]
                    if YOUTUBE_URL.startswith("https://")
                    else []
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

        if page == "musickit":
            ok, detail = musickit.request_authorization()
            if ok:
                return True, ["✓ Apple Music access granted"]
            # Declining is a normal answer here, not a failure to fix.
            return False, [f"• Apple Music access not granted ({detail or 'declined'})"]
    except Exception as exc:  # noqa: BLE001 - a step must not kill the wizard
        _log(f"step {page} failed: {exc}")
        return False, [f"✗ {exc}"]
    return True, []


def _run_with_window() -> "Optional[int]":
    """The wizard. Returns an exit code, or None to fall back to dialogs.

    The dialog fallback logs its outcome in one block at the end; the wizard
    had no equivalent, so a windowed run recorded the LaunchAgent write and
    nothing else -- a log showing no client step beside a config that was
    plainly written, and no way to tell whether the permission step ran, was
    declined, or was never reached. Every step is now recorded as it happens.

    A page the wizard never asks us to run leaves no line at all, which is
    itself the signal: "not reached" and "declined" look different here.
    """

    def logged(page: str, selected: list) -> "tuple[bool, list[str]]":
        _log(f"step {page}: running" + (f" (selected: {', '.join(selected)})" if selected else ""))
        ok, lines = _run_step(page, selected)
        for line in lines:
            _log(f"step {page}: {line}")
        _log(f"step {page}: {'ok' if ok else 'FAILED'}")
        return ok, lines

    outcome = setup_ui.run_wizard(_build_plan(), logged)
    if outcome is None:
        _log("no wizard window; falling back to dialogs")
        return None
    if not outcome:
        _log("cancelled by user")
        return 1
    _log("wizard finished")
    return 0


def is_translocated(path: "Optional[Path]" = None) -> bool:
    """Is macOS running us from a randomized read-only mount?

    Gatekeeper translocates a quarantined app launched from wherever it was
    downloaded. The bundle then lives under .../AppTranslocation/<uuid>/d/,
    which disappears on quit -- so a LaunchAgent written now points at a path
    that will not exist later, and the install silently half-works. Dragging
    the app to /Applications is what avoids it, which is why the README says
    to, and why a report of "nothing happened" needs this in the log.
    """
    p = str(path or app_bundle_path())
    return "/AppTranslocation/" in p


def _log_environment() -> None:
    """The facts a bug report needs and a user cannot be asked to gather."""
    bundle = app_bundle_path()
    _log(f"--- setup {BUNDLE_ID} v{__version__}")
    _log(f"bundle: {bundle}")
    if is_translocated(bundle):
        _log(
            "WARNING: running translocated (a randomized read-only copy). "
            "Quit, drag the app to /Applications, and open it from there."
        )
    elif not str(bundle).startswith("/Applications/"):
        _log(f"note: not running from /Applications (at {bundle.parent})")
    try:
        quarantine = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", str(bundle)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        _log(f"quarantine: {quarantine or 'none'}")
    except (OSError, subprocess.SubprocessError):
        pass
    _log(f"wizard: {'yes' if setup_ui.window_path() else 'no (dialog fallback)'}")
    _log(f"musickit helper: {'yes' if musickit.is_available() else 'no'}")


def main() -> int:
    if sys.platform != "darwin":
        _log("macOS only.")
        return 1

    _log_environment()

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

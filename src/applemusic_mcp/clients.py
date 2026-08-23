"""Which MCP clients are on this Mac, and how to add ourselves to each.

Every client here speaks the same stdio JSON-RPC we already serve, so adding
support for one is a config-file shape, not a code path. What differs is only
where the file lives, what the servers object is called, and whether the entry
needs a transport field.

Two rules apply to all of them, and both exist because an installer editing a
file the user did not write is a genuinely consequential act:

* **Never fabricate a config directory.** If the parent does not exist, the
  client is not installed, and creating the directory would leave a
  configuration for software that isn't there. Detection is therefore "the
  client left evidence on this disk", never "we could write here".

* **Never lose what is already in the file.** These files hold the user's other
  MCP servers, and some hold much more than that. Read, merge, back up,
  write atomically, preserve the mode.

Adding a client: append to ``known_clients()``. If it does not fit the shape
below, it does not belong here — write it as its own function rather than
bending the descriptor until it fits.

**ChatGPT desktop is deliberately absent.** Checked on 23 Aug 2026: its support
directory (``~/Library/Application Support/com.openai.chat``) holds only
conversation state, with no MCP configuration anywhere on disk. Its connectors
are remote MCP servers reached over HTTPS and registered server-side against
the user's account — there is no local file an installer could edit, and a
local stdio server like this one cannot be reached that way at all. Exposing it
would mean running a public HTTPS endpoint with its own authentication, which is
a different product, not a config entry. Codex is a separate matter and *is*
supported below: it speaks local stdio MCP through ``~/.codex/config.toml``.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SERVER_KEY = "apple-music"


@dataclass(frozen=True)
class Client:
    """One MCP client and the file that configures it."""

    key: str
    name: str
    config: Path
    # "json" for every client but Codex, which uses TOML.
    fmt: str = "json"
    servers_key: str = "mcpServers"
    # Extra fields this client wants inside the server entry. VS Code needs an
    # explicit transport; the others infer stdio from the presence of `command`.
    entry_extra: dict = field(default_factory=dict)
    # Other paths that prove the client is installed, for the case where it is
    # present but has never written an MCP config.
    evidence: tuple[Path, ...] = ()
    caveat: str = ""
    # Bundle identifiers this client ships under. Preferred over the file name
    # for locating the app: names change (ChatGPT Classic became ChatGPT in a
    # point release, same bundle id) and a rename must not silently drop the
    # icon or the ability to restart it.
    bundle_ids: tuple[str, ...] = ()
    # Show the installed app's own name alongside ours. For clients whose app
    # is called something a user would not connect to the name here.
    show_app_name: bool = False
    # Fallback alias when the app is not installed to read a name from.
    aka: str = ""
    # A GUI app we can quit and reopen for the user. False for anything that
    # lives in a terminal, where "restart it" is the user's job.
    restartable: bool = True

    @property
    def label(self) -> str:
        """What to call this in the picker.

        Reads the name off the installed app so a rename fixes itself, and
        falls back to the declared alias when nothing is installed.
        """
        if not self.show_app_name:
            return self.name
        app = _app_path(self)
        alias = app.stem if app is not None else self.aka
        if not alias or alias.lower() in self.name.lower():
            return self.name
        return f"{self.name} ({alias})"

    def installed(self) -> bool:
        return any(p.exists() for p in (self.config, *self.evidence))

    def entry(self, command: str, args: list[str]) -> dict:
        return {**self.entry_extra, "command": command, "args": list(args)}


def app_dirs() -> tuple[Path, ...]:
    """Where a Mac app may be installed. Separate from known_clients() so a
    test can point detection somewhere other than the real /Applications."""
    return (Path("/Applications"), Path.home() / "Applications")


def known_clients() -> list[Client]:
    """Built per call, not at import: tests relocate HOME."""
    home = Path.home()
    support = home / "Library" / "Application Support"
    apps = app_dirs()

    def app(name: str) -> tuple[Path, ...]:
        return tuple(base / f"{name}.app" for base in apps)

    return [
        Client(
            key="claude-desktop",
            bundle_ids=("com.anthropic.claudefordesktop",),
            name="Claude Desktop",
            config=support / "Claude" / "claude_desktop_config.json",
            evidence=(*app("Claude"), support / "Claude"),
        ),
        Client(
            key="claude-code",
            name="Claude Code",
            config=home / ".claude.json",
            evidence=(home / ".claude",),
            # Tested, not assumed: a session started BEFORE this edit still
            # preserves it on exit, so there is no clobber risk. What is true
            # of every client here is that MCP config is read at startup, and
            # a terminal session is not ours to restart.
            caveat="Restart Claude Code afterwards",
            restartable=False,
        ),
        Client(
            key="cursor",
            bundle_ids=("com.todesktop.230313mzl4w4u92",),
            name="Cursor",
            config=home / ".cursor" / "mcp.json",
            evidence=(*app("Cursor"), home / ".cursor"),
        ),
        Client(
            key="windsurf",
            name="Windsurf",
            config=home / ".codeium" / "windsurf" / "mcp_config.json",
            evidence=(*app("Windsurf"), home / ".codeium" / "windsurf"),
        ),
        Client(
            key="codex",
            name="Codex",
            bundle_ids=("com.openai.codex",),
            show_app_name=True,
            aka="ChatGPT",
            config=home / ".codex" / "config.toml",
            fmt="toml",
            servers_key="mcp_servers",
            # Shipped as ChatGPT Classic.app on this machine (bundle id
            # com.openai.codex); the older name is checked too.
            evidence=(
                *app("Codex"), *app("ChatGPT Classic"), home / ".codex",
            ),
        ),
        Client(
            key="vscode",
            bundle_ids=("com.microsoft.VSCode",),
            name="VS Code",
            config=support / "Code" / "User" / "mcp.json",
            servers_key="servers",
            entry_extra={"type": "stdio"},
            evidence=(*app("Visual Studio Code"), support / "Code" / "User"),
        ),
    ]


def _bundle_index() -> "dict[str, Path]":
    """Bundle identifier -> installed .app, for the applications directories.

    Reads Info.plist rather than trusting file names. Rebuilt per call: setup
    runs once, and a cache keyed on nothing would be wrong the moment a test
    (or the user) moved an app.
    """
    index: dict[str, Path] = {}
    for directory in app_dirs():
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for app in entries:
            if app.suffix != ".app":
                continue
            try:
                with open(app / "Contents" / "Info.plist", "rb") as handle:
                    info = plistlib.load(handle)
            except Exception:  # noqa: BLE001
                # Third-party bundles, so anything goes: missing plists,
                # truncated XML, expat errors that are not ValueErrors. One
                # unreadable app must not stop the scan.
                continue
            identifier = info.get("CFBundleIdentifier")
            # First match wins, so /Applications beats ~/Applications.
            if isinstance(identifier, str) and identifier not in index:
                index[identifier] = app
    return index


def _app_path(client: Client) -> Optional[Path]:
    """The installed .app for this client, if we can find one."""
    if client.bundle_ids:
        index = _bundle_index()
        for identifier in client.bundle_ids:
            found = index.get(identifier)
            if found is not None:
                return found
    # No bundle id declared, or none installed: fall back to the known names.
    return next((p for p in client.evidence if p.suffix == ".app" and p.exists()), None)


def running_pids(client: Client) -> list[int]:
    """PIDs of this client's main executable. Empty if it is not running."""
    app = _app_path(client)
    if app is None:
        return []
    try:
        proc = subprocess.run(
            ["pgrep", "-f", f"{app}/Contents/MacOS/"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return [int(line) for line in proc.stdout.split() if line.isdigit()]


def is_running(client: Client) -> bool:
    return bool(running_pids(client))


def quit_client(client: Client, timeout: float = 20.0) -> bool:
    """Ask this client to quit, and wait for it to actually go.

    SIGTERM rather than an AppleScript ``quit``: telling another application to
    quit is an Apple Event, which would make macOS ask the user to let this app
    *control* that one. That is a permanent, far broader grant than "close
    yourself once", and this fork does not ask for permissions it does not
    need. SIGTERM needs none and reaches the same normal shutdown path.

    Never escalates to SIGKILL. A client that ignores SIGTERM is one with
    unsaved state, and killing it to save the user a menu click is a bad trade
    -- the caller reports it instead.
    """
    pids = running_pids(client)
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not running_pids(client):
            return True
        time.sleep(0.25)
    return not running_pids(client)


def relaunch(client: Client) -> bool:
    """Reopen the client. LaunchServices, so no permission is involved."""
    app = _app_path(client)
    if app is None:
        return False
    try:
        proc = subprocess.run(
            ["open", "-a", str(app)], capture_output=True, text=True, timeout=30
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def detected() -> list[Client]:
    """The installed clients, in the order above."""
    return [c for c in known_clients() if c.installed()]


def find(key: str) -> Optional[Client]:
    return next((c for c in known_clients() if c.key == key), None)


# --- TOML ------------------------------------------------------------------------
#
# Codex is the one client that is not JSON. There is no TOML writer in the
# standard library (tomllib reads only), and rather than take a dependency this
# edits the raw text: our table is replaced in place if present and appended if
# not. That is not a shortcut -- a parse-and-rewrite would silently discard every
# comment and all formatting in a file the user maintains by hand, which is a
# worse outcome than the narrow edit.


def _toml_string(value: str) -> str:
    """A TOML basic string. Escapes what the spec requires, nothing more."""
    out = str(value)
    for bad, good in (
        ("\\", "\\\\"), ('"', '\\"'),
        ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"),
    ):
        out = out.replace(bad, good)
    # Remaining C0 controls have no short escape; \uXXXX is the spec's form.
    out = "".join(c if c >= " " or c in "" else f"\\u{ord(c):04X}" for c in out)
    return f'"{out}"'


def _toml_section(servers_key: str, name: str, entry: dict) -> str:
    lines = [f"[{servers_key}.{name}]"]
    lines.append(f"command = {_toml_string(entry['command'])}")
    args = ", ".join(_toml_string(a) for a in entry.get("args", []))
    lines.append(f"args = [{args}]")
    return "\n".join(lines) + "\n"


def _replace_toml_section(text: str, header: str, section: str) -> str:
    """Swap an existing ``[header]`` table for ``section``, or append it.

    A table runs until the next line that opens one, so the span to replace is
    from its header to the next top-of-line ``[``.
    """
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == header),
        None,
    )
    if start is None:
        joiner = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{joiner}\n{section}"
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].lstrip().startswith("[")),
        len(lines),
    )
    return "".join(lines[:start]) + section + "".join(lines[end:])


def is_configured(client: Client, entry: dict, config_path: Optional[Path] = None) -> bool:
    """Is our entry already present and identical? Never raises."""
    path = config_path or client.config
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data[client.servers_key][SERVER_KEY] == entry
    except (OSError, ValueError, KeyError, TypeError):
        return False


def configure(
    client: Client, entry: dict, config_path: Optional[Path] = None
) -> tuple[bool, str]:
    """Merge our entry into one client's config.

    Returns (ok, message). Declining to touch a file is a normal outcome
    reported as ``False`` with a reason — not an exception — because the caller
    is an installer summarising several clients at once.
    """
    path = config_path or client.config

    if not path.parent.exists():
        return False, (
            f"{client.name} doesn't appear to be installed "
            f"(no {path.parent}). Skipped — you can add the server by hand later."
        )

    if client.fmt == "toml":
        return _configure_toml(client, entry, path)

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

    servers = data.setdefault(client.servers_key, {})
    if not isinstance(servers, dict):
        return False, (
            f"{path.name} has a non-object '{client.servers_key}'. Left it untouched."
        )

    already = servers.get(SERVER_KEY)
    if already == entry:
        # Return before the backup: setup may run many times, and a backup per
        # launch would quietly fill the directory with copies.
        return True, f"{client.name} was already configured."

    if path.exists():
        backup = path.with_suffix(f"{path.suffix}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        # copy2 would copy contents first and metadata after, leaving the backup
        # briefly readable at the umask default. Create it with the source mode
        # already applied, then write the bytes in.
        src_mode = path.stat().st_mode & 0o777
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, src_mode)
        with os.fdopen(fd, "wb") as out:
            out.write(path.read_bytes())

    servers[SERVER_KEY] = entry

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    # Created with the FINAL mode rather than the umask default: this file holds
    # a copy of the whole config, and other MCP servers routinely keep API keys
    # in their `env` blocks — a write-then-chmod leaves those world-readable for
    # the duration of the write.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)

    verb = "Updated" if already else "Added"
    return True, f"{verb} the '{SERVER_KEY}' entry in {client.name}'s config."


def _configure_toml(client: Client, entry: dict, path: Path) -> tuple[bool, str]:
    """The Codex branch of configure(). Same guarantees, different syntax."""
    import tomllib

    raw = ""
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            existing = tomllib.loads(raw).get(client.servers_key, {})
        except (OSError, ValueError) as exc:
            return False, (
                f"Couldn't read {path.name} ({exc}). Left it untouched — add the "
                "server manually (see the README)."
            )
        if not isinstance(existing, dict):
            return False, (
                f"{path.name} has a non-table '{client.servers_key}'. Left it untouched."
            )
        current = existing.get(SERVER_KEY)
        if isinstance(current, dict) and current.get("command") == entry["command"] and list(
            current.get("args", [])
        ) == list(entry.get("args", [])):
            return True, f"{client.name} was already configured."
        had = SERVER_KEY in existing
    else:
        had = False

    if path.exists():
        backup = path.with_suffix(f"{path.suffix}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        src_mode = path.stat().st_mode & 0o777
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, src_mode)
        with os.fdopen(fd, "wb") as out:
            out.write(path.read_bytes())

    section = _toml_section(client.servers_key, SERVER_KEY, entry)
    updated = _replace_toml_section(raw, f"[{client.servers_key}.{SERVER_KEY}]", section)

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(updated)
    os.replace(tmp, path)

    return True, f"{'Updated' if had else 'Added'} the '{SERVER_KEY}' entry in {client.name}'s config."

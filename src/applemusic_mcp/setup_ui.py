"""The native first-run window, and the fallback for when it is missing.

This module only *launches* the window and parses its answer. The window is a
small signed AppKit app (``swift/amcp-setup``) that draws checkboxes and prints
JSON; it holds no policy, does no work, and needs no permissions. Everything
that follows from the user's answer happens in ``app_setup``.

Two deliberate properties:

* **A missing or broken window is a decline, never a default-yes.** Every
  failure path here returns None, and the caller falls back to asking with
  plain dialogs rather than assuming consent.

* **The plan is built in Python.** The window is told what to draw. It does not
  know what a LaunchAgent is, cannot discover clients, and cannot widen the
  set of things the user is agreeing to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

SETUP_APP = "AMCPSetup.app"
_REL = Path("Contents") / "MacOS" / "AMCPSetup"
_ENV_OVERRIDE = "APPLEMUSIC_SETUP_UI"
_TIMEOUT = 1800  # the user may leave the window open; do not time them out
# Handler signature: (page_id, selected_ids) -> (ok, summary_lines)
Handler = Callable[[str, list], "tuple[bool, list]"]


def _candidates() -> list[Path]:
    """Where the window may live, most-specific first."""
    found: list[Path] = []
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        found.append(Path(override).expanduser())

    # Inside the installed app bundle, as a nested helper.
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            found.append(parent / "Contents" / "Helpers" / SETUP_APP / _REL)
            break

    # A source checkout, after swift/amcp-setup/build.sh.
    repo = Path(__file__).resolve().parents[2]
    found.append(repo / "swift" / "amcp-setup" / SETUP_APP / _REL)
    return found


def window_path() -> Optional[Path]:
    for candidate in _candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_available() -> bool:
    return window_path() is not None


def icon_path() -> Optional[str]:
    """Something for the splash to show. The installed bundle if we are in one,
    otherwise the generated .icns from a source checkout."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return str(parent)
    icns = Path(__file__).resolve().parents[2] / "tools" / "icon" / "AppleMusicMCP.icns"
    return str(icns) if icns.exists() else None


def run_wizard(plan: dict, handler: Handler) -> Optional[bool]:
    """Drive the wizard. Returns True if it ran to the end, False if the user
    cancelled, and None if the window could not be shown at all.

    None is never "the user said yes": the caller falls back to asking with
    plain dialogs. Each step is carried out by ``handler`` while the window
    waits, so a failure is reported on the page that caused it.
    """
    exe = window_path()
    if exe is None:
        return None
    try:
        proc = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None

    def write(message: dict) -> bool:
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError):
            return False

    outcome: Optional[bool] = None
    try:
        if not write(plan):
            return None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = message.get("type")
            if kind == "run":
                selected = message.get("selected")
                ok, lines = handler(
                    str(message.get("page", "")),
                    [s for s in selected if isinstance(s, str)]
                    if isinstance(selected, list)
                    else [],
                )
                if not write({"ok": bool(ok), "lines": list(lines)}):
                    break
            elif kind == "finished":
                outcome = True
                break
            elif kind == "cancel":
                outcome = False
                break
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # The window vanished without saying anything: treat as "could not ask"
    # only if it never got as far as a decision.
    return outcome

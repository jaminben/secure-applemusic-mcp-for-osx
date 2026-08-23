"""Bridge to the signed Swift MusicKit helper.

The helper exists to do the one thing Apple Events cannot: put a catalog track
into the user's library. Music.app's ``play`` needs an object specifier, and a
track you do not own has none — which is why upstream reached for synthetic
clicks and why the REST rail needs an Apple Music credential.

MusicKit needs neither. ``MusicDataRequest`` signs the API call with both the
developer token and the Music User Token itself, derived from the helper app's
identity plus the user's consent, so **nothing secret is shipped and nothing is
stored**. See docs/PERMISSIONS.md for how that identity is established (it is
the app's Team ID and bundle id, validated server-side — notably NOT a
provisioning-profile entitlement, which is the wrong turn that cost an evening).

This module only locates the helper, calls it with an argv list, and parses its
JSON. It deliberately holds no policy: whether to add at all is decided by the
caller from the ``catalog_play`` preference.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

HELPER_APP = "AMCPMusicKit.app"
_HELPER_REL = Path("Contents") / "MacOS" / "AMCPMusicKit"
_ENV_OVERRIDE = "APPLEMUSIC_MUSICKIT_HELPER"
_TIMEOUT = 60


def _candidates() -> list[Path]:
    """Where the helper may live, most-specific first."""
    found: list[Path] = []
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        found.append(Path(override).expanduser())

    # Inside the installed app bundle, as a nested helper.
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            found.append(parent / "Contents" / "Helpers" / HELPER_APP / _HELPER_REL)
            break

    # A source checkout, after swift/amcp-musickit/build.sh.
    repo = Path(__file__).resolve().parents[2]
    found.append(repo / "swift" / "amcp-musickit" / HELPER_APP / _HELPER_REL)
    return found


def helper_path() -> Optional[Path]:
    """The helper executable, or None when this build does not ship one."""
    for candidate in _candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_available() -> bool:
    return helper_path() is not None


def _run(*args: str) -> tuple[bool, dict]:
    exe = helper_path()
    if exe is None:
        return False, {"error": "MusicKit helper not installed in this build"}
    try:
        proc = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, {"error": "MusicKit helper timed out"}
    except OSError as exc:
        return False, {"error": f"could not run the MusicKit helper: {exc}"}

    raw = (proc.stdout or "").strip()
    if not raw:
        # An unsigned or wrongly-signed helper is SIGKILLed by the kernel before
        # it can print anything ("restricted entitlements ... validation failed"),
        # so silence means a signing problem, not an API problem. Say so.
        return False, {
            "error": (
                f"MusicKit helper produced no output (exit {proc.returncode}). "
                "It is probably unsigned or signed without the right identity."
            )
        }
    try:
        payload = json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError:
        return False, {"error": f"unparseable helper output: {raw[:160]}"}
    return bool(payload.get("ok")), payload


def authorization_status() -> str:
    """'authorized' | 'denied' | 'restricted' | 'notDetermined' | 'unavailable'."""
    ok, payload = _run("status")
    if not ok and "status" not in payload:
        return "unavailable"
    return str(payload.get("status", "unknown"))


def request_authorization() -> tuple[bool, str]:
    """Show the native Apple Music permission prompt (once, at setup time)."""
    ok, payload = _run("authorize")
    return ok, str(payload.get("error") or payload.get("status", ""))


def add_to_library(catalog_id: str) -> tuple[bool, str]:
    """Add one catalog song to the library. Returns (ok, message).

    ``catalog_id`` is re-validated inside the helper too — this is a value that
    reaches Apple's API, and one validation site is one too few.
    """
    # ASCII digits only. str.isdigit() is True for Unicode digits such as
    # Arabic-Indic "٣٤٥", which would sail through a naive check and straight
    # into a URL. (Swift's Character.isNumber has the same trap.)
    raw = str(catalog_id).strip()
    if not (raw.isascii() and raw.isdigit()):
        return False, "catalog id must be numeric (ASCII digits)"
    ok, payload = _run("add", str(catalog_id))
    if ok:
        return True, f"added (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))

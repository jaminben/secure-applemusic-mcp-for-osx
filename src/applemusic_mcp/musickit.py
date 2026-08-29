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

    # Beside this module, which is where the wheel puts it. A pip/uvx install
    # has no .app parent and no source checkout, so without this the packaged
    # install silently had no MusicKit rail — the reason this project did not
    # publish to PyPI at all until the helper was proven to survive a wheel
    # with its signature and notarization intact.
    found.append(Path(__file__).resolve().parent / HELPER_APP / _HELPER_REL)

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


def _valid_catalog_id(catalog_id: str) -> Optional[str]:
    """ASCII digits only, or None.

    str.isdigit() is True for Unicode digits such as Arabic-Indic "٣٤٥", which
    would sail through a naive check and straight into a URL. (Swift's
    Character.isNumber has the same trap, which is why the helper re-checks —
    one validation site is one too few for a value that reaches Apple's API.)
    """
    raw = str(catalog_id).strip()
    return raw if (raw and raw.isascii() and raw.isdigit()) else None


def _valid_prefixed_id(value: str, prefix: str) -> Optional[str]:
    """``<prefix>`` followed by ASCII alphanumerics, or None.

    ``p.`` is a library playlist, ``i.`` a library song. These land in a URL
    PATH rather than a query value, so a stray ``/`` or ``..`` would change
    which resource is addressed instead of merely failing.
    """
    raw = str(value).strip()
    if not raw.startswith(prefix) or not (len(prefix) < len(raw) <= 64):
        return None
    tail = raw[len(prefix) :]
    return raw if (tail.isascii() and tail.isalnum()) else None


def _valid_playlist_id(playlist_id: str) -> Optional[str]:
    """A library playlist id (``p.XXXXXXXX``), or None."""
    return _valid_prefixed_id(playlist_id, "p.")


def _valid_track_ref(track_id: str, kind: str) -> Optional[str]:
    """The id a library playlist will accept for ``kind``, or None.

    ``songs`` is a CATALOG song — attaching it adds it to the library
    implicitly. ``library-songs`` is a track already in the library, named by
    its ``i.`` id. Getting these crossed is not a validation nicety: Apple
    rejects the mismatched pair, and the failure reads as "the track doesn't
    exist" rather than "you named it the wrong way".
    """
    if kind == "songs":
        return _valid_catalog_id(track_id)
    if kind == "library-songs":
        return _valid_prefixed_id(track_id, "i.")
    return None


def add_to_library(catalog_id: str) -> tuple[bool, str]:
    """Add one catalog song to the library. Returns (ok, message)."""
    if _valid_catalog_id(catalog_id) is None:
        return False, "catalog id must be numeric (ASCII digits)"
    ok, payload = _run("add", str(catalog_id).strip())
    if ok:
        return True, f"added (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))


def add_album_to_library(catalog_id: str) -> tuple[bool, str]:
    """Add one catalog ALBUM to the library.

    Songs and albums are different resources to Apple (``ids[songs]`` vs
    ``ids[albums]``); the helper hardcoded the first, which is the whole reason
    album adds still required a developer token.
    """
    if _valid_catalog_id(catalog_id) is None:
        return False, "catalog id must be numeric (ASCII digits)"
    ok, payload = _run("add-album", str(catalog_id).strip())
    if ok:
        return True, f"added album (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))


def rate_song(catalog_id: str, rating: str) -> tuple[bool, str]:
    """Love or dislike a CATALOG song.

    Tracks already in the library are rated through Music.app over Apple Events;
    a catalog id has no library object to set a rating on, which is what sent
    this down the developer-token path.
    """
    if _valid_catalog_id(catalog_id) is None:
        return False, "catalog id must be numeric (ASCII digits)"
    if str(rating).lower().strip() not in ("love", "dislike"):
        return False, "rating must be 'love' or 'dislike'"
    ok, payload = _run("rate", str(catalog_id).strip(), str(rating).lower().strip())
    if ok:
        return True, f"rated (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))


def unrate_song(catalog_id: str) -> tuple[bool, str]:
    """Clear a rating. The counterpart to :func:`rate_song`.

    Exists because the release gate must leave no residue: without a way to
    remove a rating, "exercise the rating path" and "leave the account as you
    found it" were mutually exclusive, so the gate did neither.
    """
    if _valid_catalog_id(catalog_id) is None:
        return False, "catalog id must be numeric (ASCII digits)"
    ok, payload = _run("unrate", str(catalog_id).strip())
    if ok:
        return True, f"rating cleared (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))


def add_track_to_playlist(playlist_id: str, track_id: str, kind: str = "songs") -> tuple[bool, str]:
    """Attach one track to a library playlist.

    For Apple-Music-origin playlists this is the ONLY rail: AppleScript can edit
    only the playlists Music.app itself owns.
    """
    pid = _valid_playlist_id(playlist_id)
    if pid is None:
        return False, "playlist id must look like p.XXXXXXXX"
    tid = _valid_track_ref(track_id, kind)
    if tid is None:
        return False, f"track id {track_id!r} does not match kind {kind!r}"
    ok, payload = _run("playlist-add", pid, tid, kind)
    if ok:
        return True, f"added to playlist (HTTP {payload.get('httpStatus')})"
    return False, str(payload.get("error", "unknown MusicKit error"))


def playlist_tracks(playlist_id: str) -> tuple[bool, object]:
    """Every track in a library playlist. Returns ``(ok, data | error)``.

    Needed so the tokenless rail can refuse duplicates. Skipping that check
    would be the cheaper option and the wrong one — silently stacking copies of
    a track is a bug this codebase has already paid for once.
    """
    pid = _valid_playlist_id(playlist_id)
    if pid is None:
        return False, "playlist id must look like p.XXXXXXXX"
    ok, payload = _run("playlist-tracks", pid)
    if not ok:
        return False, str(payload.get("error", "unknown MusicKit error"))
    ok, body = _json_body(payload)
    if not ok:
        return False, body
    data = body.get("data")
    return True, data if isinstance(data, list) else []


def _json_body(payload: dict) -> tuple[bool, object]:
    """Parse the ``body`` a read verb returns. Shared so every read fails the
    same way rather than each inventing its own half-guard."""
    try:
        body = json.loads(payload.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False, "could not parse the MusicKit response"
    return (True, body) if isinstance(body, dict) else (False, "unexpected response shape")


def library_song(library_id: str) -> tuple[bool, object]:
    """Metadata for one library song id (``i.XXXX``). Returns ``(ok, attrs)``.

    The last read with no public equivalent: an ``i.`` id names a row in the
    user's OWN library, which no anonymous endpoint can see. Returns Apple's
    ``attributes`` dict so callers read ``name``/``artistName`` exactly as they
    do on the REST rail.
    """
    lid = _valid_prefixed_id(library_id, "i.")
    if lid is None:
        return False, "library song id must look like i.XXXXXXXX"
    ok, payload = _run("library-song", lid)
    if not ok:
        return False, str(payload.get("error", "unknown MusicKit error"))
    ok, body = _json_body(payload)
    if not ok:
        return False, body
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return False, f"no library song {lid}"
    return True, data[0].get("attributes", {}) or {}


def catalog_search(term: str, types: str = "songs", limit: int = 25) -> tuple[bool, object]:
    """Search Apple's catalog through the account's storefront.

    The public iTunes Search API already does this with no credential, so this
    is for the margins where the two differ (relevance, and types iTunes does
    not index the same way). Prefer the public rail: this one costs a process
    launch per call. Returns Apple's ``results`` dict.
    """
    query = str(term or "").strip()
    if not query or len(query) > 512:
        return False, "term must be 1-512 characters"
    allowed = {"songs", "albums", "artists", "playlists"}
    wanted = [t.strip() for t in str(types or "").split(",") if t.strip()]
    if not wanted or not set(wanted) <= allowed:
        return False, f"types must be a subset of {sorted(allowed)}"
    try:
        count = int(limit)
    except (TypeError, ValueError):
        return False, "limit must be an integer"
    count = max(1, min(count, 25))
    ok, payload = _run("catalog-search", query, ",".join(wanted), str(count))
    if not ok:
        return False, str(payload.get("error", "unknown MusicKit error"))
    ok, body = _json_body(payload)
    if not ok:
        return False, body
    results = body.get("results")
    return True, results if isinstance(results, dict) else {}


def resolve_isrcs(codes: "list[str]") -> tuple[bool, object]:
    """Resolve ISRCs to catalog songs. Returns ``(ok, data | error message)``.

    The one query no PUBLIC Apple endpoint answers — the iTunes Search API,
    which covers this server's other catalog needs with no credential, has no
    ISRC filter at all. Batched deliberately: Apple's filter takes a list, and a
    process launch per track would make this rail unaffordable.

    ``data`` is Apple's ``data`` array verbatim, the same shape the REST rail
    returns, so callers need no second code path.
    """
    cleaned = [str(c).strip().upper() for c in (codes or []) if str(c).strip()]
    if not cleaned or len(cleaned) > 100:
        return False, "expected between 1 and 100 ISRCs"
    for code in cleaned:
        if len(code) != 12 or not (code.isascii() and code.isalnum()):
            return False, f"not an ISRC: {code}"
    ok, payload = _run("isrc", ",".join(cleaned))
    if not ok:
        return False, str(payload.get("error", "unknown MusicKit error"))
    ok, body = _json_body(payload)
    if not ok:
        return False, body
    data = body.get("data")
    return True, data if isinstance(data, list) else []

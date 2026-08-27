#!/usr/bin/env python3
"""Preflight check for the live integration gate.

Exits 0 when this machine can actually run the live tests, and non-zero with
guidance otherwise. The point is to FAIL LOUDLY rather than let the live suite
silently skip and produce a false-green release gate.

What "ready" means changed. The old version required a developer token AND a
media-user-token, and probed the catalog through ``applemusic_mcp.amp_api`` —
a module removed in d4fc279. So this script raised ImportError on every
machine, tokens or not, and `make preflight` could not complete at all. A
mandatory gate that cannot run is worse than no gate: RELEASING.md says not to
bump the version without it, so the rule was being followed by nobody.

The rail it checked is also no longer the primary one. The signed MusicKit
helper does catalog adds, ratings, playlist edits and library reads with no
stored credential, and the public iTunes endpoints cover catalog reads. A
developer token is now an optional rate-limit upgrade. So the gate asks the
question that actually matters:

    is there at least one usable WRITE rail, and is catalog resolution working?

Either rail satisfies it. A machine with only MusicKit is a valid release
machine — indeed it is the configuration most users are on.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from applemusic_mcp import auth, musickit, server  # noqa: E402

CLI = "secure-applemusic-mcp"


def _musickit_rail() -> tuple[bool, str]:
    """(usable, description). Never raises — a missing helper is a normal state."""
    if not musickit.is_available():
        return False, (
            "MusicKit helper not built. Run `make swift` (or "
            "swift/amcp-musickit/build.sh --sign <Developer ID>) — an unsigned "
            "helper builds but every write is denied."
        )
    status = musickit.authorization_status()
    if status == "authorized":
        return True, "MusicKit helper: authorized (no credential stored)"
    if status == "notDetermined":
        return False, (
            "MusicKit helper built but not yet approved. Approve it once with "
            "config(action='signin') from an MCP client, or run "
            "`swift/amcp-musickit/AMCPMusicKit.app/Contents/MacOS/AMCPMusicKit authorize`."
        )
    if status == "denied":
        return False, (
            "Apple Music access is DENIED for the helper. Re-enable it under "
            "System Settings → Privacy & Security → Media & Apple Music."
        )
    return False, f"MusicKit helper reported status {status!r}."


def _token_rail() -> tuple[bool, str]:
    if not auth.has_any_developer_token():
        return False, f"No developer token (optional — run `{CLI} login --dev` to add one)."
    try:
        auth.get_user_token()
    except Exception:
        return False, (
            f"Developer token present but no media-user-token. Run `{CLI} login` "
            "to complete that rail, or ignore it and use MusicKit."
        )
    return True, "Developer token + media-user-token: present"


def main() -> int:
    rails = []
    notes = []

    for label, check in (("musickit", _musickit_rail), ("token", _token_rail)):
        ok, message = check()
        notes.append(("✓" if ok else "·", message))
        if ok:
            rails.append(label)

    problems = []
    if not rails:
        problems.append(
            "No usable write rail. The live tests mutate a real account, so at "
            "least one of the two above must work."
        )

    # Catalog resolution is the basis of every add, and it has a credential-free
    # rail — so probe THAT, not a token endpoint. If this fails the machine has
    # no network or Apple is down, and no rail will save the live tests.
    if not problems:
        try:
            hits = server._catalog_search_itunes("Bohemian Rhapsody Queen", 1)
        except Exception as exc:  # network failure
            hits = []
            problems.append(f"Public catalog search raised: {exc}")
        if not hits:
            problems.append(
                "Public catalog search resolved no known track (network, or "
                "storefront misconfigured) — resolution is the basis of every add."
            )

    print("Live environment:")
    for mark, message in notes:
        print(f"  {mark} {message}")

    if problems:
        print("\nLIVE ENVIRONMENT NOT READY:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nFix the above, then re-run. (The live gate also needs an ACTIVE "
            "Apple Music subscription — the add/playlist tests prove that by "
            "actually mutating the account.)"
        )
        return 1

    print(f"\nReady. Usable write rail(s): {', '.join(rails)}.")
    if "musickit" not in rails:
        print(
            "  NOTE: the MusicKit rail is the one most users are on, and it is "
            "NOT being exercised by this run."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

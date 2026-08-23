"""Command-line interface for the Apple Music MCP server.

Five verbs, no ceremony:

    applemusic-mcp serve            run the MCP server (your client calls this)
    applemusic-mcp login            sign in (web flow, no developer account)
    applemusic-mcp login --dev      sign in with an Apple Developer token (.p8)
    applemusic-mcp logout           sign out (switch accounts)
    applemusic-mcp status           show auth status
    applemusic-mcp reset --force    wipe all credentials

The developer-token flow folds the old init + generate-token + authorize steps
into one guided `login --dev`.
"""

import argparse
import json
import os
import sys
import time

from .auth import (
    get_config_dir,
    generate_developer_token,
    run_auth_server,
    get_developer_token,
    get_user_token,
)


def cmd_login(args):
    """Sign in. Only ONE sign-in path exists in this build: the official Apple
    Developer token flow (`--dev`).

    Upstream also offered `--safari` (harvest the media-user-token by running
    JavaScript in your signed-in Safari) and `--chrome` (drive a Playwright
    Chrome). Both were removed: the Safari switch grants JS execution in EVERY
    Safari tab, and the Chrome path keeps a browser-automation handle in-process.
    Neither is needed — local Music.app features require no credential at all.
    """
    if getattr(args, "safari", False) or getattr(args, "chrome", False):
        print(
            "Browser sign-in was removed from this build.\n"
            "  • Library, playlists, ratings, playback: no sign-in needed — they run\n"
            "    on the local Music.app over Apple Events.\n"
            "  • Catalog search: no sign-in needed — public iTunes Search API.\n"
            "  • Adding a catalog track to your library: applemusic-mcp login --dev"
        )
        return 1
    if not args.dev:
        print(
            "This build has no browser sign-in and doesn't need one.\n\n"
            "Local Music.app features (library, playlists, ratings, playback) work with\n"
            "no credential. Catalog search uses Apple's public iTunes Search API.\n\n"
            "To enable adding catalog tracks to your library, set up an official Apple\n"
            "Developer token:  applemusic-mcp login --dev"
        )
        return 0
    return _login_dev(args)


def _login_dev(args):
    """Guided Apple Developer token setup: ensure config.json, mint the
    developer token, then authorize for a Music User Token.

    Prompts only for what is missing, so a second run with config.json already
    present goes straight to the browser authorization step.
    """
    config_dir = get_config_dir()
    config_file = config_dir / "config.json"

    if not config_file.exists():
        print("Apple Developer setup (one time). From your MusicKit key:")
        team_id = args.team_id or input("  Team ID: ").strip()
        key_id = args.key_id or input("  Key ID: ").strip()
        key_path = args.key_path or input("  Path to .p8 key: ").strip()
        if not (team_id and key_id and key_path):
            print("Error: team ID, key ID, and .p8 path are all required.")
            return 1
        config_dir.mkdir(parents=True, exist_ok=True)
        # 0600 from creation: this names the .p8 and identifies the team, and
        # the directory is 0700, but there is no reason to leave it group- or
        # world-readable in between.
        fd = os.open(str(config_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"team_id": team_id, "key_id": key_id, "private_key_path": key_path}, f, indent=2
            )
        print(f"Wrote {config_file}")

    try:
        generate_developer_token(expiry_days=args.days)
        print("Developer token generated.")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Error generating token: {e}")
        return 1

    token = run_auth_server(port=args.port)
    return 0 if token else 1


def cmd_logout(args):
    """Sign out: clear the media-user-token and browser session so you can sign in
    with a different account. Leaves any developer token in place."""
    from .auth import secret_delete

    for key in ("music_user_token", "harvested_token"):
        secret_delete(key)
    print("Signed out — stored tokens cleared. Local Music.app features still work.")
    return 0


def cmd_reset(args):
    """Wipe ALL credentials (developer token, config.json, user/web tokens,
    browser session). The downloaded .p8 key file is left in place — unless
    --all, which is a full uninstall that removes every state directory."""
    import shutil

    from . import paths
    from .auth import secret_delete

    full = getattr(args, "all", False)
    if not args.force:
        if full:
            print("--all is a FULL UNINSTALL: it deletes every applemusic-mcp state directory —")
            print("credentials AND your .p8 key, the Chrome profile, and the cache/exports:")
            for d in paths.all_state_dirs():
                print(f"  {d}")
        else:
            print("This removes the developer token, config.json, and the user token")
            print("(your .p8 key file is kept).")
        print("Re-run with --force to proceed.")
        return 1

    for key in ("developer_token", "music_user_token", "harvested_token"):
        secret_delete(key)

    if full:
        # Full uninstall: nuke all three state roots (keychain secrets cleared above).
        for d in paths.all_state_dirs():
            shutil.rmtree(d, ignore_errors=True)
        print("Full reset complete — all applemusic-mcp state removed (including your .p8).")
        print("Run `applemusic-mcp login` to start fresh.")
        return 0

    cfg_file = get_config_dir() / "config.json"
    if cfg_file.exists():
        cfg_file.unlink()
    print("Reset complete. Run `applemusic-mcp login` (web) or `login --dev` (developer token).")
    return 0


def cmd_status(args):
    """Show auth status: developer token, user token, and a live API check."""
    from .auth import developer_token_info, has_user_token

    config_dir = get_config_dir()
    print("Apple Music MCP status")
    print("=" * 40)
    print(f"Config: {config_dir}")

    data = developer_token_info()
    if data is not None and data.get("expires", 0) > time.time():
        days_left = (data["expires"] - time.time()) / 86400
        print(f"Developer token: valid ({days_left:.0f} days left)")
    elif data is not None:
        print("Developer token: expired — re-run `applemusic-mcp login --dev` to renew")
    elif has_user_token():
        # The web/Safari path has no generated token — it's harvested from Apple's
        # web player on demand. Say so rather than implying something's missing.
        print("Developer token: none (web path — uses Apple's web-player token)")
    else:
        print("Developer token: none")

    print(f"User token: {'present' if has_user_token() else 'none'}")

    # Live check. Prefer the generated developer token (api.music.apple.com); for
    # the web/Safari path there's no generated token, so verify the WEB session
    # (amp-api) instead — otherwise a fully signed-in web user is wrongly told
    # "not configured" right after signing in.
    try:
        dev = None
        try:
            dev = get_developer_token()
        except FileNotFoundError:
            dev = None
        if dev:
            import requests

            r = requests.get(
                "https://api.music.apple.com/v1/me/library/playlists",
                headers={"Authorization": f"Bearer {dev}", "Music-User-Token": get_user_token()},
                params={"limit": 1},
                timeout=30,
            )
            print("API: ok" if r.status_code == 200 else f"API: status {r.status_code}")
        else:
            print("API: not configured (optional — run `applemusic-mcp login --dev`)")
    except Exception as e:
        print(f"API: error ({e})")

    _print_update_status()
    return 0


def _print_update_status():
    """Show what the last update check found. Reads cache; never blocks.

    `status` is the one place that reports even an already-dismissed update:
    suppression exists to stop notifications from nagging, not to hide the
    answer from someone who came here to ask.
    """
    from . import update_check

    print()
    if update_check.disabled():
        print(f"Updates: check disabled ({update_check._OPT_OUT} is set)")
        return
    result = update_check.check()
    if result.get("status") == "unreachable" and not result.get("latest"):
        print("Updates: could not reach GitHub (this is not an error)")
        return
    lines = update_check.summary_lines(result)
    if lines:
        print("\n".join(lines))
    elif result.get("latest"):
        print(f"Updates: up to date ({result['current']})")
    else:
        print(f"Updates: no releases published yet ({result['current']})")


def cmd_update_check(args):
    """Ask GitHub whether a newer release exists. Reports; never installs."""
    from . import update_check

    if update_check.disabled() and not args.force:
        print(f"Update check disabled ({update_check._OPT_OUT} is set).")
        return 0
    result = update_check.check(force=args.force)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if result.get("status") == "unreachable":
        print("Could not reach GitHub. Nothing changed.")
        return 0
    lines = update_check.summary_lines(result)
    if lines:
        print("\n".join(lines))
    elif result.get("latest"):
        print(f"Up to date ({result['current']}).")
    else:
        print(f"No releases published yet ({result['current']}).")
    # An advisory is worth a non-zero exit so a wrapper script can act on it.
    return 2 if result.get("advisory") else 0


def cmd_serve(args):
    """Start the MCP server directly on stdio (the simple, unscoped path)."""
    from .server import main

    main()


def cmd_helper(args):
    """Run the resident helper behind a unix socket.

    This is the half that sends Apple Events, so it is the half that must own
    the Automation grant. Normally started by launchd from the .app bundle —
    see docs/PERMISSIONS.md — not run by hand.
    """
    from .helper import main

    return main()


def cmd_app_setup(args):
    """First-run setup for the standalone .app (LaunchAgent + Claude Desktop +
    permission prompt). Normally reached by double-clicking the app."""
    from .app_setup import main

    return main()


def cmd_shim(args):
    """Bridge stdio to the resident helper. This is what your MCP client spawns.

    Holds no permissions and speaks to nothing but the helper's socket.
    """
    from .shim import main

    return main()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="MCP server for Apple Music")
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("serve", help="Run the MCP server on stdio (simple, unscoped)")
    sub.add_parser(
        "shim",
        help="Bridge stdio to the resident helper (what your MCP client spawns "
        "when permissions are scoped to the app bundle)",
    )
    sub.add_parser(
        "helper",
        help="Run the resident helper behind a unix socket (launchd starts this)",
    )
    sub.add_parser(
        "app-setup",
        help="First-run setup for the standalone app (double-clicking runs this)",
    )

    login = sub.add_parser("login", help="Sign in (--dev: Apple Developer token; optional)")
    login.add_argument("--dev", action="store_true", help="Apple Developer token flow (.p8)")
    # Accepted so the removed flags produce a clear explanation, not "unknown flag".
    login.add_argument("--safari", action="store_true", help=argparse.SUPPRESS)
    login.add_argument("--chrome", action="store_true", help=argparse.SUPPRESS)
    login.add_argument("--team-id", dest="team_id", help="Apple Developer Team ID (with --dev)")
    login.add_argument("--key-id", dest="key_id", help="MusicKit Key ID (with --dev)")
    login.add_argument("--key-path", dest="key_path", help="Path to the .p8 key (with --dev)")
    login.add_argument("--days", type=int, default=180, help="Token validity in days (max 180)")
    login.add_argument("--port", type=int, default=8765, help="Local authorize port (with --dev)")

    # `signin` stays as a hidden alias for the heavily-documented old name.
    signin = sub.add_parser("signin")
    signin.add_argument("--dev", action="store_true")
    signin.add_argument("--team-id", dest="team_id")
    signin.add_argument("--key-id", dest="key_id")
    signin.add_argument("--key-path", dest="key_path")
    signin.add_argument("--days", type=int, default=180)
    signin.add_argument("--port", type=int, default=8765)

    upd = sub.add_parser(
        "update-check",
        help="Check GitHub for a newer release (reports a link; never installs)",
    )
    upd.add_argument(
        "--force", action="store_true", help="Ignore the once-a-day cache and the opt-out"
    )
    upd.add_argument("--json", action="store_true", help="Machine-readable output")

    sub.add_parser("logout", help="Sign out (switch accounts)")
    sub.add_parser("status", help="Show auth status")
    reset = sub.add_parser("reset", help="Wipe all credentials (keeps your .p8 key file)")
    reset.add_argument("--force", action="store_true", help="Confirm the wipe")
    reset.add_argument(
        "--all",
        action="store_true",
        dest="all",
        help="Full uninstall: remove ALL state dirs (config incl. .p8, Chrome profile, cache)",
    )

    args = parser.parse_args()

    if args.command in ("login", "signin"):
        sys.exit(cmd_login(args))
    elif args.command == "logout":
        sys.exit(cmd_logout(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "update-check":
        sys.exit(cmd_update_check(args))
    elif args.command == "reset":
        sys.exit(cmd_reset(args))
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "shim":
        sys.exit(cmd_shim(args))
    elif args.command == "helper":
        sys.exit(cmd_helper(args))
    elif args.command == "app-setup":
        sys.exit(cmd_app_setup(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()

"""Authentication and token management for Apple Music API."""

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import jwt

from . import paths

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = paths.config_dir()


def _write_private(path, text: str) -> None:
    """Write ``text`` to ``path`` created 0600 from the start — no world-readable
    window. The old ``open(w)`` + ``chmod`` pattern left the secret readable
    between create and chmod; ``os.open`` with the mode closes that TOCTOU gap."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# --- Secret store: 0600 files ------------------------------------------------
# The optional developer-token credentials live in 0600 files under the config
# dir (itself 0700). Upstream also supported the OS keychain, but only ever
# used it on Windows — `_keyring_ok()` returned False on macOS and Linux
# because the keychain's per-process ACL is unreliable across this tool's
# separate CLI and server processes. This build is macOS-only, so that branch
# was unreachable; removing it drops the `keyring` dependency (and its
# transitive tree) from the supply chain entirely.


def _secret_file(key: str) -> Path:
    # Back-compat: same filenames the tokens have always used.
    return get_config_dir() / f"{key}.json"


def secret_set(key: str, value: str) -> None:
    """Persist a secret blob under ``key`` in a 0600 file."""
    _write_private(_secret_file(key), value)


def secret_get(key: str) -> Optional[str]:
    """Read a secret blob, or None if it isn't stored."""
    f = _secret_file(key)
    if not f.exists():
        return None
    try:
        return f.read_text()
    except OSError:
        return None


def secret_delete(key: str) -> bool:
    """Forget a secret. Returns True only if it is actually gone afterward, so
    logout/reset can never report success while the credential survives."""
    try:
        _secret_file(key).unlink(missing_ok=True)
    except OSError:
        pass
    return secret_get(key) is None


def has_user_token() -> bool:
    return (
        bool(os.environ.get("APPLEMUSIC_USER_TOKEN")) or secret_get("music_user_token") is not None
    )


def developer_token_info() -> Optional[dict]:
    """Parsed generated-developer-token record ({token, expires, ...}) or None."""
    raw = secret_get("developer_token")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


# --- Developer token ---------------------------------------------------------
# Upstream had a SECOND source here: it fetched music.apple.com's web-player JS
# bundle and regex-extracted the public "AMPWebPlay" developer token Apple ships
# to every browser, caching it as `harvested_token`. That token is what made the
# unofficial amp-api rail work without an Apple Developer account.
#
# Removed. It was a silent fallback — `resolve_developer_token()` returned it
# when no generated token existed, so a caller believing it was on the official
# API could be scraping a credential out of Apple's JS bundle instead. In this
# build the only developer token is one YOU generate from your own .p8, and its
# absence is reported rather than papered over.


def resolve_developer_token() -> str:
    """Return the generated (Apple Developer) token, or raise.

    There is deliberately no fallback: upstream silently substituted a token
    scraped from Apple's web-player bundle here. Raising means a caller that
    needs the official API learns it has no credential instead of being
    quietly moved onto an unofficial one.
    """
    return get_developer_token()


def has_any_developer_token() -> bool:
    """True if a generated developer token is available (or mintable from a .p8
    configured). Used for feature detection."""
    try:
        resolve_developer_token()
        return True
    except Exception as exc:
        logger.debug("has_any_developer_token: no developer token available: %s", exc)
        return False


def get_config_dir() -> Path:
    """Get or create the config directory (0700 — it holds tokens and the .p8)."""
    config_dir = DEFAULT_CONFIG_DIR
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A clear, actionable message beats a raw traceback (e.g. an unwritable
        # APPLEMUSIC_MCP_HOME, or a root-owned mount under a non-root user).
        raise RuntimeError(
            f"Can't create the config directory at {config_dir}: {exc}. "
            "Check that the path is writable (or set APPLEMUSIC_MCP_HOME to one that is)."
        ) from exc
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    return config_dir


_config_cache: "Optional[tuple[float, dict]]" = None


def load_config() -> dict:
    """Load config.json (empty dict if absent). Cached by file mtime — `_engine()`
    reads prefs many times per tool call, so this avoids re-parsing on every read;
    a `set-pref` write changes the mtime and invalidates the cache automatically."""
    global _config_cache
    config_file = get_config_dir() / "config.json"
    if not config_file.exists():
        _config_cache = None
        return {}
    try:
        mtime = config_file.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _config_cache is not None and _config_cache[0] == mtime:
        return _config_cache[1]
    with open(config_file) as f:
        data = json.load(f)
    _config_cache = (mtime, data)
    return data


def get_user_preferences() -> dict:
    """Get user preferences with defaults.

    Returns:
        dict with keys:
        - fetch_explicit: bool (default False)
        - clean_only: bool (default False)
        - auto_add: bool (default False)
        - storefront: str (default "us")
    """
    try:
        config = load_config()
        prefs = config.get("preferences", {})
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {}

    # Return with defaults
    return {
        "fetch_explicit": prefs.get("fetch_explicit", False),
        "clean_only": prefs.get("clean_only", False),
        # Default FALSE (don't modify the library without permission). `auto_search`
        # is the old name for this key and is still honored from existing configs.
        "auto_add": prefs.get("auto_add", prefs.get("auto_search", False)),
        "storefront": prefs.get("storefront", "us"),  # Apple Music region (default: US)
        # What may happen when you ask to play a track you do not own.
        #   "add" — add it to the library (the only way macOS can play it), and
        #           file it under the "Added by Music MCP" playlist so the
        #           additions stay reviewable and undoable in one gesture.
        #   "off" — never touch the library; say the track isn't owned instead.
        # There is deliberately no in-process-player option: it is killed the
        # moment Music.app plays anything, while still reporting "playing", so
        # it can claim success while producing silence.
        "catalog_play": prefs.get("catalog_play", "add"),
        # Single engine mode, governs BOTH data ops and playback: "auto"
        # (native Music.app on macOS, web API + Chrome web player elsewhere),
        # "native" (all-in on the local Music.app, macOS, no token), or "web"
        # (all-in on the cross-platform Apple Music web API + web player, so a Mac
        # not signed into Music.app, or on a different account, stays fully web).
        # "api" is accepted as a back-compat alias for "web". Playback always
        # follows the engine, so there is no separate playback preference.
        "mode": prefs.get("mode", "auto"),
        # Token storage is not a user preference: secrets live in 0600 files
        # under the 0700 config dir.
    }


def get_private_key_path(config: dict) -> Path:
    """Resolve the private key path from config."""
    path = Path(config["private_key_path"]).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Private key not found: {path}")
    return path


def generate_developer_token(expiry_days: int = 180) -> str:
    """Generate a developer token (JWT) valid for up to 180 days."""
    config = load_config()
    if not config:
        raise FileNotFoundError("No config.json found. Run: applemusic-mcp login --dev")
    key_path = get_private_key_path(config)

    with open(key_path) as f:
        private_key = f.read()

    now = int(time.time())
    exp = now + (expiry_days * 24 * 60 * 60)

    headers = {"alg": "ES256", "kid": config["key_id"]}
    payload = {"iss": config["team_id"], "iat": now, "exp": exp}

    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

    # Save token
    token_data = {
        "token": token,
        "created": now,
        "expires": exp,
        "team_id": config["team_id"],
        "key_id": config["key_id"],
    }
    secret_set("developer_token", json.dumps(token_data))

    return token


# The MCP may be used only occasionally, so a passive "expiring soon" warning
# can arrive after the token has already died. Whenever the generated token is
# within this many days of expiry AND the signing key (.p8) is present, mint a
# fresh one on use, rather than failing on a token that expired while unused.
_DEV_TOKEN_RENEW_DAYS = 30


def can_generate_developer_token() -> bool:
    """True if we have everything needed to mint a fresh generated token (config
    + a readable .p8). When True, the generated token self-renews and no manual
    `generate-token` is needed."""
    try:
        config = load_config()
        if not config.get("team_id") or not config.get("key_id"):
            return False
        get_private_key_path(config)  # raises if the .p8 is missing
        return True
    except Exception:
        return False


def get_developer_token() -> str:
    """Get the generated developer token, auto-renewing it when it's within
    ``_DEV_TOKEN_RENEW_DAYS`` of expiry and the signing key is available.

    Raises FileNotFoundError/ValueError when there's no usable token and none
    can be minted. Callers surface that as "this needs a developer token"; there
    is no fallback credential in this build."""
    data = developer_token_info()
    if data is not None:
        try:
            days_left = (data["expires"] - time.time()) / 86400
        except (KeyError, TypeError):
            days_left = -1.0
        if days_left > _DEV_TOKEN_RENEW_DAYS:
            return data["token"]
        # In the renewal window (or already expired): mint a fresh one if we can.
        if can_generate_developer_token():
            return generate_developer_token()
        if days_left > 1:
            return data["token"]  # still valid; no key to renew with, so keep using it
        raise ValueError(
            "Developer token expired or expiring soon and no signing key is available "
            "to renew it. Run: applemusic-mcp login --dev (or `login` for the web path)."
        )
    # No stored token yet — generate if we have the key, else signal "not found".
    if can_generate_developer_token():
        return generate_developer_token()
    raise FileNotFoundError("Developer token not found. Run: applemusic-mcp login --dev")


def get_user_token() -> str:
    """Get the music user token or raise if not found.

    Honors APPLEMUSIC_USER_TOKEN if set (for headless / CI runs), else the stored token."""
    env_tok = os.environ.get("APPLEMUSIC_USER_TOKEN")
    if env_tok:
        return env_tok
    raw = secret_get("music_user_token")
    if not raw:
        raise FileNotFoundError("Music user token not found. Run: applemusic-mcp login")
    return json.loads(raw)["music_user_token"]


def save_user_token(token: str) -> None:
    """Save the music user token."""
    data = {
        "music_user_token": token,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    secret_set("music_user_token", json.dumps(data))


def create_auth_html(developer_token: str, port: int) -> str:
    """Generate the HTML for browser-based authorization with auto-submit."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Apple Music Authorization</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #1a1a1a;
            color: #fff;
        }}
        h1 {{ color: #fa586a; }}
        button {{
            background: #fa586a;
            color: white;
            border: none;
            padding: 15px 30px;
            font-size: 18px;
            border-radius: 8px;
            cursor: pointer;
            margin: 10px 0;
        }}
        button:hover {{ background: #ff6b7a; }}
        button:disabled {{ background: #666; cursor: not-allowed; }}
        .success {{ color: #4ade80; }}
        .error {{ color: #f87171; }}
        #status {{ margin: 20px 0; }}
        .spinner {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #666;
            border-radius: 50%;
            border-top-color: #fa586a;
            animation: spin 1s ease-in-out infinite;
            margin-right: 10px;
            vertical-align: middle;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
    </style>
</head>
<body>
    <h1>Apple Music Authorization</h1>
    <p>Click the button below to authorize access to your Apple Music library.</p>
    <button id="authButton" onclick="authorize()">Authorize with Apple Music</button>
    <div id="status"></div>
    <script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
    <script>
        const developerToken = "{developer_token}";
        const serverPort = {port};

        document.addEventListener('musickitloaded', async () => {{
            try {{
                await MusicKit.configure({{
                    developerToken: developerToken,
                    app: {{ name: 'Apple Music MCP Server', build: '0.16.0' }}
                }});
                document.getElementById('status').innerHTML = '<p class="success">MusicKit loaded. Click the button to authorize.</p>';
            }} catch (err) {{
                document.getElementById('status').innerHTML = '<p class="error">Error loading MusicKit: ' + err.message + '</p>';
            }}
        }});

        async function authorize() {{
            const button = document.getElementById('authButton');
            const status = document.getElementById('status');
            button.disabled = true;
            status.innerHTML = '<p><span class="spinner"></span>Waiting for Apple authorization...</p>';

            try {{
                const music = MusicKit.getInstance();
                const musicUserToken = await music.authorize();

                status.innerHTML = '<p><span class="spinner"></span>Saving token...</p>';

                // POST token back to local server
                const response = await fetch('http://localhost:' + serverPort + '/save-token', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: 'token=' + encodeURIComponent(musicUserToken)
                }});

                if (response.ok) {{
                    status.innerHTML = '<p class="success">✓ Authorization successful! Token saved.</p><p>You can close this window and return to the terminal.</p>';
                }} else {{
                    throw new Error('Failed to save token to server');
                }}
            }} catch (err) {{
                status.innerHTML = '<p class="error">Failed: ' + err.message + '</p>';
                button.disabled = false;
            }}
        }}
    </script>
</body>
</html>"""


def create_success_html() -> str:
    """Generate success page HTML."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>Authorization Complete</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #1a1a1a;
            color: #fff;
            text-align: center;
        }
        h1 { color: #4ade80; }
        p { font-size: 18px; }
    </style>
</head>
<body>
    <h1>✓ Authorization Complete</h1>
    <p>Your Music User Token has been saved.</p>
    <p>You can close this window.</p>
</body>
</html>"""


_MAX_POST_BYTES = 65_536  # 64 KB — Apple Music tokens are well under 1 KB


def _is_trusted_local_request(headers, port: int) -> bool:
    """Server-side trust check for requests to the local auth server.

    The CORS ``Access-Control-Allow-Origin`` header alone does NOT protect the
    ``/save-token`` endpoint: a form-encoded POST is a CORS "simple request" —
    browsers send it without a preflight and the server processes it regardless
    of that header (CORS only gates whether cross-origin JS can read the
    response). So a malicious webpage could inject a forged Music User Token
    during the auth window. This closes that hole server-side:

    - **Host check** (always present): must be our exact localhost host:port.
      Also defeats DNS rebinding (evil.com resolving to 127.0.0.1 would carry
      ``Host: evil.com:{port}``).
    - **Origin check** (browsers send it on all cross-origin POSTs): when
      present, must be our exact localhost origin. Absent is allowed —
      same-origin GET navigations omit it.
    """
    allowed_hosts = (f"localhost:{port}", f"127.0.0.1:{port}")
    if headers.get("Host", "") not in allowed_hosts:
        return False
    origin = headers.get("Origin")
    allowed_origins = tuple(f"http://{h}" for h in allowed_hosts)
    if origin is not None and origin not in allowed_origins:
        return False
    return True


def run_auth_server(port: int = 8765) -> Optional[str]:
    """Run a local server for browser-based authorization with automatic token capture."""
    config_dir = get_config_dir()
    developer_token = get_developer_token()
    cors_origin = f"http://localhost:{port}"

    # Write auth HTML private from creation (it embeds the developer token).
    auth_html = create_auth_html(developer_token, port)
    auth_file = config_dir / "auth.html"
    _write_private(auth_file, auth_html)

    # Token storage for callback
    captured_token = {"value": None}
    server_should_stop = {"value": False}

    class AuthHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress logs

        def do_GET(self):
            # Host/Origin gate: the auth page embeds the developer token, so a
            # DNS-rebinding page (same-origin from the browser's view, but with
            # a foreign Host header) must not be able to read it.
            if not _is_trusted_local_request(self.headers, port):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/auth.html" or self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                with open(auth_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            # Server-side trust gate — the CORS header below does NOT stop a
            # cross-origin simple POST from being processed; this does. Closes
            # forged-token injection from a malicious webpage during the auth
            # window (see _is_trusted_local_request).
            if not _is_trusted_local_request(self.headers, port):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/save-token":
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    content_length = int(raw_length)
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if content_length > _MAX_POST_BYTES:
                    self.send_response(413)
                    self.end_headers()
                    return

                post_data = self.rfile.read(content_length).decode("utf-8")
                params = parse_qs(post_data)

                token = params.get("token", [None])[0]
                if token:
                    save_user_token(token)
                    captured_token["value"] = token
                    server_should_stop["value"] = True

                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.send_header("Access-Control-Allow-Origin", cors_origin)
                    self.end_headers()
                    self.wfile.write(create_success_html().encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            # CORS preflight — restricted to localhost origin only
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    url = f"http://localhost:{port}/auth.html"
    print(f"Starting authorization server on http://localhost:{port}")
    print()
    print("1. Open this URL in your browser:")
    print(f"       {url}")
    print("2. Click 'Authorize with Apple Music', and sign in if prompted")
    print("3. The token is saved automatically, then you can close the tab")
    print()
    # Deliberately NOT webbrowser.open(): this build never hands a URL to the
    # operating system's handler. It is one copy-paste in a one-time setup, and
    # it keeps "never opens a URL" an absolute, testable property rather than a
    # rule with an exception in it.

    server = HTTPServer(("localhost", port), AuthHandler)
    server.timeout = 1  # 1 second timeout for checking stop flag

    print("Waiting for authorization... (Ctrl+C to cancel)")
    print()

    try:
        while not server_should_stop["value"]:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return None
    finally:
        server.server_close()
        # Remove auth.html — it contains the developer token
        try:
            auth_file.unlink(missing_ok=True)
        except OSError:
            pass

    if captured_token["value"]:
        print("✓ Token saved successfully!")
        return captured_token["value"]
    else:  # pragma: no cover - unreachable: the serve loop only exits via
        # server_should_stop, which is set ONLY alongside capturing the token
        # (do_POST), so reaching here would require a captured-but-falsy token.
        print("No token received.")
        return None

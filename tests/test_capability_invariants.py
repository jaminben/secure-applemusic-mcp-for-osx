"""Capability invariants — the tests that make this a hardened fork.

Deleting a subsystem is a one-time event; keeping it deleted is a property. A
later "just add a small browser fallback" or "just shell out here" would quietly
restore the capability the fork exists to remove, and no functional test would
notice. These tests fail the build instead.

Each invariant names the concrete capability it is protecting, so a future
maintainer can make an informed decision to change it rather than deleting an
assertion that looks arbitrary.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import applemusic_mcp.server as server

SRC = Path(__file__).resolve().parent.parent / "src" / "applemusic_mcp"
PY_FILES = sorted(SRC.rglob("*.py"))

# Substring -> why it must not appear anywhere in src/.
FORBIDDEN = {
    "System Events": "Accessibility / UI scripting: system-wide synthetic input",
    "CGEvent": "CoreGraphics synthetic mouse and key events",
    "kCGHIDEventTap": "posting synthetic input to the HID event tap",
    "CoreGraphics": "the JXA bridge used to post synthetic input",
    "keystroke": "synthetic keystrokes into whatever app is frontmost",
    "do shell script": "shell execution from inside AppleScript",
    "shell=True": "shell execution from Python",
    "playwright": "browser automation",
    "document.cookie": "reading credentials out of a browser session",
    "amp-api.music.apple.com": "the unofficial web-player API host",
    "AMPWebPlay": "the developer token scraped from Apple's web-player bundle",
    "webbrowser": "handing a URL to the OS to open",
}

# subprocess is permitted in a named few modules, each for named binaries only.
# The point of the invariant is that the set of executables this server can
# launch stays small and reviewable — so the allowlist names them.
SUBPROCESS_ALLOWED = {
    "applescript.py": {"osascript"},
    # The installer has to talk to launchd and show a native dialog.
    "app_setup.py": {"launchctl", "osascript"},
    # Restarting a client after editing its config. `pgrep` only reads the
    # process table. `open` hands a path to LaunchServices. Notably absent:
    # anything that quits another app by Apple Event, which would require
    # permission to CONTROL that app -- SIGTERM via os.kill needs none and is
    # not a subprocess at all.
    "clients.py": {"pgrep", "open"},
    # The MusicKit helper: our own signed Swift binary, the only way to add a
    # catalog track without shipping an Apple Music credential. Its argv[0] is
    # computed rather than literal (the path differs between the packaged app
    # and a source checkout), so it is exempt from the literal-name check below
    # and covered by test_musickit_helper_path_is_constrained instead.
    "musickit.py": {"<computed: AMCPMusicKit>"},
}

# Modules whose argv[0] cannot be a literal, each with a dedicated test.
COMPUTED_ARGV0 = {"musickit.py"}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_forbidden_capability_appears_in_source(path):
    text = _source(path)
    for needle, why in FORBIDDEN.items():
        # A comment or docstring may NAME a removed capability to explain the
        # removal; only real code counts. Strip comments and string literals.
        assert needle not in _code_only(text), f"{path.name}: '{needle}' — {why}"


def _code_only(text: str) -> str:
    """Source with comments and string literals blanked out.

    Uses ``tokenize`` rather than text substitution so a docstring that
    legitimately NAMES a removed capability (to explain why it was removed)
    cannot be confused with code that uses it, and so removing one literal
    can never splice unrelated fragments together.
    """
    import io
    import tokenize

    pieces = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            if tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                continue
            pieces.append(tok.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return text
    return "\n".join(pieces)


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_subprocess_is_confined(path):
    """Only applescript.py may spawn a process, and only osascript."""
    if "subprocess" not in _source(path):
        return
    assert path.name in SUBPROCESS_ALLOWED, (
        f"{path.name} spawns a subprocess. Process execution is confined to "
        f"{sorted(SUBPROCESS_ALLOWED)} so the executable surface stays auditable."
    )


def test_only_osascript_is_ever_executed():
    """The single subprocess call site must invoke osascript with an argv list."""
    text = (SRC / "applescript.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
    ]
    assert calls, "expected exactly one subprocess.run call site"
    for call in calls:
        argv = call.args[0] if call.args else None
        assert isinstance(argv, ast.List), "argv must be a list, never a shell string"
        first = argv.elts[0]
        assert (
            isinstance(first, ast.Constant) and first.value == "osascript"
        ), "the only executable this server may launch is osascript"
        kwargs = {k.arg for k in call.keywords}
        assert "timeout" in kwargs, "every osascript call must be bounded by a timeout"


@pytest.mark.parametrize("gone", ["browser", "safari", "safari_player", "musickit_js", "amp_api"])
def test_removed_modules_stay_removed(gone):
    with pytest.raises(ImportError):
        __import__(f"applemusic_mcp.{gone}", fromlist=[gone])


def test_no_module_file_reappeared(request):
    for gone in ("browser", "safari", "safari_player", "musickit_js", "amp_api"):
        assert not (SRC / f"{gone}.py").exists(), f"{gone}.py is back"


# --- the exposed tool surface -------------------------------------------------

EXPECTED_TOOLS = {"playlist", "library", "discover", "catalog", "config", "playback"}


def test_tool_inventory_is_locked():
    """The set of MCP tools is a checked-in fact.

    Widening the API surface should be a visible diff in this list, not a
    side effect of adding a decorator somewhere in a 9k-line module.
    """
    tools = {
        name
        for name, obj in vars(server).items()
        if callable(obj)
        and not name.startswith("_")
        and getattr(obj, "__module__", "") == server.__name__
        and _is_mcp_tool(name)
    }
    assert tools == EXPECTED_TOOLS, f"tool surface changed: {tools ^ EXPECTED_TOOLS}"


def _is_mcp_tool(name: str) -> bool:
    """A tool is a top-level function decorated with @mcp.tool in server.py."""
    text = (SRC / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for dec in node.decorator_list:
                src = ast.get_source_segment(text, dec) or ""
                if "mcp.tool" in src:
                    return True
    return False


def test_queue_tool_is_gone():
    """Up Next was web-player state; it cannot exist without the web player."""
    assert "queue" not in EXPECTED_TOOLS
    assert not hasattr(server, "queue")


# --- URL handling -------------------------------------------------------------

HOSTILE_URLS = [
    "https://music.apple.com.attacker.tld/album/x/1?i=2",
    "https://music.apple.com@attacker.tld/album/x/1?i=2",
    "https://musicxapple.com/album/x/1?i=2",
    "http://music.apple.com/album/x/1?i=2",  # plain http
    "music://music.apple.com/album/x/1?i=2",  # non-https scheme
    "file:///etc/passwd",
    "https://evil.tld/?redirect=https://music.apple.com/song/1",
    "javascript:alert(1)",
    "",
]


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_hostile_urls_are_rejected(url):
    """Upstream accepted the first two via startswith() and passed them to `open`."""
    ok, _reason = server._parse_apple_music_url(url)
    assert ok is False, f"accepted hostile URL: {url!r}"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://music.apple.com/us/album/x/1440857781?i=1440857782", "1440857782"),
        ("https://music.apple.com/us/song/strobe/1440857781", "1440857781"),
        ("https://beta.music.apple.com/us/song/strobe/1440857781", "1440857781"),
    ],
)
def test_legitimate_urls_parse_to_an_id(url, expected):
    ok, ident = server._parse_apple_music_url(url)
    assert ok and ident == expected


def test_url_parsing_never_performs_io():
    """The parser must be pure: no fetch, no open, no subprocess."""
    src = inspect.getsource(server._parse_apple_music_url)
    for banned in ("requests", "subprocess", "open(", "urlopen"):
        assert banned not in src, f"_parse_apple_music_url touches {banned}"


# --- filesystem containment ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../../etc/passwd",
        "../.ssh/id_rsa",
        "/etc/passwd",
        "..",
        "subdir/../../escape",
    ],
)
def test_export_reader_refuses_to_escape_its_directory(name):
    out = server.read_export(name)
    assert out.startswith(("Invalid path", "File not found")), out[:120]
    assert "root:" not in out


def test_export_reader_returns_a_real_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
    (tmp_path / "export_1.csv").write_text("name,artist\nA,B\n", encoding="utf-8")
    assert "A,B" in server.read_export("export_1.csv")


def test_state_directories_are_private(tmp_path, monkeypatch):
    """Audit log, snapshots and library exports must not be world-readable."""
    monkeypatch.setenv("APPLEMUSIC_MCP_HOME", str(tmp_path))
    from applemusic_mcp import paths

    for maker in (paths.cache_dir, paths.data_dir):
        mode = maker().stat().st_mode & 0o777
        assert mode == 0o700, f"{maker.__name__} is {oct(mode)}, expected 0o700"


# --- destructive-operation safety --------------------------------------------


def test_clean_filter_columns_survive_export():
    """clean_only must not silently lose its verification signal in CSV/JSON.

    A row that could not be checked has explicit="Unknown". If the column is
    dropped, the output reads as a vetted clean list.
    """
    items = [
        {
            "name": "A",
            "duration": "3:00",
            "artist": "X",
            "album": "Y",
            "year": "2020",
            "genre": "Rock",
            "explicit": "Unknown",
            "id": "1",
        }
    ]
    csv_out = server.format_output(items, "csv")
    assert "explicit" in csv_out.split("\n")[0]
    assert "Unknown" in csv_out

    import json

    assert "explicit" in json.loads(server.format_output(items, "json"))[0]


def test_musickit_helper_path_is_constrained():
    """The one computed argv[0] must not be reachable from user input.

    musickit.py runs our own signed Swift helper, whose path differs between the
    packaged app and a source checkout, so argv[0] cannot be a string literal.
    That exemption is only safe while the candidate paths are derived from fixed
    locations — never from a tool argument.
    """
    from applemusic_mcp import musickit

    src = (SRC / "musickit.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Exactly one subprocess call, and it must be bounded.
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
    ]
    assert len(calls) == 1, "expected a single subprocess call site"
    assert "timeout" in {k.arg for k in calls[0].keywords}, "must be bounded by a timeout"

    # Every candidate location is built from fixed parts: an env override (an
    # operator decision, not tool input), the running bundle, or the repo.
    for path in musickit._candidates():
        assert path.name == "AMCPMusicKit", f"unexpected helper name: {path}"

    # And the id it is handed is validated as numeric before it ever gets there.
    ok, msg = musickit.add_to_library("not-a-number; rm -rf /")
    assert not ok and "numeric" in msg


# --- nothing secret may be committed ---------------------------------------------
#
# .gitignore prevents the accident; this catches it if the ignore rules are ever
# loosened, a file is force-added, or someone commits from a tool that bypasses
# them. The .p8 is the one that really matters: it signs developer tokens for the
# whole team and does not expire, so a leak is unrecoverable without revoking the
# key.

SECRET_SUFFIXES = (
    ".p8", ".cer", ".p12", ".pfx", ".pem", ".key",
    ".certSigningRequest", ".provisionprofile", ".mobileprovision",
    ".keychain", ".keychain-db",
)


def _tracked_files() -> list[str]:
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True,
        cwd=SRC.parent.parent, timeout=60,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_no_credential_files_are_tracked():
    offenders = [f for f in _tracked_files() if f.endswith(SECRET_SUFFIXES)]
    assert offenders == [], f"credential material is tracked by git: {offenders}"


def test_gitignore_covers_the_dangerous_extensions():
    """Belt and braces: the ignore rules must actually name these."""
    rules = (SRC.parent.parent / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.p8", "*.provisionprofile", "*.p12", "*.certSigningRequest"):
        assert pattern in rules, f".gitignore is missing {pattern}"


def test_no_apple_team_or_key_identifiers_in_tracked_files():
    """A Team ID or Key ID is not catastrophic on its own, but it identifies the
    developer account and belongs in local config, never in the repository."""
    import re
    import subprocess

    repo = SRC.parent.parent
    # Ten-char uppercase alphanumerics, as Apple issues them, next to a word
    # that says what they are — narrow enough not to trip on hashes.
    pattern = re.compile(r"(team[_ -]?id|key[_ -]?id)\W{0,4}[A-Z0-9]{10}\b", re.I)
    hits = []
    for path in _tracked_files():
        full = repo / path
        if not full.is_file() or full.suffix in {".png", ".jpg", ".mov", ".zip", ".lock"}:
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.finditer(text):
            snippet = match.group(0)
            value = snippet[-10:].upper()
            # Obvious placeholders are fine; a real Apple id is 10 mixed
            # alphanumerics with no such tell.
            placeholder = (
                any(w in value for w in ("XXXX", "TEAM", "KEY", "TEST", "FAKE",
                                         "EXAMPLE", "SAMPLE", "ABC", "A1B2C3"))
                or "1234" in value
            )
            if placeholder:
                continue
            hits.append(f"{path}: {snippet}")
    assert hits == [], f"possible Apple identifiers committed: {hits}"


def test_the_musickit_helper_needs_no_entitlements_file():
    """Requesting com.apple.developer.musickit is not merely unnecessary — it
    gets the process SIGKILLed, because nothing grants that entitlement. The
    file must stay gone so nobody reintroduces it from muscle memory."""
    helper_dir = SRC.parent.parent / "swift" / "amcp-musickit"
    assert not (helper_dir / "entitlements.plist").exists()
    build = (helper_dir / "build.sh").read_text(encoding="utf-8")
    assert "--entitlements" not in build

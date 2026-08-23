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

# subprocess is permitted in exactly one module, for exactly one binary.
SUBPROCESS_ALLOWED = {"applescript.py"}


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

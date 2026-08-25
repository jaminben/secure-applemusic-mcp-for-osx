"""What the server tells clients about itself.

The handshake is the only place a user sees the server's name, version and
icon, and every field here reaches them through a different mechanism -- so
each one gets checked against what a client would actually receive rather
than against the attribute we set.
"""

from applemusic_mcp import __version__
from applemusic_mcp import server as S


def _server_info():
    """Exactly what an initialize response would advertise."""
    mcp = S._build_server()
    return mcp._mcp_server.create_initialization_options()


def test_advertises_our_version_not_the_sdks():
    """The regression this file exists for.

    FastMCP takes no `version` keyword, so passing one raised TypeError and the
    construction ladder silently fell through to an attempt without it. The
    version then defaulted to the MCP SDK's own -- clients were shown a
    dependency's number as this server's, drifting on every SDK bump.
    """
    assert _server_info().server_version == __version__


def test_version_is_not_the_mcp_sdk_version():
    """Belt and braces: pin the actual failure mode, not just the happy path."""
    import importlib.metadata as md

    sdk = md.version("mcp")
    got = _server_info().server_version
    assert got != sdk or __version__ == sdk


def test_advertises_the_unofficial_name():
    assert _server_info().server_name == S.SERVER_NAME
    assert "Unofficial" in S.SERVER_NAME


def test_identity_carries_the_website():
    assert S._identity()["website_url"] == S.WEBSITE_URL


def test_identity_carries_an_icon_when_the_sdk_supports_it():
    """An icon is decoration -- absent is tolerable, malformed is not."""
    icons = S._identity().get("icons")
    if not icons:
        return  # SDK too old for mcp.types.Icon; the ladder drops it by design
    icon = icons[0]
    assert icon.src.startswith("data:image/")
    assert icon.mimeType == "image/png"
    assert icon.sizes


def test_build_server_survives_an_sdk_that_rejects_identity(monkeypatch):
    """The ladder's reason for existing: an unknown keyword is a TypeError."""

    class OldFastMCP:
        def __init__(self, name, **kwargs):
            if kwargs:
                raise TypeError("unexpected keyword")
            self.name = name
            self._mcp_server = type("L", (), {"version": None})()

    monkeypatch.setattr(S, "FastMCP", OldFastMCP)
    mcp = S._build_server()
    assert mcp.name == S.SERVER_NAME
    assert mcp._mcp_server.version == __version__


def test_set_version_never_raises(monkeypatch):
    """Metadata must not be able to kill server construction."""

    class Hostile:
        @property
        def _mcp_server(self):
            raise RuntimeError("nope")

    S._set_version(Hostile())  # must not raise

"""Engine resolution for the single-engine (native Music.app) build.

Upstream resolved four engines — native / safari / chrome / api — from a `mode`
preference plus a per-call `engine=` override. This build ships ONE: Apple
Events to the local Music.app. The Chrome (Playwright) and Safari
(`do JavaScript`) players were removed, so the contract these tests pin down is:

  * every path resolves to 'native' on macOS and 'none' elsewhere, and
  * a request for a REMOVED engine is refused, never silently substituted.

That second property is the security-relevant one. Silently downgrading
`engine='chrome'` to native would let a caller believe it was driving an
isolated browser session when it was in fact driving the user's real library.
"""

from __future__ import annotations

import pytest

import applemusic_mcp.server as server

REMOVED_ENGINES = ["safari", "chrome", "web", "browser", "api"]


@pytest.fixture
def mode(monkeypatch):
    """Set the `mode` pref + platform; return a setter."""

    def _set(mode_value, mac=True):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": mode_value})
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", mac)

    return _set


# -- playback engine --------------------------------------------------------


@pytest.mark.parametrize("mode_value", ["auto", "native"])
@pytest.mark.parametrize("mac,expected", [(True, "native"), (False, "none")])
def test_playback_engine_is_native_or_none(mode, mode_value, mac, expected):
    mode(mode_value, mac)
    assert server._playback_engine() == expected


@pytest.mark.parametrize("removed", REMOVED_ENGINES)
def test_removed_engine_override_resolves_to_none(mode, removed):
    """A removed engine must resolve to 'none' — not fall back to native."""
    mode("auto", True)
    assert server._playback_engine(removed) == "none"


@pytest.mark.parametrize("removed", REMOVED_ENGINES)
def test_removed_engine_pref_does_not_silently_use_native(mode, removed):
    """Same rule when the value comes from a stale config rather than a call."""
    mode(removed, True)
    assert server._playback_engine(removed) == "none"


def test_native_override_wins_on_mac(mode):
    mode("auto", True)
    assert server._playback_engine("native") == "native"


# -- data engine ------------------------------------------------------------


@pytest.mark.parametrize("mac,expected", [(True, "native"), (False, "none")])
def test_data_engine(mode, mac, expected):
    mode("auto", mac)
    assert server._engine() == expected


def test_data_engine_force_api_env(mode, monkeypatch):
    """The APPLEMUSIC_FORCE_API_MODE test hook still works."""
    mode("auto", True)
    monkeypatch.setenv("APPLEMUSIC_FORCE_API_MODE", "1")
    assert server._engine() == "api"


# -- guidance ---------------------------------------------------------------


@pytest.mark.parametrize("removed", REMOVED_ENGINES)
def test_no_player_msg_names_the_removal(mode, removed):
    """The error must say the engine was REMOVED, so a caller doesn't keep
    retrying a mode that can never come back in this build."""
    mode("auto", True)
    msg = server._no_player_msg(removed)
    assert "not available in this build" in msg
    assert "native" in msg


def test_no_player_msg_without_applescript(mode):
    mode("auto", False)
    msg = server._no_player_msg()
    assert "Music.app" in msg


# -- the removed modules must stay removed ----------------------------------


@pytest.mark.parametrize("gone", ["browser", "safari", "safari_player", "musickit_js"])
def test_web_engine_modules_are_absent(gone):
    """Importing a removed engine must fail. If one of these ever imports again,
    the browser-automation and Safari-Apple-Events attack surface is back."""
    with pytest.raises(ImportError):
        __import__(f"applemusic_mcp.{gone}", fromlist=[gone])


def test_no_queue_tool():
    """Up Next lived in the web player's MusicKit instance; it must not exist."""
    assert not hasattr(server, "queue")

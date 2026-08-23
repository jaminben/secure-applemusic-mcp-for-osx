"""Regression tests for the pre-1.0 release-audit findings."""

from __future__ import annotations

import applemusic_mcp.server as server

# -- HIGH: destructive web ops must not destroy the wrong playlist ----------










# -- HIGH: in-MCP signin must offer the macOS Safari path -------------------






# -- MEDIUM: off-macOS playlist add must de-dup + honor auto_add -------------

import types  # noqa: E402


def _ri(input_type, value, artist="", error=None):
    return types.SimpleNamespace(input_type=input_type, value=value, artist=artist, error=error)








# -- MEDIUM: native catalog->playlist attach must `duplicate` AT MOST ONCE ---


class _Resp:
    def __init__(self, code, js):
        self.status_code = code
        self._js = js

    def json(self):
        return self._js


def _stub_attach(monkeypatch, add_calls, verify):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "get_headers", lambda: {})
    monkeypatch.setattr(server, "get_storefront", lambda: "us")
    monkeypatch.setattr(server, "_VERIFY_DELAY_S", 0)
    monkeypatch.setattr(server, "_SYNC_POLL_BUDGET_S", 5)
    monkeypatch.setattr(server, "_SYNC_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(server, "_SYNC_NUDGE_AFTER_S", 999)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *a, **k: _Resp(
            200,
            {
                "results": {
                    "songs": {
                        "data": [
                            {"id": "123", "attributes": {"name": "Africa", "artistName": "Toto"}}
                        ]
                    }
                }
            },
        ),
    )
    monkeypatch.setattr(server.requests, "post", lambda *a, **k: _Resp(202, {}))
    monkeypatch.setattr(server.asc, "find_library_track", lambda n, a: (True, {}))  # synced
    monkeypatch.setattr(
        server.amp_api, "resolve_playlist", lambda n, **k: {"id": "p.user", "canEdit": True}
    )
    monkeypatch.setattr(server.amp_api, "playlist_kind", lambda pl: "user")

    def fake_add(pl, nm, ar, al):
        add_calls.append(1)
        return True, "added", None

    monkeypatch.setattr(server, "_smart_as_add_track_to_playlist", fake_add)
    monkeypatch.setattr(server, "_verify_track_in_playlist", verify)






# -- task #6: native control confirms real state (no false "paused") --------


def test_pause_that_doesnt_stick_is_honest(monkeypatch):
    """If Music is still 'playing' after a pause, say so + point at the all-engines
    view — don't claim 'Playback: pause' (the old false-success / 'won't stay paused')."""
    monkeypatch.setattr(server.asc, "pause", lambda: (True, ""))
    monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "playing"}))
    out = server._playback_control("pause")
    assert "still playing" in out.lower() and "now_playing" in out


def test_pause_that_sticks_reports_paused(monkeypatch):
    monkeypatch.setattr(server.asc, "pause", lambda: (True, ""))
    monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "paused"}))
    out = server._playback_control("pause")
    assert "Playback: pause" in out and "paused" in out


# -- dev-session: transactional swap never loses the old track --------------


def test_swap_aborts_when_add_not_confirmed(monkeypatch):
    """If the new track's add can't be confirmed (Music.app revert), the OLD track is
    NOT removed — the Coltrane data-loss case."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "_playlist_add", lambda *a, **k: "Error: did not persist (revert)")
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "removed")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "aborted" in out.lower() and removed == []  # old track kept


def test_swap_removes_old_only_after_add_confirms(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(
        server, "_playlist_add", lambda *a, **k: "Added Coltrane to Jazz (via Music.app)"
    )
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "Removed")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "Swapped" in out and removed == ["Old Song"]


def test_swap_aborts_when_strict_confirm_fails(monkeypatch):
    """The add reports success but the STRICT confirm can't find the exact track (a
    reverted add hiding behind a similar title) → old track kept. The substring-verify
    data-loss fix."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(
        server, "_playlist_add", lambda *a, **k: "Added One to Jazz (via Music.app)"
    )
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: False)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "Removed")
    out = server.playlist(action="add", playlist="Jazz", track="One", replace="Old Song")
    assert "aborted" in out.lower() and removed == []


def test_add_landed_is_conservative():
    assert server._add_landed("Added X to Y (via Music.app)") is True
    for bad in [
        "Error: nope",
        "Added but did not persist after retry",
        "couldn't confirm",
        "re-run this add",
        "relaunch Music.app",
        "Nothing added",
    ]:
        assert server._add_landed(bad) is False, bad


# -- off-mac (Win/Linux) swap: API read-back guards the destructive remove --------


def _fake_resolved(api_id="p.1", name="Jazz"):
    import types as _t

    return _t.SimpleNamespace(api_id=api_id, applescript_name=name, error=None, fuzzy_match=None)






def test_confirm_swap_track_requires_exact_name(monkeypatch):
    """The strict confirm must NOT accept a substring collision — adding 'One' while
    'One More Time' is present must return False (the data-loss fix)."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    listing = (True, [{"name": "One More Time", "artist": "Daft Punk"}])
    monkeypatch.setattr(server, "_get_playlist_track_names", lambda pid: listing)
    assert server._confirm_swap_track("One", "", api_id="p.1") is False
    exact = (True, [{"name": "One", "artist": "Metallica"}])
    monkeypatch.setattr(server, "_get_playlist_track_names", lambda pid: exact)
    assert server._confirm_swap_track("One", "", api_id="p.1") is True


# -- config dir: clean error instead of a raw traceback when unwritable -----------


def test_get_config_dir_raises_clean_error(monkeypatch):
    """An unwritable APPLEMUSIC_MCP_HOME (or root-owned mount) should give an
    actionable message, not a raw OSError traceback."""
    import pytest

    import applemusic_mcp.auth as auth

    def boom(*a, **k):
        raise PermissionError("Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    with pytest.raises(RuntimeError, match="config directory"):
        auth.get_config_dir()


def test_env_var_user_token_injection(monkeypatch):
    """APPLEMUSIC_USER_TOKEN is the headless/container injection path (for an Apple ID
    that requires a hardware key that can't reach the box)."""
    import applemusic_mcp.auth as auth

    monkeypatch.setenv("APPLEMUSIC_USER_TOKEN", "INJECTED-TOK")
    assert auth.get_user_token() == "INJECTED-TOK"
    assert auth.has_user_token() is True

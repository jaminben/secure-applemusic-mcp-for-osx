"""The MusicKit rail: the credential-free way to add a catalog track.

Why it exists: Music.app's `play` needs an object specifier, and a track the
user does not own has none — so playing it requires putting it in the library
first. Every other route to that costs something the user should not have to
pay: Accessibility (system-wide synthetic input), a browser session, or an
Apple Music credential shipped inside the app. MusicKit's `MusicDataRequest`
signs the call from the app's own identity, so nothing secret is distributed.

These tests cover the Python side. The Swift helper is exercised by hand
against the live service — it needs a signed bundle and a real account.
"""

from __future__ import annotations

import sys

import pytest

from applemusic_mcp import musickit, server

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS helper")


# --- input validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "abc", "123; rm -rf /", "../../etc/passwd", "1e5", "-1", "12 34", "٣٤٥"],
)
def test_non_numeric_ids_never_reach_the_helper(bad, monkeypatch):
    """The id is interpolated into a URL that reaches Apple. Validate before
    spawning anything — and note this is the SECOND check; the Swift side
    validates too, because one validation site is one too few."""
    called = []
    monkeypatch.setattr(musickit, "_run", lambda *a: called.append(a) or (True, {}))
    ok, msg = musickit.add_to_library(bad)
    assert not ok
    assert "numeric" in msg
    assert called == [], f"{bad!r} reached the helper"


def test_numeric_id_is_passed_through(monkeypatch):
    seen = {}

    def fake_run(*args):
        seen["args"] = args
        return True, {"ok": True, "httpStatus": 202}

    monkeypatch.setattr(musickit, "_run", fake_run)
    ok, msg = musickit.add_to_library("1440812085")
    assert ok and "202" in msg
    assert seen["args"] == ("add", "1440812085")


# --- failure reporting ---------------------------------------------------------


def test_silent_helper_is_reported_as_a_signing_problem(monkeypatch, tmp_path):
    """A helper signed with an entitlement no profile grants is SIGKILLed before
    it can print. Silence therefore means a signing problem, not an API problem,
    and saying so saves the next person the evening it cost to work out."""

    class Proc:
        stdout = ""
        stderr = ""
        returncode = -9

    fake = tmp_path / "AMCPMusicKit"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(musickit, "helper_path", lambda: fake)
    monkeypatch.setattr(musickit.subprocess, "run", lambda *a, **k: Proc())

    ok, payload = musickit._run("status")
    assert not ok
    assert "unsigned" in payload["error"] or "signing" in payload["error"]


def test_absent_helper_is_not_an_error_state(monkeypatch):
    """A source checkout has no helper; that is a configuration, not a fault."""
    monkeypatch.setattr(musickit, "helper_path", lambda: None)
    assert musickit.is_available() is False
    assert musickit.authorization_status() == "unavailable"
    ok, msg = musickit.add_to_library("123")
    assert not ok and "not installed" in msg


# --- the library-write policy ---------------------------------------------------


def test_catalog_play_off_blocks_every_add(monkeypatch):
    """Adding to someone's music library is a real side effect and must be
    refusable. With catalog_play='off' nothing is added by either rail."""
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"catalog_play": "off"})
    tried = []
    monkeypatch.setattr(server.musickit, "is_available", lambda: tried.append("mk") or True)
    monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: tried.append("rest"))

    ok, msg = server._add_songs_to_library(["1440812085"])
    assert not ok
    assert tried == [], "no rail may run when catalog_play is off"
    assert "catalog_play" in msg


def test_musickit_is_preferred_over_the_token_rail(monkeypatch):
    """Prefer the rail that needs no credential."""
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"catalog_play": "add"})
    monkeypatch.setattr(server.musickit, "is_available", lambda: True)
    monkeypatch.setattr(server.musickit, "add_to_library", lambda cid: (True, "added (HTTP 202)"))
    rest = []
    monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: rest.append(a) or (True, ""))
    monkeypatch.setattr(server, "_record_auto_added", lambda ids, known=None: None)

    ok, msg = server._add_songs_to_library(["1440812085"])
    assert ok and "MusicKit" in msg
    assert rest == [], "the token rail must not be used when MusicKit works"


def test_falls_back_to_the_token_rail_when_musickit_refuses(monkeypatch):
    """A helper that is present but unauthorized must not block a working token."""
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"catalog_play": "add"})
    monkeypatch.setattr(server.musickit, "is_available", lambda: True)
    monkeypatch.setattr(server.musickit, "add_to_library", lambda cid: (False, "not authorized"))
    monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: (True, "Added 1 song"))
    monkeypatch.setattr(server, "_record_auto_added", lambda ids, known=None: None)

    ok, msg = server._add_songs_to_library(["1440812085"])
    assert ok and "Added" in msg


# --- the audit trail -------------------------------------------------------------


def test_auto_added_tracks_are_recorded_and_filed(monkeypatch, tmp_path):
    """Playing an unowned track necessarily writes to the library, so those
    writes are collected under one playlist and logged — findable and undoable
    in one gesture rather than silently mixed in with chosen music."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    logged = []
    monkeypatch.setattr(server.audit_log, "log_action", lambda a, d, **k: logged.append((a, d)))

    class Cache:
        def get_track_info(self, cid):
            return {"name": "Banana Pancakes", "artist": "Jack Johnson"}

    monkeypatch.setattr(server, "get_track_cache", lambda: Cache())
    filed = []
    monkeypatch.setattr(
        server.asc,
        "add_track_to_playlist",
        lambda pl, name, artist=None: filed.append((pl, name)) or (True, ""),
    )
    # The playlist is resolved before the track is filed, and created if it is
    # missing. Both must be stubbed: an unstubbed call here reaches real
    # Music.app, raises, and is swallowed by the best-effort handler -- so the
    # filing assertion below fails for a reason that has nothing to do with
    # filing. That is exactly how this test broke once already.
    created = []
    monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda pl: ([{"name": "x"}], ""))
    monkeypatch.setattr(
        server.asc, "create_playlist", lambda name, desc=None: created.append(name) or (True, "")
    )

    server._record_auto_added(["1440857795"])

    assert logged and logged[0][0] == "auto_added"
    assert filed == [(server.AUTO_ADD_PLAYLIST, "Banana Pancakes")]
    assert created == [], "an existing playlist must not be recreated"
    assert server.AUTO_ADD_PLAYLIST == "Added by Music MCP"


def test_the_auto_playlist_is_created_on_first_use(monkeypatch):
    """Filing into a playlist that does not exist yet must create it, not fail."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    created, filed = [], []
    monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda pl: ([], "no such playlist"))
    monkeypatch.setattr(
        server.asc, "create_playlist", lambda name, desc=None: created.append(name) or (True, "")
    )
    monkeypatch.setattr(
        server.asc,
        "add_track_to_playlist",
        lambda pl, name, artist=None: filed.append((pl, name, artist)) or (True, ""),
    )

    assert server.file_under_auto_playlist("Banana Pancakes", "Jack Johnson") is True
    assert created == [server.AUTO_ADD_PLAYLIST]
    assert filed == [(server.AUTO_ADD_PLAYLIST, "Banana Pancakes", "Jack Johnson")]


def test_filing_failure_never_fails_the_add(monkeypatch):
    """Bookkeeping must not turn a successful add into a reported failure."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)

    class Cache:
        def get_track_info(self, cid):
            return {"name": "X", "artist": "Y"}

    monkeypatch.setattr(server, "get_track_cache", lambda: Cache())
    monkeypatch.setattr(
        server.asc,
        "add_track_to_playlist",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Music.app is not running")),
    )
    server._record_auto_added(["123"])  # must not raise


def test_filing_creates_the_playlist_on_first_use(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda pl: (False, "not found"))
    created, added = [], []
    monkeypatch.setattr(
        server.asc, "create_playlist", lambda n, d="": created.append(n) or (True, "")
    )
    monkeypatch.setattr(
        server.asc,
        "add_track_to_playlist",
        lambda pl, n, artist=None: added.append((pl, n)) or (True, ""),
    )
    assert server.file_under_auto_playlist("Pancakes", "Emancipator") is True
    assert created == [server.AUTO_ADD_PLAYLIST]
    assert added == [(server.AUTO_ADD_PLAYLIST, "Pancakes")]


def test_filing_reuses_an_existing_playlist(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda pl: (True, []))
    created = []
    monkeypatch.setattr(server.asc, "create_playlist", lambda n, d="": created.append(n))
    monkeypatch.setattr(server.asc, "add_track_to_playlist", lambda *a, **k: (True, ""))
    server.file_under_auto_playlist("X", "Y")
    assert created == [], "must not recreate the playlist every time"


def test_filing_happens_after_the_sync_not_before():
    """Regression: the add returns HTTP 202 and iCloud propagates a few seconds
    later. Filing straight after the 202 found nothing to file and silently did
    nothing — the playlist stayed empty while tracks piled up in the library.

    So the filing call must sit AFTER the find_library_track poll succeeds.
    """
    import inspect

    src = inspect.getsource(server._catalog_miss_play)
    poll = src.index("find_library_track")
    filing = src.index("file_under_auto_playlist")
    assert filing > poll, "must file only once the track exists locally"


def test_names_are_passed_through_rather_than_looked_up(monkeypatch):
    """The track cache is only filled by the developer-token API path, so on the
    tokenless iTunes rail it is empty. Relying on it filed nothing."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)

    class EmptyCache:
        def get_track_info(self, cid):
            return None

    monkeypatch.setattr(server, "get_track_cache", lambda: EmptyCache())
    filed = []
    monkeypatch.setattr(server, "file_under_auto_playlist", lambda n, a=None: filed.append(n))

    server._record_auto_added(["123"], known=[("Pancakes", "Emancipator")])
    assert filed == ["Pancakes"], "caller-supplied names must be used"

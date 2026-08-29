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
from pathlib import Path

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
    monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
    monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: tried.append("rest"))

    ok, msg = server._add_songs_to_library(["1440812085"])
    assert not ok
    assert tried == [], "no rail may run when catalog_play is off"
    assert "catalog_play" in msg


def test_musickit_is_preferred_over_the_token_rail(monkeypatch):
    """Prefer the rail that needs no credential."""
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"catalog_play": "add"})
    monkeypatch.setattr(server.musickit, "is_available", lambda: True)
    monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
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
    monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
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


# ---------------------------------------------------------------------------
# The MusicKit rail as a *routing* decision.
#
# Regression: a notarized bundle with an authorized helper could add a catalog
# track to the library, but `_unified_auto_search_to_playlist` gated on
# `_can_use_library_api()` — tokens only — and so refused, telling the user to
# run `applemusic-mcp login --dev`: a binary this build does not install, for a
# rail the helper made unnecessary. The capability was one layer below the gate
# that turned it away.
# ---------------------------------------------------------------------------


class TestMusicKitIsAWriteRail:
    def test_authorized_helper_counts_as_a_rail(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        assert server._can_use_musickit_rail() is True

    def test_unauthorized_helper_does_not(self, monkeypatch):
        """Present but not consented to: `add` refuses, so promising the rail
        here would only move the failure later and make it less legible."""
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "notDetermined")
        assert server._can_use_musickit_rail() is False

    def test_missing_helper_does_not(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        assert server._can_use_musickit_rail() is False

    def test_forced_tokenless_disables_it(self, monkeypatch):
        """APPLEMUSIC_FORCE_TOKENLESS is documented as disabling every write
        path, and status blames it by name. Quietly honouring MusicKit anyway
        would make that flag lie."""
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        assert server._can_use_musickit_rail() is False


class TestSetupHintNeverSendsAnyoneToAShell:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("notDetermined", "config(action='signin')"),
            ("denied", "System Settings"),
            ("restricted", "restricted"),
        ],
    )
    def test_hint_matches_the_authorization_state(self, monkeypatch, status, expected):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: status)
        assert expected in server._musickit_setup_hint()

    def test_no_helper_points_at_the_signed_bundle(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        assert "notarized download" in server._musickit_setup_hint()

    @pytest.mark.parametrize("status", ["notDetermined", "denied", "restricted", "unavailable"])
    def test_no_branch_names_a_shell_command(self, monkeypatch, status):
        monkeypatch.setattr(server.musickit, "is_available", lambda: status != "unavailable")
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: status)
        hint = server._musickit_setup_hint()
        assert "applemusic-mcp" not in hint
        assert "login --dev" not in hint


class TestPlaylistAddRoutesToMusicKitWithoutAToken:
    def test_tokenless_host_with_a_helper_is_not_turned_away(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        seen = {}

        def fake(name, artist, playlist):
            seen.update(name=name, artist=artist, playlist=playlist)
            return True, "Pancakes - Emancipator (added via MusicKit)", ["found in catalog"]

        monkeypatch.setattr(server, "_tokenless_search_and_add_to_playlist", fake)

        ok, msg, steps = server._unified_auto_search_to_playlist("Pancakes", "Emancipator", "Chill")
        assert ok is True
        assert seen == {"name": "Pancakes", "artist": "Emancipator", "playlist": "Chill"}
        assert "found in catalog" in steps

    def test_combined_name_is_split_before_the_musickit_rail_sees_it(self, monkeypatch):
        """The ' - ' split happens above the gate, so both rails get the clean
        query — not just the token one."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        seen = {}
        monkeypatch.setattr(
            server,
            "_tokenless_search_and_add_to_playlist",
            lambda n, a, p: (seen.update(name=n, artist=a), (True, "ok", []))[1],
        )
        server._unified_auto_search_to_playlist("Pancakes - Emancipator", "", "Chill")
        assert seen == {"name": "Pancakes", "artist": "Emancipator"}

    def test_no_rail_at_all_gives_an_in_app_fix(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        ok, msg, _ = server._unified_auto_search_to_playlist("Pancakes", "Emancipator", "Chill")
        assert ok is False
        assert "applemusic-mcp" not in msg, "must not name a binary this build lacks"
        assert "login --dev" not in msg


class TestTokenlessSearchAndAdd:
    def test_uses_the_public_catalog_rail_then_musickit_then_applescript(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_find_catalog_hit_for",
            lambda n, a: {"id": "123", "name": "Pancakes", "artist": "Emancipator"},
        )
        added = []
        monkeypatch.setattr(
            server,
            "_add_songs_to_library",
            lambda ids, known=None: (added.append((ids, known)), (True, "ok"))[1],
        )
        monkeypatch.setattr(
            server,
            "_sync_then_attach_native",
            lambda n, a, p, steps, rail: (True, f"{n} (via {rail})", steps),
        )

        ok, msg, steps = server._tokenless_search_and_add_to_playlist(
            "pancakes", "emancipator", "Chill"
        )
        assert ok is True
        assert "via MusicKit" in msg
        # Apple's canonical name/artist go downstream, not the caller's casing —
        # the attach step polls the local library by name.
        assert added == [(["123"], [("Pancakes", "Emancipator")])]
        assert any("no credential" in s for s in steps)

    def test_missing_from_the_catalog_is_reported_without_blaming_auth(self, monkeypatch):
        monkeypatch.setattr(server, "_find_catalog_hit_for", lambda n, a: {})
        ok, msg, _ = server._tokenless_search_and_add_to_playlist("Nope", "Nobody", "Chill")
        assert ok is False
        assert "Nope - Nobody" in msg
        assert "login" not in msg

    def test_a_refused_library_add_surfaces_the_helper_reason(self, monkeypatch):
        monkeypatch.setattr(
            server, "_find_catalog_hit_for", lambda n, a: {"id": "9", "name": "S", "artist": "A"}
        )
        monkeypatch.setattr(
            server, "_add_songs_to_library", lambda ids, known=None: (False, "not authorized")
        )
        ok, msg, _ = server._tokenless_search_and_add_to_playlist("S", "A", "Chill")
        assert ok is False
        assert "not authorized" in msg


class TestSyncThenAttachIsSharedByBothRails:
    def _stub_a_clean_attach(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "find_library_track", lambda n, a: (True, {}))
        monkeypatch.setattr(
            server, "_smart_as_add_track_to_playlist", lambda p, n, a, x: (True, "added", None)
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda p, n, a: True)

    def test_the_rail_label_reaches_the_success_message(self, monkeypatch):
        """One implementation, two callers — the only thing that differs is which
        rail is named, so the timing logic can never drift between them."""
        self._stub_a_clean_attach(monkeypatch)
        ok, msg, _ = server._sync_then_attach_native("S", "A", "Chill", [], "MusicKit")
        assert ok is True
        assert "added to library via MusicKit" in msg

        ok, msg, _ = server._sync_then_attach_native("S", "A", "Chill", [], "the Apple Music API")
        assert "added to library via the Apple Music API" in msg


# ---------------------------------------------------------------------------
# Same routing bug, second site: playing a catalog track you don't own.
#
# `_catalog_miss_play` is add-then-play, and EVERY step below its gate was
# already credential-free — public iTunes for the id, MusicKit for the add,
# Apple Events for the play. Only the gate asked for a token.
# ---------------------------------------------------------------------------


class TestCatalogPlayRoutesToMusicKitWithoutAToken:
    def _stub_add_then_play(self, monkeypatch, added):
        monkeypatch.setattr(server, "_find_catalog_id_for", lambda n, a: "123")
        monkeypatch.setattr(
            server,
            "_add_songs_to_library",
            lambda ids, known=None: (added.append(ids), (True, "ok"))[1],
        )
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)
        monkeypatch.setattr(server.asc, "find_library_track", lambda n, a: (True, {}))
        monkeypatch.setattr(server, "file_under_auto_playlist", lambda n, a=None: None)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Strobe"))

    def test_an_authorized_helper_is_enough(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        added = []
        self._stub_add_then_play(monkeypatch, added)

        out = server._catalog_miss_play("Strobe", "deadmau5", "", reveal=False)
        assert added == [["123"]], "the add must actually run, not be gated away"
        assert "Added and playing" in out

    def test_a_token_still_works_on_its_own(self, monkeypatch):
        """The MusicKit rail is an OR, not a replacement — a token-only host
        (source checkout, no Swift build) must be unaffected."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        added = []
        self._stub_add_then_play(monkeypatch, added)

        out = server._catalog_miss_play("Strobe", "deadmau5", "", reveal=False)
        assert added == [["123"]]
        assert "Added and playing" in out

    def test_neither_rail_gives_an_in_app_fix(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        out = server._catalog_miss_play("Strobe", "deadmau5", "", reveal=False)
        assert "Strobe by deadmau5" in out
        assert "applemusic-mcp" not in out
        assert "login --dev" not in out
        assert "notarized download" in out

    def test_forced_tokenless_blames_the_flag_not_your_auth(self, monkeypatch):
        """`_forced_tokenless` is documented as requiring write-gate errors to
        name it. This gate didn't, so setting the flag produced 'go get a
        developer token' — advice that cannot possibly help while it is set."""
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        out = server._catalog_miss_play("Strobe", "deadmau5", "", reveal=False)
        assert "APPLEMUSIC_FORCE_TOKENLESS" in out
        assert "login" not in out


# ---------------------------------------------------------------------------
# The verbs added so the developer token stops being load-bearing.
# ---------------------------------------------------------------------------


class TestIdentifierValidation:
    """Every id here is interpolated into a URL that reaches Apple. The helper
    re-validates in Swift too — one validation site is one too few."""

    @pytest.mark.parametrize("bad", ["", "  ", "12a", "١٢٣٤٥", "1;2", "-1"])
    def test_catalog_ids_must_be_ascii_digits(self, bad):
        assert musickit._valid_catalog_id(bad) is None

    def test_a_good_catalog_id_is_returned_stripped(self):
        assert musickit._valid_catalog_id("  1440783617 ") == "1440783617"

    @pytest.mark.parametrize(
        "bad", ["", "abc", "p.", "p", "p.../etc/passwd", "p.ab/cd", "p." + "a" * 100]
    )
    def test_playlist_ids_must_be_p_dot_alnum(self, bad):
        """This one lands in a URL PATH: a stray '/' or '..' would change WHICH
        resource is addressed rather than merely failing."""
        assert musickit._valid_playlist_id(bad) is None

    def test_a_good_playlist_id_passes(self):
        assert musickit._valid_playlist_id("p.AbC123") == "p.AbC123"


class TestNewVerbsRejectBadInputBeforeSpawning:
    def _never_runs(self, monkeypatch):
        def boom(*a):
            raise AssertionError("the helper must not be spawned for invalid input")

        monkeypatch.setattr(musickit, "_run", boom)

    def test_album_add(self, monkeypatch):
        self._never_runs(monkeypatch)
        assert musickit.add_album_to_library("nope")[0] is False

    def test_rate_rejects_an_unknown_rating(self, monkeypatch):
        self._never_runs(monkeypatch)
        ok, msg = musickit.rate_song("1440783617", "sideways")
        assert ok is False and "love" in msg

    def test_playlist_add_rejects_a_path_traversal(self, monkeypatch):
        self._never_runs(monkeypatch)
        assert musickit.add_track_to_playlist("p.../../x", "1440783617")[0] is False

    @pytest.mark.parametrize("bad", [[], ["SHORT"], ["A" * 13], ["US-ABC-12-345"], ["ÜSABC123456"]])
    def test_isrc_shape_is_enforced(self, monkeypatch, bad):
        """Length, ASCII and alphanumeric — the properties that make the value
        safe in a URL. Note "xxxxxxxxxxxx" is deliberately ACCEPTED here: it is
        not a real ISRC, but semantic validation belongs to the server's
        `_parse_isrc_list`, and duplicating it in the transport would drift."""
        self._never_runs(monkeypatch)
        assert musickit.resolve_isrcs(bad)[0] is False

    def test_more_than_a_hundred_isrcs_is_refused(self, monkeypatch):
        self._never_runs(monkeypatch)
        assert musickit.resolve_isrcs(["GBUM71029604"] * 101)[0] is False


class TestNewVerbsShapeTheirResults:
    def test_album_add_reports_the_http_status(self, monkeypatch):
        monkeypatch.setattr(musickit, "_run", lambda *a: (True, {"httpStatus": 202}))
        ok, msg = musickit.add_album_to_library("1065973699")
        assert ok is True and "202" in msg

    def test_rate_passes_the_normalised_verb(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"httpStatus": 200}))[1]
        )
        musickit.rate_song("1440783617", "LOVE")
        assert seen == [("rate", "1440783617", "love")]

    def test_isrc_returns_apples_data_array_verbatim(self, monkeypatch):
        monkeypatch.setattr(
            musickit,
            "_run",
            lambda *a: (True, {"body": '{"data":[{"id":"1","type":"songs"}]}'}),
        )
        ok, data = musickit.resolve_isrcs(["GBUM71029604"])
        assert ok is True
        assert data == [{"id": "1", "type": "songs"}]

    def test_isrc_uppercases_and_joins(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"body": "{}"}))[1]
        )
        musickit.resolve_isrcs(["gbum71029604", " USABC1234567 "])
        assert seen == [("isrc", "GBUM71029604,USABC1234567")]

    def test_an_unparseable_body_is_an_error_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(musickit, "_run", lambda *a: (True, {"body": "not json"}))
        assert musickit.resolve_isrcs(["GBUM71029604"])[0] is False


class TestServerPrefersMusicKitForTheNewOperations:
    def test_album_add_tries_musickit_first(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        monkeypatch.setattr(
            server.musickit, "add_album_to_library", lambda cid: (True, "added album (HTTP 202)")
        )

        def never(*a, **k):
            raise AssertionError("the token rail must not run when MusicKit succeeds")

        monkeypatch.setattr(server, "_add_to_library_api", never)
        ok, msg = server._add_album_to_library("1065973699")
        assert ok is True and "202" in msg

    def test_album_add_falls_back_when_the_helper_refuses(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        monkeypatch.setattr(
            server.musickit, "add_album_to_library", lambda cid: (False, "not authorized")
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, kind: (True, "via token"))
        assert server._add_album_to_library("1065973699") == (True, "via token")

    def test_rating_tries_musickit_first(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        monkeypatch.setattr(server.musickit, "rate_song", lambda cid, r: (True, "rated"))

        def never():
            raise AssertionError("no headers should be needed when MusicKit succeeds")

        monkeypatch.setattr(server, "get_headers", never)
        assert server._rate_song_api("1440783617", "love") == (True, "Marked as love")

    def test_isrc_uses_musickit_when_there_is_no_token(self, monkeypatch):
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        seen = []

        def fake(batch):
            seen.append(list(batch))
            return True, [
                {
                    "id": "1440783617",
                    "type": "songs",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "isrc": "GBUM71029604",
                    },
                }
            ]

        monkeypatch.setattr(server.musickit, "resolve_isrcs", fake)

        def never():
            raise AssertionError("no token exists — nothing may ask for headers")

        monkeypatch.setattr(server, "get_headers", never)
        out = server._catalog_resolve_isrc("GBUM71029604")
        assert seen == [["GBUM71029604"]]
        assert "Strobe" in out

    def test_isrc_stays_on_the_token_rail_when_one_exists(self, monkeypatch):
        """MusicKit is an OR, not a replacement: a token host is unaffected, and
        its per-request rate-limit accounting keeps working."""
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)

        def never(batch):
            raise AssertionError("the token rail must win when a token is configured")

        monkeypatch.setattr(server.musickit, "resolve_isrcs", never)
        monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "Bearer x"})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": []}

        monkeypatch.setattr(server.requests, "get", lambda *a, **k: Resp())
        server._catalog_resolve_isrc("GBUM71029604")


class TestTheKillSwitchCoversEveryMusicKitWrite:
    """APPLEMUSIC_FORCE_TOKENLESS is documented as disabling every API write —
    catalog add, playlist edit, rating. The MusicKit rails gated on "is the
    helper binary on disk", which honoured neither that switch nor the user's
    Apple Music consent, so a flag someone set to stop writes did not stop
    these ones. Security review caught it; this keeps it caught."""

    def _helper_that_would_write(self, monkeypatch, calls):
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        for verb in ("add_to_library", "add_album_to_library"):
            monkeypatch.setattr(
                server.musickit, verb, lambda cid, v=verb: calls.append(v) or (True, "written")
            )
        monkeypatch.setattr(
            server.musickit, "rate_song", lambda cid, r: calls.append("rate") or (True, "written")
        )
        # The token rail is unreachable in this configuration anyway; make that
        # explicit so a fall-through cannot be mistaken for a pass.
        monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: (False, "no token"))

    def test_song_add_is_refused(self, monkeypatch):
        calls = []
        self._helper_that_would_write(monkeypatch, calls)
        server._add_songs_to_library(["1440783617"])
        assert calls == [], "the kill switch must stop the MusicKit write"

    def test_album_add_is_refused(self, monkeypatch):
        calls = []
        self._helper_that_would_write(monkeypatch, calls)
        server._add_album_to_library("1065973699")
        assert calls == []

    def test_rating_is_refused(self, monkeypatch):
        calls = []
        self._helper_that_would_write(monkeypatch, calls)
        monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "x"})

        class Resp:
            status_code = 401

        monkeypatch.setattr(server.requests, "put", lambda *a, **k: Resp())
        server._rate_song_api("1440783617", "love")
        assert calls == []

    def test_unconsented_helper_is_also_refused(self, monkeypatch):
        """Same gate, second property: a helper present but not yet granted
        Apple Music access must not be spawned to attempt a write."""
        calls = []
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "notDetermined")
        monkeypatch.setattr(
            server.musickit, "add_to_library", lambda cid: calls.append("add") or (True, "written")
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda *a, **k: (False, "no token"))
        server._add_songs_to_library(["1440783617"])
        assert calls == []


# ---------------------------------------------------------------------------
# Attaching to an Apple-Music-origin playlist.
#
# The one operation with NO AppleScript equivalent: Music.app can only edit the
# playlists it owns. So unlike every other rail here, this isn't "the same work
# without a credential" — without it, a `p.` playlist simply cannot be edited
# tokenlessly at all.
# ---------------------------------------------------------------------------


class TestTrackRefValidation:
    def test_a_catalog_id_is_a_songs_ref(self):
        assert musickit._valid_track_ref("1440783617", "songs") == "1440783617"

    def test_a_library_id_is_a_library_songs_ref(self):
        assert musickit._valid_track_ref("i.AbC123", "library-songs") == "i.AbC123"

    def test_the_kinds_are_not_interchangeable(self):
        """Apple rejects a mismatched pair, and the failure reads as 'the track
        doesn't exist' rather than 'you named it the wrong way'."""
        assert musickit._valid_track_ref("1440783617", "library-songs") is None
        assert musickit._valid_track_ref("i.AbC123", "songs") is None

    @pytest.mark.parametrize("bad", ["i.", "i.ab/cd", "i.../../x", "x.AbC123"])
    def test_library_refs_reject_path_shapes(self, bad):
        assert musickit._valid_track_ref(bad, "library-songs") is None

    def test_an_unknown_kind_is_refused(self):
        assert musickit._valid_track_ref("1440783617", "collections") is None


class TestPlaylistBridgeVerbs:
    def test_add_passes_the_kind_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"httpStatus": 204}))[1]
        )
        musickit.add_track_to_playlist("p.Ab1", "i.Cd2", "library-songs")
        assert seen == [("playlist-add", "p.Ab1", "i.Cd2", "library-songs")]

    def test_add_defaults_to_a_catalog_song(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"httpStatus": 204}))[1]
        )
        musickit.add_track_to_playlist("p.Ab1", "1440783617")
        assert seen == [("playlist-add", "p.Ab1", "1440783617", "songs")]

    def test_a_bad_playlist_id_never_spawns(self, monkeypatch):
        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.add_track_to_playlist("p.../x", "1440783617")[0] is False
        assert musickit.playlist_tracks("nope")[0] is False

    def test_tracks_returns_apples_data_array(self, monkeypatch):
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (True, {"body": '{"data":[{"id":"i.A"}]}'})
        )
        ok, rows = musickit.playlist_tracks("p.Ab1")
        assert ok is True and rows == [{"id": "i.A"}]

    def test_tracks_survives_an_unparseable_body(self, monkeypatch):
        monkeypatch.setattr(musickit, "_run", lambda *a: (True, {"body": "["}))
        assert musickit.playlist_tracks("p.Ab1")[0] is False


class TestApiModeAddViaMusicKit:
    def _resolved(self):
        return server.ResolvedPlaylist(raw_input="p.Ab1", api_id="p.Ab1", applescript_name=None)

    def _no_dupes(self, monkeypatch):
        monkeypatch.setattr(server, "_get_playlist_track_names", lambda pid: (True, []))
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)

    def test_names_resolve_over_the_public_catalog_and_attach_as_songs(self, monkeypatch):
        self._no_dupes(monkeypatch)
        monkeypatch.setattr(
            server,
            "_find_catalog_hit_for",
            lambda n, a: {"id": "1440783617", "name": "Strobe", "artist": "deadmau5"},
        )
        sent = []
        monkeypatch.setattr(
            server.musickit,
            "add_track_to_playlist",
            lambda pid, tid, kind: (sent.append((pid, tid, kind)), (True, "ok"))[1],
        )
        out = server._api_mode_add_via_musickit(
            self._resolved(), [], [{"name": "strobe", "artist": "deadmau5"}], [], False
        )
        assert sent == [("p.Ab1", "1440783617", "songs")]
        assert "Strobe" in out

    def test_library_ids_attach_by_reference(self, monkeypatch):
        self._no_dupes(monkeypatch)
        sent = []
        monkeypatch.setattr(
            server.musickit,
            "add_track_to_playlist",
            lambda pid, tid, kind: (sent.append((tid, kind)), (True, "ok"))[1],
        )
        server._api_mode_add_via_musickit(self._resolved(), ["i.AbC123"], [], [], False)
        assert sent == [("i.AbC123", "library-songs")]

    def test_an_id_that_is_neither_is_skipped_with_a_reason(self, monkeypatch):
        self._no_dupes(monkeypatch)
        monkeypatch.setattr(server.musickit, "add_track_to_playlist", lambda *a: (True, "ok"))
        out = server._api_mode_add_via_musickit(self._resolved(), ["DEADBEEF1234"], [], [], False)
        assert "not a catalog id" in out

    def test_duplicates_are_refused(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_get_playlist_track_names",
            lambda pid: (True, [{"id": "i.X", "name": "Strobe", "artist": "deadmau5"}]),
        )
        monkeypatch.setattr(
            server,
            "_find_catalog_hit_for",
            lambda n, a: {"id": "1440783617", "name": "Strobe", "artist": "deadmau5"},
        )

        def never(*a):
            raise AssertionError("a duplicate must not be attached")

        monkeypatch.setattr(server.musickit, "add_track_to_playlist", never)
        out = server._api_mode_add_via_musickit(
            self._resolved(), [], [{"name": "Strobe", "artist": "deadmau5"}], [], False
        )
        assert "already in playlist" in out

    def test_allow_duplicates_skips_the_read_entirely(self, monkeypatch):
        def never(pid):
            raise AssertionError("no need to read the playlist when dupes are allowed")

        monkeypatch.setattr(server, "_get_playlist_track_names", never)
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)
        monkeypatch.setattr(server, "_itunes_track_by_id", lambda tid: {"name": "S", "artist": "A"})
        monkeypatch.setattr(server.musickit, "add_track_to_playlist", lambda *a: (True, "ok"))
        out = server._api_mode_add_via_musickit(self._resolved(), ["1440783617"], [], [], True)
        assert "Added" in out

    def test_a_failed_duplicate_read_is_reported_not_swallowed(self, monkeypatch):
        """A failed read is not proof of an empty playlist. Proceeding quietly
        is how copies stack."""
        monkeypatch.setattr(
            server, "_get_playlist_track_names", lambda pid: (False, "helper timed out")
        )
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)
        monkeypatch.setattr(server, "_itunes_track_by_id", lambda tid: {"name": "S", "artist": "A"})
        monkeypatch.setattr(server.musickit, "add_track_to_playlist", lambda *a: (True, "ok"))
        out = server._api_mode_add_via_musickit(self._resolved(), ["1440783617"], [], [], False)
        assert "Could not check for duplicates" in out
        assert "helper timed out" in out

    def test_a_refused_attach_is_surfaced(self, monkeypatch):
        self._no_dupes(monkeypatch)
        monkeypatch.setattr(server, "_itunes_track_by_id", lambda tid: {"name": "S", "artist": "A"})
        monkeypatch.setattr(
            server.musickit, "add_track_to_playlist", lambda *a: (False, "Apple returned 403")
        )
        out = server._api_mode_add_via_musickit(self._resolved(), ["1440783617"], [], [], False)
        assert "nothing was added" in out
        assert "403" in out


class TestDuplicateCheckHasBothRails:
    def test_musickit_rail_shapes_rows_like_the_rest_rail(self, monkeypatch):
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(
            server.musickit,
            "playlist_tracks",
            lambda pid: (
                True,
                [{"id": "i.A", "attributes": {"name": "Strobe", "artistName": "deadmau5"}}],
            ),
        )

        def never():
            raise AssertionError("no token exists — nothing may ask for headers")

        monkeypatch.setattr(server, "get_headers", never)
        ok, rows = server._get_playlist_track_names("p.Ab1")
        assert ok is True
        assert rows == [{"id": "i.A", "name": "Strobe", "artist": "deadmau5"}]

    def test_a_token_host_still_uses_the_rest_rail(self, monkeypatch):
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)

        def never(pid):
            raise AssertionError("the token rail must win when a token exists")

        monkeypatch.setattr(server.musickit, "playlist_tracks", never)
        monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "x"})

        class Resp:
            status_code = 404

        monkeypatch.setattr(server.requests, "get", lambda *a, **k: Resp())
        ok, rows = server._get_playlist_track_names("p.Ab1")
        assert ok is True and rows == []


class TestPlaylistAddRoutesApiModeToMusicKit:
    def test_a_p_id_with_no_token_takes_the_musickit_rail(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)

        def never():
            raise AssertionError("no token exists — nothing may ask for headers")

        monkeypatch.setattr(server, "get_headers", never)
        seen = {}
        monkeypatch.setattr(
            server,
            "_api_mode_add_via_musickit",
            lambda r, ids, names, steps, dupes: seen.update(api_id=r.api_id, ids=ids, names=names)
            or "done",
        )
        out = server._playlist_add("p.Ab1", track="1440783617")
        assert out == "done"
        assert seen["api_id"] == "p.Ab1"
        assert seen["ids"] == ["1440783617"]


# ---------------------------------------------------------------------------
# The last two reads: a library song, and Apple's own catalog search.
# ---------------------------------------------------------------------------


class TestLibrarySongLookup:
    def test_returns_apples_attributes(self, monkeypatch):
        monkeypatch.setattr(
            musickit,
            "_run",
            lambda *a: (
                True,
                {"body": '{"data":[{"attributes":{"name":"Strobe","artistName":"deadmau5"}}]}'},
            ),
        )
        ok, attrs = musickit.library_song("i.AbC123")
        assert ok is True
        assert attrs["name"] == "Strobe" and attrs["artistName"] == "deadmau5"

    @pytest.mark.parametrize("bad", ["1440783617", "i.", "i.../x", "p.AbC123", ""])
    def test_only_library_ids_are_accepted(self, monkeypatch, bad):
        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.library_song(bad)[0] is False

    def test_an_empty_data_array_is_an_error_not_an_empty_dict(self, monkeypatch):
        """Returning {} would read as 'a track with no name' downstream."""
        monkeypatch.setattr(musickit, "_run", lambda *a: (True, {"body": '{"data":[]}'}))
        ok, msg = musickit.library_song("i.AbC123")
        assert ok is False and "i.AbC123" in msg


class TestCatalogSearchVerb:
    def test_passes_a_normalised_query(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"body": '{"results":{}}'}))[1]
        )
        musickit.catalog_search("  strobe  ", "songs,albums", 100)
        # limit is clamped to Apple's maximum rather than rejected.
        assert seen == [("catalog-search", "strobe", "songs,albums", "25")]

    @pytest.mark.parametrize("types", ["", "tracks", "songs,tracks", "  "])
    def test_types_are_whitelisted(self, monkeypatch, types):
        """`types` is a parameter Apple routes on: an unexpected value returns
        an empty result rather than an error, which is the worst failure shape."""

        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.catalog_search("strobe", types)[0] is False

    @pytest.mark.parametrize("term", ["", "   ", "x" * 513])
    def test_term_bounds(self, monkeypatch, term):
        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.catalog_search(term)[0] is False

    def test_a_non_integer_limit_is_refused(self, monkeypatch):
        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.catalog_search("strobe", "songs", "lots")[0] is False


class TestCatalogSearchIsASecondOpinion:
    def _results(self):
        return {
            "songs": {
                "data": [
                    {
                        "id": "1440783617",
                        "attributes": {
                            "name": "Strobe",
                            "artistName": "deadmau5",
                            "albumName": "For Lack of a Better Name",
                            "durationInMillis": 634000,
                            "releaseDate": "2009-09-22",
                            "genreNames": ["Electronic"],
                            "contentRating": "clean",
                            "url": "https://music.apple.com/us/album/strobe/1",
                        },
                    }
                ]
            }
        }

    def test_rows_match_the_itunes_shape(self, monkeypatch):
        """Both rails feed the same formatter, so a caller must not be able to
        tell which one answered."""
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(
            server.musickit, "catalog_search", lambda q, t, l: (True, self._results())
        )
        rows = server._catalog_search_musickit("strobe", 5)
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {
            "name",
            "duration",
            "artist",
            "album",
            "year",
            "genre",
            "explicit",
            "id",
            "catalog_id",
            "url",
        }
        assert row["year"] == "2009" and row["explicit"] == "No"

    def test_it_is_not_consulted_without_the_rail(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: False)

        def never(*a):
            raise AssertionError("must not spawn without the rail")

        monkeypatch.setattr(server.musickit, "catalog_search", never)
        assert server._catalog_search_musickit("strobe", 5) == []

    def test_a_failed_search_is_empty_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(
            server.musickit, "catalog_search", lambda q, t, l: (False, "helper timed out")
        )
        assert server._catalog_search_musickit("strobe", 5) == []


class TestUnrate:
    def test_clears_a_rating(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            musickit, "_run", lambda *a: (seen.append(a), (True, {"httpStatus": 204}))[1]
        )
        ok, msg = musickit.unrate_song("1440783617")
        assert ok is True and "204" in msg
        assert seen == [("unrate", "1440783617")]

    def test_a_bad_id_never_spawns(self, monkeypatch):
        def boom(*a):
            raise AssertionError("must not spawn")

        monkeypatch.setattr(musickit, "_run", boom)
        assert musickit.unrate_song("12a")[0] is False


# ---------------------------------------------------------------------------
# Field report: `config auth-status` said catalog adds were ready while the
# direct library-add path answered "this build ships no MusicKit helper".
#
# Two bugs, both introduced by the conversion rather than surviving it:
#   * _library_add's MESSAGE was updated to point at config(action='signin')
#     while its GATE still asked only about tokens, so a MusicKit-only host was
#     refused — the one converted site that got prose without routing.
#   * _musickit_setup_hint had no "authorized" case, so the dict lookup fell to
#     the default and told the user their build had no helper while an
#     authorized one sat right there.
# ---------------------------------------------------------------------------


class TestTheHintNeverDeniesAHelperItCanSee:
    def test_authorized_is_not_reported_as_a_missing_build(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        hint = server._musickit_setup_hint()
        assert "ships no MusicKit helper" not in hint
        assert "already granted" in hint

    @pytest.mark.parametrize(
        "status", ["authorized", "notDetermined", "denied", "restricted", "unknown"]
    )
    def test_a_present_helper_is_never_called_absent(self, monkeypatch, status):
        """Whatever the state, if the binary is on disk the hint must not tell
        the user to go install it."""
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: status)
        assert "ships no MusicKit helper" not in server._musickit_setup_hint()

    def test_a_genuinely_absent_helper_still_says_so(self, monkeypatch):
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        assert "ships no MusicKit helper" in server._musickit_setup_hint()


class TestDirectLibraryAddMatchesThePlaylistRoute:
    """The two routes disagreeing is the symptom that exposed this: adding via
    the playlist worked while the direct library add refused."""

    def _musickit_only(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server.audit_log, "log_action", lambda *a, **k: None)

        def never():
            raise AssertionError("no token exists — nothing may ask for headers")

        monkeypatch.setattr(server, "get_headers", never)

    def test_a_musickit_only_host_is_not_refused(self, monkeypatch):
        self._musickit_only(monkeypatch)
        monkeypatch.setattr(
            server,
            "_catalog_search_itunes",
            lambda q, n: [{"id": "1440783617", "name": "Strobe", "artist": "deadmau5"}],
        )
        added = []
        monkeypatch.setattr(
            server,
            "_add_songs_to_library",
            lambda ids, known=None: (added.append(ids), (True, "ok"))[1],
        )
        out = server._library_add(track="Strobe", artist="deadmau5")
        assert "isn't set up yet" not in out
        assert added == [["1440783617"]]

    def test_by_catalog_id_too(self, monkeypatch):
        self._musickit_only(monkeypatch)
        added = []
        monkeypatch.setattr(
            server,
            "_add_songs_to_library",
            lambda ids, known=None: (added.append(ids), (True, "ok"))[1],
        )
        server._library_add(track="1440783617")
        assert added == [["1440783617"]]

    def test_albums_go_through_the_album_rail(self, monkeypatch):
        """cmdAdd hardcodes ids[songs], so an album must not be sent as a song."""
        self._musickit_only(monkeypatch)
        seen = []
        monkeypatch.setattr(
            server, "_add_album_to_library", lambda cid: (seen.append(cid), (True, "ok"))[1]
        )
        monkeypatch.setattr(
            server,
            "_add_songs_to_library",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("an album must not use the songs rail")
            ),
        )
        server._library_add(album="1065973699")
        assert seen == ["1065973699"]

    def test_neither_rail_still_refuses(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        out = server._library_add(track="Strobe", artist="deadmau5")
        assert "isn't set up yet" in out
        assert "applemusic-mcp" not in out


class TestStatusDoesNotContradictItself:
    """Found by reading a real status output: "Catalog add: OK (MusicKit — no
    credential stored)" and "Adding catalog tracks needs sign-in" appeared four
    lines apart in the SAME response, because the summary line gated on tokens
    only. Fourth site with this bug, and the most visible one — status is what a
    confused user reads first."""

    def test_musickit_only_host_is_not_told_to_sign_in(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")
        out = server._auth_action("status")
        # This host now takes the plain-answer path, which is the same fix seen
        # from the other end: it cannot contradict itself because it no longer
        # reports rails at all. What must never come back is a sign-in demand.
        assert "needs sign-in" not in out
        assert "signin" not in out

    def test_a_host_with_neither_rail_is_still_told(self, monkeypatch):
        # _write_rail returns "none" off macOS, so the native branch carrying
        # this line is only reached with AppleScript available.
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: False)
        monkeypatch.setattr(server.musickit, "is_available", lambda: False)
        out = server._auth_action("status")
        assert "needs sign-in" in out


class TestStatusAnswersTheQuestionAsked:
    """ "What is this signed in to?" — "nothing" is both the true answer and the
    whole point of the project, and it used to be something you had to infer
    from two lines of token bookkeeping plus a rail report."""

    def _clean_host(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "has_any_developer_token", lambda: False)
        monkeypatch.setattr(server, "has_user_token", lambda: False)
        monkeypatch.setattr(server, "_can_use_musickit_rail", lambda: True)
        monkeypatch.setattr(server.musickit, "is_available", lambda: True)
        monkeypatch.setattr(server.musickit, "authorization_status", lambda: "authorized")

    def test_it_leads_with_nothing(self, monkeypatch):
        self._clean_host(monkeypatch)
        out = server._auth_action("status")
        assert out.startswith("Signed in to: nothing.")

    def test_it_drops_the_token_bookkeeping(self, monkeypatch):
        """The old output opened with two 'not configured (optional — …)' lines
        about tokens the user does not have and does not need."""
        self._clean_host(monkeypatch)
        out = server._auth_action("status")
        for jargon in ("Developer Token:", "Music User Token:", "Engines:", "Writes:", "Mode:"):
            assert jargon not in out, f"{jargon!r} is bookkeeping, not an answer"

    def test_a_configured_token_still_gets_the_full_breakdown(self, monkeypatch):
        """The detail earns its keep in every other configuration — someone who
        opted into a token is debugging something."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "has_any_developer_token", lambda: True)
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        monkeypatch.setattr(server, "_api_session_status", lambda: "ok")
        out = server._auth_action("status")
        assert "Signed in to: nothing." not in out
        assert "Writes:" in out

    def test_forced_tokenless_is_never_reported_as_fine(self, monkeypatch):
        """The kill switch must not be swallowed by the happy path."""
        self._clean_host(monkeypatch)
        monkeypatch.setenv("APPLEMUSIC_FORCE_TOKENLESS", "1")
        out = server._auth_action("status")
        assert "Signed in to: nothing." not in out
        assert "APPLEMUSIC_FORCE_TOKENLESS" in out


class TestTheHelperShipsInsideTheWheel:
    """PyPI distribution was ruled out because a wheel would carry no MusicKit
    helper, making the packaged install the weakest build of the product. It
    turned out the helper survives a wheel intact — 148K, three files, with the
    signature in regular files and the notarization ticket in
    Contents/CodeResources, so nothing depends on extended attributes."""

    def test_the_packaged_location_is_searched(self, monkeypatch):
        """A pip/uvx install has no .app parent and no source checkout. Without
        this candidate the packaged install has no MusicKit rail at all."""
        import applemusic_mcp.musickit as mk

        beside_module = Path(mk.__file__).resolve().parent / mk.HELPER_APP / mk._HELPER_REL
        assert beside_module in mk._candidates()

    def test_the_env_override_still_wins(self, monkeypatch):
        """Ordering matters: an explicit override must beat every discovered
        location, including the new one."""
        import applemusic_mcp.musickit as mk

        monkeypatch.setenv(mk._ENV_OVERRIDE, "/tmp/some/other/helper")
        assert mk._candidates()[0] == Path("/tmp/some/other/helper")

    def test_pyproject_ships_the_helper_beside_the_module(self):
        """The wheel must place the .app where _candidates() looks. These two
        are coupled only by this test — nothing else fails if they drift."""
        import applemusic_mcp.musickit as mk

        cfg = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = cfg.read_text(encoding="utf-8")
        assert "force-include" in text, "the wheel no longer ships the helper"
        assert f"applemusic_mcp/{mk.HELPER_APP}" in text, (
            f"the wheel ships the helper somewhere other than beside the module; "
            f"_candidates() looks for applemusic_mcp/{mk.HELPER_APP}"
        )

    def test_the_wheel_is_not_tagged_pure_python(self):
        """A py3-none-any tag would let pip install this on Linux and Windows,
        where the signed helper is dead weight that cannot run."""
        hook = Path(__file__).resolve().parents[1] / "scripts" / "wheel_tag.py"
        assert hook.exists(), "the platform-tag build hook is gone"
        text = hook.read_text(encoding="utf-8")
        assert "macosx" in text and "pure_python" in text

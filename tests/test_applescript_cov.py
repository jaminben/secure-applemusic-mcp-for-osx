"""Fast, fully-mocked unit tests for applemusic_mcp.applescript.

These tests NEVER shell out to osascript or launch Music.app. Every I/O
boundary (run_applescript, subprocess.run, the JXA/CoreGraphics helpers, the
Quartz lock probe) is monkeypatched, so coverage is hardware-independent and
runs in well under a second.

They are deliberately NOT marked `slow` so they run in the default fast subset.
The companion file tests/test_applescript.py keeps the live/slow integration
tests; this file only exercises parsing, formatting, and control-flow.
"""

import sys
import types

import pytest

from applemusic_mcp import applescript as asc

# Keep a handle to the real implementation so its own logic can still be tested
# in isolation (with subprocess mocked). The conftest session sweep guards itself
# with `is_available()`, so we do NOT stub run_applescript globally — that would
# leak into the live tests in tests/test_applescript.py. Instead every test here
# defaults to a no-op via the function-scoped autouse fixture below.
_real_run_applescript = asc.run_applescript


@pytest.fixture(autouse=True)
def _default_stub_run_applescript(monkeypatch):
    """Default every test in THIS module to a no-op run_applescript, overridden
    per-test by a Router/Seq. monkeypatch auto-reverts, so it never leaks into
    other modules' live tests."""
    monkeypatch.setattr(asc, "run_applescript", lambda script: (False, ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Router:
    """Route run_applescript calls to canned (ok, text) by substring match."""

    def __init__(self, rules, default=(True, "")):
        self.rules = rules
        self.default = default
        self.calls = []

    def __call__(self, *args):
        script = args[0] if args else ""
        self.calls.append(script)
        for sub, resp in self.rules:
            if sub in script:
                return resp
        return self.default


class Seq:
    """Return queued responses in order; default once exhausted.

    Accepts zero-arg calls so it doubles as a fake time.monotonic/time.time.
    """

    def __init__(self, responses, default=(True, "")):
        self.responses = list(responses)
        self.default = default
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args[0] if args else None)
        if self.responses:
            return self.responses.pop(0)
        return self.default


class Recorder:
    """run_applescript stub that records every script and returns a constant."""

    def __init__(self, ok, text=""):
        self.resp = (ok, text)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args[0] if args else "")
        return self.resp


def const(ok, text=""):
    return lambda *args: (ok, text)


def code_only(script):
    """Strip `--` comment lines so assertions test generated code, not prose.

    Without this, a comment explaining which idiom to avoid trips any test
    asserting that idiom is absent.
    """
    return "\n".join(ln for ln in script.splitlines() if not ln.strip().startswith("--"))


def proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fast_env(monkeypatch):
    """Kill real sleeps and reset the module-level search-field cache."""
    monkeypatch.setattr(asc.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(asc, "_search_field_cache", None, raising=False)
    yield


def setrun(monkeypatch, fake):
    monkeypatch.setattr(asc, "run_applescript", fake)
    return fake


# ===========================================================================
# Pure string / parsing helpers
# ===========================================================================
def test_is_available(monkeypatch):
    monkeypatch.setattr(asc.sys, "platform", "darwin")
    monkeypatch.setattr(asc.shutil, "which", lambda _: "/usr/bin/osascript")
    assert asc.is_available() is True
    monkeypatch.setattr(asc.shutil, "which", lambda _: None)
    assert asc.is_available() is False
    monkeypatch.setattr(asc.sys, "platform", "linux")
    assert asc.is_available() is False


def test_escape_for_applescript():
    # control chars stripped, backslash then quote escaped
    out = asc._escape_for_applescript('a\r\nb\tc"d\\e')
    assert "\n" not in out and "\t" not in out and "\r" not in out
    assert '\\"' in out and "\\\\" in out


def test_name_contains_clause_variants():
    # Title that is only punctuation -> fallback single clause from original
    only_punct = asc._name_contains_clause("...")
    assert only_punct.startswith("name contains")
    # Single surviving fragment -> no parens
    single = asc._name_contains_clause("Wait...")
    assert single == 'name contains "Wait"'
    # Multiple fragments -> parenthesized AND
    multi = asc._name_contains_clause("That's a No No")
    assert multi.startswith("(") and " and " in multi


@pytest.mark.parametrize(
    "line,expected_name",
    [
        ("no delimiter here", None),
        ("a|||b|||c", None),  # < 7 parts
    ],
)
def test_parse_library_track_line_rejects(line, expected_name):
    assert asc._parse_library_track_line(line) is None


def test_parse_library_track_line_full():
    d = asc._parse_library_track_line("N|||Ar|||Al|||125|||Rock|||2020|||PID|||true")
    assert d["name"] == "N" and d["duration"] == "2:05" and d["explicit"] == "Yes"
    # bad duration -> empty; only 7 fields -> Unknown explicit
    d2 = asc._parse_library_track_line("N|||Ar|||Al|||xx|||Rock|||2020|||PID")
    assert d2["duration"] == "" and d2["explicit"] == "Unknown"
    # explicit false -> No
    d3 = asc._parse_library_track_line("N|||Ar|||Al|||10|||G|||Y|||P|||false")
    assert d3["explicit"] == "No"


def test_track_filter_clause():
    assert asc._track_filter_clause(track_id="X").startswith("whose persistent ID")
    assert "artist contains" in asc._track_filter_clause("Song", artist="A")
    assert asc._track_filter_clause("Song").startswith("whose ")
    assert asc._track_filter_clause() is None


def test_library_track_query_and_resolve():
    assert "artist contains" in asc._library_track_query("S", "A")
    assert asc._library_track_query("S").startswith("first track")
    with_artist = asc._resolve_library_track_applescript("S", "A")
    assert "allArtists" in with_artist
    no_artist = asc._resolve_library_track_applescript("S")
    assert "allArtists" not in no_artist and "targetTrack" in no_artist


# ===========================================================================
# run_applescript (the real implementation) + classify_error
# ===========================================================================
def test_run_applescript_paths(monkeypatch):
    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(0, " hi \n"))
    assert _real_run_applescript("x") == (True, "hi")

    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(1, "", " boom "))
    assert _real_run_applescript("x") == (False, "boom")

    def _timeout(*a, **k):
        raise asc.subprocess.TimeoutExpired(cmd="osascript", timeout=30)

    monkeypatch.setattr(asc.subprocess, "run", _timeout)
    ok, msg = _real_run_applescript("x")
    assert ok is False and "timed out" in msg

    def _boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(asc.subprocess, "run", _boom)
    ok, msg = _real_run_applescript("x")
    assert ok is False and "nope" in msg


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", asc.ERROR_UNKNOWN),
        ("AppleScript timed out after 30 seconds", asc.ERROR_TIMEOUT),
        ("error -1743 Not authorized", asc.ERROR_AUTOMATION_DENIED),
        ("not authorised", asc.ERROR_AUTOMATION_DENIED),
        ("Music isn't running (-10810)", asc.ERROR_MUSIC_NOT_RUNNING),
        ("syntax error: expected end but found x", asc.ERROR_SYNTAX),
        ("can't get track 99", asc.ERROR_UNKNOWN),
    ],
)
def test_classify_error(text, expected):
    assert asc.classify_error(text) == expected


# ===========================================================================
# Playback one-liners
# ===========================================================================
def test_playback_oneliners(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "ok"))
    for fn in (
        asc.play,
        asc.pause,
        asc.playpause,
        asc.stop,
        asc.next_track,
        asc.previous_track,
        asc.get_player_state,
        asc.get_repeat,
    ):
        assert fn() == (True, "ok")
    assert asc.seek(12.5) == (True, "ok")
    assert asc.set_volume(250) == (True, "ok")  # clamped, no raise
    assert asc.set_shuffle(True) == (True, "ok")
    assert "tell application" in r.calls[-1]


def test_get_current_track(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_current_track() == (False, "err")

    setrun(monkeypatch, const(True, "STOPPED"))
    assert asc.get_current_track() == (True, {"state": "stopped"})

    setrun(monkeypatch, const(True, "name:Song\nartist:A\nbad-line\nyear:2020"))
    ok, info = asc.get_current_track()
    assert ok and info["state"] == "playing" and info["name"] == "Song" and info["year"] == "2020"


def test_get_volume(monkeypatch):
    setrun(monkeypatch, const(True, "42"))
    assert asc.get_volume() == (True, 42)
    setrun(monkeypatch, const(True, "loud"))
    ok, msg = asc.get_volume()
    assert ok is False and "Invalid volume" in msg
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_volume() == (False, "err")


def test_get_shuffle(monkeypatch):
    setrun(monkeypatch, const(True, "true"))
    assert asc.get_shuffle() == (True, True)
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_shuffle() == (False, "err")


def test_set_repeat(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "ok"))
    assert asc.set_repeat("none") == (True, "ok")  # synonym -> off
    assert "to off" in r.calls[-1]
    assert asc.set_repeat("one") == (True, "ok")
    ok, msg = asc.set_repeat("bogus")
    assert ok is False and "Invalid repeat" in msg


# ===========================================================================
# Playlist operations
# ===========================================================================
def test_get_playlists(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_playlists() == (False, "err")
    setrun(monkeypatch, const(True, "P|||ID|||true|||5|||3:00\nshort|||line"))
    ok, pls = asc.get_playlists()
    assert ok and len(pls) == 1 and pls[0]["smart"] is True and pls[0]["track_count"] == 5


def test_get_playlist_tracks_bulk_and_slow(monkeypatch):
    # bulk fails with Can't get -> slow fallback succeeds
    seq = Seq([(False, "Can't get foo"), (True, "N|||Ar|||Al|||bad|||||||||PID")])
    setrun(monkeypatch, seq)
    ok, tracks = asc.get_playlist_tracks("PL")
    assert ok and tracks[0]["duration"] == "" and tracks[0]["id"] == "PID"
    assert len(seq.calls) == 2  # both bulk + slow ran

    # ERROR: prefix
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    ok, msg = asc.get_playlist_tracks("PL")
    assert ok is False and msg == "Playlist not found"

    # both paths failing reports the error rather than hanging on
    setrun(monkeypatch, const(False, "boom"))
    assert asc.get_playlist_tracks("PL") == (False, "boom")


@pytest.mark.parametrize(
    "bulk_error, retries, why",
    [
        (
            "Music got an error: Can't get name of {x}. (-1728)",
            True,
            "logic-level: the case the per-track loop survives",
        ),
        ("Cannot connect to target", True, "unclassified -> cascade, as classify_error intends"),
        (
            "AppleScript timed out after 30 seconds",
            False,
            "slow path sends ~7x more events; it would time out too",
        ),
        (
            "Not authorized to send Apple events to Music. (-1743)",
            False,
            "permission denied is not per-track",
        ),
        ("Music got an error: Application isn't running. (-600)", False, "nothing to talk to"),
        (
            "syntax error: Expected end of line but found identifier.",
            False,
            "our bug; retrying hides it",
        ),
    ],
)
def test_get_playlist_tracks_retries_only_logic_level_failures(
    monkeypatch, bulk_error, retries, why
):
    """Only a failure that could plausibly be per-track earns the slow path.

    What changed is the axis of the decision. The old gate matched English
    error text (`"Can" in output and "get" in output`) -- so it retried a
    timeout never (no such words) and "Cannot connect to target" always
    ("tar-get"), neither for a reason connected to the failure. The gate is
    now the error's CLASS: an environmental failure is not retried, because
    the slow path issues ~7x more Apple Events, so a Music that already
    timed out fails again and the user waits 60s for the same error.
    Unclassified errors still cascade, which is what classify_error's
    `unknown` category exists for.
    """
    seq = Seq([(False, bulk_error), (True, "N|||Ar|||Al|||125|||G|||2020|||PID")])
    setrun(monkeypatch, seq)
    ok, result = asc.get_playlist_tracks("PL")

    assert len(seq.calls) == (2 if retries else 1), why
    if retries:
        assert ok and result[0]["id"] == "PID"
    else:
        assert (ok, result) == (False, bulk_error)


def test_get_playlist_tracks_bulk_rejects_non_numeric_limit(monkeypatch):
    """`limit` is interpolated into the script, so it must be an int -- but a
    bad one has to come back as (False, message), not raise out of a function
    whose whole contract is a tuple."""
    recorder = Recorder(True, "")
    setrun(monkeypatch, recorder)
    ok, msg = asc._get_playlist_tracks_bulk("PL", "; do evil")
    assert ok is False and "must be an integer" in msg
    assert recorder.calls == [], "nothing should reach osascript"

    # success bulk with good duration
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||125|||G|||2020|||PID"))
    ok, tracks = asc.get_playlist_tracks("PL")
    assert ok and tracks[0]["duration"] == "2:05"

    # Music reports an unset year as 0. The bulk read has no per-track
    # try/catch to blank it, and ~12% of a real library hits this, so a
    # passthrough would have consumers reporting a release year of 0.
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||125|||G|||0|||PID"))
    ok, tracks = asc.get_playlist_tracks("PL")
    assert ok and tracks[0]["year"] == ""
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||125|||G|||2020|||PID"))
    assert asc.get_playlist_tracks("PL")[1][0]["year"] == "2020", "real years survive"


def test_get_playlist_tracks_bulk_limits_property_fetch_to_slice(monkeypatch):
    """Regression test: the bulk fast-path must bound its property reads to
    (at most) `limit` tracks, not pull properties for the whole playlist.

    Before the fix, `_get_playlist_tracks_bulk` unconditionally read every
    property (name/artist/album/duration/genre/year/persistent ID) for
    *all* tracks in the playlist via `<property> of allTracks`, and only
    applied `limit` afterward when formatting the output string. That makes
    the AppleScript call cost O(playlist size) instead of O(limit), which is
    what caused timeouts on multi-thousand-track playlists even when only a
    handful of tracks were requested.
    """
    recorder = Recorder(True, "")
    setrun(monkeypatch, recorder)

    asc._get_playlist_tracks_bulk("PL", 5)

    assert len(recorder.calls) == 1
    script = recorder.calls[0]

    # None of the bulk property reads should be taken against the full,
    # unsliced `allTracks` list -- they must be bounded to (at most) `limit`
    # tracks first.
    for prop in ("name", "artist", "album", "duration", "genre", "year", "persistent ID"):
        assert f"{prop} of allTracks" not in script, (
            f"bulk fetch reads '{prop}' from the full playlist track list "
            "instead of a slice bounded by `limit` -- this is O(playlist "
            "size) instead of O(limit) and will time out on large playlists"
        )


def test_get_playlist_tracks_bulk_uses_reference_form(monkeypatch):
    """The bulk read must address the tracks as a range OF THE PLAYLIST.

    `set allTracks to tracks of targetPlaylist` materializes a plain
    AppleScript list, and `<property> of <plain list>` does not distribute:
    Music raises -1728 and names every track in the error, so the fast path
    failed 100% of the time on Music 1.5.6 and every call silently served
    from the slow path (which skips genre/year). Only the collection-reference
    form -- `name of tracks 1 thru N of targetPlaylist` -- is evaluated by the
    app, and it is the idiom SKILL.md has documented all along.
    """
    recorder = Recorder(True, "")
    setrun(monkeypatch, recorder)
    asc._get_playlist_tracks_bulk("PL", 5)
    script = code_only(recorder.calls[0])

    for prop in ("name", "artist", "album", "duration", "genre", "year", "persistent ID"):
        assert f"{prop} of tracks 1 thru maxTracks of targetPlaylist" in script, (
            f"'{prop}' must be read as a range of targetPlaylist; reading it off an "
            "intermediate list variable raises -1728 and kills the fast path"
        )
        # A 1-track range returns a bare value rather than a 1-item list, so
        # without the coercion `item 1 of allNames` indexes into the string
        # and yields its first CHARACTER ("T" instead of "Take On Me").
        assert (
            f"(get {prop} of tracks 1 thru maxTracks of targetPlaylist) as list" in script
        ), f"'{prop}' must be coerced `as list` or limit=1 returns one character"

    assert "set allTracks to" not in script


@pytest.mark.parametrize(
    "limit, expected",
    [
        (5, "set maxTracks to 5"),
        (0, "set maxTracks to 0"),
        (-3, "set maxTracks to 0"),  # negative would count back from the END
    ],
)
def test_get_playlist_tracks_bulk_clamps_limit(monkeypatch, limit, expected):
    """Both range clamps are load-bearing -- each bad bound is an AppleScript
    error, not an empty result.

    `tracks 1 thru 0` raises -1728, and its message enumerates the entire
    playlist (1,096,166 bytes on a 12,457-track playlist). A negative limit is
    worse than useless: `tracks 1 thru -3` means "through the third-from-last",
    i.e. nearly the whole playlist -- the exact cost this bound exists to avoid.
    """
    recorder = Recorder(True, "")
    setrun(monkeypatch, recorder)
    asc._get_playlist_tracks_bulk("PL", limit)
    script = recorder.calls[0]

    assert expected in script
    assert "if maxTracks < 1 then return" in script, "limit<1 must return before the range"
    assert (
        "if maxTracks > trackCount then set maxTracks to trackCount" in script
    ), "a range past the end of the playlist is an error, not a truncation"


def test_create_playlist(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "PID"))
    assert asc.create_playlist("X") == (True, "PID")
    assert "description" not in r.calls[-1]
    asc.create_playlist("X", "desc")
    assert "description" in r.calls[-1]


def test_resolve_folder_path_applescript():
    assert "Empty folder path" in asc._resolve_folder_path_applescript("  /  ")
    assert "Folder not found" in asc._resolve_folder_path_applescript("Solo")
    multi = asc._resolve_folder_path_applescript("A/B/C")
    assert "Subfolder not found: B" in multi and "Subfolder not found: C" in multi


def test_get_folder_tree(monkeypatch):
    setrun(monkeypatch, const(True, "[Folder]\n  Child"))
    assert asc.get_folder_tree() == (True, "[Folder]\n  Child")


def test_create_folder_path(monkeypatch):
    assert asc.create_folder_path("  ") == (False, "Empty folder path")
    r = setrun(monkeypatch, Recorder(True, "LEAF"))
    assert asc.create_folder_path("A") == (True, "LEAF")
    asc.create_folder_path("A/B")
    assert "folder1" in r.calls[-1] and "move folder1 to folder0" in r.calls[-1]


def test_create_folder(monkeypatch):
    setrun(monkeypatch, const(True, "FID"))
    assert asc.create_folder("F") == (True, "FID")


def test_move_to_folder(monkeypatch):
    setrun(monkeypatch, const(True, "Moved"))
    assert asc.move_to_folder("PL", "Dest") == (True, "Moved")
    setrun(monkeypatch, const(True, "ERROR:Folder not found: Dest"))
    assert asc.move_to_folder("PL", "Dest") == (False, "Folder not found: Dest")


def test_move_to_root(monkeypatch):
    setrun(monkeypatch, const(True, "Moved 'X' to top level (playlist recreated)"))
    ok, msg = asc.move_to_root("X")
    assert ok and "top level" in msg
    setrun(monkeypatch, const(True, "ERROR:Playlist is already at top level"))
    assert asc.move_to_root("X") == (False, "Playlist is already at top level")


def test_get_playlist_path(monkeypatch):
    setrun(monkeypatch, const(True, "A/B/PL"))
    assert asc.get_playlist_path("PL") == (True, "A/B/PL")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.get_playlist_path("PL") == (False, "Playlist not found")


def test_delete_folder_and_playlist_and_rename(monkeypatch):
    setrun(monkeypatch, const(True, "Deleted folder: F"))
    assert asc.delete_folder("F") == (True, "Deleted folder: F")
    setrun(monkeypatch, const(True, "ERROR:Folder not found: F"))
    assert asc.delete_folder("F") == (False, "Folder not found: F")

    # delete_playlist now pre-flights an ambiguity check via get_playlists().
    # With a constant stub the listing comes back unparseable (no rows), which
    # the guard treats as "couldn't enumerate" and passes through.
    setrun(monkeypatch, const(True, "Deleted playlist: P"))
    assert asc.delete_playlist("P") == (True, "Deleted playlist: P")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.delete_playlist("P") == (False, "Playlist not found")

    setrun(monkeypatch, const(True, "Renamed: A -> B"))
    assert asc.rename_playlist("A", "B")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.rename_playlist("A", "B") == (False, "Playlist not found")


def test_track_exists_in_playlist(monkeypatch):
    setrun(monkeypatch, const(False, "boom"))
    assert asc.track_exists_in_playlist("PL", "T") == (False, "boom")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.track_exists_in_playlist("PL", "T") == (False, "Playlist not found")
    setrun(monkeypatch, const(True, "FOUND:T - A"))
    assert asc.track_exists_in_playlist("PL", "T", artist="A") == (True, "T - A")
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc.track_exists_in_playlist("PL", "T") == (True, False)


def test_add_track_to_playlist(monkeypatch):
    # with artist -> fallback query branch + album
    r = setrun(monkeypatch, Recorder(True, "Added"))
    assert asc.add_track_to_playlist("PL", "T", artist="A", album="Al") == (True, "Added")
    assert "artist contains" in r.calls[-1]  # fallback path present
    # without artist -> simple branch
    r = setrun(monkeypatch, Recorder(True, "Added"))
    asc.add_track_to_playlist("PL", "T")
    assert "artist contains" not in r.calls[-1]
    # ERROR
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.add_track_to_playlist("PL", "T") == (False, "Track not found: T")


def test_remove_track_from_playlist(monkeypatch):
    assert asc.remove_track_from_playlist("PL") == (False, "Must provide track_name or track_id")
    setrun(monkeypatch, const(True, "Removed T by A from PL"))
    assert asc.remove_track_from_playlist("PL", "T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found in playlist"))
    assert asc.remove_track_from_playlist("PL", track_id="X") == (
        False,
        "Track not found in playlist",
    )


def test_remove_from_library(monkeypatch):
    assert asc.remove_from_library() == (False, "Must provide track_name or track_id")
    # remove_from_library now enumerates matches first and refuses if >1. A
    # constant stub yields one parseable row, so the removal proceeds.
    setrun(monkeypatch, const(True, "T|||A"))
    assert asc.remove_from_library("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found in library"))
    assert asc.remove_from_library(track_id="X") == (False, "Track not found in library")


def test_search_playlist(monkeypatch):
    setrun(monkeypatch, const(False, "boom"))
    assert asc.search_playlist("PL", "q") == (False, "boom")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.search_playlist("PL", "q") == (False, "Playlist not found")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||PID|||true\n\nN2|||Ar2|||Al2|||PID2"))
    ok, tracks = asc.search_playlist("PL", "q")
    assert ok and tracks[0]["explicit"] == "Yes" and tracks[1]["explicit"] == "Unknown"


def test_download_tracks(monkeypatch):
    assert asc.download_tracks("1", "PL")[0] is False  # both
    assert asc.download_tracks()[0] is False  # neither
    setrun(monkeypatch, const(True, "Downloading playlist: PL"))
    assert asc.download_tracks(playlist_name="PL")[0] is True
    assert asc.download_tracks(track_ids=" , ")[0] is False  # no valid ids
    r = setrun(monkeypatch, Recorder(True, "Downloading 2 track(s)"))
    assert asc.download_tracks(track_ids="a,b")[0] is True
    assert "download (first track" in r.calls[-1]
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.download_tracks(playlist_name="PL") == (False, "Playlist not found")


def test_play_playlist(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "Now playing: PL"))
    asc.play_playlist("PL", shuffle=True)
    assert "shuffle enabled to true" in r.calls[-1]
    asc.play_playlist("PL")
    assert "shuffle enabled to false" in r.calls[-1]
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.play_playlist("PL") == (False, "Playlist not found")


def test_find_library_track(monkeypatch):
    assert asc.find_library_track("  ") == (False, "")
    setrun(monkeypatch, const(True, "Name|||Artist"))
    assert asc.find_library_track("Name", artist="Artist") == (True, "Name|||Artist")
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc.find_library_track("Name") == (False, "")


def test_play_track(monkeypatch):
    setrun(monkeypatch, const(True, "Now playing: T by A"))
    assert asc.play_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.play_track("T") == (False, "Track not found: T")


def test_check_playing(monkeypatch):
    setrun(monkeypatch, const(True, "playing"))
    assert asc._check_playing() is True
    setrun(monkeypatch, const(True, "paused"))
    assert asc._check_playing() is False


# ===========================================================================
# Library search / listing
# ===========================================================================
def test_get_library_songs(monkeypatch):
    assert asc.get_library_songs(limit=-1)[0] is False
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_songs() == (False, "err")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||60|||G|||2020|||P|||true\nbad"))
    ok, tracks = asc.get_library_songs(limit=5)
    assert ok and len(tracks) == 1 and tracks[0]["duration"] == "1:00"


def test_get_loved_songs(monkeypatch):
    assert asc.get_loved_songs(limit=-1)[0] is False
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_loved_songs() == (False, "err")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||60|||G|||2020|||P|||false"))
    ok, tracks = asc.get_loved_songs(limit=10)
    assert ok and tracks[0]["explicit"] == "No"


def test_get_library_songs_page(monkeypatch):
    assert asc.get_library_songs_page(0, 0) == (False, [], 0, "limit must be > 0")
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_songs_page(0, 10) == (False, [], 0, "err")
    setrun(monkeypatch, const(True, "total:bad\ntotal:3\nN|||Ar|||Al|||60|||G|||2020|||P|||true"))
    ok, tracks, total, err = asc.get_library_songs_page(0, 10)
    assert ok and total == 3 and len(tracks) == 1 and err == ""


def test_library_search_source():
    assert "genre contains" in asc._library_search_source("Rock", "genre")
    assert "only songs" in asc._library_search_source("q", "songs")
    assert asc._library_search_source("q", "all").endswith('"q" ')


def test_search_library_page_and_wrapper(monkeypatch):
    assert asc.search_library_page("q", limit=0) == (False, [], 0, "limit must be > 0")
    setrun(monkeypatch, const(False, "err"))
    assert asc.search_library_page("q") == (False, [], 0, "err")
    setrun(monkeypatch, const(True, "total:bad\ntotal:1\nN|||Ar|||Al|||60|||G|||2020|||P|||true"))
    ok, tracks, total, err = asc.search_library_page("q")
    assert ok and total == 1 and tracks[0]["name"] == "N"

    # wrapper success + failure
    setrun(monkeypatch, const(True, "total:0\n"))
    assert asc.search_library("q") == (True, [])
    setrun(monkeypatch, const(False, "boom"))
    assert asc.search_library("q") == (False, "boom")


# ===========================================================================
# Track metadata
# ===========================================================================
def test_love_dislike(monkeypatch):
    setrun(monkeypatch, const(True, "Loved: T"))
    assert asc.love_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.love_track("T") == (False, "Track not found: T")
    setrun(monkeypatch, const(True, "Disliked: T"))
    assert asc.dislike_track("T", artist="A")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.dislike_track("T") == (False, "Track not found: T")


def test_rating(monkeypatch):
    setrun(monkeypatch, const(True, "60"))
    assert asc.get_rating("T") == (True, 60)
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.get_rating("T") == (False, "Track not found: T")
    setrun(monkeypatch, const(True, "notanint"))
    ok, msg = asc.get_rating("T")
    assert ok is False and "Invalid rating" in msg

    setrun(monkeypatch, const(True, "Set rating to 100 for: T"))
    assert asc.set_rating("T", 250)[0] is True  # clamped
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.set_rating("T", 40) == (False, "Track not found: T")


# ===========================================================================
# AirPlay / utilities / stats
# ===========================================================================
def test_airplay(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_airplay_devices() == (False, "err")
    setrun(monkeypatch, const(True, "Living Room\n\nKitchen"))
    assert asc.get_airplay_devices() == (True, ["Living Room", "Kitchen"])

    setrun(monkeypatch, const(True, "Switched to: Living Room"))
    assert asc.set_airplay_device("Living")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Device not found: X"))
    assert asc.set_airplay_device("X") == (False, "Device not found: X")


def test_reveal_track(monkeypatch):
    setrun(monkeypatch, const(True, "Revealed: T"))
    assert asc.reveal_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.reveal_track("T") == (False, "Track not found: T")


def test_get_library_stats(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_stats() == (False, "err")
    setrun(monkeypatch, const(True, "100|||5|||playing|||true|||all|||50"))
    ok, stats = asc.get_library_stats()
    assert ok and stats["track_count"] == 100 and stats["shuffle"] is True
    setrun(monkeypatch, const(True, "only|||three|||parts"))
    assert asc.get_library_stats()[0] is False


# ===========================================================================
# library_snapshot / library_diff
# ===========================================================================
def test_library_snapshot_failures(monkeypatch):
    setrun(monkeypatch, const(False, "countfail"))
    ok, d = asc.library_snapshot()
    assert ok is False and "Failed to count" in d["error"]

    setrun(monkeypatch, const(True, "notanint"))
    ok, d = asc.library_snapshot()
    assert ok is False and "Invalid track count" in d["error"]

    # count ok, playback ok, playlist fetch fails
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "100")),
                ("set ps to player state", (True, "playing\n50\ntrue\nall\nSong\nArtist\nAlbum")),
                ("repeat with p in user playlists", (False, "plfail")),
            ]
        ),
    )
    ok, d = asc.library_snapshot()
    assert ok is False and "Failed to get playlists" in d["error"]


def test_library_snapshot_success(monkeypatch):
    playlist_data = (
        "PLAYLIST:My List|||FOLDER:\n"
        "T1|||A1|||Alb1\n"
        "PLAYLIST:Nested|||FOLDER:Summer\n"
        "T2|||A2|||Alb2\n"
        "FOLDER:Summer|||PATH:\n"
        "\n"
        "shortline\n"
    )
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "100")),
                ("set ps to player state", (True, "playing\n50\ntrue\nall\nSong\nArtist\nAlbum")),
                ("repeat with p in user playlists", (True, playlist_data)),
            ]
        ),
    )
    ok, snap = asc.library_snapshot()
    assert ok and snap["track_count"] == 100
    assert snap["playback"]["volume"] == 50 and snap["playback"]["shuffle"] is True
    assert "My List" in snap["playlists"]
    assert snap["playlists"]["My List"]["tracks"][0]["name"] == "T1"
    assert "Summer/Nested" in snap["playlists"]
    assert "Summer" in snap["folders"]


def test_library_snapshot_empty_playback(monkeypatch):
    """Playback query returns not-ok -> playback_state stays {}."""
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "5")),
                ("set ps to player state", (False, "pbfail")),
                ("repeat with p in user playlists", (True, "")),
            ]
        ),
    )
    ok, snap = asc.library_snapshot()
    assert ok and snap["playback"] == {} and snap["playlists"] == {}


def test_library_diff():
    before = {
        "track_count": 10,
        "playback": {"player_state": "stopped", "volume": 40},
        "playlists": {
            "Keep": {"tracks": [{"name": "T1", "artist": "A1"}]},
            "Gone": [{"name": "X", "artist": "Y"}],  # old list format
            "ListBoth": [{"name": "L1", "artist": "LA"}],  # list format, in both
            "Weird": None,  # neither list nor dict -> _get_tracks returns []
        },
        "folders": {"OldFolder": {}},
    }
    after = {
        "track_count": 12,
        "playback": {"player_state": "playing", "volume": 40},
        "playlists": {
            "Keep": {"tracks": [{"name": "T1", "artist": "A1"}, {"name": "T2", "artist": "A2"}]},
            "New": {"tracks": []},
            "ListBoth": [{"name": "L1", "artist": "LA"}, {"name": "L2", "artist": "LB"}],
            "Weird": None,
        },
        "folders": {"NewFolder": {}},
    }
    diff = asc.library_diff(before, after)
    assert diff["track_count_change"] == 2
    assert "player_state" in diff["playback_changes"]
    assert "New" in diff["playlists_added"] and "Gone" in diff["playlists_removed"]
    assert "Keep" in diff["playlists_changed"]
    assert "NewFolder" in diff["folders_added"] and "OldFolder" in diff["folders_removed"]
    assert diff["is_clean"] is False

    # clean case
    same = {"track_count": 1, "playback": {}, "playlists": {}, "folders": {}}
    assert asc.library_diff(same, same)["is_clean"] is True


# --- AppleScript string coercion ------------------------------------------------


def test_library_stats_script_coerces_every_value_to_text():
    """`&` in AppleScript only yields a STRING when the left operand is text.

    `integer & "|||"` builds a LIST, which osascript prints comma-separated —
    collapsing the ||| delimiting and parsing every field into garbage. The
    visible symptom was volume permanently reading 0. Verified live:
        set n to 1234
        return n & "|||" & 60        -> '1234, |||, 60'
        return (n as text) & "|||" & (60 as text) -> '1234|||60'
    """
    import inspect

    src = inspect.getsource(asc.get_library_stats)
    body = src[src.index("tell application") : src.index("end tell")]
    # Every numeric/boolean value must be coerced before it meets a delimiter.
    for var in ("trackCount", "playlistCount", "shuffleState", "vol"):
        assert f"({var} as text)" in body, f"{var} is concatenated without `as text`"
    assert "return trackCount &" not in body, "leading operand must be text"


def test_library_stats_parses_a_well_formed_line(monkeypatch):
    monkeypatch.setattr(
        asc, "run_applescript", lambda s: (True, "1234|||45|||playing|||false|||off|||60")
    )
    ok, stats = asc.get_library_stats()
    assert ok
    assert stats == {
        "track_count": 1234,
        "playlist_count": 45,
        "player_state": "playing",
        "shuffle": False,
        "repeat": "off",
        "volume": 60,
    }


def test_library_stats_reports_the_list_rendering_instead_of_zeroing(monkeypatch):
    """The exact broken output, which used to yield volume=0 and stray commas.

    It must now fail loudly: a wrong number reads as a real measurement, so
    silently defaulting to 0 is what kept this bug invisible.
    """
    broken = "1234, |||, 45, |||, playing, |||, false, |||, off, |||, 60"
    monkeypatch.setattr(asc, "run_applescript", lambda s: (True, broken))
    ok, err = asc.get_library_stats()
    assert not ok
    assert "Failed to parse" in err


def test_library_stats_rejects_a_short_line(monkeypatch):
    monkeypatch.setattr(asc, "run_applescript", lambda s: (True, "1|||2|||playing"))
    ok, err = asc.get_library_stats()
    assert not ok and "Failed to parse" in err


# --- catalog deep links --------------------------------------------------------
#
# `open location` is a URL sink: Music.app is unsandboxed and will fetch what we
# hand it. It is far narrower than upstream's `open` (which reached any scheme
# handler, including the browser), but it is still a boundary, so it is tested
# as one.


@pytest.mark.parametrize(
    "url",
    [
        "music://music.apple.com/us/song/1",  # the scheme upstream took blindly
        "https://music.apple.com.attacker.tld/x",  # suffix host
        "https://music.apple.com@attacker.tld/x",  # userinfo
        "http://music.apple.com/x",  # not https
        "file:///etc/passwd",
        "",
    ],
)
def test_open_catalog_location_refuses_non_apple_urls(url, monkeypatch):
    """Validated HERE, not just at the caller — a boundary that relies on every
    caller remembering to check is not a boundary."""
    called = []
    monkeypatch.setattr(asc, "run_applescript", lambda s: called.append(s) or (True, ""))
    ok, msg = asc.open_catalog_location(url)
    assert not ok
    assert "Refusing" in msg
    assert called == [], "must not reach AppleScript at all"


def test_open_catalog_location_accepts_a_real_apple_url(monkeypatch):
    seen = []
    monkeypatch.setattr(asc, "run_applescript", lambda s: seen.append(s) or (True, ""))
    ok, _ = asc.open_catalog_location("https://music.apple.com/us/album/x/1?i=2")
    assert ok and seen


def test_deep_link_forms_derives_the_native_scheme_and_strips_tracking():
    forms = asc._deep_link_forms(
        "https://music.apple.com/us/album/born-to-die/1440811595?i=1440812085&uo=4"
    )
    assert all("uo=" not in f for f in forms), "uo= is an affiliate param, not part of the link"
    assert any(f.startswith("music://") for f in forms), "native scheme must be tried"
    # The /song/<id> form addresses the TRACK; album?i= opens the album page.
    assert any("/song/1440812085" in f for f in forms)
    # Every derived form must still point at Apple.
    for form in forms:
        assert "music.apple.com" in form


def test_deep_link_forms_never_invents_a_host():
    """The music:// form is derived from a validated https URL, never taken as
    input — which is exactly what upstream got wrong."""
    forms = asc._deep_link_forms("https://beta.music.apple.com/us/album/x/1?i=2")
    assert all(".music.apple.com" in f or "//music.apple.com" in f for f in forms)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MORNING DEW (DONK) REMIX [feat. JAŸ-Z]", "morning dew donk remix feat jayz"),
        ("Beyoncé", "beyonce"),
        ("Summertime Sadness", "summertime sadness"),
    ],
)
def test_name_folding_handles_apples_diacritics(raw, expected):
    """Apple writes JAY-Z as JAŸ-Z (U+0178); a plain lowercase compare misses it."""
    assert asc._normalize_for_compare(raw) == expected


def test_deep_link_only_claims_success_when_the_RIGHT_track_plays(monkeypatch):
    """Music plays the collection from the START, not the ?i= selection.

    So "something is playing" is not evidence the request succeeded — claiming
    it would hijack the user's audio and then misreport it.
    """
    monkeypatch.setattr(asc, "run_applescript", lambda s: (True, ""))
    monkeypatch.setattr(asc, "play_selection", lambda: (False, "nothing selected"))
    monkeypatch.setattr(
        asc,
        "get_current_track",
        lambda: (
            True,
            {"name": "This Is What Makes Us Girls", "artist": "Lana Del Rey", "state": "playing"},
        ),
    )
    ok, msg = asc.open_catalog_and_play_selection(
        "https://music.apple.com/us/album/x/1?i=2", want_name="Summertime Sadness", per_form=0.1
    )
    assert not ok, "a different track playing is not success"
    assert "didn't start the requested track" in msg


def test_deep_link_succeeds_when_the_requested_track_plays(monkeypatch):
    monkeypatch.setattr(asc, "run_applescript", lambda s: (True, ""))
    monkeypatch.setattr(
        asc,
        "get_current_track",
        lambda: (
            True,
            {"name": "Repeat It", "artist": "Martin Garrix & Ed Sheeran", "state": "playing"},
        ),
    )
    ok, msg = asc.open_catalog_and_play_selection(
        "https://music.apple.com/us/album/x/1?i=2", want_name="Repeat It", per_form=0.1
    )
    assert ok and "Repeat It" in msg

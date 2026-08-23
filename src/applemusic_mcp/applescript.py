"""AppleScript integration for Music.app on macOS.

This module provides direct control of the Music app via AppleScript,
enabling capabilities not available through the REST API like playback
control, deleting tracks from playlists, and deleting playlists.

Only available on macOS with the Music app installed.

Security Notes:
    - All user input (track names, playlist names, etc.) is escaped via
      _escape_for_applescript() which escapes backslashes first, then quotes,
      before embedding in AppleScript strings. This prevents injection attacks.
    - Scripts are executed via subprocess.run() with capture_output=True
      and a 30-second timeout to prevent hangs.
    - The osascript binary location is verified via shutil.which() before use.
"""

import re
import subprocess
import sys
import shutil
import time
from typing import Optional


def is_available() -> bool:
    """Check if AppleScript is available (macOS with osascript)."""
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _escape_for_applescript(s: str) -> str:
    """Escape a string for safe use in AppleScript.

    Backslashes must be escaped first, then quotes, to prevent
    injection attacks and handle edge cases like 'Playlist\\Test'.

    Also strips control characters (newlines, tabs, carriage returns)
    which could break out of AppleScript string literals. osascript
    accepts literal newlines inside quoted strings, so an unescaped
    newline followed by '& do shell script "..."' is a real injection
    vector — the shell command executes even if the overall expression
    errors out.
    """
    # Strip control characters that could break out of string literals
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s.replace("\\", "\\\\").replace('"', '\\"')


# "Smart punctuation" the iTunes Store / typesetting interchanges with ASCII.
# Music.app stores many titles with typographic glyphs — the right single
# quote (U+2019, "That's a No No"), curly double quotes, or a one-character
# ellipsis (U+2026, "Wait…") — while users and the model type the ASCII forms
# ("That's", '"', "Wait..."). AppleScript's `contains` is glyph-exact for
# punctuation — there is no "ignoring punctuation" — so a literal
# `name contains "Wait..."` misses a title stored as "Wait…", and vice versa.
# We sidestep the character entirely by splitting on every ambiguous form.
#
# Deliberately EXCLUDED: hyphens / en- & em-dashes. Hyphens are far too common
# in ordinary titles ("Peek-A-Boo") — splitting on them would shatter the query
# into tiny fragments that false-match unrelated tracks. The apostrophe,
# double-quote, and ellipsis families above are the high-value, low-risk set.
# See issue #26.
_AMBIGUOUS_PUNCT = [
    "...",  # multi-char first so it's preferred over any single char below
    "'",
    "’",
    "‘",
    "ʼ",
    "‛",  # apostrophe family: U+0027 U+2019 U+2018 U+02BC U+201B
    '"',
    "“",
    "”",  # double-quote family: U+0022 U+201C U+201D
    "…",  # horizontal ellipsis U+2026 (ASCII equivalent is the "..." above)
]
_AMBIGUOUS_PUNCT_RE = re.compile("|".join(re.escape(p) for p in _AMBIGUOUS_PUNCT))


def _name_contains_clause(track_name: str) -> str:
    """Build a ``name contains …`` AppleScript clause robust to smart punctuation.

    For a title containing punctuation that the store may have typeset, we never
    match the punctuation glyph itself; instead we split on every ambiguous form
    and require the name to contain each non-empty surrounding fragment::

        "That's a No No" -> (name contains "That" and name contains "s a No No")
        "Wait..."        -> name contains "Wait"   # also matches stored "Wait…"

    That matches whether Music stored the ASCII form or the typographic one
    (curly apostrophe/quotes, one-char ellipsis), and is also immune to the
    glyph being normalized in transit between the client and this server.
    Titles with no ambiguous punctuation produce the simple single-``contains``
    clause unchanged.

    The fragments are ANDed together (and, at the call sites, further ANDed
    with an ``artist contains`` filter), so short fragments like ``"s"`` can't
    cause a false match on their own.
    """
    fragments = [f for f in _AMBIGUOUS_PUNCT_RE.split(track_name) if f.strip()]
    # No usable fragment (title was only punctuation/whitespace) — fall back to
    # the original so we still produce a valid, if glyph-exact, clause.
    if not fragments:
        return f'name contains "{_escape_for_applescript(track_name)}"'
    clauses = [f'name contains "{_escape_for_applescript(f)}"' for f in fragments]
    # One surviving fragment (no ambiguous punctuation, or it was only at the
    # edges) needs no parens; build from the FRAGMENT, not the original — the
    # original may still carry the stripped glyph (e.g. "Wait..." -> "Wait").
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " and ".join(clauses) + ")"


def _find_playlist_applescript(safe_name: str) -> str:
    """Generate AppleScript code to find a playlist by name.

    Tries user playlists first (exact, then partial match), then falls
    back to folder playlists (exact, then partial match).

    Args:
        safe_name: Already-escaped playlist name

    Returns:
        AppleScript code snippet that sets targetPlaylist variable
    """
    return f"""
        try
            -- Try exact match on user playlists
            set targetPlaylist to first user playlist whose name is "{safe_name}"
        on error
            try
                -- Partial match on user playlists
                set targetPlaylist to first user playlist whose name contains "{safe_name}"
            on error
                try
                    -- Exact match on folder playlists
                    set targetPlaylist to first folder playlist whose name is "{safe_name}"
                on error
                    try
                        -- Partial match on folder playlists
                        set targetPlaylist to first folder playlist whose name contains "{safe_name}"
                    on error
                        return "ERROR:Playlist not found"
                    end try
                end try
            end try
        end try"""


def _parse_explicit(raw: str) -> str:
    """Map Music.app's explicit property to Yes / No / Unknown.

    Music.app leaves `explicit` unset on many cloud tracks, and the scripts
    emit "unknown" when reading it raises. Collapsing that to "No" is the
    dangerous direction: it turns "I could not tell" into "this is clean",
    which is the one answer a caller filtering for clean content must never
    be given without it being true. Only a literal true/false is trusted.
    """
    v = (raw or "").strip().lower()
    if v == "true":
        return "Yes"
    if v == "false":
        return "No"
    return "Unknown"


def _parse_library_track_line(line: str) -> Optional[dict]:
    """Parse one ``|||``-delimited library track line into a dict.

    Format produced by the AppleScript blocks in get_library_songs /
    get_library_songs_page / search_library:
        name|||artist|||album|||duration_seconds|||genre|||year|||id|||explicit
    Returns None when the line is malformed (skipped by callers).
    The 8th `explicit` field is recent; older outputs may stop at 7 fields.
    """
    if "|||" not in line:
        return None
    parts = line.split("|||")
    if len(parts) < 7:
        return None
    try:
        dur_sec = float(parts[3])
        minutes = int(dur_sec) // 60
        seconds = int(dur_sec) % 60
        duration = f"{minutes}:{seconds:02d}"
    except (ValueError, TypeError):
        duration = ""
    explicit = "Unknown"
    if len(parts) >= 8:
        explicit = _parse_explicit(parts[7])
    return {
        "name": parts[0],
        "artist": parts[1],
        "album": parts[2],
        "duration": duration,
        "genre": parts[4],
        "year": parts[5],
        "id": parts[6],
        "explicit": explicit,
    }


def _track_filter_clause(
    track_name: str = "", artist: Optional[str] = None, track_id: Optional[str] = None
) -> Optional[str]:
    """Build the ``whose ...`` AppleScript clause that selects one track.

    Used by remove_track_from_playlist / remove_from_library — both want
    "match by persistent ID exactly, or fall back to name (+ optional
    artist) contains".

    Returns the clause (without leading/trailing space) or None when the
    caller didn't provide anything to match on.
    """
    if track_id:
        return f'whose persistent ID is "{_escape_for_applescript(track_id)}"'
    if track_name:
        name_clause = _name_contains_clause(track_name)
        if artist:
            safe_artist = _escape_for_applescript(artist)
            return f'whose {name_clause} and artist contains "{safe_artist}"'
        return f"whose {name_clause}"
    return None


def _library_track_query(track_name: str, artist: Optional[str] = None) -> str:
    """Build the AppleScript fragment that resolves a single library track.

    Used by play/love/dislike/rating/reveal — all of them want the same
    "first track in library playlist 1 matching name (and optionally artist)"
    selector. Centralizing avoids drift between callers.

    Both inputs are escaped here; callers pass raw user-supplied strings.
    """
    name_clause = _name_contains_clause(track_name)
    if artist:
        safe_artist = _escape_for_applescript(artist)
        return (
            f"first track of library playlist 1 whose {name_clause} "
            f'and artist contains "{safe_artist}"'
        )
    return f"first track of library playlist 1 whose {name_clause}"


def _resolve_library_track_applescript(track_name: str, artist: Optional[str] = None) -> str:
    """Emit AppleScript that resolves ``targetTrack`` from library playlist 1.

    Two-stage matcher (issue #26), embedded inside a
    ``tell application "Music" … end tell`` block:

    1. **Fast pass** — a glyph-exact ``whose`` filter (punctuation-robust via
       :func:`_name_contains_clause`). Music resolves this in-engine at C speed
       with no data transfer, so the common case never pays for stage 2.
    2. **Fold fallback** — only on a miss. Bulk-fetch every track name (and
       artist, when given) in a SINGLE Apple Event, then match in-memory under
       ``ignoring punctuation and diacriticals``. That uses Apple's own Unicode
       equivalence tables, so curly quotes, ellipses, and accents (é≈e) all
       match their ASCII forms — generic, no hand-maintained glyph list.

       The bulk-fetch is the crux: ``name of t`` *inside* a repeat is one Apple
       Event per track (~17s on a 12k library), while ``get name of every
       track`` is one event for the whole list (~0s). Music ignores AppleScript's
       ``ignoring`` context inside a ``whose`` filter, so the fold must happen in
       this in-memory loop, not in stage 1.

    Emits ``set targetTrack to …`` on success, or
    ``return "ERROR:Track not found: …"`` on a total miss. The returned snippet
    is indented to sit directly under the ``tell`` line.
    """
    safe_track = _escape_for_applescript(track_name)
    fast_query = _library_track_query(track_name, artist)
    if artist:
        safe_artist = _escape_for_applescript(artist)
        artist_fetch = "\n            set allArtists to (get artist of every track of lib)"
        match_cond = (
            f'(((item i of allNames) as text) contains "{safe_track}") '
            f'and (((item i of allArtists) as text) contains "{safe_artist}")'
        )
    else:
        artist_fetch = ""
        match_cond = f'((item i of allNames) as text) contains "{safe_track}"'
    return f"""        try
            set targetTrack to {fast_query}
        on error
            set lib to library playlist 1
            set allNames to (get name of every track of lib){artist_fetch}
            set idx to 0
            ignoring punctuation and diacriticals
                repeat with i from 1 to (count of allNames)
                    if {match_cond} then
                        set idx to i
                        exit repeat
                    end if
                end repeat
            end ignoring
            if idx is 0 then return "ERROR:Track not found: {safe_track}"
            set targetTrack to track idx of library playlist 1
        end try"""


def run_applescript(script: str) -> tuple[bool, str]:
    """Execute AppleScript and return (success, output/error).

    Args:
        script: AppleScript code to execute

    Returns:
        Tuple of (success: bool, output: str)
        On success, output is the script's return value.
        On failure, output is the error message.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out after 30 seconds"
    except Exception as e:
        return False, str(e)


# AppleScript error categories. Used to map osascript stderr text to a
# stable category so callers can produce actionable user-facing messages
# without each callsite re-matching the same regexes.
ERROR_MUSIC_NOT_RUNNING = "music_not_running"
ERROR_AUTOMATION_DENIED = "automation_denied"
ERROR_TIMEOUT = "timeout"
ERROR_SYNTAX = "syntax"
ERROR_UNKNOWN = "unknown"


def classify_error(text: str) -> str:
    """Categorize an AppleScript error string.

    The osascript stderr surface is messy: error wording shifts across macOS
    versions, but the numeric error codes (-609, -1728, -1743, etc.) and a
    handful of stable phrases ("Not authorized", "isn't running") are
    reliable. Match those first; fall through to ``unknown`` so callers can
    still surface the raw text.

    Categories:
      - music_not_running: Music.app isn't running or has crashed (-609,
        -10810, "isn't running", "Connection is invalid")
      - automation_denied: parent process lacks Automation permission for
        Music.app (-1743, "Not authorized", "not allowed assistive access")
      - timeout: our 30s subprocess timeout fired
      - syntax: AppleScript itself rejected the script (developer bug)
      - unknown: anything else — caller should surface raw error
    """
    if not text:
        return ERROR_UNKNOWN
    t = text.lower()

    # Timeout is our own message — match exactly.
    if "applescript timed out" in t:
        return ERROR_TIMEOUT

    # Automation permissions denied. -1743 is the canonical code; phrasings
    # vary across macOS versions but consistently mention authorization.
    # NOTE: bare "not allowed" is too broad — Music.app emits "operation
    # not allowed on smart playlists" and similar logic-level errors.
    # Match the full Automation-denial phrases instead.
    if (
        "-1743" in t
        or "not authorized" in t
        or "not allowed assistive" in t
        or "assistive access" in t
        or "not authorised" in t  # British English variant in some locales
    ):
        return ERROR_AUTOMATION_DENIED

    # Music.app not running / connection invalid. -609 is "Connection is
    # invalid"; -10810 is "Application isn't running"; phrasing variants
    # cover both startup-time and mid-session crashes.
    if (
        "-609" in t
        or "-10810" in t
        or "isn't running" in t
        or "is not running" in t
        or "connection is invalid" in t
        or "can't get application" in t
    ):
        return ERROR_MUSIC_NOT_RUNNING

    # AppleScript-level syntax errors (means we have a bug, not the user).
    # Parens explicit so future readers don't have to remember Python's
    # `and` > `or` precedence rule.
    if ("syntax error" in t) or ("expected" in t and "but found" in t):
        return ERROR_SYNTAX

    # Note: -1728 ("can't get") deliberately classifies as ERROR_UNKNOWN.
    # It's a logic-level error (track/playlist doesn't exist) rather than
    # an environmental one, so callers with legitimate API fallback paths
    # should still cascade. v0.9.3 handled the broken-track iteration
    # case at the AppleScript level, not via this classifier.
    return ERROR_UNKNOWN


# =============================================================================
# Playback Control
# =============================================================================


def play() -> tuple[bool, str]:
    """Start or resume playback."""
    return run_applescript('tell application "Music" to play')


def pause() -> tuple[bool, str]:
    """Pause playback."""
    return run_applescript('tell application "Music" to pause')


def playpause() -> tuple[bool, str]:
    """Toggle play/pause."""
    return run_applescript('tell application "Music" to playpause')


def stop() -> tuple[bool, str]:
    """Stop playback."""
    return run_applescript('tell application "Music" to stop')


def next_track() -> tuple[bool, str]:
    """Skip to next track."""
    return run_applescript('tell application "Music" to next track')


def previous_track() -> tuple[bool, str]:
    """Go to previous track."""
    return run_applescript('tell application "Music" to previous track')


def get_player_state() -> tuple[bool, str]:
    """Get current player state (playing, paused, stopped)."""
    return run_applescript('tell application "Music" to get player state as string')


def get_current_track() -> tuple[bool, dict]:
    """Get info about currently playing track.

    Returns:
        Tuple of (success, track_info_dict or error_string)
    """
    script = """
    tell application "Music"
        if player state is stopped then
            return "STOPPED"
        end if
        set t to current track
        set output to ""
        if player state is paused then
            set output to output & "state:paused" & "\\n"
        else if player state is playing then
            set output to output & "state:playing" & "\\n"
        else
            set output to output & "state:" & (player state as text) & "\\n"
        end if
        set output to output & "name:" & (name of t) & "\\n"
        set output to output & "artist:" & (artist of t) & "\\n"
        set output to output & "album:" & (album of t) & "\\n"
        set output to output & "duration:" & (duration of t) & "\\n"
        set output to output & "position:" & (player position) & "\\n"
        try
            set output to output & "genre:" & (genre of t) & "\\n"
        end try
        try
            set output to output & "year:" & (year of t) & "\\n"
        end try
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output
    if output == "STOPPED":
        return True, {"state": "stopped"}

    # Parse key:value pairs (state is included in the output now — paused tracks
    # were previously misreported as "playing" because it was hardcoded here).
    track_info: dict = {}
    for line in output.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            track_info[key.strip()] = value.strip()
    track_info.setdefault("state", "playing")
    return True, track_info




def get_volume() -> tuple[bool, int | str]:
    """Get current volume (0-100).

    Returns:
        Tuple of (success, volume 0-100 or error message string)
    """
    success, output = run_applescript('tell application "Music" to get sound volume')
    if success:
        try:
            return True, int(output)
        except ValueError:
            return False, f"Invalid volume value: {output}"
    return False, output


def set_volume(volume: int) -> tuple[bool, str]:
    """Set volume (0-100)."""
    volume = max(0, min(100, volume))
    return run_applescript(f'tell application "Music" to set sound volume to {volume}')






def get_shuffle() -> tuple[bool, bool | str]:
    """Get shuffle state.

    Returns:
        Tuple of (success, shuffle enabled bool or error message string)
    """
    success, output = run_applescript('tell application "Music" to get shuffle enabled')
    if success:
        return True, output.lower() == "true"
    return False, output


def set_shuffle(enabled: bool) -> tuple[bool, str]:
    """Set shuffle on/off."""
    value = "true" if enabled else "false"
    return run_applescript(f'tell application "Music" to set shuffle enabled to {value}')


def get_repeat() -> tuple[bool, str]:
    """Get repeat mode (off, one, all)."""
    return run_applescript('tell application "Music" to get song repeat as string')


def set_repeat(mode: str) -> tuple[bool, str]:
    """Set repeat mode (off, one, all). Accepts ``none`` as a synonym for ``off``
    so the cross-platform ``playback`` contract matches the browser engine, which
    uses ``none``/``one``/``all`` (MusicKit's vocabulary)."""
    if mode == "none":
        mode = "off"
    if mode not in ("off", "one", "all"):
        return False, f"Invalid repeat mode: {mode}. Use 'off'/'none', 'one', or 'all'"
    return run_applescript(f'tell application "Music" to set song repeat to {mode}')


def seek(position: float) -> tuple[bool, str]:
    """Seek to position in seconds."""
    return run_applescript(f'tell application "Music" to set player position to {position}')


# =============================================================================
# Playlist Operations
# =============================================================================


def get_playlists() -> tuple[bool, list[dict]]:
    """Get all user playlists with details.

    Returns:
        Tuple of (success, list of playlist dicts or error string)
    """
    # Each property is read defensively: a single playlist that can't return,
    # say, its persistent ID (cloud playlists mid-sync raise -1728) must not
    # abort the whole listing. We still emit the row by name so it stays
    # addressable, just with whatever fields it would give up.
    script = """
    tell application "Music"
        set output to ""
        repeat with p in user playlists
            try
                set pName to name of p
            on error
                set pName to ""
            end try
            try
                set pId to persistent ID of p
            on error
                set pId to ""
            end try
            try
                set pSmart to smart of p
            on error
                set pSmart to false
            end try
            try
                set pCount to count of tracks of p
            on error
                set pCount to 0
            end try
            try
                set pTime to time of p
            on error
                set pTime to "0:00"
            end try
            if pName is not "" then
                set output to output & pName & "|||" & pId & "|||" & pSmart & "|||" & pCount & "|||" & pTime & "\\n"
            end if
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    playlists = []
    for line in output.split("\n"):
        if "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 5:
                playlists.append(
                    {
                        "name": parts[0],
                        "id": parts[1],
                        "smart": parts[2].lower() == "true",
                        "track_count": int(parts[3]) if parts[3].isdigit() else 0,
                        "duration": parts[4],
                    }
                )
    return True, playlists


_BAD_LIMIT = "limit must be an integer"


def _clamp_track_limit(limit: int) -> Optional[int]:
    """Coerce `limit` to a non-negative int, or None if it isn't a number.

    Both track-fetch paths interpolate this straight into AppleScript, so it
    has to be a number before it gets there. Negatives must not survive
    either: an AppleScript range counts a negative bound back from the END
    (`tracks 1 thru -3` means "through the third-from-last"), so a negative
    `limit` would read nearly the whole playlist -- the opposite of a bound.
    """
    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return None


def _get_playlist_tracks_bulk(safe_name: str, limit: int) -> tuple[bool, str]:
    """Try bulk property fetch for playlist tracks (fast path).

    Returns (success, output) where output is raw AppleScript output or error.
    """
    safe_limit = _clamp_track_limit(limit)
    if safe_limit is None:
        return False, f"{_BAD_LIMIT}, got {limit!r}"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}

        set trackCount to count of tracks of targetPlaylist
        if trackCount is 0 then return ""

        -- Bound the range to `limit` BEFORE reading any properties, so cost
        -- is O(limit) instead of O(playlist size) -- large playlists must
        -- not pay for tracks beyond what was requested. Both clamps are
        -- load-bearing: `tracks 1 thru 0` and a range past the end are each
        -- an AppleScript error (-1728), not an empty result.
        set maxTracks to {safe_limit}
        if maxTracks > trackCount then set maxTracks to trackCount
        if maxTracks < 1 then return ""

        -- One Apple Event per property, addressed as a range of
        -- targetPlaylist. This must NOT go through an intermediate
        -- `set allTracks to tracks of targetPlaylist`: that materializes a
        -- plain AppleScript list, and `<property> of <plain list>` does not
        -- distribute -- Music raises -1728 ("Can't get name of {{...}}") and
        -- names every track in the message. `as list` because a 1-track
        -- range returns a bare value, so `item 1 of` would otherwise index
        -- into the string and yield its first character.
        set allNames to (get name of tracks 1 thru maxTracks of targetPlaylist) as list
        set allArtists to (get artist of tracks 1 thru maxTracks of targetPlaylist) as list
        set allAlbums to (get album of tracks 1 thru maxTracks of targetPlaylist) as list
        set allDurations to (get duration of tracks 1 thru maxTracks of targetPlaylist) as list
        set allGenres to (get genre of tracks 1 thru maxTracks of targetPlaylist) as list
        set allYears to (get year of tracks 1 thru maxTracks of targetPlaylist) as list
        set allIds to (get persistent ID of tracks 1 thru maxTracks of targetPlaylist) as list

        -- Combine into output
        set output to ""
        repeat with i from 1 to maxTracks
            set tName to item i of allNames
            set tArtist to item i of allArtists
            set tAlbum to item i of allAlbums
            set tDuration to item i of allDurations
            set tGenre to item i of allGenres
            set tYear to item i of allYears
            set tId to item i of allIds
            set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "\\n"
        end repeat
        return output
    end tell
    """
    return run_applescript(script)


def _get_playlist_tracks_slow(safe_name: str, limit: int) -> tuple[bool, str]:
    """Per-track iteration fallback for playlists with shared tracks (slow path).

    Optimized for shared tracks: skips genre/year (saves ~33% time).
    Returns (success, output) where output is raw AppleScript output or error.
    """
    safe_limit = _clamp_track_limit(limit)
    if safe_limit is None:
        return False, f"{_BAD_LIMIT}, got {limit!r}"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}

        set allTracks to tracks of targetPlaylist
        set trackCount to count of allTracks
        if trackCount is 0 then return ""

        -- Per-track iteration (slower but handles shared tracks)
        -- Optimized: skip genre/year to reduce try/catch overhead
        set output to ""
        set maxTracks to {safe_limit}
        if trackCount < maxTracks then set maxTracks to trackCount
        repeat with i from 1 to maxTracks
            set t to item i of allTracks
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                -- Skip genre/year for speed (shared tracks typically have them but try/catch is expensive)
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||||||||" & tId & "\\n"
            on error
                -- Skip tracks that can't be read (extremely rare)
            end try
        end repeat
        return output
    end tell
    """
    return run_applescript(script)


def get_playlist_tracks(playlist_name: str, limit: int = 500) -> tuple[bool, list[dict]]:
    """Get tracks in a playlist by name.

    Uses fast bulk fetch when possible, falls back to per-track iteration
    for tracks whose properties the bulk read cannot get.

    Args:
        playlist_name: Name of the playlist
        limit: Maximum number of tracks to return (default 500). A limit
            below 1 yields an empty list.

    Returns:
        Tuple of (success, list of track dicts or error string)
    """
    if _clamp_track_limit(limit) is None:
        return False, f"{_BAD_LIMIT}, got {limit!r}"

    safe_name = _escape_for_applescript(playlist_name)

    # Try bulk fetch first (~10x faster, and the only path that returns
    # genre/year -- the slow path deliberately skips them).
    success, output = _get_playlist_tracks_bulk(safe_name, limit)

    # Retry per-track only for a logic-level failure -- a track whose
    # properties the bulk read cannot get (-1728), which is the case the
    # per-track try/catch exists to survive. classify_error() files that
    # under `unknown` precisely so callers with a fallback cascade here.
    #
    # It replaces a `"Can" in output and "get" in output` test that was both
    # too narrow and too broad: English-only, and it matches "Cannot connect
    # to target" ("tar-get"). Retrying an environmental failure is worse than
    # not retrying -- the slow path sends ~7x more Apple Events, so a Music
    # that timed out or refused automation fails it again at double the wait.
    if not success and classify_error(output) == ERROR_UNKNOWN:
        success, output = _get_playlist_tracks_slow(safe_name, limit)

    if not success:
        return False, output
    if output.startswith("ERROR:"):
        return False, output[6:]

    tracks = []
    for line in output.split("\n"):
        if "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 7:
                # Format duration
                try:
                    dur_sec = float(parts[3])
                    minutes = int(dur_sec) // 60
                    seconds = int(dur_sec) % 60
                    duration = f"{minutes}:{seconds:02d}"
                except (ValueError, TypeError):
                    duration = ""

                # Music reports an unset year as 0, and the bulk read has no
                # per-track try/catch to turn that into "". Passing "0"
                # through would have a consumer report a release year of 0 --
                # report it as unknown, the same as the slow path does.
                year = "" if parts[5].strip() == "0" else parts[5]

                tracks.append(
                    {
                        "name": parts[0],
                        "artist": parts[1],
                        "album": parts[2],
                        "duration": duration,
                        "genre": parts[4],
                        "year": year,
                        "id": parts[6],
                    }
                )
    return True, tracks


def create_playlist(name: str, description: str = "") -> tuple[bool, str]:
    """Create a new playlist.

    Args:
        name: Playlist name
        description: Optional description

    Returns:
        Tuple of (success, playlist_id or error)
    """
    safe_name = _escape_for_applescript(name)
    safe_desc = _escape_for_applescript(description)

    if description:
        script = f"""
        tell application "Music"
            set newPlaylist to make new user playlist with properties {{name:"{safe_name}", description:"{safe_desc}"}}
            return persistent ID of newPlaylist
        end tell
        """
    else:
        script = f"""
        tell application "Music"
            set newPlaylist to make new user playlist with properties {{name:"{safe_name}"}}
            return persistent ID of newPlaylist
        end tell
        """
    return run_applescript(script)


def _resolve_folder_path_applescript(path: str) -> str:
    """Generate AppleScript to resolve a slash-separated folder path.

    e.g. "Summer/Chill/Deep" resolves to the "Deep" folder inside "Chill" inside "Summer".
    Sets `targetFolder` to the resolved folder playlist.

    Args:
        path: Slash-separated folder path

    Returns:
        AppleScript code block that sets targetFolder
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return '        return "ERROR:Empty folder path"'

    safe_parts = [_escape_for_applescript(p) for p in parts]

    if len(safe_parts) == 1:
        return f"""        try
            set targetFolder to first folder playlist whose name is "{safe_parts[0]}"
        on error
            return "ERROR:Folder not found: {safe_parts[0]}"
        end try"""

    # Multi-level: walk down the tree
    lines = []
    lines.append(
        f"""        try
            set targetFolder to first folder playlist whose name is "{safe_parts[0]}"
        on error
            return "ERROR:Folder not found: {safe_parts[0]}"
        end try"""
    )

    for part in safe_parts[1:]:
        lines.append(
            f"""        try
            set targetFolder to first folder playlist of targetFolder whose name is "{part}"
        on error
            return "ERROR:Subfolder not found: {part}"
        end try"""
        )

    return "\n".join(lines)


def get_folder_tree() -> tuple[bool, str]:
    """Get the folder hierarchy as indented text (up to 3 levels deep).

    Note: Currently hardcoded to display root folders, their immediate
    subfolders, and playlists within those subfolders. Deeper nesting
    is not shown in the tree output (though it can be created and
    navigated via slash paths).

    Returns:
        Tuple of (success, indented tree string)
    """
    script = """
    tell application "Music"
        set output to ""
        set allFolders to every folder playlist
        set allPlaylists to every user playlist

        -- Build tree: for each folder, list its children
        repeat with f in allFolders
            set fName to name of f
            -- Check if this folder has a parent (is nested)
            try
                set pName to name of parent of f
                -- Skip nested folders in top-level listing, they'll appear under parent
            on error
                -- Top-level folder
                set output to output & "[" & fName & "]" & linefeed
                -- List folder's direct children
                repeat with p in allPlaylists
                    try
                        if name of parent of p is fName then
                            set output to output & "  " & name of p & linefeed
                        end if
                    end try
                end repeat
                -- List nested folders
                repeat with f2 in allFolders
                    try
                        if name of parent of f2 is fName then
                            set output to output & "  [" & name of f2 & "]" & linefeed
                            -- One more level deep
                            repeat with p2 in allPlaylists
                                try
                                    if name of parent of p2 is name of f2 then
                                        set output to output & "    " & name of p2 & linefeed
                                    end if
                                end try
                            end repeat
                        end if
                    end try
                end repeat
            end try
        end repeat

        -- Top-level playlists (no parent folder)
        repeat with p in allPlaylists
            try
                set pParent to parent of p
                -- Has parent, skip (already listed under folder)
            on error
                -- Check it's not a folder itself
                set isFolder to false
                repeat with f in allFolders
                    if name of f is name of p then
                        set isFolder to true
                        exit repeat
                    end if
                end repeat
                if not isFolder then
                    set output to output & name of p & linefeed
                end if
            end try
        end repeat

        return output
    end tell
    """
    return run_applescript(script)


def create_folder_path(path: str) -> tuple[bool, str]:
    """Create a folder path, creating intermediate folders as needed.

    e.g. "Summer/Chill/Deep" creates Summer, then Chill inside it, then Deep inside that.

    Args:
        path: Slash-separated folder path

    Returns:
        Tuple of (success, leaf folder ID or error)
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return False, "Empty folder path"

    safe_parts = [_escape_for_applescript(p) for p in parts]

    # Build AppleScript that creates each level
    create_lines = []
    for i, part in enumerate(safe_parts):
        if i == 0:
            # Top-level: create if not exists
            create_lines.append(
                f"""
        try
            set folder{i} to first folder playlist whose name is "{part}"
        on error
            set folder{i} to make new folder playlist with properties {{name:"{part}"}}
        end try"""
            )
        else:
            # Nested: create inside parent if not exists
            create_lines.append(
                f"""
        try
            set folder{i} to first folder playlist of folder{i-1} whose name is "{part}"
        on error
            set folder{i} to make new folder playlist with properties {{name:"{part}"}}
            move folder{i} to folder{i-1}
        end try"""
            )

    last_idx = len(safe_parts) - 1
    script = f"""
    tell application "Music"
{"".join(create_lines)}
        return persistent ID of folder{last_idx}
    end tell
    """
    return run_applescript(script)


def create_folder(name: str) -> tuple[bool, str]:
    """Create a new folder playlist.

    Args:
        name: Folder name

    Returns:
        Tuple of (success, folder_id or error)
    """
    safe_name = _escape_for_applescript(name)
    script = f"""
    tell application "Music"
        set newFolder to make new folder playlist with properties {{name:"{safe_name}"}}
        return persistent ID of newFolder
    end tell
    """
    return run_applescript(script)


def move_to_folder(item_name: str, folder_path: str) -> tuple[bool, str]:
    """Move a playlist or folder into a folder.

    Args:
        item_name: Name of the playlist or folder to move
        folder_path: Target folder name or slash-separated path (e.g. "Summer/Chill")

    Returns:
        Tuple of (success, message or error)
    """
    safe_item = _escape_for_applescript(item_name)
    script = f"""
    tell application "Music"
{_resolve_folder_path_applescript(folder_path)}
{_find_playlist_applescript(safe_item)}
        move targetPlaylist to targetFolder
        return "Moved '" & name of targetPlaylist & "' to folder '" & name of targetFolder & "'"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def move_to_root(item_name: str) -> tuple[bool, str]:
    """Move a playlist out of its parent folder to the top level.

    Note: Music.app's AppleScript interface does not support moving playlists
    out of folders. This recreates the playlist at root with the same tracks.
    The playlist's persistent ID will change.

    Args:
        item_name: Name of the playlist to move to root

    Returns:
        Tuple of (success, message or error)
    """
    safe_item = _escape_for_applescript(item_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_item)}
        try
            set pParent to parent of targetPlaylist
        on error
            return "ERROR:Playlist is already at top level"
        end try
        set origName to name of targetPlaylist
        set tempName to origName & " _MOVING_"
        set newPlaylist to make new user playlist with properties {{name:tempName}}
        try
            repeat with t in tracks of targetPlaylist
                duplicate t to newPlaylist
            end repeat
        end try
        delete targetPlaylist
        set name of newPlaylist to origName
        return "Moved '" & origName & "' to top level (playlist recreated)"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_playlist_path(playlist_name: str) -> tuple[bool, str]:
    """Get the full folder path of a playlist or folder.

    Args:
        playlist_name: Name of the playlist or folder

    Returns:
        Tuple of (success, slash-separated path or error)
        e.g. "Summer/Chill/Road Trip" or just "Road Trip" if at root
    """
    safe_name = _escape_for_applescript(playlist_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set pathParts to {{name of targetPlaylist}}
        set current to targetPlaylist
        repeat
            try
                set current to parent of current
                set beginning of pathParts to name of current
            on error
                exit repeat
            end try
        end repeat
        set AppleScript's text item delimiters to "/"
        return pathParts as text
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def delete_folder(folder_path: str) -> tuple[bool, str]:
    """Delete a folder by name or slash-separated path.

    Args:
        folder_path: Folder name or path (e.g. "Summer" or "Summer/Chill")

    Returns:
        Tuple of (success, message or error)
    """
    script = f"""
    tell application "Music"
{_resolve_folder_path_applescript(folder_path)}
        set folderName to name of targetFolder
        delete targetFolder
        return "Deleted folder: " & folderName
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def delete_playlist(playlist_name: str) -> tuple[bool, str]:
    """Delete a playlist by name.

    Args:
        playlist_name: Name of the playlist to delete

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(playlist_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set playlistName to name of targetPlaylist
        delete targetPlaylist
        return "Deleted playlist: " & playlistName
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def rename_playlist(playlist_name: str, new_name: str) -> tuple[bool, str]:
    """Rename a playlist.

    Args:
        playlist_name: Current name of the playlist
        new_name: New name for the playlist

    Returns:
        Tuple of (success, message or error)
    """
    safe_old = _escape_for_applescript(playlist_name)
    safe_new = _escape_for_applescript(new_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_old)}
        set oldName to name of targetPlaylist
        set name of targetPlaylist to "{safe_new}"
        return "Renamed: " & oldName & " → {safe_new}"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def track_exists_in_playlist(
    playlist_name: str, track_name: str, artist: Optional[str] = None
) -> tuple[bool, bool | str]:
    """Quick check if a track exists in a playlist.

    Args:
        playlist_name: Playlist to check
        track_name: Track name to look for
        artist: Optional artist to match

    Returns:
        Tuple of (success, exists: bool | error: str)
        On success, second element is True/False for exists.
        On failure, second element is error message.
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    name_clause = _name_contains_clause(track_name)

    if artist:
        safe_artist = _escape_for_applescript(artist)
        track_filter = f'whose {name_clause} and artist contains "{safe_artist}"'
    else:
        track_filter = f"whose {name_clause}"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        set matchingTracks to (every track of targetPlaylist {track_filter})
        if (count of matchingTracks) > 0 then
            return "FOUND:" & name of (item 1 of matchingTracks) & " - " & artist of (item 1 of matchingTracks)
        else
            return "NOT_FOUND"
        end if
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output
    if output.startswith("ERROR:"):
        return False, output[6:]
    if output.startswith("FOUND:"):
        return True, output[6:]  # Return the matched track info
    return True, False  # NOT_FOUND


def add_track_to_playlist(
    playlist_name: str, track_name: str, artist: Optional[str] = None, album: Optional[str] = None
) -> tuple[bool, str]:
    """Add a track from library to a playlist.

    Args:
        playlist_name: Target playlist name
        track_name: Name of the track to add (partial match supported)
        artist: Optional artist name to disambiguate (prefers exact match, falls back to contains)
        album: Optional album name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    safe_track = _escape_for_applescript(track_name)

    # Primary query uses `artist is` (exact); fallback (when artist is given)
    # relaxes to `artist contains` so partial matches still resolve.
    name_clause = _name_contains_clause(track_name)
    conditions = [name_clause]
    if artist:
        safe_artist = _escape_for_applescript(artist)
        conditions.append(f'artist is "{safe_artist}"')
    if album:
        safe_album = _escape_for_applescript(album)
        conditions.append(f'album contains "{safe_album}"')

    track_query = f'first track of library playlist 1 whose {" and ".join(conditions)}'

    fallback_query = None
    if artist:
        fallback_conditions = [name_clause, f'artist contains "{safe_artist}"']
        if album:
            fallback_conditions.append(f'album contains "{safe_album}"')
        fallback_query = (
            f'first track of library playlist 1 whose {" and ".join(fallback_conditions)}'
        )

    if fallback_query:
        script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to {track_query}
        on error
            try
                set targetTrack to {fallback_query}
            on error
                return "ERROR:Track not found: {safe_track}"
            end try
        end try
        duplicate targetTrack to targetPlaylist
        return "Added " & name of targetTrack & " (" & album of targetTrack & ") by " & artist of targetTrack & " to " & name of targetPlaylist
    end tell
    """
    else:
        script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to {track_query}
        on error
            return "ERROR:Track not found: {safe_track}"
        end try
        duplicate targetTrack to targetPlaylist
        return "Added " & name of targetTrack & " (" & album of targetTrack & ") by " & artist of targetTrack & " to " & name of targetPlaylist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def remove_track_from_playlist(
    playlist_name: str,
    track_name: str = "",
    artist: Optional[str] = None,
    track_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Remove a track from a playlist (not from library).

    Args:
        playlist_name: Playlist to remove from
        track_name: Name of the track to remove (partial match supported)
        artist: Optional artist name to disambiguate (partial match)
        track_id: Optional persistent ID (exact match, overrides name/artist)

    Returns:
        Tuple of (success, message or error)
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    track_filter = _track_filter_clause(track_name, artist, track_id)
    if track_filter is None:
        return False, "Must provide track_name or track_id"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to (first track of targetPlaylist {track_filter})
        on error
            return "ERROR:Track not found in playlist"
        end try
        set trackName to name of targetTrack
        set trackArtist to artist of targetTrack
        delete targetTrack
        return "Removed " & trackName & " by " & trackArtist & " from {safe_playlist}"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def remove_from_library(
    track_name: str = "", artist: Optional[str] = None, track_id: Optional[str] = None
) -> tuple[bool, str]:
    """Remove a track from the library entirely.

    Args:
        track_name: Name of the track to remove (partial match)
        artist: Optional artist name to disambiguate (partial match)
        track_id: Optional persistent ID (exact match, overrides name/artist)

    Returns:
        Tuple of (success, message or error)
    """
    track_filter = _track_filter_clause(track_name, artist, track_id)
    if track_filter is None:
        return False, "Must provide track_name or track_id"

    script = f"""
    tell application "Music"
        try
            set targetTrack to (first track of library playlist 1 {track_filter})
        on error
            return "ERROR:Track not found in library"
        end try
        set trackName to name of targetTrack
        set trackArtist to artist of targetTrack
        delete targetTrack
        return "Removed from library: " & trackName & " by " & trackArtist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def search_playlist(playlist_name: str, query: str) -> tuple[bool, list[dict]]:
    """Search for tracks in a playlist using native AppleScript search.

    Uses Music app's native search (same as typing in search field).
    Much faster than manually iterating through all tracks.

    Args:
        playlist_name: Name of the playlist to search
        query: Search term (matches name, artist, album, etc.)

    Returns:
        Tuple of (success, list of matching tracks or error message)
    """
    safe_name = _escape_for_applescript(playlist_name)
    safe_query = _escape_for_applescript(query)

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set foundTracks to search targetPlaylist for "{safe_query}"
        set output to ""
        repeat with t in foundTracks
            try
                set trackName to name of t
                set trackArtist to artist of t
                set trackAlbum to album of t
                set trackId to persistent ID of t
                try
                    set trackExplicit to explicit of t
                on error
                    set trackExplicit to "unknown"
                end try
                set output to output & trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & trackId & "|||" & trackExplicit & "\\n"
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)

    if not success:
        return False, output

    if output.startswith("ERROR:"):
        return False, output[6:]

    # Parse results
    tracks = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) >= 4:
            # Parse explicit field (added in 5th position)
            explicit = "Unknown"
            if len(parts) >= 5:
                explicit = _parse_explicit(parts[4])

            tracks.append(
                {
                    "name": parts[0],
                    "artist": parts[1],
                    "album": parts[2],
                    "id": parts[3],
                    "explicit": explicit,
                }
            )

    return True, tracks


def download_tracks(track_ids: str = "", playlist_name: str = "") -> tuple[bool, str]:
    """Download cloud tracks or playlist for offline playback.

    Args:
        track_ids: Comma-separated persistent IDs to download
        playlist_name: Name of playlist to download all tracks from

    Returns:
        Tuple of (success, message or error)
    """
    if track_ids and playlist_name:
        return False, "Error: Provide either track_ids or playlist_name, not both"
    if not track_ids and not playlist_name:
        return False, "Error: Provide track_ids or playlist_name"

    if playlist_name:
        # Download entire playlist
        safe_name = _escape_for_applescript(playlist_name)
        script = f"""
        tell application "Music"
{_find_playlist_applescript(safe_name)}
            download targetPlaylist
            return "Downloading playlist: " & name of targetPlaylist
        end tell
        """
    else:
        # Download individual tracks by ID
        ids = [tid.strip() for tid in track_ids.split(",") if tid.strip()]
        if not ids:
            return False, "Error: No valid track IDs provided"

        # Build AppleScript to download each track
        download_cmds = []
        for track_id in ids:
            safe_id = _escape_for_applescript(track_id)
            download_cmds.append(
                f'download (first track of library playlist 1 whose persistent ID is "{safe_id}")'
            )

        script = f"""
        tell application "Music"
            {chr(10).join(f"            {cmd}" for cmd in download_cmds)}
            return "Downloading {len(ids)} track(s)"
        end tell
        """

    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def play_playlist(playlist_name: str, shuffle: bool = False) -> tuple[bool, str]:
    """Start playing a playlist.

    Args:
        playlist_name: Name of the playlist to play
        shuffle: Whether to shuffle the playlist

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(playlist_name)
    shuffle_cmd = "set shuffle enabled to true" if shuffle else "set shuffle enabled to false"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        {shuffle_cmd}
        play targetPlaylist
        return "Now playing: " & name of targetPlaylist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def find_library_track(name: str, artist: str = "") -> tuple[bool, str]:
    """Direct lookup of a library track by name (+ optional artist), via the
    Music object model.

    Unlike :func:`search_library` (which runs Music's native ``search`` command
    and depends on the search index), this is a ``whose name contains`` object
    query, so it finds a track the instant it lands in the library — without
    waiting for the index to update after a UI add. Used to verify add-to-library
    succeeded. Apostrophe/quote-robust via :func:`_name_contains_clause`.

    Returns (True, "name|||artist") on a match, else (False, "").
    """
    if not name.strip():
        return False, ""
    cond = _name_contains_clause(name)
    if artist:
        cond = f'{cond} and artist contains "{_escape_for_applescript(artist)}"'
    ok, out = run_applescript(
        f"""
    tell application "Music"
        try
            set t to (first track of library playlist 1 whose {cond})
            return (name of t) & "|||" & (artist of t)
        on error
            return "NOT_FOUND"
        end try
    end tell"""
    )
    if ok and "|||" in out.strip():
        return True, out.strip()
    return False, ""


def play_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Play a specific track from library.

    Args:
        track_name: Name of the track to play
        artist: Optional artist name to disambiguate

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        play targetTrack
        return "Now playing: " & name of targetTrack & " by " & artist of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output










def _check_playing() -> bool:
    """Check if Music is currently playing."""
    ok, state = run_applescript('tell application "Music" to get player state')
    return ok and state.strip() == "playing"


























# =============================================================================
# Library Search
# =============================================================================


def get_library_songs(limit: int = 100) -> tuple[bool, list[dict]]:
    """Get songs from the library (no search query required).

    Args:
        limit: Maximum number of songs to return (default 100, 0 for all)

    Returns:
        Tuple of (success, list of track dicts or error)

    Note: Large libraries (10,000+ tracks) with limit=0 may timeout (30s).
    """
    if limit < 0:
        return False, "limit must be >= 0 (use 0 for all songs)"
    limit_clause = f"if resultCount >= {limit} then exit repeat" if limit > 0 else ""

    script = f"""
    tell application "Music"
        set output to ""
        set resultCount to 0
        repeat with t in tracks of library playlist 1
            {limit_clause}
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to "unknown"
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
                set resultCount to resultCount + 1
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    tracks = [t for t in (_parse_library_track_line(line) for line in output.split("\n")) if t]
    return True, tracks


def get_loved_songs(limit: int = 0) -> tuple[bool, list[dict]]:
    """Get loved / Favorite songs from the library.

    Music.app renamed "Loved" to "Favorite" and the corresponding track
    property changed accordingly: newer versions expose ``favorited`` while
    older ones use ``loved``. Worse, even where ``loved`` works for direct
    access (``set loved of t to true``), using it inside a ``whose`` filter
    can fail at runtime with "The variable loved is not defined" depending on
    the Music.app version.

    To stay robust across versions we try, in order:
      1. ``whose favorited is true`` (fast, native; modern property)
      2. ``whose loved is true`` (fast, native; legacy property)
      3. a manual scan reading ``favorited``/``loved`` per track (slow but
         works when neither property is filterable)
    Each step is wrapped in ``try`` so an unsupported property falls through
    instead of aborting the whole script.

    Args:
        limit: Maximum number of songs to return (default 0 for all)

    Returns:
        Tuple of (success, list of track dicts or error)
    """
    if limit < 0:
        return False, "limit must be >= 0 (use 0 for all songs)"
    limit_clause = f"if resultCount >= {limit} then exit repeat" if limit > 0 else ""

    script = f"""
    tell application "Music"
        set output to ""
        set resultCount to 0
        set lovedTracks to {{}}
        try
            set lovedTracks to (every track of library playlist 1 whose favorited is true)
        on error
            try
                set lovedTracks to (every track of library playlist 1 whose loved is true)
            on error
                -- whose-filter unsupported for both property names; scan manually
                set lovedTracks to {{}}
                repeat with t in (every track of library playlist 1)
                    set isFav to false
                    try
                        set isFav to favorited of t
                    on error
                        try
                            set isFav to loved of t
                        end try
                    end try
                    if isFav then set end of lovedTracks to t
                end repeat
            end try
        end try
        repeat with t in lovedTracks
            {limit_clause}
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to "unknown"
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
                set resultCount to resultCount + 1
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    tracks = [t for t in (_parse_library_track_line(line) for line in output.split("\n")) if t]
    return True, tracks


def get_library_songs_page(offset: int, limit: int) -> tuple[bool, list[dict], int, str]:
    """Get a single page of songs from the library using O(limit) range access.

    Args:
        offset: Zero-based starting position
        limit: Number of songs to return (must be > 0)

    Returns:
        Tuple of (success, tracks, total_count, error)
    """
    if limit <= 0:
        return False, [], 0, "limit must be > 0"
    start_pos = offset + 1
    end_pos = offset + limit

    script = f"""
    tell application "Music"
        set total to count of tracks of library playlist 1
        set output to "total:" & total & "\\n"
        if total is 0 or {offset} >= total then
            return output
        end if
        set endPos to {end_pos}
        if endPos > total then set endPos to total
        set trackList to tracks of library playlist 1
        repeat with t in items {start_pos} through endPos of trackList
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to "unknown"
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, [], 0, output

    total = 0
    tracks = []
    for line in output.split("\n"):
        if line.startswith("total:"):
            try:
                total = int(line[6:].strip())
            except ValueError:
                pass
            continue
        parsed = _parse_library_track_line(line)
        if parsed:
            tracks.append(parsed)
    return True, tracks, total, ""


def _library_search_source(query: str, types: str) -> str:
    """Build the AppleScript expression that yields the ``searchResults`` list.

    For ``types="genre"`` we filter on the track's genre field directly. Music.app's
    ``search … for`` full-text command never looks at the genre field, so routing a
    genre name through it would false-match tracks merely *named* after the genre
    (e.g. searching "Rock" returning a pop song titled "Rock Your Body"). The other
    types keep the existing full-text search with its kind modifier.

    Args:
        query: Search query (genre name for ``types="genre"``)
        types: "all", "artists", "albums", "songs", or "genre"

    Returns:
        An AppleScript expression assignable to ``searchResults``.
    """
    safe = _escape_for_applescript(query)
    if types == "genre":
        return f'(every track of library playlist 1 whose genre contains "{safe}")'

    # Map search types to AppleScript full-text search kinds
    search_map = {
        "all": "",
        "artists": "only artists",
        "albums": "only albums",
        "songs": "only songs",
    }
    modifier = search_map.get(types, "")
    return f'search library playlist 1 for "{safe}" {modifier}'


def search_library_page(
    query: str, types: str = "all", offset: int = 0, limit: int = 100
) -> tuple[bool, list[dict], int, str]:
    """Search the local library, returning one offset/limit page plus the total hit count.

    Mirrors :func:`get_library_songs_page`: Music runs its native search (or the
    genre filter), then we slice the result list with O(limit) range access
    (``items start through end``) so paging past the first screenful of hits is
    cheap. The legacy path capped output at the first 100 hits with no way to
    reach the rest.

    Args:
        query: Search query (genre name for ``types="genre"``)
        types: "all", "artists", "albums", "songs", or "genre"
        offset: Zero-based starting position
        limit: Number of hits to return (must be > 0)

    Returns:
        Tuple of (success, tracks, total_count, error)
    """
    if limit <= 0:
        return False, [], 0, "limit must be > 0"

    start_pos = offset + 1
    end_pos = offset + limit
    script = f"""
    tell application "Music"
        set searchResults to {_library_search_source(query, types)}
        set total to count of searchResults
        set output to "total:" & total & "\\n"
        if total is 0 or {offset} >= total then
            return output
        end if
        set endPos to {end_pos}
        if endPos > total then set endPos to total
        repeat with t in items {start_pos} through endPos of searchResults
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to "unknown"
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, [], 0, output

    total = 0
    tracks = []
    for line in output.split("\n"):
        if line.startswith("total:"):
            try:
                total = int(line[6:].strip())
            except ValueError:
                pass
            continue
        parsed = _parse_library_track_line(line)
        if parsed:
            tracks.append(parsed)
    return True, tracks, total, ""


def search_library(query: str, types: str = "all") -> tuple[bool, list[dict]]:
    """Search the local library (first 100 hits).

    Thin wrapper over :func:`search_library_page` for callers that only need
    matches to resolve or verify a track and never page. Preserves the
    ``(success, tracks)`` shape that existing callers unpack.

    Args:
        query: Search query
        types: Type of search - "all", "artists", "albums", "songs", "genre"

    Returns:
        Tuple of (success, list of track dicts or error message string)
    """
    success, tracks, _total, error = search_library_page(query, types, offset=0, limit=100)
    if not success:
        return False, error
    return True, tracks


# =============================================================================
# Track Metadata
# =============================================================================


def love_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Mark a track as loved.

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set loved of targetTrack to true
        set disliked of targetTrack to false
        return "Loved: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def dislike_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Mark a track as disliked.

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set disliked of targetTrack to true
        set loved of targetTrack to false
        return "Disliked: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_rating(track_name: str, artist: Optional[str] = None) -> tuple[bool, int | str]:
    """Get track rating (0-100, where 20=1 star, 40=2 stars, etc).

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, rating 0-100 or error message string)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        return rating of targetTrack as integer
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    try:
        return True, int(output)
    except (ValueError, TypeError):
        return False, f"Invalid rating value: {output}"


def set_rating(track_name: str, rating: int, artist: Optional[str] = None) -> tuple[bool, str]:
    """Set track rating (0-100, where 20=1 star, 40=2 stars, etc).

    Args:
        track_name: Name of the track (partial match supported)
        rating: Rating value 0-100
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    rating = max(0, min(100, rating))
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set rating of targetTrack to {rating}
        return "Set rating to {rating} for: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


# =============================================================================
# AirPlay
# =============================================================================


def get_airplay_devices() -> tuple[bool, list[str]]:
    """Get list of available AirPlay devices."""
    script = """
    tell application "Music"
        set deviceNames to name of every AirPlay device
        set output to ""
        repeat with d in deviceNames
            set output to output & d & "\\n"
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    devices = [d.strip() for d in output.split("\n") if d.strip()]
    return True, devices


def set_airplay_device(device_name: str) -> tuple[bool, str]:
    """Switch audio output to a specific AirPlay device.

    Args:
        device_name: Name of the AirPlay device (or partial match)

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(device_name)

    script = f"""
    tell application "Music"
        try
            set targetDevice to first AirPlay device whose name contains "{safe_name}"
        on error
            return "ERROR:Device not found: {safe_name}"
        end try
        set current AirPlay devices to {{targetDevice}}
        return "Switched to: " & name of targetDevice
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


# =============================================================================
# Utilities
# =============================================================================


def reveal_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Reveal a track in the Music app window.

    Args:
        track_name: Name of the track
        artist: Optional artist name to disambiguate

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        reveal targetTrack
        activate
        return "Revealed: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_library_stats() -> tuple[bool, dict]:
    """Get library statistics."""
    script = """
    tell application "Music"
        set trackCount to count of tracks of library playlist 1
        set playlistCount to count of user playlists
        set playerState to player state as string
        set shuffleState to shuffle enabled
        set repeatState to song repeat as string
        set vol to sound volume

        return trackCount & "|||" & playlistCount & "|||" & playerState & "|||" & shuffleState & "|||" & repeatState & "|||" & vol
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    parts = output.split("|||")
    if len(parts) >= 6:
        return True, {
            "track_count": int(parts[0]) if parts[0].isdigit() else 0,
            "playlist_count": int(parts[1]) if parts[1].isdigit() else 0,
            "player_state": parts[2],
            "shuffle": parts[3].lower() == "true",
            "repeat": parts[4],
            "volume": int(parts[5]) if parts[5].isdigit() else 0,
        }
    return False, "Failed to parse library stats"


# =============================================================================
# Library snapshot / diff
# =============================================================================

def library_snapshot() -> tuple[bool, dict]:
    """Capture a full snapshot of the Music library for integrity checking.

    Returns a dict with:
        - track_count: total library tracks
        - playback: dict with player state, volume, shuffle, repeat, current track
        - playlists: dict mapping playlist name -> list of {name, artist, album}

    This is intentionally thorough (captures full track lists) so diffs can
    detect any accidental additions, removals, or reorders.
    """
    # Get total track count
    ok, count_str = run_applescript(
        'tell application "Music" to return (count of tracks of library playlist 1) as text'
    )
    if not ok:
        return False, {"error": f"Failed to count tracks: {count_str}"}
    try:
        track_count = int(count_str.strip())
    except ValueError:
        return False, {"error": f"Invalid track count: {count_str}"}

    # Get playback state
    ok, pb_str = run_applescript(
        """
tell application "Music"
    set ps to player state as text
    set v to (sound volume) as text
    set sh to (shuffle enabled) as text
    set rp to song repeat as text
    set ct to ""
    set ca to ""
    set calb to ""
    try
        set ct to name of current track
        set ca to artist of current track
        set calb to album of current track
    end try
    return ps & return & v & return & sh & return & rp & return & ct & return & ca & return & calb
end tell"""
    )
    playback_state = {}
    if ok and pb_str:
        lines = pb_str.strip().split("\n")
        playback_state = {
            "player_state": lines[0] if len(lines) > 0 else "unknown",
            "volume": int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 0,
            "shuffle": lines[2].strip() == "true" if len(lines) > 2 else False,
            "repeat": lines[3].strip() if len(lines) > 3 else "unknown",
            "current_track": lines[4].strip() if len(lines) > 4 and lines[4].strip() else None,
            "current_artist": lines[5].strip() if len(lines) > 5 and lines[5].strip() else None,
            "current_album": lines[6].strip() if len(lines) > 6 and lines[6].strip() else None,
        }

    # Get all user playlists and their contents, with folder paths
    ok, playlist_data = run_applescript(
        """
tell application "Music"
    set r to ""
    repeat with p in user playlists
        set pName to name of p
        set pKind to smart of p
        if pKind is false and pName is not "Music" and pName is not "Music Videos" then
            -- Build folder path
            set folderPath to ""
            try
                set current to p
                set pathParts to {}
                repeat
                    try
                        set current to parent of current
                        set beginning of pathParts to name of current
                    on error
                        exit repeat
                    end try
                end repeat
                if (count of pathParts) > 0 then
                    set AppleScript's text item delimiters to "/"
                    set folderPath to pathParts as text
                    set AppleScript's text item delimiters to ""
                end if
            end try
            set r to r & "PLAYLIST:" & pName & "|||FOLDER:" & folderPath & return
            repeat with t in tracks of p
                try
                    set r to r & name of t & "|||" & artist of t & "|||" & album of t & return
                on error
                    -- skip inaccessible tracks (broken file references, error -1728)
                end try
            end repeat
        end if
    end repeat
    -- Also list folder playlists
    repeat with f in folder playlists
        set fPath to ""
        try
            set current to f
            set pathParts to {}
            repeat
                try
                    set current to parent of current
                    set beginning of pathParts to name of current
                on error
                    exit repeat
                end try
            end repeat
            if (count of pathParts) > 0 then
                set AppleScript's text item delimiters to "/"
                set fPath to pathParts as text
                set AppleScript's text item delimiters to ""
            end if
        end try
        set r to r & "FOLDER:" & name of f & "|||PATH:" & fPath & return
    end repeat
    return r
end tell"""
    )
    if not ok:
        return False, {"error": f"Failed to get playlists: {playlist_data}"}

    playlists = {}
    folders = {}
    current_playlist = None
    for line in playlist_data.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("PLAYLIST:"):
            # Format: PLAYLIST:name|||FOLDER:path
            parts = line.split("|||FOLDER:")
            pl_name = parts[0][9:]
            folder_path = parts[1] if len(parts) > 1 else ""
            full_path = f"{folder_path}/{pl_name}" if folder_path else pl_name
            current_playlist = full_path
            playlists[full_path] = {"folder": folder_path, "tracks": []}
        elif line.startswith("FOLDER:"):
            # Format: FOLDER:name|||PATH:parent_path
            current_playlist = None  # Folder lines end any playlist track context
            parts = line.split("|||PATH:")
            f_name = parts[0][7:]
            parent_path = parts[1] if len(parts) > 1 else ""
            full_path = f"{parent_path}/{f_name}" if parent_path else f_name
            folders[full_path] = {"name": f_name, "parent": parent_path}
        elif current_playlist is not None and "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 3:
                playlists[current_playlist]["tracks"].append(
                    {
                        "name": parts[0],
                        "artist": parts[1],
                        "album": parts[2],
                    }
                )

    return True, {
        "track_count": track_count,
        "playback": playback_state,
        "playlists": playlists,
        "folders": folders,
    }


def library_diff(before: dict, after: dict) -> dict:
    """Compare two library snapshots and return differences.

    Args:
        before: snapshot dict from library_snapshot()
        after: snapshot dict from library_snapshot()

    Returns:
        Dict with:
            - track_count_change: int (positive = added, negative = removed)
            - playback_changes: dict of changed playback settings
            - playlists_added: list of playlist names
            - playlists_removed: list of playlist names
            - playlists_changed: dict of {name: {added: [...], removed: [...]}}
            - is_clean: True if no library changes detected (playback state changes are tracked separately)
    """
    # Compare playback state
    before_pb = before.get("playback", {})
    after_pb = after.get("playback", {})
    playback_changes = {}
    for key in ["player_state", "volume", "shuffle", "repeat", "current_track", "current_artist"]:
        if before_pb.get(key) != after_pb.get(key):
            playback_changes[key] = {"before": before_pb.get(key), "after": after_pb.get(key)}

    result = {
        "track_count_change": after.get("track_count", 0) - before.get("track_count", 0),
        "playback_changes": playback_changes,
        "playlists_added": [],
        "playlists_removed": [],
        "playlists_changed": {},
        "is_clean": True,
    }

    before_pl = before.get("playlists", {})
    after_pl = after.get("playlists", {})

    # Find added/removed playlists
    for name in after_pl:
        if name not in before_pl:
            result["playlists_added"].append(name)
    for name in before_pl:
        if name not in after_pl:
            result["playlists_removed"].append(name)

    # Compare track lists for playlists that exist in both
    # Handle both old format (list) and new format (dict with "tracks" key)
    def _get_tracks(pl_entry):
        if isinstance(pl_entry, list):
            return pl_entry
        if isinstance(pl_entry, dict):
            return pl_entry.get("tracks", [])
        return []

    for name in before_pl:
        if name in after_pl:
            before_tracks = {f"{t['name']}|{t['artist']}" for t in _get_tracks(before_pl[name])}
            after_tracks = {f"{t['name']}|{t['artist']}" for t in _get_tracks(after_pl[name])}
            added = after_tracks - before_tracks
            removed = before_tracks - after_tracks
            if added or removed:
                result["playlists_changed"][name] = {
                    "added": list(added),
                    "removed": list(removed),
                }

    # Compare folders
    before_folders = set(before.get("folders", {}).keys())
    after_folders = set(after.get("folders", {}).keys())
    result["folders_added"] = list(after_folders - before_folders)
    result["folders_removed"] = list(before_folders - after_folders)

    # Determine if clean (library changes only — playback state changes don't count)
    if (
        result["track_count_change"] != 0
        or result["playlists_added"]
        or result["playlists_removed"]
        or result["playlists_changed"]
        or result["folders_added"]
        or result["folders_removed"]
    ):
        result["is_clean"] = False

    return result

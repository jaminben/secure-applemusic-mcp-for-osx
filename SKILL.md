---
name: apple-music
version: 0.1.1
description: Apple Music integration for macOS — local Music.app via Apple Events, plus the official Apple Music API for catalog reads. No UI automation, no web player, no queue.
---

# Apple Music Integration

> **This is the hardened macOS-only fork
> ([secure-applemusic-mcp-for-osx](https://github.com/jaminben/secure-applemusic-mcp-for-osx)).**
> Relative to upstream: no UI automation / Accessibility, no browser or Safari
> web player, no Up Next queue tool, no URL opening, and no stored credentials
> by default. Sections below describing those capabilities are marked REMOVED —
> do not attempt them. Six tools exist: playlist, library, discover, catalog,
> config, playback.

Guide for integrating with Apple Music. Three approaches: AppleScript (direct control), UI automation (catalog without API), and MusicKit API (cross-platform).

## When to Use

Invoke when users ask to:
- Manage playlists (create, add/remove tracks, list)
- Control playback (play, pause, skip, volume)
- Play an Apple Music URL (album, playlist, or song)
- Search catalog or library
- Add songs to library
- Access listening history or recommendations

## Critical Rule: Library-First Workflow

**You CANNOT add catalog songs directly to playlists.**

Songs must be in the user's library first:
- ❌ Catalog ID → Playlist (fails)
- ✅ Catalog ID → Library → Playlist (works)

**Why:** Playlists use library IDs (`i.abc123`), not catalog IDs (`1234567890`).

This applies to both AppleScript and API approaches.

---

# AppleScript (macOS)

Zero setup. Works immediately with the Music app.

**Run via Bash:**
```bash
osascript -e 'tell application "Music" to playpause'
osascript -e 'tell application "Music" to return name of current track'
```

**Multi-line scripts:**
```bash
osascript <<'EOF'
tell application "Music"
    set t to current track
    return {name of t, artist of t}
end tell
EOF
```

## Available Operations

| Category | Operations |
|----------|------------|
| **Playback** | play, pause, stop, resume, next track, previous track, fast forward, rewind, play URL |
| **Player State** | player position, player state, sound volume, mute, shuffle enabled/mode, song repeat |
| **Current Track** | name, artist, album, duration, time, rating, loved, disliked, genre, year, track number |
| **Library** | search, list tracks, get track properties, set ratings |
| **Playlists** | list, create, delete, rename, add tracks, remove tracks, get tracks |
| **AirPlay** | list devices, select device, current device |

## Track Properties (Read)

```applescript
tell application "Music"
    set t to current track
    -- Basic info
    name of t           -- "Hey Jude"
    artist of t         -- "The Beatles"
    album of t          -- "1 (Remastered)"
    album artist of t   -- "The Beatles"
    composer of t       -- "Lennon-McCartney"
    genre of t          -- "Rock"
    year of t           -- 1968

    -- Timing
    duration of t       -- 431.0 (seconds)
    time of t           -- "7:11" (formatted)
    start of t          -- start time in seconds
    finish of t         -- end time in seconds

    -- Track info
    track number of t   -- 21
    track count of t    -- 27
    disc number of t    -- 1
    disc count of t     -- 1

    -- Ratings
    rating of t         -- 0-100 (20 per star)
    loved of t          -- true/false
    disliked of t       -- true/false

    -- Content rating. DO NOT trust this for filtering.
    -- On Apple Music (cloud) tracks this property is unset, and reading it
    -- RAISES "A descriptor type mismatch occurred" rather than returning
    -- false. Catching that and defaulting to false is what turns "I could
    -- not tell" into "this is clean": on a real 463-track playlist Music.app
    -- yielded zero explicit tracks where the catalog rated eight of them
    -- explicit. If a caller asked for clean content, resolve the rating
    -- against the catalog (`contentRating == "explicit"`, matching on name
    -- AND artist) and report anything you could not verify as unknown.
    explicit of t       -- true/false, RAISES on most cloud tracks

    -- Playback
    played count of t   -- 42
    played date of t    -- date last played
    skipped count of t  -- 3
    skipped date of t   -- date last skipped

    -- IDs
    persistent ID of t  -- "ABC123DEF456"
    database ID of t    -- 12345
end tell
```

## Track Properties (Writable)

```applescript
tell application "Music"
    set t to current track
    set rating of t to 80          -- 4 stars
    set loved of t to true
    set disliked of t to false
    set name of t to "New Name"    -- rename track
    set genre of t to "Alternative"
    set year of t to 1995
end tell
```

## Player State Properties

```applescript
tell application "Music"
    player state          -- stopped, playing, paused, fast forwarding, rewinding
    player position       -- current position in seconds (read/write)
    sound volume          -- 0-100 (read/write)
    mute                  -- true/false (read/write)
    shuffle enabled       -- true/false (read/write)
    shuffle mode          -- songs, albums, groupings
    song repeat           -- off, one, all (read/write)
    current track         -- track object
    current playlist      -- playlist object
    current stream URL    -- URL if streaming
end tell
```

## Playback Commands

```applescript
tell application "Music"
    -- Play controls
    play                          -- play current selection
    pause
    stop
    resume
    playpause                     -- toggle play/pause
    next track
    previous track
    fast forward
    rewind

    -- Play specific content
    play (first track of library playlist 1 whose name contains "Hey Jude")
    play user playlist "Road Trip"

    -- Settings
    set player position to 60     -- seek to 1:00
    set sound volume to 50        -- 0-100
    set mute to true
    set shuffle enabled to true
    set song repeat to all        -- off, one, all
end tell
```

### Matching titles with typographic punctuation / accents

`whose name contains "..."` is **glyph-exact** — unlike Music's native `search`
command, it does *not* fold a straight apostrophe (U+0027) against the curly one
(U+2019) Music often stores, nor accents. So `whose name contains "That's a No
No"` misses a title stored as `That's a No No`. Two robust strategies:

```applescript
-- 1. Match the quote-free fragments instead of the apostrophe itself.
--    Works regardless of which apostrophe variant is stored or typed.
play (first track of library playlist 1 whose ¬
    (name contains "That" and name contains "s a No No"))

-- 2. For accents/ellipsis/anything: bulk-fetch names in ONE Apple Event and
--    fold in-memory. `ignoring punctuation and diacriticals` uses Apple's
--    Unicode tables (curly quotes, ellipses, café≈cafe). Do NOT access
--    `name of t` per-iteration in a loop — that is one Apple Event per track
--    (~17s on a 12k library); fetch the whole list at once (~instant).
tell application "Music"
    set allNames to (get name of every track of library playlist 1)
    set idx to 0
    ignoring punctuation and diacriticals
        repeat with i from 1 to (count of allNames)
            if ((item i of allNames) as text) contains "Fur Elise" then
                set idx to i
                exit repeat
            end if
        end repeat
    end ignoring
    if idx > 0 then play (track idx of library playlist 1)
end tell
```

## Library Queries

```applescript
tell application "Music"
    -- All library tracks
    every track of library playlist 1

    -- Search by name
    tracks of library playlist 1 whose name contains "Beatles"

    -- Search by artist
    tracks of library playlist 1 whose artist contains "Beatles"

    -- Search by album
    tracks of library playlist 1 whose album contains "Abbey Road"

    -- Combined search
    tracks of library playlist 1 whose name contains "Hey" and artist contains "Beatles"

    -- By genre
    tracks of library playlist 1 whose genre is "Rock"

    -- By year
    tracks of library playlist 1 whose year is 1969

    -- By rating
    tracks of library playlist 1 whose rating > 60  -- 3+ stars

    -- Favorite (loved) tracks. Music.app renamed Loved -> Favorite and the
    -- property with it: newer versions use `favorited`, older use `loved`,
    -- and the unsupported name can fail INSIDE a whose-filter with "The
    -- variable loved is not defined". Try `whose favorited is true` first,
    -- fall back to `whose loved is true`, each wrapped in `try`.
    tracks of library playlist 1 whose favorited is true
    tracks of library playlist 1 whose loved is true  -- legacy Music versions

    -- Recently played (sort by played date)
    tracks of library playlist 1 whose played date > (current date) - 7 * days
end tell
```

## Playlist Operations

```applescript
tell application "Music"
    -- List all playlists
    name of every user playlist

    -- Get playlist
    user playlist "Road Trip"
    first user playlist whose name contains "Road"

    -- Create playlist
    make new user playlist with properties {name:"New Playlist", description:"My playlist"}

    -- Delete playlist
    delete user playlist "Old Playlist"

    -- Rename playlist
    set name of user playlist "Old Name" to "New Name"

    -- Get playlist tracks
    every track of user playlist "Road Trip"
    name of every track of user playlist "Road Trip"

    -- Get the FIRST N, one Apple Event per property. Read the range off the
    -- playlist itself. Do NOT stash the tracks in a variable first --
    -- `set t to tracks of pl` materializes a plain list, and a property of a
    -- plain list does not distribute: Music raises -1728 and names every
    -- track of the playlist in the error message.
    set pl to user playlist "Road Trip"
    set n to 25
    if n > (count of tracks of pl) then set n to (count of tracks of pl)
    if n < 1 then return {}                        -- `tracks 1 thru 0` errors
    get name of tracks 1 thru n of pl as list      -- `as list`: n=1 returns a
                                                   -- bare value, not a list

    -- Add track to playlist (must be library track)
    set targetPlaylist to user playlist "Road Trip"
    set targetTrack to first track of library playlist 1 whose name contains "Hey Jude"
    duplicate targetTrack to targetPlaylist

    -- Remove track from playlist
    delete (first track of user playlist "Road Trip" whose name contains "Hey Jude")

    -- Playlist properties
    duration of user playlist "Road Trip"   -- total duration
    time of user playlist "Road Trip"       -- formatted duration
    count of tracks of user playlist "Road Trip"
end tell
```

## AirPlay

```applescript
tell application "Music"
    -- List AirPlay devices
    name of every AirPlay device

    -- Get current device
    current AirPlay devices

    -- Set output device
    set current AirPlay devices to {AirPlay device "Living Room"}

    -- Multiple devices
    set current AirPlay devices to {AirPlay device "Living Room", AirPlay device "Kitchen"}

    -- Device properties
    set d to AirPlay device "Living Room"
    name of d
    kind of d           -- computer, AirPort Express, Apple TV, AirPlay device, Bluetooth device
    active of d         -- true if playing
    available of d      -- true if reachable
    selected of d       -- true if in current devices
    sound volume of d   -- 0-100
end tell
```

## String Escaping

Always escape user input:
```python
def escape_applescript(s):
    return s.replace('\\', '\\\\').replace('"', '\\"')

safe_name = escape_applescript(user_input)
script = f'tell application "Music" to play user playlist "{safe_name}"'
```

## Common Failures

osascript stderr messages map to a small set of environmental states. When AppleScript fails, classify before deciding what to tell the user — surfacing the raw stderr is misleading; cascading silently to an API path leaks unrelated errors (e.g. "Developer token not found" when the real problem was Music.app being closed).

| Stderr signal | What it means | What to tell the user |
|---|---|---|
| `(-609)`, `Connection is invalid`, `(-10810)`, `isn't running`, `Can't get application "Music"` | Music.app isn't running or has crashed mid-session | "Music.app isn't running. Open it and retry." |
| `(-1743)`, `Not authorized`, `not allowed assistive access`, `assistive access` | The host process (Claude Desktop, Python CLI, Terminal) hasn't been granted Automation permission for Music | "Open System Settings → Privacy & Security → Automation, find the app running this code, enable the 'Music' toggle." |
| `AppleScript timed out after 30 seconds` | The 30s subprocess timeout fired — Music.app stuck or still launching | "Music.app may be unresponsive — quit and reopen it." |
| `syntax error`, `expected … but found …` | The script itself is malformed (developer bug) | Report the raw error — this is on us, not the user. |
| Anything else | Logic-level error (track not found, playlist empty, etc.) | Surface the raw stderr; safe to cascade to API if a legitimate fallback exists. |

**Don't bare-match `not allowed`** — Music.app emits "operation not allowed on smart playlists" and similar logic-level errors that should NOT classify as Automation-denial. Match the full `not allowed assistive` or `assistive access` phrase.

**Don't gate on `is_available()` alone** — that only checks `darwin + osascript exists`. It can't tell you whether Music.app is running or whether Automation permissions have been granted. The error-categorization above is your only signal for those states.

## Limitations

- **macOS only** — no Windows/Linux
- **UI features require display** — UI automation won't work headless or with Music.app minimized
- **Music.app must be running** — `is_available()` returns True as soon as `osascript` is on PATH, but every `tell application "Music"` block needs Music.app actually running. First call after a fresh boot may also trigger an Automation-permission prompt the user has to approve once.

---

# UI Automation (macOS) — REMOVED IN THIS BUILD

Upstream drove Music.app through its UI with System Events (synthetic
keystrokes, AX-tree walks) and CoreGraphics mouse events. That required
Accessibility permission, which is system-wide synthetic input and cannot be
scoped to one app, so the whole subsystem was removed from this fork.

Consequences for anything reading this file:

- There are no `ui_*` operations. Do not attempt Top Results clicking, the
  hover trick, search-field typing, popover row matching, or window recovery.
- Playing a catalog track you do NOT own is add-then-play: add it to the
  library over the official API, wait for the iCloud sync, then play it by
  name. `playback(action="play", track=..., add_to_library=True)` does this.
  Without a developer token it is not possible; say so rather than improvising.
- There is no Up Next queue tool. Up Next lived in the web player's MusicKit
  instance, which this build does not have.
- `playback(url=...)` parses the URL for a catalog id. It never opens it.

# MusicKit API

Cross-platform but requires Apple Developer account ($99/year) and token setup.

## Authentication

**Requirements:**
1. Apple Developer account
2. MusicKit key (.p8 file) from [developer portal](https://developer.apple.com/account/resources/authkeys/list)
3. Developer token (JWT, 180 day max)
4. User music token (browser OAuth)

**Generate developer token:**
```python
import jwt, datetime

with open('AuthKey_XXXXXXXXXX.p8') as f:
    private_key = f.read()

token = jwt.encode(
    {
        'iss': 'TEAM_ID',
        'iat': int(datetime.datetime.now().timestamp()),
        'exp': int((datetime.datetime.now() + datetime.timedelta(days=180)).timestamp())
    },
    private_key,
    algorithm='ES256',
    headers={'alg': 'ES256', 'kid': 'KEY_ID'}
)
```

**Get user token:** Browser OAuth to `https://authorize.music.apple.com/woa`

**Headers for all requests:**
```
Authorization: Bearer {developer_token}
Music-User-Token: {user_music_token}
```

**Base URL:** `https://api.music.apple.com/v1`

## Rate Limits (read this before any bulk loop)

Apple throttles with `HTTP 429` / code `42900` on a **rolling ~60-minute window**, and
sends **no `Retry-After` and no `X-Rate-Limit` header** — remaining quota is unobservable,
so you only find out by getting blocked.

| Token | Practical ceiling | Notes |
|---|---|---|
| Your own developer token (`login --dev`, your `.p8`) | ~3600 requests/hour, Apple's documented figure | Your quota alone |
| Apple's public web-player token (plain `login`, tokenless/Safari path) | a few hundred/hour, measured | Shared, not per-user — the ceiling is far lower and not yours to spend |

Consequences that bite:

- **Waiting briefly does nothing.** A 15-minute zero-traffic cooldown still 429s. It clears
  roughly an hour after the burst ends, then re-throttles after a few more requests.
  Every retry inside the window adds to the count keeping you blocked.
- **A 429 can look like an empty result, not an error.** One search per track ("resolve
  title+artist → catalog id") is the standard import shape and the fastest way to hit this;
  if the caller treats an empty search as "song not found", a throttle silently produces
  false negatives. Treat empty-after-429 as *unknown*, never as *absent*, and never cache it.
- **For bulk work, use `login --dev`.** Your own developer token gets its own, much larger
  quota. Reported and measured in [#42](https://github.com/epheterson/applemusic-mcp/issues/42).

### Batch-resolve by ISRC instead of searching per track

Where the caller has ISRCs (Spotify / Rekordbox / Plex exports all carry them),
`GET /v1/catalog/{storefront}/songs?filter[isrc]=ISRC1,ISRC2,…` resolves **25 per request**
and matches **exactly** — ~25x fewer requests than a search per track, and no fuzzy-match
errors. Via the tool: `catalog(action="resolve", isrcs="…", format="json")`.

Two things to get right, because `filter[isrc]` is a *filter*, not a search:

- **Misses are silent.** Apple returns only what it matched and omits the rest, so the
  unresolved ISRCs exist only as a diff of your request set against `attributes.isrc` on
  the responses. Compute that diff — don't assume a full response.
- **Keep "asked and absent" separate from "never asked."** If a batch fails (429) the
  remaining ISRCs have *unknown* status, not "not in the catalog." Collapsing the two
  recreates the false-negative bug this section exists to warn about.

One ISRC can map to several catalog songs (regional releases, remasters), so carry the
match count rather than silently taking the first.

**Without ISRCs, dry-run before you write.** Title matching is fuzzy, and its failures are
silent: `Dont Let Me Down` with no artist resolves to The Chainsmokers, not The Beatles.
Resolve the whole list first, show the proposed matches with a confidence marker (exact vs.
fuzzy), and only write once they've been reviewed — never interleave "search, then add"
per track. Via the tool: `catalog(action="resolve", tracks=..., format="json")`, which adds
nothing and returns `ids` ready for `playlist(action="add")` — or
`playlist(action="add", ..., dry_run=True)` to preview against a specific playlist,
which also diffs against what's already in it. Note this costs one request
per track (Apple has no batch title+artist endpoint), so it is *not* a rate-limit fix —
use ISRCs for that.

Manually:

```python
resolved = {}
for i in range(0, len(isrcs), 25):
    batch = isrcs[i : i + 25]
    r = requests.get(
        f"https://api.music.apple.com/v1/catalog/{storefront}/songs",
        headers=headers,
        params={"filter[isrc]": ",".join(batch)},
    )
    if r.status_code == 429:
        break                      # later batches only extend the window
    for song in r.json().get("data", []):
        resolved.setdefault(song["attributes"]["isrc"], song["id"])
```

## Available Endpoints

### Catalog (Public - dev token only)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/catalog/{storefront}/search` | GET | Search songs, albums, artists, playlists |
| `/catalog/{storefront}/songs/{id}` | GET | Song details |
| `/catalog/{storefront}/albums/{id}` | GET | Album details |
| `/catalog/{storefront}/albums/{id}/tracks` | GET | Album tracks |
| `/catalog/{storefront}/artists/{id}` | GET | Artist details |
| `/catalog/{storefront}/artists/{id}/albums` | GET | Artist's albums |
| `/catalog/{storefront}/artists/{id}/songs` | GET | Artist's top songs |
| `/catalog/{storefront}/artists/{id}/related-artists` | GET | Similar artists |
| `/catalog/{storefront}/playlists/{id}` | GET | Playlist details |
| `/catalog/{storefront}/charts` | GET | Top charts |
| `/catalog/{storefront}/genres` | GET | All genres |
| `/catalog/{storefront}/search/suggestions` | GET | Search autocomplete |
| `/catalog/{storefront}/stations/{id}` | GET | Radio station |

### Library (Requires user token)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/me/library/songs` | GET | All library songs |
| `/me/library/albums` | GET | All library albums |
| `/me/library/artists` | GET | All library artists |
| `/me/library/playlists` | GET | All library playlists |
| `/me/library/playlists/{id}` | GET | Playlist details |
| `/me/library/playlists/{id}/tracks` | GET | Playlist tracks |
| `/me/library/search` | GET | Search library |
| `/me/library` | POST | Add to library |
| `/catalog/{sf}/songs/{id}/library` | GET | Get library ID from catalog ID |

### Playlist Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/me/library/playlists` | POST | Create playlist |
| `/me/library/playlists/{id}/tracks` | POST | Add tracks to playlist |

### Personalization

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/me/recommendations` | GET | Personalized recommendations |
| `/me/history/heavy-rotation` | GET | Frequently played |
| `/me/recent/played` | GET | Recently played |
| `/me/recent/added` | GET | Recently added |

### Ratings

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/me/ratings/songs/{id}` | GET | Get song rating |
| `/me/ratings/songs/{id}` | PUT | Set song rating |
| `/me/ratings/songs/{id}` | DELETE | Remove rating |
| `/me/ratings/albums/{id}` | GET/PUT/DELETE | Album ratings |
| `/me/ratings/playlists/{id}` | GET/PUT/DELETE | Playlist ratings |

### Storefronts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/storefronts` | GET | All storefronts |
| `/storefronts/{id}` | GET | Storefront details |
| `/me/storefront` | GET | User's storefront |

## Common Query Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `term` | Search query | `term=beatles` |
| `types` | Resource types | `types=songs,albums` |
| `limit` | Results per page (max 25) | `limit=10` |
| `offset` | Pagination offset | `offset=25` |
| `include` | Related resources | `include=artists,albums` |
| `extend` | Additional attributes | `extend=editorialNotes` |
| `l` | Language code | `l=en-US` |

## Search Example

```bash
GET /v1/catalog/us/search?term=wonderwall&types=songs&limit=10

Response:
{
  "results": {
    "songs": {
      "data": [{
        "id": "1234567890",
        "type": "songs",
        "attributes": {
          "name": "Wonderwall",
          "artistName": "Oasis",
          "albumName": "(What's the Story) Morning Glory?",
          "durationInMillis": 258773,
          "releaseDate": "1995-10-02",
          "genreNames": ["Alternative", "Music"]
        }
      }]
    }
  }
}
```

## Library-First Workflow (Complete)

Adding a catalog song to a playlist requires 4 API calls:

```python
import requests

headers = {
    "Authorization": f"Bearer {dev_token}",
    "Music-User-Token": user_token
}

# 1. Search catalog
r = requests.get(
    "https://api.music.apple.com/v1/catalog/us/search",
    headers=headers,
    params={"term": "Wonderwall Oasis", "types": "songs", "limit": 1}
)
catalog_id = r.json()['results']['songs']['data'][0]['id']

# 2. Add to library
requests.post(
    "https://api.music.apple.com/v1/me/library",
    headers=headers,
    params={"ids[songs]": catalog_id}
)

# 3. Get library ID (catalog ID → library ID)
r = requests.get(
    f"https://api.music.apple.com/v1/catalog/us/songs/{catalog_id}/library",
    headers=headers
)
library_id = r.json()['data'][0]['id']

# 4. Add to playlist (library IDs only!)
requests.post(
    f"https://api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks",
    headers={**headers, "Content-Type": "application/json"},
    json={"data": [{"id": library_id, "type": "library-songs"}]}
)
```

## Create Playlist

```bash
POST /v1/me/library/playlists
Content-Type: application/json

{
  "attributes": {
    "name": "Road Trip",
    "description": "Summer vibes"
  },
  "relationships": {
    "tracks": {
      "data": []
    }
  }
}
```

## Ratings

```bash
# Love a song (value: 1 = love, -1 = dislike)
PUT /v1/me/ratings/songs/{id}
Content-Type: application/json

{"attributes": {"value": 1}}
```

## Limitations

- **No playback control** - API cannot play/pause/skip
- **Playlist editing** - can only modify API-created playlists
- **Token management** - dev tokens expire every 180 days
- **Rate limits** - Apple enforces request limits

---

# Common Mistakes

**❌ Using catalog IDs in playlists:**
```python
# WRONG
json={"data": [{"id": "1234567890", "type": "songs"}]}
```
**Fix:** Add to library first, get library ID, then add.

**❌ Playing catalog songs via AppleScript:**
```applescript
# WRONG
play track id "1234567890"
```
**Fix:** Song must be in library.

**❌ Unescaped AppleScript strings:**
```python
# WRONG
name = "Rock 'n Roll"
script = f'tell application "Music" to play playlist "{name}"'
```
**Fix:** Escape quotes.

**❌ Expired tokens:**
Dev tokens last 180 days max.
**Fix:** Check expiration, handle 401 errors.

---

# The Easy Way: applemusic-mcp

The [applemusic-mcp](https://github.com/epheterson/applemusic-mcp) MCP server handles all this complexity automatically: AppleScript escaping, token management, library-first workflow, ID conversions.

**Install:**
```bash
git clone https://github.com/epheterson/applemusic-mcp.git
cd applemusic-mcp && python3 -m venv venv && source venv/bin/activate
pip install -e .
```

**Configure Claude Desktop:**
```json
{
  "mcpServers": {
    "Apple Music": {
      "command": "/path/to/applemusic-mcp/venv/bin/python",
      "args": ["-m", "applemusic_mcp"]
    }
  }
}
```

On macOS, most features work immediately. For catalog features or Windows/Linux, see the repo README.

| Manual | applemusic-mcp |
|--------|----------------|
| 4 API calls to add song | `playlist(action="add", auto_add=True)` |
| Copy URL + open in Music | `playback(action="play", url="...")` |
| Add a catalog song to your library | `library(action="add")` (unified API — dev or web token) |
| Track library changes manually | `library(action="snapshot")` |
| AppleScript escaping | Automatic |
| Token management | Automatic with warnings |

# Apple Music MCP servers, compared

There are several. This is an honest read of the three most-starred, plus this
fork — written by the author of the fork, so weigh it accordingly. Where a claim
is checkable, the check is named.

Star counts and last-push dates: 22 August 2026.

| | [kennethreitz/mcp-applemusic](https://github.com/kennethreitz/mcp-applemusic) | [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp) | [Cifero74/mcp-apple-music](https://github.com/Cifero74/mcp-apple-music) | **this fork** |
|---|---|---|---|---|
| ★ | 90 | 86 | 38 | — |
| Last push | Sep 2025 | Aug 2026 | May 2026 | Aug 2026 |
| Size | ~165 lines | ~18k lines | mid-size | ~10k lines |
| How it reaches Music | AppleScript | AppleScript + web API + browser | Apple Music REST API | AppleScript only |
| Platforms | macOS | macOS, Windows, Linux | any | macOS |
| **Apple Developer account** | not needed | not needed | **required** (.p8 + Team/Key ID) | not needed (optional: raises the rate limit) |
| Install | clone + uv | pip / uvx | clone + setup wizard | **download, drag, double-click** |

## What each one is for

**kennethreitz/mcp-applemusic — the minimal one.** Ten tools, one file, no
credentials, no Accessibility. If you want play/pause/next, search, and "make me
a playlist from these exact track names", it does that and nothing else, and you
can read the whole thing in five minutes. Caveat below.

**epheterson/applemusic-mcp — the capable one.** By far the most functionality:
playlists and folders, library management, catalog search, ratings, the **Up Next
queue**, listening history and recommendations, CSV/JSON export, and playback on
**Windows and Linux** as well as macOS via a Chrome web player. Works without an
Apple Developer account by harvesting the token Apple ships to every browser. If
you want the most features, or you aren't on a Mac, this is the one — and this
fork is downstream of it, so credit where it's due.

**Cifero74/mcp-apple-music — the official-API one.** Talks to the documented
Apple Music REST API with your own MusicKit key: catalog search, library,
playlists, listening history, recommendations. Costs you a developer account and
a setup wizard; buys you a sanctioned API and no AppleScript at all. Note the API
cannot *play* audio — this is for managing your library, not driving playback.

**This fork — the locked-down one.** Deliberately *less* capable than upstream:
macOS only, no web player, no Up Next queue. What you get instead is a much
smaller capability surface (below) and an installer a non-technical person can
actually use.

## Full functional scope

Taken from each project's source on 22 Aug 2026, not from its README.

**kennethreitz** — 10 flat tools: `itunes_play`, `itunes_pause`, `itunes_next`,
`itunes_previous`, `itunes_search`, `itunes_play_song`, `itunes_create_playlist`,
`itunes_library`, `itunes_current_song`, `itunes_all_songs`.

**Cifero74** — 11 flat tools: `search_catalog`, `search_library`,
`get_library_songs` / `_albums` / `_artists` / `_playlists`,
`get_playlist_tracks`, `create_playlist`, `add_tracks_to_playlist`,
`get_recently_played`, `get_recommendations`. Note what's absent: it is
**read-plus-create only** — no playback, no ratings, no delete, no remove.

**epheterson** — 7 dispatcher tools carrying ~70 actions: `playlist` (14),
`library` (10), `catalog` (12), `discover` (7), `playback` (6), `queue` (10),
`config` (14).

**this fork** — the same 7 tools minus `queue`, with an **identical action list**
in the six that remain. The differences are in which engine serves an action and
what it requires, not in what you can ask for.

| Capability | kennethreitz | epheterson | Cifero74 | this fork |
|---|---|---|---|---|
| Play / pause / next / previous | ✅ | ✅ | ❌ | ✅ |
| Seek, volume, shuffle, repeat | ❌ | ✅ | ❌ | ✅ |
| Now playing | ✅ | ✅ | ❌ | ✅ |
| Play a specific track by name | ✅ | ✅ | ❌ | ✅ |
| Play an album / playlist | ❌ | ✅ | ❌ | ✅ |
| Play a catalog track you don't own | ❌ | ✅ (needs Accessibility) | ❌ | ✅ (MusicKit — subscription only) |
| **Up Next queue** (list/set/jump/remove/autoplay) | ❌ | ✅ | ❌ | ❌ |
| **AirPlay output switching** | ❌ | ✅ | ❌ | ✅ |
| Library search | ✅ | ✅ | ✅ | ✅ |
| Library browse (songs/albums/artists) | ✅ list all | ✅ | ✅ | ✅ |
| Add to library | ❌ | ✅ (harvested token) | ❌ | ✅ (MusicKit — no credential) |
| Remove from library | ❌ | ✅ | ❌ | ✅ |
| Recently played / added | ❌ | ✅ | ✅ played | ✅ |
| Favorites / loved | ❌ | ✅ | ❌ | ✅ |
| Ratings (love / dislike / stars) | ❌ | ✅ | ❌ | ✅ |
| List playlists | ✅ | ✅ | ✅ | ✅ |
| Create playlist | ✅ | ✅ | ✅ | ✅ |
| Add tracks to playlist | ✅ at create | ✅ | ✅ | ✅ |
| Remove tracks from playlist | ❌ | ✅ | ❌ | ✅ |
| Rename / delete playlist | ❌ | ✅ | ❌ | ✅ |
| Copy playlist, swap a track | ❌ | ✅ | ❌ | ✅ |
| Dry-run a playlist add | ❌ | ✅ | ❌ | ✅ |
| **Folders** (create/move/tree/nested paths) | ❌ | ✅ | ❌ | ✅ |
| Catalog search | ❌ | ✅ | ✅ | ✅ (tokenless) |
| Song / album / artist details | ❌ | ✅ | ❌ | ✅ |
| Resolve by ISRC, bulk track matching | ❌ | ✅ | ❌ | ✅ |
| Charts, genres, search suggestions | ❌ | ✅ | ❌ | ✅ |
| Recommendations / heavy rotation | ❌ | ✅ | ✅ recs | ✅ |
| Similar artists, top songs, stations | ❌ | ✅ | ❌ | ✅ |
| CSV / JSON export | ❌ | ✅ | ❌ | ✅ |
| Library snapshot + diff | ❌ | ✅ | ❌ | ✅ |
| Audit log of every change | ❌ | ✅ | ❌ | ✅ |
| Explicit-content filter (`clean_only`) | ❌ | ✅ | ❌ | ✅ (+ verified in exports) |
| Works on Windows / Linux | ❌ | ✅ | ✅ | ❌ |

### Reading that table

**You lose exactly two things** relative to upstream by choosing this fork: the
**Up Next queue**, and **non-macOS support**. Both are consequences of removing
the browser engine — Up Next lives inside the web player's MusicKit instance and
has no AppleScript equivalent, and the web player was what made Windows and
Linux work.

**Playing the whole catalog is the one to look at.** All four rows in that
line deserve unpacking, because it is where the projects differ most:

- kennethreitz and Cifero74 cannot do it at all (Cifero74 cannot play audio).
- Upstream can, by deep-linking Music.app and clicking play with a synthetic
  mouse event — which is why it needs the **Accessibility** automation
  permission, granted system-wide to every app or none.
- This fork does it through **MusicKit**, from the signed app's own identity.
  No Accessibility. No developer account. No credential stored anywhere. An
  Apple Music subscriber approves one prompt and can then play anything in the
  catalog, not just what they already own.

So the differentiator is not the capability — upstream has it too. It is
getting that capability without the system-wide permission or the developer
account, which is the difference between software you can hand to a friend and
software you cannot.

**What needs a credential differs, and adding to your library is where it
shows.** Only two of the four can put a catalog track into your library at all:
kennethreitz has no catalog access, and Cifero74 needs a developer account for
everything yet still cannot do this one. That leaves upstream and this fork —
and they get there differently. Upstream harvests the token Apple ships to every
browser: no developer account, but a real credential, obtained by a route Apple
did not intend and stored on disk. This fork uses MusicKit, so the request is
signed from the app's own code-signing identity plus one consent prompt, and
there is no credential at any point — none issued, none harvested, none stored.

So the precise claim is not "the only one that works without a developer
account" — upstream does too. It is **the only one that adds to your library
without a credential of any kind.** A source checkout without the bundled helper
still falls back to an optional developer token; the packaged app never does.

**Catalog search is tokenless here** via Apple's public iTunes Search API, and it
returns an explicit flag — so `clean_only` is *verified* rather than assumed.

## Capability surface

The dimension this fork exists for. "Can it, in principle, do the thing" — not
"does it choose to".

| | kennethreitz | epheterson | Cifero74 | this fork |
|---|---|---|---|---|
| Needs the **Accessibility API** (system-wide synthetic input; not assistive tech) | no | **yes**, for some paths | no | **never** |
| Drives a **browser** (Playwright/Chrome) | no | yes | no | no |
| Reads your **Safari cookies** | no | yes (opt-in sign-in) | no | no |
| Hands URLs to the OS (`open`) | no | yes | no | no |
| Stores credentials by default | no | yes | yes | **no** |
| Escapes input into AppleScript | **no** — see below | yes | n/a (REST) | yes |
| Permission scoped to the app, not your terminal | no | no | n/a | **yes** |

Two rows deserve explanation rather than a tick.

**Accessibility.** Upstream needs it for one job: playing a catalog track you
don't own, by deep-linking Music.app and clicking the play button with synthetic
mouse events. It's a legitimate way to solve a real gap. But Accessibility is
system-wide synthetic input — it cannot be scoped to Music.app — so granting it
means the process can type into any application. This fork removed those code
paths and replaced them with add-to-library-then-play, which needs no such
permission. A test keeps them removed.

**Escaping.** kennethreitz's server interpolates tool parameters straight into
AppleScript with no escaping, e.g.

```python
whose name contains "{query}"
```

A parameter containing a double quote ends the string literal early, and
AppleScript can reach the shell. Reported as
[issue #8](https://github.com/kennethreitz/mcp-applemusic/issues/8) with the
fix, before this document was written. Worth knowing if you point it at
anything you don't control — including track names in a shared playlist, which
a model reads and can feed back into a tool call. Upstream's escaping, by
contrast, is genuinely careful: I checked all ~90 interpolation sites and tested
the escaper against a live `osascript` with quote, U+2028/9, VT, FF and NEL
payloads. Nothing escaped.

## Ease of install

| | What you actually do |
|---|---|
| kennethreitz | clone, `uv sync`, hand-edit Claude's JSON config |
| epheterson | `uvx applemusic-mcp` or pip, then `login`, then hand-edit the config |
| Cifero74 | create an Apple Developer account, make a MusicKit key, download a `.p8`, run a wizard, hand-edit the config |
| **this fork** | download the zip, drag to /Applications, **double-click once** |

Only this fork ships a self-contained app with its own Python runtime and an
installer that edits Claude Desktop's config for you (after asking, and after
backing it up). If the person installing it doesn't use a terminal, that is the
whole difference.

The trade is real: you get the easy install and the small capability surface by
giving up Windows/Linux, the web player, and Up Next.

## Which to pick

- **Non-technical Mac user who wants Claude to run their music** → this fork.
- **You want every feature, or you're on Windows/Linux** → upstream
  (epheterson). It does more than this fork and always will.
- **You want the official API and already have a developer account** → Cifero74.
- **You want something tiny you can audit in one sitting** → kennethreitz, after
  the escaping issue is fixed (or with the patch from the issue applied).

## Corrections

Numbers were read from the GitHub API and the projects' own source and READMEs
on 22 August 2026; projects change. If something here is wrong or out of date,
please open an issue — particularly if you maintain one of these and think it's
been characterised unfairly.

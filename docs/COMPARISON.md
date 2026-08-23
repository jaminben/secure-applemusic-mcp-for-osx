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
| **Apple Developer account** | not needed | not needed | **required** (.p8 + Team/Key ID) | optional |
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

## Capability surface

The dimension this fork exists for. "Can it, in principle, do the thing" — not
"does it choose to".

| | kennethreitz | epheterson | Cifero74 | this fork |
|---|---|---|---|---|
| Needs **Accessibility** (system-wide synthetic input) | no | **yes**, for some paths | no | **never** |
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

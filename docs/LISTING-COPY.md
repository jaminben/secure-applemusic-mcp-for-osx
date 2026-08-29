# Listing copy

One source for every directory submission, so the wording does not drift as it
gets pasted into four forms. When something here changes, change it here first,
then push it outward.

Where each string already lives:

| String | Lives in | Reaches |
|---|---|---|
| Title | `server.json` → `title` | the MCP Registry and everything mirroring it |
| Short description | `server.json` → `description` (hard 100-char cap) | same, plus answer engines |
| Long description | GitHub repo description (350-char cap) | GitHub search, API scrapers |
| Everything below | nowhere yet — this file is the source | submission forms |

---

## Name

**Unofficial Apple Music for macOS**

Registry name (do not change — it is the authenticated namespace):
`io.github.jaminben/secure-applemusic-mcp-for-osx`

## Short description — 88 chars

> Ask for a playlist. Get a real one, playing in Apple Music — no account, nothing stored.

Leads with the outcome rather than a capability list, because in a directory
grid every entry is a capability list. The cap is 100 and the registry rejects
anything longer.

## One-line capability summary

> Play, search, rate and build playlists in Apple Music on your Mac, and add
> music you don't own yet — through a signed Mac app that stores no credential.

## Long description

> Apple Music MCP server for macOS. Control Apple Music with Claude: play,
> search, build playlists, add music to your library. A free Mac app you
> download — no Apple Developer account, no API key, no terminal.

## Why it is different

The line to use where a form asks what sets it apart. It is narrower than the
obvious claim on purpose — see `COMPARISON.md`, which shows why the blanket
version ("the only one that needs no developer account") is false.

> It is the only Apple Music MCP server that can add music to your library
> without a credential of any kind. The others that can do it at all need
> something first: one wants an Apple Developer account and a `.p8` key you
> download and store; the other harvests the token Apple ships to every browser,
> which means keeping a real credential and turning on "Allow JavaScript from
> Apple Events" — a Safari setting that permits script execution in every tab
> you have open. This one uses Apple's MusicKit, so each request is signed from
> the app's own code-signing identity plus a one-time consent prompt. Nothing is
> issued, harvested, or kept.

## Install

Not a package. Download the notarized app, drag it to Applications,
double-click it once; the first-run window writes your client config.

    https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-arm64.zip

That URL is version-independent and always resolves to the newest release, so
no listing needs editing when a version ships. It only stays true while every
release uploads the unversioned asset name — see RELEASING.md.

## Facts forms ask for

| Field | Value |
|---|---|
| Transport | stdio |
| Tools | 6 — `playback`, `library`, `playlist`, `catalog`, `discover`, `config` |
| Platform | macOS 14+, Apple Silicon |
| Requires | The Music app, signed into Apple Music. A subscription for catalog playback and adds. |
| Auth | None |
| Licence | See `LICENSE` |
| Repository | https://github.com/jaminben/secure-applemusic-mcp-for-osx |
| Icon | `docs/images/icon-512.png` (raw.githubusercontent URL in `server.json`) |
| Screenshot | `docs/images/playlist-conversation.png` |

## Where to submit

Publish to the official registry **first** — Glama and PulseMCP mirror it, so
one publish seeds two directories.

| Where | How | Notes |
|---|---|---|
| Official MCP Registry | automatic, on tag, via `publish.yml` | Has never run. See RELEASING.md. |
| Glama | auto-indexes GitHub; claim the listing | |
| mcp.so | submit form, or their GitHub issues | |
| Smithery | `smithery mcp add <name>` validates first | |
| PulseMCP | mirrors the registry | |
| punkpeye/awesome-mcp-servers | pull request | PR #13115 open |
| mcpservers.org | **web form** at <https://mcpservers.org/submit> | needs a contact email; free listing with a paid upsell |
| wong2/awesome-mcp-servers | fork + PR | their repo refuses outside PRs; branch ready, open by hand |
| LobeHub | community store | not yet investigated |

### mcpservers.org form

Five fields, and it wants a contact email, so this one has to be submitted by
hand. Paste:

- **Server Name:** Unofficial Apple Music for macOS
- **Short Description:** Ask for a playlist. Get a real one, playing in Apple
  Music — no account, nothing stored.
- **Link:** https://github.com/jaminben/secure-applemusic-mcp-for-osx
- **Category:** whichever of media / entertainment / productivity the dropdown
  offers; there is no Apple Music entry on the site to match against.
- **Contact Email:** yours

Decline the paid upgrade unless you want it — the free listing is the point.

### awesome-mcp-servers entry

    - [Unofficial Apple Music](https://github.com/jaminben/secure-applemusic-mcp-for-osx) 🍎 —
      Play, search and build playlists in Apple Music on macOS. A notarized Mac
      app; no developer account, no API key, no credential stored.

## Before submitting

The "why it is different" copy makes comparative claims about three named
projects. All three were re-verified against their current `main` sources on
29 August 2026, which is when the Cifero74 claim was corrected: it has no
add-to-library tool, but `add_tracks_to_playlist` accepts catalog tracks, and
that route puts them in the library. The claim above survived the correction —
it is about credentials, not capability — but re-check before reusing this copy
later. `COMPARISON.md` carries the dates.

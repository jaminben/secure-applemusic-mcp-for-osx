# secure-applemusic-mcp-for-osx

An MCP server for Apple Music on macOS that controls **Music.app and nothing
else**.

A hardened fork of [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp)
(forked at `0acf697`). Upstream is the more capable project — cross-platform, web
playback, Up Next, works without an Apple Developer account. This fork trades
those away for a smaller capability surface.

> **On the name.** "Secure" is the goal, not a certification. This has not been
> independently audited. The checkable claim is the one below.

## What it does not do

| | |
|---|---|
| **No Accessibility** | No synthetic keystrokes, no synthetic clicks, no reading other apps' windows. |
| **No browser automation** | No Playwright, no Chrome driven in-process. |
| **No reading your browser** | Never touches Safari cookies or runs JavaScript in your tabs. |
| **No opening URLs** | URLs are *parsed*, never handed to `open` or a browser. |
| **No shell** | One binary is ever executed: `osascript`, argv list, with a timeout. |
| **No stored credentials by default** | Nothing in `~/.config` to steal. |
| **No unofficial API** | Only `api.music.apple.com` and the public iTunes Search API. |

These are enforced by [`tests/test_capability_invariants.py`](tests/test_capability_invariants.py),
which runs as its own CI job. Deleting a subsystem is a one-time event; keeping
it deleted is a property.

## What it does

Through Apple Events to Music.app — **no credential required**:

- Playlists: list, tree/folders, create, add, copy, move, remove, rename, delete
- Library: search, browse, recently added/played, favorites, rate (incl. stars),
  remove, snapshot/diff
- Playback: play, pause, next/previous, seek, volume, shuffle, repeat,
  now-playing, reveal, AirPlay device selection
- Catalog search via Apple's **public** iTunes Search API

Optional, via an official Apple Developer token (`login --dev`) — needed for one
thing only: **adding a catalog track you don't already own** to your library.
Also unlocks richer catalog metadata, charts, and recommendations.

### What upstream has that this doesn't

Windows/Linux support, the Chrome and Safari web players, the **Up Next queue**
(it lives in the web player's MusicKit instance and cannot survive its removal),
and playing catalog tracks without owning them and without a developer token
(upstream did that with UI automation; here it's add-then-play).

## Install

**Requires:** macOS 12+, the Music app signed into your Apple Music account.
Nothing else — no Python, no Homebrew, no command line.

1. Download `AppleMusicMCP-<version>-macos-<arch>.zip` from
   [Releases](https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases)
   and check it against `SHA256SUMS.txt`.
2. Unzip and drag **AppleMusicMCP.app** to `/Applications`.
3. Double-click it once.

Setup asks before each step, and each one can be skipped:

| Step | What it does | Why |
|---|---|---|
| Background helper | Installs a LaunchAgent that starts the helper at login | Being started by launchd is what gives the app its **own** permission identity |
| Claude Desktop | Adds one `apple-music` entry to your config | So Claude can reach it. Your other servers are kept and the file is backed up first |
| Permission | Triggers the macOS "control Music" prompt | Asking now means the grant lands on **this app**, not on whatever spawns your client |

Then restart Claude Desktop. That's it.

The app is self-contained: it carries its own Python runtime, so there's nothing
to install and nothing on your `PATH` to conflict with.

> **Gatekeeper:** an unsigned download is quarantined — right-click → **Open**
> once to get past it. Signed builds don't have this problem; if you build it
> yourself, sign it (see below), because macOS ties the Music permission to the
> signing identity and an unsigned app re-prompts whenever it changes.

### Why the app, rather than just a command

macOS attributes a permission grant to the **responsible process** — for a
server your MCP client spawns, that's the *client*. So the obvious setup means
approving "Automation → Music" for your whole terminal, and every program you
run from it inherits that.

The app avoids this by splitting in two:

```
Claude Desktop ──stdio──▶ shim ──unix socket (0600)──▶ helper ──▶ Music.app
                     no permissions                 owns the grant
```

The shim holds nothing and can't talk to Music; the launchd-started helper owns
the grant. You get one revocable row in System Settings → Privacy & Security →
Automation, and your terminal gets nothing.
[docs/PERMISSIONS.md](docs/PERMISSIONS.md) has the details.

### From source

```sh
git clone https://github.com/jaminben/secure-applemusic-mcp-for-osx
cd secure-applemusic-mcp-for-osx
./install.sh --scoped --sign "My Local Signing Cert"   # or plain ./install.sh
make app                                               # or build the .app yourself
```

`install.sh` installs into a private `0700` virtualenv from the checkout you're
standing in — nothing is piped from the network into a shell. Plain `./install.sh`
skips the bundle and configures the simpler (unscoped) stdio server.

### Uninstall

Move the app to the Trash, then:

```sh
launchctl bootout gui/$(id -u)/io.github.jaminben.secure-applemusic-mcp
rm -f ~/Library/LaunchAgents/io.github.jaminben.secure-applemusic-mcp.plist
tccutil reset AppleEvents io.github.jaminben.secure-applemusic-mcp
```

Remove the `apple-music` entry from Claude Desktop's config (a backup sits next
to it), and delete `~/.config/applemusic-mcp` and `~/.cache/applemusic-mcp` if
you want the credentials and audit log gone too. From a source install:
`./install.sh --uninstall`.

> **Never grant this Accessibility.** It cannot use it. If something asks, that's
> a bug — please file it.

## Security

See [SECURITY.md](SECURITY.md) for the threat model, what is and isn't reachable,
and the residual risks (Music.app is an unsandboxed deputy; prompt injection is
real; there is no sandbox).

Three issues inherited from upstream are fixed here — a bypassable Apple Music
URL check, an inert path-traversal guard on the `exports://` resource, and
destructive operations acting on a substring guess. See
[CHANGELOG.md](CHANGELOG.md#fixed--inherited-security-issues) for details, and
[DISCLOSURE.md](DISCLOSURE.md) for their upstream reporting status.

## Relationship to upstream

MIT, © Eric Pheterson, retained in full. Upstream history is preserved in git
and in [CHANGELOG-upstream.md](CHANGELOG-upstream.md).

We track upstream **security and correctness fixes to retained modules**;
feature commits touching removed subsystems are ignored by design. Because the
deletions are large, cherry-pick by file rather than merging:

```sh
git fetch upstream --tags
git log --oneline fork-base..upstream/main -- src/applemusic_mcp/applescript.py
```

The capability invariants are the safety net for a cherry-pick that would drag a
removed capability back in.

## Comparison with other Apple Music MCP servers

[kennethreitz/mcp-applemusic](https://github.com/kennethreitz/mcp-applemusic) is
the minimal option: ~165 lines, 10 tools, osascript only, no credentials, and no
Accessibility — a genuinely small capability surface. It builds its AppleScript
with unescaped f-string interpolation of tool parameters, so a track or playlist
name containing a quote breaks out of the string literal; consider that before
pointing it at input you don't control.

This fork keeps upstream's escaping (backslash-then-quote, control characters
stripped, applied at every interpolation site) and its much larger feature set,
while removing the capabilities upstream added on top.

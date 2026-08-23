# secure-applemusic-mcp-for-osx

An MCP server for Apple Music on macOS that controls **Music.app and nothing
else**.

A hardened fork of [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp)
(forked at `0acf697`). Upstream is the more capable project — cross-platform, web
playback, Up Next, works without an Apple Developer account. This fork trades
those away for a smaller capability surface.

## Why this exists

Two goals, and the order matters:

1. **Anyone should be able to install it** — not just people who use a terminal.
2. **It should be safe to hand to that person.**

Both came out of looking at what already existed. As of August 2026 every other
Apple Music MCP server needed a command line: clone a repo or run a package
manager, then hand-edit Claude's JSON config. One also wanted an Apple
Developer account, a MusicKit key and a `.p8` file before it would do anything.
That's a fair bar for a developer tool and an impossible one for everybody else
— and "let Claude run my music" is not a developer's request. Hence: download,
drag, double-click, and an installer that edits the config for you.

The security half is that same survey seen from the other side. The most capable
of those servers reaches a few features through **Accessibility** — macOS's
system-wide synthetic-input permission. It's an honest fix for a real gap
(playing a catalog track you don't own: deep-link Music.app, then click the play
button with a synthetic mouse event). The trouble is that Accessibility cannot
be scoped to a single app. Granting it to a music server also grants the ability
to type into any window, click anything on screen, and read other applications'
interfaces. And you would be granting that to a program which takes its
instructions from a model reading text nobody controls — track names, playlist
titles, whatever it just searched. That is a very short path from prompt
injection to synthetic keystrokes, so this fork removed those code paths
entirely and [a test](tests/test_capability_invariants.py) keeps them removed.

The same instinct produced the app bundle. A server your MCP client spawns
inherits the *client's* permissions, so the ordinary setup means approving
"Automation → Music" for your entire terminal, and everything you ever run from
it. Here the grant lands on one launchd-started helper instead — a single
revocable row in System Settings, and your terminal gets nothing.

None of this makes the fork better than upstream; it makes it **smaller**.
Upstream does more and always will. This one is built to be handed to someone
who is never going to read its source.

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
tools/make-signing-cert.sh                             # one-off local signing cert
SIGN_ID="Apple Music MCP Self-Signed" make app         # build a signed .app
```

`install.sh` installs into a private `0700` virtualenv from the checkout you're
standing in — nothing is piped from the network into a shell. Plain `./install.sh`
skips the bundle and configures the simpler (unscoped) stdio server.

### Sharing it with someone else

Two things decide whether it "just works" on their Mac.

**Architecture.** The zip is built for one architecture. `AppleMusicMCP-*-arm64`
is Apple Silicon (M1 and later); build `--arch x86_64` for an Intel Mac. If in
doubt, ask them for  → About This Mac.

**Gatekeeper.** A *self-signed* build is not a notarized one. On their machine
macOS will refuse it on first open, and the right-click → Open trick no longer
works on recent macOS. They need:

> System Settings → Privacy & Security → scroll down → **Open Anyway**

That is one extra step, and it is the honest cost of not paying for notarization.
Self-signing still earns its keep: it gives the app a stable identity, so the
Music permission survives updates instead of re-prompting every time.

For a genuinely frictionless hand-off — no scary dialog at all — sign with a
**Developer ID Application** certificate and notarize:

```sh
SIGN_ID="Developer ID Application: Your Name (TEAMID)" make app
xcrun notarytool submit dist/AppleMusicMCP-*.zip \
    --apple-id you@example.com --team-id TEAMID --wait
xcrun stapler staple dist/AppleMusicMCP.app
```

That needs a paid Apple Developer account ($99/yr). Nothing else about the app
changes.

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

Full write-up with sources in [docs/COMPARISON.md](docs/COMPARISON.md). The
short version, by stars (Aug 2026):

| | Best for | Needs a dev account | Install |
|---|---|---|---|
| [kennethreitz/mcp-applemusic](https://github.com/kennethreitz/mcp-applemusic) ★90 | Something tiny you can audit in one sitting | no | clone + edit JSON |
| [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp) ★86 | **Most features**; Windows/Linux; Up Next queue | no | pip/uvx + edit JSON |
| [Cifero74/mcp-apple-music](https://github.com/Cifero74/mcp-apple-music) ★38 | The official REST API | **yes** | wizard + edit JSON |
| **this fork** | A Mac user who doesn't use a terminal | no | **double-click** |

Upstream (epheterson) does more than this fork and always will — web playback,
Windows/Linux, the Up Next queue. This fork trades those away for a smaller
capability surface (no Accessibility, no browser automation, no stored
credentials) and an installer that doesn't need a terminal.

One thing worth flagging if you're choosing between them: kennethreitz's server
interpolates tool parameters into AppleScript without escaping, so a quote in a
track or playlist name breaks out of the string literal. Reported with a fix as
[issue #8](https://github.com/kennethreitz/mcp-applemusic/issues/8).

# Unofficial Apple Music MCP

<!-- mcp-name: io.github.jaminben/secure-applemusic-mcp-for-osx -->

**Let Claude play your music. Download, drag, double-click. That's the install.**

<p align="center">
  <img src="https://raw.githubusercontent.com/jaminben/secure-applemusic-mcp-for-osx/main/docs/images/playlist-conversation.png" width="720"
       alt="Claude asked to build a birthday playlist of top songs from August 10th over five years, appropriate for a seven-year-old. It creates the playlist in Apple Music, starts it playing, and lists the 17 tracks it chose, skipping the explicit versions.">
</p>

<p align="center">
  <sub><i>One request. A real playlist, playing, with the explicit versions left out.</i></sub>
</p>

<p align="center">
  <a href="https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-arm64.zip">
    <b>⬇ Download for Apple Silicon</b>
  </a>
  &nbsp;·&nbsp;
  <a href="https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-x86_64.zip">
    <b>⬇ Download for Intel</b>
  </a>
  <br>
  <sub>
    Not sure which? Apple menu → About This Mac. "Apple M1/M2/M3…" is Apple Silicon.
  </sub>
  <br>
  <sub>
    notarized by Apple · no terminal · no developer account ·
    <a href="https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest">all downloads &amp; checksums</a>
  </sub>
</p>

---

## Ask for music in plain English

*"Build my kid a Hogwarts birthday playlist. Soundtracks only, nothing scary."*

*"I've worn out The Heist. What should I try next?"*

*"More like that Eric Ericson choir record, but skip the Christmas half."*

Claude does the work in the Music app you already have. The playlist it builds
is a real playlist. It syncs to your phone.

| | |
|---|---|
| **Playlists** | Make them, add to them, reorder, rename, sort into folders, delete |
| **Your library** | Search it, browse it, rate songs, mark favorites, see what you played |
| **Playback** | Play, pause, skip, seek, shuffle, repeat, volume, AirPlay |
| **The full catalog** | Search all of Apple Music, play it, add it to your library |

Playing or adding music you don't already own needs an Apple Music
subscription. That's Apple's rule, not this server's. Everything else works
without one.

## Install

<p align="center">
  <img src="https://raw.githubusercontent.com/jaminben/secure-applemusic-mcp-for-osx/main/docs/images/installer-welcome.png" alt="The first-run setup window: Control Apple Music with AI" width="460">
</p>

**You need** macOS 12 or later and the Music app signed in to your Apple
account. Playing or adding catalog music needs macOS 14 or later.

1. [**Download the app**](https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-arm64.zip)
   for Apple Silicon, or the [Intel build](https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-x86_64.zip).
2. Unzip it. Drag **UnofficialAppleMusicMCP.app** to your Applications folder.
3. Double-click it once.

That's the whole install. No Python, no Homebrew, no Terminal. The app brings
everything it needs with it.

Apple notarized these builds, so the app just opens. No "unidentified
developer" warning, no right-click trick, no trip to System Settings.

> **Drag it to Applications first.** If you open it from your Downloads folder,
> macOS runs it from a temporary copy that vanishes when you quit, and the
> background helper ends up pointing at a file that no longer exists.

The setup window then asks you three questions. You can skip any of them.

| It asks | What happens | Why |
|---|---|---|
| Install the background helper? | Adds a small program that starts when you log in | This is what lets the permission belong to the app instead of to your terminal |
| Set up your AI apps? | Adds one entry to Claude Desktop, Claude Code, Cursor, Windsurf, Codex or VS Code | So they can find it. Your other tools stay put, and it backs up every file first |
| Allow access to Music? | Shows the standard macOS permission box | Approving it here means the permission lands on this app |

Restart the AI apps you picked, and you're done.

> **Building it yourself?** Sign it. See [From source](#from-source). macOS ties
> the Music permission to a signature. An unsigned app looks like a brand new
> app on every rebuild, so it will ask for permission again each time.

## Why it asks for so little

Software can reach whatever you let it reach. That matters more than usual
here, because this software takes its orders from an AI.

The most capable Apple Music server I found needs a macOS permission called
**Accessibility**. The name misleads. It has nothing to do with screen readers
or assistive technology. It lets one app drive another: type keystrokes, click
buttons, read what's on your screen.

macOS will not limit it to one app. You grant it for everything or not at all.

So a music server holding that permission can also type into your email. And it
takes its instructions from a model reading text nobody has vetted: track
names, playlist titles, whatever it just searched. A booby-trapped playlist
title becomes a keystroke.

This fork deleted that code. It asks for three things instead:

| It gets | It does not get |
|---|---|
| **Control of the Music app**, and nothing else. Revoke it in System Settings any time | The Accessibility permission. There is no code left that could use it |
| **A connection to Apple Music**, so it can add songs to your library | Any browser, your cookies, your tabs |
| **One background program**, started when you log in | Your terminal's permissions, or anything else on your Mac |

That's the whole list, and
[a test suite](tests/test_capability_invariants.py) fails the build if any of it
creeps back.

It still plays the entire Apple Music catalog. Most servers pay for that with
the permission above. This one goes through Apple's own MusicKit, signed with
the app's identity: one approval box, no developer account, and no password or
key stored anywhere on your Mac.

> **"Secure" is the goal, not a certificate.** Nobody independent has audited
> this. The claims worth trusting are the ones a test can check, and those are
> listed below.

### Why an app instead of a command

macOS blames the parent. When your AI app launches a helper program, the
permission lands on the AI app, not the helper. The usual setup therefore hands
"control Music" to your entire terminal, and to everything you ever run from it.

This app splits in two instead:

```
Claude Desktop ──stdio──▶ shim ──unix socket (0600)──▶ helper ──▶ Music.app
                     no permissions                 owns the grant
```

The shim talks to Claude and cannot touch Music. The helper owns the permission
and is started by macOS itself. You get one row in System Settings you can
switch off, and your terminal gets nothing.
[docs/PERMISSIONS.md](docs/PERMISSIONS.md) has the details.

## What it will never do

| | |
|---|---|
| **No Accessibility** | No fake keystrokes, no fake clicks, no reading other apps' windows |
| **No browser** | No Playwright, no Chrome driven from inside the app |
| **No reading your browser** | Never touches Safari cookies or runs JavaScript in your tabs |
| **No opening links** | It reads Apple Music links, it never hands them to a browser |
| **No shell** | It runs exactly one program: `osascript`, with a timeout |
| **No stored passwords** | There is nothing in `~/.config` worth stealing |
| **No private APIs** | Only `api.music.apple.com` and Apple's public search API |

Every row is a test in [`tests/test_capability_invariants.py`](tests/test_capability_invariants.py),
running as its own CI job. Deleting a subsystem is a one-time event. Keeping it
deleted is a property.

## For developers

A hardened fork of [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp).
Upstream does more and always will: Windows and Linux, the Chrome and Safari web
players, and the Up Next queue, which lived inside the web player and could not
survive its removal. This fork trades those for a smaller blast radius and an
installer your relatives can use.

### With pip, pipx or uvx

The wheel carries the same signed MusicKit helper the app does, so this is a
full install, not a reduced one.

```bash
pipx install secure-applemusic-mcp-for-osx     # or: uv tool install …
```

> Every [release](https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest)
> also has the wheel attached with checksums, if you would rather verify a file
> first.

There's no setup window on this path, so two steps are yours.

**1. Add it to your MCP client.** For Claude Desktop, in
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "unofficial-apple-music": {
      "command": "secure-applemusic-mcp",
      "args": ["serve"]
    }
  }
}
```

For Claude Code: `claude mcp add unofficial-apple-music -- secure-applemusic-mcp serve`.
Other clients take the same command in their own config file. Use the full path
if it isn't on your client's `PATH`, since GUI apps often don't inherit a shell
`PATH`. `which secure-applemusic-mcp` prints what to paste.

**2. Approve Apple Music once.** Restart the client, then ask it to run
`config(action='signin')`. A native prompt appears and nothing is stored. Until
you do, everything works except adding music you don't already own.

The wheel is macOS-only and covers both chips. `pip` refuses to install it
elsewhere rather than hand you a build with no MusicKit.

### From source

```sh
git clone https://github.com/jaminben/secure-applemusic-mcp-for-osx
cd secure-applemusic-mcp-for-osx
./install.sh --scoped --sign "My Local Signing Cert"   # or plain ./install.sh
tools/make-signing-cert.sh                             # one-off local signing cert
SIGN_ID="Apple Music MCP Self-Signed" make app         # build a signed .app
```

`install.sh` builds a private `0700` virtualenv from the checkout you're
standing in. Nothing is piped from the network into a shell. Plain
`./install.sh` skips the bundle and sets up the simpler unscoped stdio server.

Without the bundled MusicKit helper, a source checkout falls back to an optional
Apple Developer token (`login --dev`), which also unlocks richer catalog
metadata, charts and recommendations.

### Sharing a build you made

The [release](https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest)
is notarized, so it opens on anyone's Mac with nothing to click past. Just send
them the right one: `-arm64` for Apple Silicon, `-x86_64` for Intel.

Your own build is different. Self-signed is not notarized, macOS refuses it on
first open, and the old right-click trick no longer works. Your friend would
need:

> System Settings → Privacy & Security → scroll down → **Open Anyway**

Self-signing still earns its keep, because it gives the app a stable identity
and the Music permission survives rebuilds.

To hand yours over as cleanly as the release, sign with a **Developer ID
Application** certificate and notarize. `make release` runs the whole sequence,
including stapling the ticket into the bundle. (A zip made *before* stapling
still needs the network to validate, which is the usual reason a "notarized" app
gets refused on someone else's machine.)

```sh
SIGN_ID="Developer ID Application: Your Name (TEAMID)" make release
```

Notary credentials are documented at the top of
[`tools/notarize.sh`](tools/notarize.sh). It needs a paid Apple Developer
account ($99/yr). Nothing else about the app changes.

### Tracking upstream

MIT, © Eric Pheterson, retained in full. Forked at `0acf697`; upstream history
is preserved in git and in [CHANGELOG-upstream.md](CHANGELOG-upstream.md).

We take upstream security and correctness fixes to the modules we kept, and
ignore feature commits touching deleted subsystems. The deletions are large, so
cherry-pick by file rather than merging:

```sh
git fetch upstream --tags
git log --oneline fork-base..upstream/main -- src/applemusic_mcp/applescript.py
```

The capability tests are the safety net for a cherry-pick that would drag a
deleted capability back in.

## Uninstall

Drag the app to the Trash, then paste this into Terminal:

```sh
launchctl bootout gui/$(id -u)/io.github.jaminben.secure-applemusic-mcp
rm -f ~/Library/LaunchAgents/io.github.jaminben.secure-applemusic-mcp.plist
tccutil reset AppleEvents io.github.jaminben.secure-applemusic-mcp
```

Remove the `unofficial-apple-music` entry from Claude Desktop's config; a backup
sits next to it. To clear the cache and audit log too, delete
`~/.config/applemusic-mcp` and `~/.cache/applemusic-mcp`. From a source install:
`./install.sh --uninstall`.

> **Never give this the Accessibility permission.** It cannot use it. If
> anything asks you for it, that's a bug. Please report it.

## Security

[SECURITY.md](SECURITY.md) has the threat model, what is and isn't reachable,
and the risks that remain: the Music app itself is not sandboxed, prompt
injection is real, and this server has no sandbox of its own.

Three problems inherited from upstream are fixed here: an Apple Music link check
that could be fooled, a path-traversal guard on the `exports://` resource that
did nothing, and destructive operations that acted on a substring guess.
[CHANGELOG.md](CHANGELOG.md#fixed--inherited-security-issues) has the details
and [DISCLOSURE.md](DISCLOSURE.md) tracks how they were reported upstream.

## Questions

**Do I need an Apple Developer account, an API key, or a `.p8` file?**
No. Apple's MusicKit signs each request using the app's own signature plus your
one-time approval. There is no key to make, paste or store. The optional
developer token only raises Apple's rate limit for bulk jobs like importing a
huge playlist.

**Do I need an Apple Music subscription?**
Not for your own library, your playlists, or local playback. Yes for playing or
adding anything from Apple's catalog.

**Does it work on Windows or Linux?**
No. It drives the Mac Music app directly. Use
[upstream](https://github.com/epheterson/applemusic-mcp) if you need those.

**Which AI apps does it work with?**
Claude Desktop, Claude Code, Cursor, Windsurf, Codex and VS Code. The setup
window finds the ones you have and configures them for you.

**Does anything get stored or sent anywhere?**
No password is stored, because there isn't one. The app talks to Apple and to
nothing else. No tracking, no third-party service, no account with this project.

**What will macOS ask me for?**
Two things: permission to control the Music app, and permission to use Apple
Music. It never asks for Accessibility, never drives a browser, and never sends
fake clicks.

**How do I update it?**
Download the latest zip, replace the app, restart your AI app. The download
links above always point at the newest release.

**Is this made by Apple?**
No. It's an independent project, not affiliated with, endorsed by or supported
by Apple. "Apple Music" and "MusicKit" are Apple's trademarks.

## Other Apple Music MCP servers

Full write-up with sources in [docs/COMPARISON.md](docs/COMPARISON.md). The
short version:

| | Best for | Needs a dev account | Install |
|---|---|---|---|
| [kennethreitz/mcp-applemusic](https://github.com/kennethreitz/mcp-applemusic) | Something tiny you can read in one sitting | no | clone + edit JSON |
| [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp) | **Most features**; Windows/Linux; Up Next queue | no | pip/uvx + edit JSON |
| [Cifero74/mcp-apple-music](https://github.com/Cifero74/mcp-apple-music) | The official REST API | **yes** | wizard + edit JSON |
| **this fork** | Anyone who doesn't want to open Terminal | no | **double-click** |

Worth knowing if you're choosing: kennethreitz's server drops tool parameters
straight into AppleScript without escaping them, so a quote mark in a track or
playlist name breaks out of the string. Reported with a fix as
[issue #8](https://github.com/kennethreitz/mcp-applemusic/issues/8).

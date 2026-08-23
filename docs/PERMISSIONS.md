# Granting permission to *this server* and not to your terminal

## The problem

macOS TCC attributes an Automation grant to the **responsible process**, not to
the process that literally sent the Apple Event. When an MCP client spawns
`secure-applemusic-mcp serve` as a child, responsibility normally walks *up* the
process tree to the nearest ancestor app — your terminal, or Claude Code. So the
prompt you approve says "Terminal wants to control Music", and what you have
actually granted is:

> every program you ever run from that terminal may control Music.app

That is the opposite of scoping. Putting the executable inside a `.app` bundle
does **not**, by itself, fix it: responsibility is inherited from the spawning
process, so a bundle exec'd as a child of the terminal is still attributed to the
terminal. This is the part people usually get wrong.

**The reliable test:** trigger any Music operation and read the TCC prompt. The
app *named in the prompt* is the identity that receives the grant. If it says
your terminal's name, the scoping did not work.

## Option A — dedicated macOS user account (simplest, works today)

Log in as a second account, install and run the server there, and grant
Automation → Music in that account. TCC is per-user, so the grant cannot leak to
your main session.

Cost: that account has its own Music library, so this only suits you if the
Apple Music account you want to drive lives there (or you're happy signing in).
No code required, and nothing to keep working across macOS updates.

## Option B — app bundle launched by launchd (proper scoping)

The dependable way to break responsibility inheritance is to have **launchd**
start the process, not your terminal. launchd-started processes are responsible
for themselves, so the app bundle gets its own TCC identity and its own row in
System Settings → Privacy & Security → Automation.

That conflicts with stdio MCP, which needs the client to own the process's stdin
and stdout. The resolution is a split:

```
MCP client ──stdio──▶ shim (no permissions, no Apple Events)
                        │ unix socket, 0600, in ~/Library/Application Support/
                        ▼
                     helper  ← started by launchd from Hardened.app
                        │      holds the Automation grant
                        ▼
                     Music.app
```

The shim is a few dozen lines that copy bytes between stdio and the socket. All
Apple Events happen in the helper, which is the only thing that ever needs
permission. As a bonus the helper is a single long-lived process, so Music.app
automation stops re-prompting.

This is implemented. One command sets it all up:

```sh
./install.sh --scoped                       # or: --scoped --sign "My Cert"
```

which installs the package, builds and signs the bundle, loads the LaunchAgent,
waits for the helper to come up, and prints the client config to paste. The
pieces, if you'd rather do it by hand:

| Piece | What it is |
|---|---|
| `secure-applemusic-mcp shim` | what your MCP client spawns. Pure byte pump; imports no Apple Events code and needs no permission. |
| `secure-applemusic-mcp helper` | what launchd starts from the bundle. Owns the Automation grant; forks one session per connection. |
| `~/Library/Application Support/<bundle-id>/helper.sock` | the link between them. `0600`, inside a `0700` directory. |

Point your client at the **shim**, never the helper:

```json
{ "mcpServers": { "apple-music": {
    "command": "/Users/you/.local/bin/secure-applemusic-mcp",
    "args": ["shim"] } } }
```

Remove it all with `./install.sh --uninstall`.

One implementation note, because it cost an afternoon and will bite anyone
reimplementing this: `dup2`-ing the socket onto fds 0 and 1 is not enough. The
MCP stdio transport wraps the inherited `sys.stdin.buffer`, whose seekability
was decided at interpreter startup — and launchd gives the helper a *regular
file* for stdio, so that cached answer is "seekable". Wrapping it over a socket
then fails with `ESPIPE: Illegal seek`. The session has to rebuild
`sys.stdin`/`sys.stdout` from the socket fd.

### Signing matters more than you'd expect

TCC keys an entry on the code-signing identity, not just the bundle ID.

- **Unsigned or ad-hoc (`codesign -s -`)**: the cdhash changes on every rebuild,
  so macOS treats each build as a different program and re-prompts — or, worse,
  silently denies with a stale entry.
- **A stable identity** (a self-signed certificate from Keychain Access is enough
  for personal use; a Developer ID if you distribute it) gives a stable
  designated requirement, so the grant survives rebuilds.

```sh
codesign --force --deep --sign "My Local Signing Cert" Hardened.app
```

### Info.plist essentials

- `CFBundleIdentifier` — stable and unique; this is half the TCC key.
- `LSUIElement` = `true` — no Dock icon, no menu bar.
- `NSAppleEventsUsageDescription` — the sentence shown in the permission prompt.
  Write something honest: *"Controls Music.app to manage your library and
  playback."*

## Option C — seatbelt (`sandbox-exec`) underneath either of the above

Defense in depth, not a substitute for the above. seatbelt can filter Apple
Events *by destination*, which fits this build well:

```scheme
(version 1)
(deny default)
(allow process-fork)
(allow file-read*)                                  ; tighten to interpreter + state dirs
(allow file-write* (subpath (param "STATE")))
(allow network-outbound (remote tcp))               ; pair with an egress allowlist
(allow process-exec (literal "/usr/bin/osascript"))
(allow appleevent-send (appleevent-destination "com.apple.Music"))
```

With `(deny default)` the Music allowance is the only one, so Apple Events to
Terminal, Finder, or Safari — the classic sandbox escapes — are refused.

Caveats, stated plainly:

- `sandbox-exec` is deprecated and undocumented. Profile mistakes fail quietly.
  Verify a real Music operation still works after applying it.
- It constrains the **operations** of this process tree. It does not revoke or
  narrow the TCC grant, which belongs to the responsible app (Options A/B).
- It filters Apple Events. It does not obviously block `CGEventPost`, which
  reaches WindowServer by a different route. That does not matter for this build
  — the synthetic-input code is gone and a test keeps it gone — but do not
  assume a seatbelt profile alone would stop it in some other program.

## What "no Accessibility" buys you

Denying Accessibility is the highest-value single control, and this build is
designed so you never need it:

- System Events returns `-1743` / "not allowed assistive access", which this
  codebase already classifies as `ERROR_AUTOMATION_DENIED` and reports cleanly.
- Synthetic `CGEventPost` events are dropped by the HID filter for an untrusted
  process.

Both synthetic-input channels die at the OS layer, no profile required. Nothing
in this build asks for that permission — if something prompts you for
Accessibility, treat it as a bug and file it.


---

# Rejected: native MusicKit (Swift) instead of a developer token

Worth recording, because it is the obvious idea and it does not work on macOS.

**The appeal.** A Swift helper using Apple's `MusicKit` framework would need no
developer token in the app at all: the `com.apple.developer.musickit` entitlement
plus the user's consent replaces it, so nothing secret is ever distributed. It
would also swap the localhost browser-authorization page for a native dialog.

**Why it fails.** MusicKit on macOS is read-and-play-in-process only. Checked by
compiling against the real SDK (macOS 15.7, Swift 6.2, target macOS 14):

| API | macOS |
|---|---|
| `MusicAuthorization` | available |
| `MusicCatalogResourceRequest` (catalog reads) | available |
| `MusicLibraryRequest` (library reads) | available |
| `ApplicationMusicPlayer` (in-process player) | available |
| `SystemMusicPlayer` (drives Music.app) | **`@available(macOS, unavailable)`** |
| `MusicLibrary.shared.edit` / `.add` | **`@available(macOS, unavailable)`** |

So the one thing we wanted — add a catalog track to the library — is not
expressible in MusicKit on macOS at all. Apple gates library mutation to iOS.

`ApplicationMusicPlayer` *can* play an unowned catalog track without a token,
but it is an in-process player, not Music.app: transport (`pause`, `next`,
`now_playing`) would not reach it, reintroducing exactly the two-engine split
this fork removed with the browser player — and the process would have to stay
resident to hold the audio.

**Note for anyone re-checking this:** `swiftc -parse` does NOT evaluate
availability, so a parse-only probe compiles all of the above cleanly and looks
like a green light. Use `-typecheck` or a real build.

**What to do instead.** The REST rail (`POST /me/library` with a developer token,
then play by name over Apple Events) does the whole job in about 8 seconds and
keeps a single player. For distribution the token question is answered by a
broker, not by MusicKit — see the shipping options in the README.

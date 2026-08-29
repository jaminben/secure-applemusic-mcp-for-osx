# Releasing applemusic-mcp

Releases are automatic: pushing a **version bump** to `main` makes
`.github/workflows/release.yml` tag it and create a GitHub Release, which
dispatches `publish.yml` to push the package to PyPI and then to the [official
MCP registry](https://registry.modelcontextprotocol.io) (via GitHub OIDC, no
stored token). There is no manual release step — which means **the version bump
itself is the point of no return.**

The registry step runs after PyPI, so the registry never advertises a package
version that failed to upload. Note what it does *not* protect: by the time it
runs, the tag, the GitHub Release, and the PyPI upload have all happened. The
version-surface gate that actually guards a release is the pre-tag
`check_versions.py` in `release.yml`.

## Why there is a LOCAL gate

GitHub CI runs on Ubuntu with no Apple Music account and no tokens, so it can
only test the mocked logic. The catalog→library/playlist add, folders, move, and
ratings — the core of this tool — run as live mutations against a real,
signed-in account. **CI structurally cannot validate it.** That gap is exactly
how issue #28/#37 shipped, and how the branch-A "DELETE is broken on the public
host" bug reached a release with every unit test still green.

So the gate is local and manual, and it is **mandatory before a version bump.**

## The ritual

On a Mac signed into Apple Music with an **active subscription**, with **either**
write rail working:

- the signed MusicKit helper (`make swift` with a Developer ID, then approve it
  once via `config(action='signin')`) — no credential is stored, and this is the
  rail most users are on, so it is the one worth exercising; or
- a developer token plus a media-user-token (`secure-applemusic-mcp login --dev`),
  which is optional and only buys a larger rate-limit quota.

The lifecycle test drives Music.app over Apple Events, so the process running
pytest needs Automation → Music granted (System Settings → Privacy & Security →
Automation). Without it the test **fails** rather than skipping, on purpose.

```bash
make preflight
```

This runs, in order:

0. a version-surface check (`scripts/check_versions.py`) — the repo carries the
   version in seven places, and two of them do not follow an edit to
   `pyproject.toml`: `server.json` (nothing automates it; it once sat at `1.0.0`
   while the package shipped 0.16.0) and `uv.lock` (only updates when `uv` runs,
   so a bump can commit without it). `release.yml` runs the same check before
   tagging, so a mismatched release is blocked rather than published,
1. the fast/mocked suite (same as CI),
2. a live-environment check (`scripts/check_live_env.py`) — at least one usable
   write rail, plus catalog resolution over the public endpoint — that **fails
   loudly** instead of letting the live tests silently skip. It prints which
   rails it found, and warns when MusicKit is *not* among them, since that is
   the rail users have,
3. the live integration suite (`tests/test_live_integration.py`, `TEST_API=1`)
   against your real account — it creates and deletes a `_UI_TEST_…` playlist and
   clears the rating it sets (which is what `musickit.unrate_song` exists for).
   Fully self-cleaning — no residue.

It refuses to pass if the core live tests **skipped** rather than passed (a
half-ready environment that skips everything is a false green).

**This gate is macOS-only.** It was briefly described as cross-platform, back
when it exercised a REST rail; the lifecycle it now runs attaches tracks through
Music.app, which is the path users take.

A note on how this gate decayed, because it is the failure mode to watch for:
between `d4fc279` and v0.2.0 it could not run *at all* on any machine — the
environment check imported a module that commit had deleted, `preflight.sh`
invoked a test file the same commit had deleted, and the Makefile's interpreter
never reached the script. Every one of those is invisible until someone actually
runs it, and nothing else does. **Run `make preflight` on a change that touches
the release machinery, not only on a change you intend to release.**

## Native UI gate — `make preflight-ui` (UI-touching releases)

`make preflight` validates the API engine. It does **not** exercise the native
Music.app UI-automation paths (catalog deep-link playback via CoreGraphics, UI
catalog search, transport controls) — those are version-fragile and have no CI
coverage. Any release that changes `applescript.py` UI logic or the native
playback flow must **also** pass `make preflight-ui` on **both** support
machines:

- the **iMac** (macOS 15 / Music 1.5)
- the **mini** (macOS 26 / Music 26)

Run it from an **unlocked, active console session** (Screen Sharing's GUI login,
**not** SSH — synthetic mouse clicks need a real WindowServer session), signed
into Apple Music, with Accessibility granted. It plays **muted** and pauses after
each test. Like the API gate, it refuses to read as green if the core playback
tests **skipped** (a locked screen skips everything — that is not validation).

## Only when `make preflight` is green (plus `make preflight-ui` on both Macs for UI changes):

1. Bump the version everywhere. There are **seven** surfaces, not three:
   `pyproject.toml`, `src/applemusic_mcp/__init__.py`, the
   `secure-applemusic-mcp-for-osx` stanza in `uv.lock`, the `version:`
   frontmatter in `SKILL.md`, the `version` in `server.json`, and a
   `## [x.y.z]` heading in `CHANGELOG.md`. Don't count them by hand — run
   `python scripts/check_versions.py`, which is the same check `release.yml`
   runs before it will tag.
2. Add a `CHANGELOG.md` entry under the new version.
3. Update `SKILL.md` if any user-facing behavior changed (errors, setup,
   add/lookup flow).
4. Merge to `main`. `release.yml` tags and creates the GitHub Release; the tag
   then fires `publish.yml`, which publishes `server.json` to the MCP Registry.
5. Build, notarize and **upload the release assets** — the tag does not do this,
   and the stable-name zip is what every external link resolves to. See
   "Distribution: the app, not a package" below.

## If you can't run the live gate

Don't bump the version. A docs-only or tooling-only change that touches no
runtime behavior can skip the live gate (note that in the PR), but anything that
touches `applescript.py` or the add/resolve/playback flow in `server.py` must
pass `make preflight` first.

## Distribution: two channels

**The notarized app is the primary channel**, and the PyPI wheel is a real
second one — it carries the signed MusicKit helper, so a `uvx` install is a full
install rather than a crippled one. See "The PyPI wheel" below for what makes
that work and what to verify before publishing.

That was not always true. The helper lives inside the `.app` bundle, and
`musickit._candidates()` used to look only in a `.app` parent of
`sys.executable` or a source checkout — so a wheel had **no MusicKit rail** at
all: no catalog adds, no ratings on catalog songs, no editing an
Apple-Music-origin playlist. Publishing that would have made the weakest version
of the product the default install for everyone arriving from a directory,
because aggregators mirror whatever the registry advertises.

It was also never working. The PyPI trusted publisher was never configured, so
the `Publish to PyPI` job failed on v0.1.0, v0.1.1, v0.2.0 and v0.2.1 with
`invalid-publisher`, and the registry job sat behind `needs: publish` and never
ran at all. Nobody noticed, because the tag and the GitHub Release are created
by a *different* workflow that was succeeding.

`publish.yml` currently has one job: publish `server.json` to the MCP Registry.
The `packages` array is gone and the download URL travels in `_meta` under
`io.modelcontextprotocol.registry/publisher-provided`.

**Both of those want revisiting now the wheel is real.** Once the PyPI trusted
publisher exists, restore a build+publish job and add a `packages` entry with
`registryType: "pypi"` pointing at the wheel — that gives the registry an
installable package, which is also the leading suspicion for why Glama and
PulseMCP have not rendered the listing.

**The download link is version-independent on purpose.** Everything — the README
button, `server.json`, every directory listing — points at:

```
https://github.com/jaminben/secure-applemusic-mcp-for-osx/releases/latest/download/UnofficialAppleMusicMCP-macos-arm64.zip
```

`releases/latest/download/<stable-name>` always resolves to the newest release,
so no listing anywhere needs editing when you ship. That only holds if every
release uploads the **unversioned** asset name alongside the versioned one — see
the asset step below. Skip it and every external link silently keeps serving the
previous version.

### The PyPI wheel

The wheel carries the signed MusicKit helper beside the module, so
`uvx secure-applemusic-mcp-for-osx serve` is a full install rather than a
crippled one. That was not obvious — it is why this project shipped without
PyPI until it was tested. What makes it work:

- the helper is **148K and four files**, with the code signature in
  `_CodeSignature/CodeResources` plus the Mach-O's own `LC_CODE_SIGNATURE`, and
  the notarization ticket in `Contents/CodeResources`. All regular files, so a
  zip round trip preserves them; nothing lives in an extended attribute.
- pip preserves the executable bit, so the helper still runs.
- **it must be notarized and stapled on its own.** The outer app's ticket does
  not travel with it. Un-stapled it still passes Gatekeeper *online*, because
  Apple's notary recognises the hash — but not offline. `swift/amcp-musickit`
  is submitted separately and stapled before the wheel is built.
- the binary is **universal** (arm64 + x86_64). The app bundle can be
  single-arch because it only runs on the Mac that downloaded it; a wheel is
  resolved against a platform tag, and an arm64-only wheel silently excludes
  every Intel Mac.
- `scripts/wheel_tag.py` forces `py3-none-macosx_14_0_universal2`. Without it
  hatchling infers `py3-none-any` and pip installs it happily on Linux, where
  the helper cannot run.

Verify a built wheel before publishing — install it with `--no-deps` into a
clean venv and confirm the rail is live, not just that the files are present:

```python
from applemusic_mcp import musickit
assert musickit.is_available()
assert musickit.authorization_status() == "authorized"
ok, data = musickit.resolve_isrcs(["GBUM71029604"])   # a signed, server-validated call
assert ok
```

`xcrun stapler validate` on the installed `.app` should also still pass.

### Publishing it

### If a wrapper package ever ships

Superseded — see "The PyPI wheel" above. The wheel carries the real signed
helper rather than wrapping a download, which is better than the wrapper idea
this section used to describe.

What is still true from it: the ownership check for a PyPI package looks for
`mcp-name: io.github.jaminben/secure-applemusic-mcp-for-osx` in the package
README, which is already at README line 3. And `registryType: "mcpb"` remains
the other option the registry supports for GitHub-release artifacts — relevant
if Smithery matters, since it takes a hosted URL or an `.mcpb` and nothing else.

### Release assets

Every release must carry **four** files, all built from the *stapled* bundle —
`tools/build-app.sh --zip` produces its zip BEFORE notarization, so re-zip from
`dist/UnofficialAppleMusicMCP.app` after `tools/notarize.sh` or you ship a
download that trips Gatekeeper:

- `UnofficialAppleMusicMCP-<version>-macos-arm64.zip`
- `UnofficialAppleMusicMCP-macos-arm64.zip` — the stable name every link uses
- `UnofficialAppleMusicMCP-<version>-macos-arm64.zip.sha256`
- `SHA256SUMS.txt`

Verify the download the way a user gets it, since the quarantine bit is only set
on a real download:

```bash
xattr -w com.apple.quarantine "0083;00000000;Safari;" /tmp/copy-of.app
spctl -a -vvv -t exec /tmp/copy-of.app   # want: accepted / Notarized Developer ID
```

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
   frontmatter in `SKILL.md`, both version fields in `server.json`, and a
   `## [x.y.z]` heading in `CHANGELOG.md`. Don't count them by hand — run
   `python scripts/check_versions.py`, which is the same check `release.yml`
   runs before it will tag.
2. Add a `CHANGELOG.md` entry under the new version.
3. Update `SKILL.md` if any user-facing behavior changed (errors, setup,
   add/lookup flow).
4. Merge to `main`. The release + PyPI publish fire automatically. Watch the
   `publish` run — see the section below, because it has never yet succeeded.

## If you can't run the live gate

Don't bump the version. A docs-only or tooling-only change that touches no
runtime behavior can skip the live gate (note that in the PR), but anything that
touches `applescript.py` or the add/resolve/playback flow in `server.py` must
pass `make preflight` first.

## PyPI publishing is not set up yet

**As of v0.2.0 the package has never been uploaded to PyPI.** All three
`publish` runs — v0.1.0, v0.1.1, v0.2.0 — failed identically, and
`https://pypi.org/pypi/secure-applemusic-mcp-for-osx/json` still returns 404.
The tag and the GitHub Release are created fine; only the upload fails:

```
Trusted publishing exchange failure:
  * `invalid-publisher`: valid token, but no corresponding publisher
```

That is a PyPI-side configuration gap, not a workflow bug. Nothing in this
repo can fix it — a maintainer has to add the publisher on PyPI once.

**The one-time fix.** Because the project does not exist on PyPI yet, this must
be a *pending* publisher (PyPI's flow for a name that has never been uploaded);
a regular trusted publisher cannot be added to a project that isn't there. At
<https://pypi.org/manage/account/publishing/>, add:

| Field | Value |
|---|---|
| PyPI project name | `secure-applemusic-mcp-for-osx` |
| Owner | `jaminben` |
| Repository | `secure-applemusic-mcp-for-osx` |
| Workflow name | `publish.yml` |
| Environment | `pypi` |

These are the exact claims the failing run presents; the workflow does not need
to change to match them.

**Then re-publish without re-tagging.** `publish.yml` has `workflow_dispatch`,
so a failed upload does not burn the version:

```bash
gh workflow run publish.yml --repo jaminben/secure-applemusic-mcp-for-osx
```

It rebuilds from `main` and uploads whatever version is there. This works
because PyPI never received a file — a version is only burned once an upload
*succeeds*, since PyPI refuses to replace an existing artifact. So a failed
publish is recoverable; a *successful* publish of a bad build is not.

**Order matters and is deliberate.** The registry step runs after PyPI and is
skipped when PyPI fails, so the MCP registry never advertises a version PyPI
does not have. What it does not protect: by the time `publish` runs, the tag
and the GitHub Release already exist. That asymmetry is why the local
`make preflight` gate is the one that matters — see the top of this file.

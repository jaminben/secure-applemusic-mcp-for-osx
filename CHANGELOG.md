# Changelog

All notable changes to this fork are documented here. For history before the
fork point, see [CHANGELOG-upstream.md](CHANGELOG-upstream.md).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-08-25

### Fixed

- **The setup wizard promised a step it might not have.** The splash listed one
  bullet per following page but hardcoded four, while the "Play anything on
  Apple Music" page only exists when the MusicKit helper is built. Any build
  without it advertised a step the wizard never delivered.
- **The Codex/TOML client config crashed on Python 3.10.** `clients.py` imported
  `tomllib`, which is stdlib only from 3.11, while the package advertises
  `>=3.10`. Added the `tomli` backport, scoped to `python_version < "3.11"`.
- **The server advertised an empty version to clients.** The version was written
  to the mcp 1.x attribute inside a bare `except`, so on the 2.x SDK the app
  bundles it silently failed and `serverInfo.version` shipped as `""`.
- **The permission primer never asked for permission.** It read the application's
  `name`, which AppleScript answers from the app bundle without sending an Apple
  Event -- so it exited 0 without consulting TCC, on every machine, and reported
  success. It now reads `player state`, a real Apple Event.
- **Recently-played came from the API, which cannot see local playback.** Reads
  Music.app's own played dates instead, falling back to the API.

### Changed

- The MCP entry is now `unofficial-apple-music` in client configs, with a
  migration that removes the superseded `apple-music` key when it points at our
  own binary.
- The app bundle is `UnofficialAppleMusicMCP.app`. The bundle identifier is
  unchanged, so existing Automation grants survive.
- The build no longer embeds the builder's home directory (pip's
  `direct_url.json` and CPython's `_sysconfigdata_`), and fails if any trace
  remains.
- A Developer ID build now refuses to package without its Swift helpers, rather
  than shipping with catalog playback silently missing.
- Coverage is measured on macOS, where the whole suite runs, instead of on the
  Linux matrix where 144 tests skip by construction.

## [0.1.0] - 2026-08-22

### Added — update checking, with a security-advisory escalation

- **`update-check` command and a once-a-day background check.** Asks GitHub for
  the latest release and for published security advisories, compares against the
  installed version, and reports. It never downloads, installs, or opens a URL:
  this process holds the Automation grant, so applying an update automatically
  would turn a compromised release channel into code execution with Music
  control.
- **Advisories are not suppressible.** A routine update notice is shown once per
  version so a daily check can't become a daily nag; an advisory whose affected
  range covers the running version re-notifies until the install is out of range.
  Ranges that can't be parsed fail closed — reported as "not affected", because a
  false alarm trains people to ignore the real one.
- **Everything the API returns is treated as hostile.** Tags are validated against
  a version shape (with length-capped components) rather than escaped, URLs must
  live under this repo, and free text is stripped of control characters and capped
  before reaching a terminal or a notification.
- **Runs in the helper**, forked so a slow lookup never stalls `accept()`, and
  triggered by a client connecting rather than by a timer of its own. Notifications
  go through the single existing `osascript` call site, keeping the
  process-execution surface at one module.
- Opt out with `APPLEMUSIC_MCP_NO_UPDATE_CHECK=1`. `status` always reports the
  last result, including one already dismissed. Documented in SECURITY.md.

### Added — standalone app and scoped permissions

- **`AppleMusicMCP.app`** — a self-contained bundle with its own vendored Python
  (`tools/build-app.sh`, `make app`). No Python, Homebrew, or command line
  needed: unzip, drag to /Applications, double-click once. Relocatable by
  construction — no venv and no absolute paths are baked in.
- **First-run setup asks before each step** and each is skippable: install the
  background helper, add the Claude Desktop entry, request the Music permission.
  The Claude config is *merged* (other servers preserved), backed up first,
  written atomically, and its mode preserved; an unparseable config is left
  untouched rather than overwritten. A dialog that fails to display reads as
  "skip", never as consent.
- **Scoped permission transport** — `shim` (what the client spawns; no
  permissions, no Apple Events) talking over a `0600` unix socket to `helper`
  (started by launchd from the bundle; owns the Automation grant). This is what
  stops the grant landing on your terminal. See
  [docs/PERMISSIONS.md](docs/PERMISSIONS.md).
- `install.sh` for source installs (private `0700` virtualenv, `--scoped`,
  `--uninstall`), and `make release` producing wheel, sdist, signed `.app`, zip
  and `SHA256SUMS.txt`.
- `tools/make-signing-cert.sh` — creates a self-signed code-signing certificate
  in its own keychain, so builds are signed non-interactively. macOS keys the
  Automation grant on the signing identity, so a stable certificate is what
  stops the permission being re-prompted after every rebuild. (Self-signed is
  not notarized: someone else downloading the app still has to allow it in
  System Settings → Privacy & Security.)
- [docs/COMPARISON.md](docs/COMPARISON.md) — an honest comparison against the
  three most-starred Apple Music MCP servers, including where each is the
  better choice than this one.

Forked from [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp)
at `0acf697` (v0.18.5+). The goal of the fork is a smaller *capability* surface,
not more features: everything that let the server reach beyond Apple Music has
been removed, and the removals are enforced by tests rather than by convention.

### Removed — capabilities

- **Accessibility / UI automation.** All 31 functions that drove Music.app
  through `System Events` (synthetic keystrokes, AX-tree walks, menu clicks) and
  the four JXA helpers that posted synthetic mouse events via `CGEventPost` to
  the HID event tap. Accessibility is system-wide synthetic input and cannot be
  scoped to one app; it was the broadest permission the project required.
- **Browser automation.** `browser.py` (Playwright/Chrome), launched with real
  Keychain access and extensions enabled so Touch ID sign-in worked — a full
  browser-automation handle held in-process.
- **Safari Apple-Events JavaScript.** `safari.py` and `safari_player.py`, which
  read `document.cookie` from the user's signed-in Safari to harvest the
  `media-user-token`. This required "Allow JavaScript from Apple Events", which
  is not scoped to music.apple.com — it grants JS execution in *every* Safari
  tab, making it broader than Accessibility for data theft.
- **The unofficial amp-api rail.** `amp_api.py` talked to
  `amp-api.music.apple.com` using the `AMPWebPlay` token scraped out of Apple's
  web-player JS bundle plus the harvested cookie.
- **URL handoff to the OS.** No code path passes a URL to `open` or
  `webbrowser`. `playback(url=)` now *parses* a URL for its catalog id and never
  fetches, opens, or navigates to it.
- **The `queue` tool.** Up Next is state inside the web player's MusicKit
  instance; it cannot exist without the web player.
- **`keyring`, `playwright`, `pyobjc-framework-Quartz` dependencies.** Runtime
  dependencies drop from 7 to 4 (`mcp`, `pyjwt`, `requests`, `cryptography`).

### Fixed — inherited security issues

- **Apple Music URL validation was bypassable.** `open_catalog_song` used
  `startswith("https://music.apple.com")`, which accepts
  `https://music.apple.com.attacker.tld/…` and
  `https://music.apple.com@attacker.tld/…`, then passed the URL to `open`.
  Replaced with strict `urlparse` hostname matching, https-only, parse-only.
- **`exports://` path-traversal guard was inert.** `is_relative_to` on an
  unresolved path returns True for `cache/../../x`, and the check ran *after*
  `.exists()`. Now resolves both sides and rejects absolute or `..` components.
- **Destructive operations acted on a substring guess.** `delete_playlist`,
  `rename_playlist`, and `remove_from_library` used `name contains` and took the
  *first* match, so removing "Love" could permanently delete an unrelated track.
  They now refuse when the name matches more than one item and list the
  candidates. (Upstream's web rail already did this; the native rail never did,
  so keeping only the native rail would have kept only the unsafe behaviour.)
- **`clean_only` silently lost its verification signal in CSV and exported
  JSON.** The note is suppressed for those formats on the grounds that each row
  carries `explicit`, but the CSV field list and the JSON export key set both
  dropped the column — so a clean-filtered export read as vetted while
  containing unverified tracks. `explicit` is now carried in both.
- **State directories outside the config dir were world-readable.** The cache
  root holds the audit log, library snapshots, and library exports, created
  0755/0644. All state roots are now 0700.
- **`storefront` was unvalidated** and interpolated into API URL paths; now
  constrained to a two-letter country code.

### Added

- `tests/test_capability_invariants.py` — 50 tests asserting the removals hold:
  no forbidden capability appears in code (comments and docstrings may still
  *name* one to explain the removal), `subprocess` is confined to one module and
  one binary (`osascript`, argv list, always with a timeout), the removed
  modules stay unimportable, the MCP tool inventory is locked to six tools,
  hostile URLs are rejected, the export reader cannot escape its directory, and
  state dirs are 0700.
- `rate_limit.py` — the credential-free remnant of `amp_api`: the sticky-429
  marker that lets an empty result be reported as "rate limited" rather than
  "no such song".
- Tokenless catalog search via Apple's public iTunes Search API, replacing the
  UI-scraping fallback. No credential, no Accessibility, and it carries an
  explicit flag so `clean_only` is verified rather than assumed.

### Changed

- macOS-only. Off-macOS code paths return a clear error instead of routing to a
  removed rail.
- One playback engine (`native`). A request for a removed engine
  (`safari`/`chrome`/`web`/`api`) is refused, never silently downgraded — a
  caller must never believe it is driving an isolated browser session when it is
  driving the real library.
- Default install stores **no credentials at all**. Library, playlists, ratings
  and playback need none. A developer token (`login --dev`) is opt-in and only
  required to add a catalog track you don't already own.
- `login --dev` prints the authorization URL instead of opening a browser.

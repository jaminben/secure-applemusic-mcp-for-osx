# Changelog

All notable changes to this fork are documented here. For history before the
fork point, see [CHANGELOG-upstream.md](CHANGELOG-upstream.md).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-08-28

### Added

- **Intel Macs are supported.** Releases now ship two apps —
  `-macos-arm64.zip` and `-macos-x86_64.zip` — and both nested Swift helpers are
  universal binaries. There is no single universal app: the bundle vendors a
  per-architecture CPython and uv publishes no universal2 build, so one bundle
  would have to carry two Pythons.
- **A PyPI wheel that actually works.** It carries the signed, notarized
  MusicKit helper beside the module, so `pipx install` is a full install rather
  than one missing its main rail. This was previously believed impossible; it
  turns out the helper is 148K and four files, with its signature and
  notarization ticket all in regular files, so it survives a wheel intact.
  Verified on both architectures with a live, Apple-validated API call.
- **A landing page** at <https://jaminben.github.io/secure-applemusic-mcp-for-osx/>
  for people who want to know what it does rather than evaluate the source.

### Fixed

- **Intel installs were broken by an upstream dependency, on both channels.**
  `mcp` requires `pyjwt[crypto]`, which pulls `cryptography`, and cryptography
  stopped publishing Intel macOS wheels at version 49 — so any Intel install
  tried to compile Rust from source and failed. Pinned by marker on Intel only,
  so Apple Silicon keeps current upstream.
- The `jwt` import is now lazy and `pyjwt`/`cryptography` moved to a `dev-token`
  extra. It is used at exactly one line, on the optional developer-token rail,
  and a module-level import made it a hard requirement of every install.

### Changed

- **One command builds and ships a release.** `make release-assets` builds both
  architectures, notarizes and staples each, zips from the *stapled* bundle,
  builds the wheel with its own notarized helper, writes checksums, and can
  attach it all to the tag. It ends by setting the quarantine bit on each zip
  and asking Gatekeeper — the check that catches a zip made before stapling,
  which is a mistake this project had made in every release to date.
- `docs/COMPARISON.md` corrected: Cifero74 *can* put a catalog track in your
  library, via `add_tracks_to_playlist` with `track_type: "songs"` — the same
  mechanism this fork uses. The differentiator is unchanged and narrower than
  the obvious claim: this is the only one that does it with no credential of
  any kind.
- The README leads with a real conversation instead of the installer window, and
  documents pip/pipx/uvx for the first time.

## [0.2.2] - 2026-08-28

### Changed

- **"What is this signed in to?" now answers "nothing".** That is the true
  answer and the point of the project, and it was something you had to infer.
  The reply opened with two lines of token bookkeeping about credentials you do
  not have and do not need, then a rail report, a mode, an engine list and a
  writes line — every line accurate, the whole thing answering a different
  question. When there is genuinely no credential and the signed helper works,
  it now says so and stops. The full breakdown still runs for every other
  configuration, where someone is debugging and wants it.

### Fixed

- **Status contradicted itself four lines apart.** A real response reported
  `Catalog add: OK (MusicKit — no credential stored)` and, four lines below,
  `Adding catalog tracks needs sign-in`. The summary line gated on developer
  tokens alone, so every user of the packaged app was told to sign in for
  something the line above reports as working. Fourth site with that bug, and
  the worst placed — status is what someone reads when already confused.

### Distribution

- **The registry entry describes the app, not a PyPI package.** `server.json`
  advertised a `uvx` install, which produces a build with no MusicKit rail: the
  signed helper lives inside the `.app`, and a wheel does not carry it. That
  would have made the weakest build the default install for anyone arriving
  from a directory. The `packages` array is gone; the download, checksums,
  requirements and privacy posture travel in `_meta` instead, and `publish.yml`
  no longer waits on a PyPI job that had never once succeeded. A wrapper package
  that downloads or embeds the signed app remains a sensible future release and
  is written up in RELEASING.md.
- Adds `title`, `websiteUrl` and `icons`, and cuts the description to fit the
  registry's 100-character cap — the old one was 167, so the publish would have
  been rejected on validation even after PyPI was fixed.

### Documentation

- A real conversation is now the README hero: one request for a child-appropriate
  birthday playlist, and the result playing in Apple Music with the explicit
  versions skipped. The installer window moves to the Install section.
- `COMPARISON.md` had this fork needing a token to add to your library, which
  stopped being true at 0.2.0. Corrected, and the credential cost of each
  project's library add is now a headline row.
- `docs/LISTING-COPY.md`: one source for directory submission copy.
- GitHub repo description, topics and homepage were unset or inherited from
  upstream — the description claimed Windows and Linux support this fork does
  not have.

## [0.2.1] - 2026-08-28

### Fixed

- **The direct library add refused a rail it could see.** `config(action='status')`
  reported catalog adds as ready while `library(action='add')` answered "this
  build ships no MusicKit helper" — both from the same process, seconds apart.
  Reported from the field against 0.2.0.

  `_library_add` was the one site converted in 0.2.0 that got the new *message*
  without the new *routing*: its error text pointed at `config(action='signin')`
  while its gate still asked only about developer tokens, so a MusicKit-only
  host was refused. It now checks both rails like the other sites, and its adds
  go through the rail-aware helpers instead of calling the token rail directly.
- **The setup hint could deny a helper it could see.** `_musickit_setup_hint`
  looked the authorization status up in a dict whose default was "this build
  ships no MusicKit helper" — and `authorized` was not a key, so an authorized
  helper produced the one message guaranteed to be wrong. "No helper" is now
  reachable only when there is genuinely no helper, and an unrecognised status
  names itself rather than being guessed at.
- **Two silent false negatives in catalog search.** `_search_catalog_songs` and
  `_search_catalog_albums` called `get_headers()` inside a bare `except` that
  returned `[]`, which every caller reads as "not in the catalog". On a host
  with no developer token that turned "you have no token" into "that song does
  not exist", with no error surfaced. Both take the public rail first now, so
  `library(action='add', track=…)` and `album=…` work without a credential.

## [0.2.0] - 2026-08-27

### Fixed

- **A fully-capable machine was told to go get a credential it did not need.**
  Adding a catalog track to a playlist, playing a catalog track you don't own,
  adding by album, and adding by catalog ID all gated on "is a developer token
  configured?" — while every step below those gates already had a credential-free
  rail: the public iTunes endpoints for catalog reads, the signed MusicKit helper
  for the library write, and Apple Events for the attach. A notarized bundle with
  MusicKit authorized could do the work and was refused. Reported by a user who
  hit it creating a playlist.
- **The advice those refusals gave could not be followed.** They named
  `applemusic-mcp login --dev`; the console script this package installs is
  `secure-applemusic-mcp`, so pasting it gives "command not found". Several
  offered a choice between two identical commands — the fossil of a web-login /
  developer-login pair whose first half was removed. Error paths now point at
  `config(action='signin')`, which shows the native Apple Music prompt, stores
  nothing, and needs no developer account. No error path sends anyone to a shell
  to authenticate.
- **`APPLEMUSIC_FORCE_TOKENLESS=1` did not stop every write.** It is documented
  as disabling every API write, and status blames it by name so nobody
  misdiagnoses it as missing auth. The MusicKit rails gated on "is the helper
  binary on disk", which honoured neither that switch nor the user's Apple Music
  consent. Found by security review of this change.
- **The test suite could mutate a real Apple Music account.** `is_available()`
  reported whether the developer's checkout happened to contain a built, signed,
  authorized helper, so on such a machine tests fell through to live, signed
  calls. Apple returns HTTP 200 for a rating on a nonexistent id, so nothing
  failed loudly. The helper now defaults to absent in tests.

### Added

- **A public `lookup` rail to match the existing `search` one.** `search` answers
  "what is called this?"; `lookup` answers "what IS this id?". Only the first was
  wired up, so paths needing the second declared a developer token mandatory —
  for catalog facts Apple serves to anyone. This covers album tracklists and
  catalog-id resolution. Play-by-catalog-id previously full-text-searched for the
  id's digits, which is a different question that occasionally answers the right
  one.
- **Four MusicKit helper verbs**: `add-album` (the helper hardcoded `ids[songs]`,
  which is why album adds still needed a token), `rate`, `playlist-add`, and
  `isrc` — the last being the one query no public Apple endpoint answers, since
  the iTunes Search API has no ISRC filter. Every identifier is validated in
  Python and again in Swift before it reaches a URL.

- **Apple-Music-origin playlists can now be edited with no credential.** This is
  the one operation with no AppleScript equivalent at all — Music.app edits only
  the playlists it owns — so `_playlist_add`'s API mode (reached by passing an
  explicit `p.` id) previously required a developer token outright. It now has a
  MusicKit rail, including the duplicate check: `_get_playlist_track_names` grew
  a second rail rather than letting the tokenless path skip the check, because
  silently stacking copies of a track is a bug this codebase has already paid
  for once.

- **A library id was the last thing that genuinely needed a token.** `i.` ids
  name rows in the user's own library, which no public endpoint can read — but
  MusicKit reads them, so this was a missing verb, not a missing capability.
  Passing a library id to `playlist(action='add')` now works with no credential.
- **The release gate had not run since `d4fc279`.** That commit removed the
  amp-api rail, but left `scripts/check_live_env.py` importing the deleted
  module and `scripts/preflight.sh` invoking a deleted test file. `make
  preflight` died with an ImportError at step 3 on every machine, tokens or
  not — while RELEASING.md kept calling it mandatory. Both are rebuilt around
  the MusicKit rail: the gate now passes if EITHER write rail works, and a
  machine with only MusicKit is a valid release machine (it is the
  configuration most users are on).

### Added

- **`unrate`**, so the gate can leave no residue. Without a way to remove a
  rating, "exercise the rating path" and "leave the account as you found it"
  were mutually exclusive.
- **`library-song`** (resolve an `i.` id) and **`catalog-search`** (Apple's own
  search, consulted only when the free public index comes up empty, so the
  process launch is not paid on every query).

### Changed

- **The API-mode playlist add stopped doing unnecessary work.** It POSTed to
  `/me/library` and then polled `/me/library/search` up to ten times to recover
  a library id the playlist endpoint never needed — a write plus up to ten reads
  per track, reporting "could not find it in library after adding" whenever
  iCloud was slower than one second. A catalog song attaches directly as type
  `songs`, which adds it to the library implicitly; that is what
  `_auto_search_and_add_to_playlist` has always done for these playlists.

### Known gaps

- Ratings and album adds still fall back to the token rail when MusicKit refuses.
- `test_full_mutation_lifecycle` needs Music.app Automation permission for the
  process running pytest, so the gate must be run from an unlocked console
  session — as RELEASING.md already requires for `preflight-ui`.

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

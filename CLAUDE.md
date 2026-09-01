# Working in this repo

A hardened, macOS-only fork of `epheterson/applemusic-mcp`, shipped as a
notarized `.app` and a PyPI wheel. The fork's reason to exist is a **smaller
capability surface**, so anything that widens it is a regression, not a feature.

## Tracking downloads and traffic

**Whenever you pull download, traffic, or listing stats, record them:**

```sh
make stats          # collect and append to stats/downloads.csv
make stats-show     # print the trend
```

Do not read these numbers out of an ad-hoc `gh api` or `curl` call and let them
evaporate into the transcript. Run the collector, then quote from what it wrote.
If the user asks for a number the collector does not yet gather, add it to
`scripts/collect_stats.py` rather than fetching it by hand.

Why: **GitHub's traffic API keeps only 14 days.** Views and clones older than a
fortnight are unrecoverable — no export, no archive. Per-asset release counts
never expire but are only ever exposed as a running total, so "how did 0.3.1 do
in its first week" is unanswerable unless something wrote it down at the time.

`stats/downloads.csv` is long format — `date,source,metric,value` — because the
metric set grows with every release. Re-running replaces only the `(date,
source)` pairs it actually collected, so a later run that hits a rate limit
cannot delete what an earlier successful run recorded, and hand-added rows
survive.

Two rows did not come from the collector and are worth knowing about:

- **2026-08-30** was reconstructed from a session transcript after the fact,
  dated by bracketing commits `df90a00` (08-29 23:14) and `ff2efe8`
  (08-30 22:31). Its per-asset counts sum to its recorded total, which is the
  check that says the reconstruction is internally consistent.
- **`source=pepy`** is a page scrape, not an API read. pepy counts mirror
  traffic and pypistats does not, which is why they disagree by roughly 5x.
  They are kept as separate sources so the two are never averaged together.

### The one rule that matters

**A source that cannot be reached records nothing. It never records 0.**

Unavailable sources are the normal case here, not an exception:

| Source | Typical failure |
|---|---|
| `pypistats.org` | HTTP 429; it rate-limits aggressively and stays limited for a while |
| MCP Registry | refuses connections outright for minutes at a time (`curl` exit 7) |
| `pepy.tech` API | 401 without an API key |

A `0` written for a failed fetch is indistinguishable, a month later, from a
real collapse in downloads. In `make stats-show`, `-` means unreachable and `0`
means zero. Keep that distinction intact in anything you add.

### Reading the numbers honestly

Most traffic to this repo is automated. At ~100 unique cloners against a
single-digit number of unique human visitors and 0 stars, the clone count is
crawlers and directory indexers. First-day PyPI counts are dominated by mirrors,
and pepy and pypistats disagree because pepy includes mirror traffic. Say so
rather than presenting a big number as adoption.

## Release facts worth not re-deriving

- **Minimum macOS is 12.0**, not 14. `LSMinimumSystemVersion` is 12.0 and only
  the MusicKit helper targets 14.0 — so the app runs on Monterey, and it is
  *catalog playback and library adds* that need Sonoma. This was stated wrongly
  in three places at once; check README, `docs/index.html` and `server.json`
  together when it changes.
- **Both architectures ship.** arm64 and x86_64 since 0.3.0. Any download link
  that offers only one is a bug.
- **The unversioned asset names carry all the traffic.** README, landing page
  and every directory listing resolve through
  `releases/latest/download/UnofficialAppleMusicMCP-macos-<arch>.zip`. Uploading
  only the versioned names leaves every one of those links serving the previous
  release, silently.
- **`server.json` only reaches directories on publish.** Editing it in git
  changes nothing downstream until a new version goes to the MCP Registry.
- Releases go through `tools/release-assets.sh` (`make release-assets`), which
  encodes the ordering traps — notarize before zipping, stage each arch
  separately, staple with absolute paths. Read its header before changing it.

## Directory submissions

Live: PyPI, MCP Registry (`isLatest`, `packages=1`), mcpservers.org (submitted).

Blocked and *why* — do not retry these without asking:

- **LobeHub** — its CLI requires a GitHub OAuth grant with `repo` and `workflow`
  scope: read/write to every repository, plus the ability to modify Actions
  workflows. Ben declined this, deliberately. `lhm.plugin.json` is fixed and
  ready if that ever changes.
- **Glama** — also requires GitHub OAuth to prove write access. Scope unknown;
  its sign-in is JS-rendered and could not be inspected headlessly. Glama claims
  to ingest the official registry as "a superset", so it may list the project
  without any grant. Check before spending one.
- **PulseMCP** — submissions paused, no form exists. Their stated path is the
  official registry, which is already done.

## Verification habits

This project has burned several hours on checks that reported success without
testing anything. Two real examples worth not repeating:

- `codesign ... | head -2 && echo valid` — the `&&` chains off `head`, so it
  printed "valid" unconditionally.
- A Gatekeeper check ending in `|| true`, which passed on an unquarantined copy
  and therefore always passed.

When you write a check, make it fail first and confirm you saw it fail. And do
not verify a signed bundle by running Python from inside it: that writes
`__pycache__` into the bundle and breaks the seal (`codesign` then reports
"file added"), and deleting those files breaks it the other way.

Do not probe a local OAuth callback port to see if a login flow is alive. The
CLI accepts the probe *as* the callback, finds no `code`, and exits — killing a
session the user already approved in the browser.

## Tests

```sh
make test        # fast, mocked, what CI runs
make invariants  # the capability invariants — the fork's reason to exist
make preflight   # pre-release gate: fast + live env + live API
```

`tests/test_capability_invariants.py` fails the build if a removed capability
(Accessibility, browser automation, shell execution, stored credentials) creeps
back. It is not an ordinary test file — treat a failure there as a design
question, never as a test to update.

Live tests touch a **real Apple Music account**. `tests/conftest.py` has an
autouse fixture that hides the MusicKit helper by default; do not weaken it. A
test once wrote a real rating to Ben's library because a mocked path fell
through to the live one.

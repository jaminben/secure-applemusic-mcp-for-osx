# Security Policy

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/jaminben/secure-applemusic-mcp-for-osx/security/advisories/new).
If the issue also affects [upstream](https://github.com/epheterson/applemusic-mcp),
please tell them too — this fork exists to reduce capability, not to hoard fixes.

## What this is, plainly

A **local stdio MCP server** that drives the Music.app on your Mac through Apple
Events. It is **not sandboxed**. It runs as your user, with your privileges, and
can do anything your user account can do. What limits it is the shape of its tool
API and the macOS permission you grant it — not a sandbox.

Do not read the repository name as a claim that it has been audited or is free of
vulnerabilities. The claim it does make is narrower and checkable: **a smaller set
of capabilities than upstream**, with the removals enforced by tests
(`tests/test_capability_invariants.py`) rather than by convention.

## What it can reach

| Capability | Status |
|---|---|
| Apple Events → **Music.app** | **Required.** The one permission this build needs. |
| Apple Events → System Events (Accessibility) | **Removed.** No synthetic keystrokes or clicks. |
| Apple Events → Safari (`do JavaScript`) | **Removed.** No reading of browser cookies. |
| Browser automation (Playwright/Chrome) | **Removed.** |
| Handing a URL to `open` / `webbrowser` | **Removed.** URLs are parsed, never opened. |
| Process execution | One binary: `osascript`, argv list, 30s timeout, in one module. |
| Shell | None. No `shell=True`, no `do shell script`, no `eval`/`exec`. |
| Network | `api.music.apple.com` and `itunes.apple.com` only, HTTPS, with timeouts. |
| Filesystem writes | Three state dirs under `$HOME`, all `0700`. |
| Filesystem reads | The export dir, via a resolved-path containment check. |

## Credentials

**The default install stores nothing.** Library, playlists, ratings, playback and
catalog search all work with no credential: local operations go through Apple
Events, and catalog search uses Apple's public iTunes Search API.

A credential is needed for exactly one thing — adding a catalog track you don't
already own to your library. That is opt-in via an **official Apple Developer
token** (`secure-applemusic-mcp login --dev`), stored in `0600` files created
atomically inside the `0700` config dir. Your `.p8` signing key stays where you
put it and is never copied.

Upstream's credential-harvesting paths — reading `media-user-token` out of your
Safari cookies, and scraping the `AMPWebPlay` token from Apple's web-player JS
bundle — are gone, along with the unofficial `amp-api.music.apple.com` rail they
fed. There is no unofficial rail to silently fall back to.

## Granting permission correctly

macOS attributes a TCC grant to the **responsible process**, which for a CLI
spawned by an MCP client is usually the *client* (your terminal, or Claude Code)
— not this server. So granting "Automation → Music" the naive way gives that
permission to everything you run from that terminal.

- **Do not grant Accessibility.** This build cannot use it; if something asks,
  something is wrong.
- Grant **Automation → Music only**, and deny prompts for Safari, Finder, or
  System Events.
- For a scoped grant, run the server from its own app bundle or a dedicated
  macOS user account so TCC attributes the permission to *it*. See
  [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## Known residual risks

Honest limits, not a clean bill of health:

- **Music.app is an unsandboxed deputy.** Its scripting dictionary includes
  `open location`, `export`, `convert`, and `save`. This server never calls
  those, but the Apple Events channel itself can express them.
- **Prompt injection.** Track names, album titles and playlist descriptions are
  attacker-influenceable text that flows into the model's context, and the model
  holds tools that delete playlists and remove library tracks. Destructive
  operations refuse ambiguous matches, but they are not otherwise confirmed by
  the server — your MCP client's approval prompt is the gate.
- **No sandbox.** Any code-execution bug here or in a dependency (`mcp`,
  `requests`, `pyjwt`, `cryptography`) inherits the process's full privileges
  and its Automation grant.
- **Destructive operations are irreversible.** The audit log records what
  happened; it does not undo it.

## Differences from upstream's SECURITY.md

Upstream's document had drifted: it described a `secure_storage` preference that
does not exist in the code, and stated that values passed to the browser are
never string-interpolated into a script body — true for its Playwright path, but
its Safari engine did interpolate JS into an AppleScript literal (safely, but the
claim did not describe that path). Both statements are moot here since both
subsystems are gone, but they are worth naming: a security document that
describes a control users cannot actually enable is itself a risk.

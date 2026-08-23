# Upstream disclosure status

This fork fixes three security issues that also affect
[epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp).
Publishing fixes for someone else's unreported vulnerabilities is a disclosure
event whether or not it's framed as one, so the status is tracked here.

**Status: REPORTED 2026-08-23, awaiting maintainer response.**
Private advisory: [GHSA-82fm-fh3q-54fj](https://github.com/epheterson/applemusic-mcp/security/advisories/GHSA-82fm-fh3q-54fj)
(state: triage). All three findings plus the two lower-severity notes were filed
together, with an offer to split them or send PRs.

**Do not push the `hardened` branch or make this repository's fixes public until
the maintainer has had a reasonable window to respond.** Filing the report
started the clock; it did not end it.

## Reported (2026-08-23, GHSA-82fm-fh3q-54fj)

- [x] **Apple Music URL validation is bypassable** (`applescript.py:1707`,
      `open_catalog_song`). `startswith("https://music.apple.com")` accepts
      `https://music.apple.com.attacker.tld/…` and
      `https://music.apple.com@attacker.tld/…`; the URL is then passed to
      `subprocess.run(["open", …])`. Reachable from the `playback` tool via
      `playback(action="play", url=…)`. `browser.py:640` and
      `safari_player.py:255` validate correctly with `urlparse().hostname` — the
      native path was missed. Impact: a prompt-injected model gets an outbound
      channel (track metadata is attacker-influenceable text that reaches the
      model's context).
      **Fix:** parse with `urlparse`, require `hostname == "music.apple.com"` or
      a `.music.apple.com` suffix; apply to the `music://` branch too.

- [x] **`exports://{filename}` traversal guard is ineffective**
      (`server.py:911`). `is_relative_to` on an unresolved path returns `True`
      for `cache_dir/../../x`, and the check runs *after* `.exists()`. Currently
      mitigated only by the MCP SDK's URI-template matching (`[^/]+`), which is
      an external control, and the branch is marked `pragma: no cover` as
      unreachable — so a change in the SDK would go unnoticed.
      **Fix:** `.resolve()` both sides before comparing; reject absolute paths
      and `..` components up front.

- [x] **Destructive operations act on a substring guess.**
      `_track_filter_clause` (`applescript.py:201`) builds `whose name contains`,
      and `remove_from_library` (`applescript.py:1461`) deletes the *first*
      match — permanently. `_find_playlist_applescript` (`applescript.py:112`)
      does the same for `delete_playlist` / `rename_playlist`. So
      `library(action="remove", track="Love")` can delete an unrelated song and
      `playlist(action="delete", name="Work")` can delete "Workout". Neither
      takes a `confirm` or `dry_run` (unlike `playlist(action="add")`).
      Notably, upstream's **web** rail already refuses an ambiguous delete and
      lists the candidates (`_playlist_delete_api`) — only the native rail is
      unsafe, so this is an inconsistency as much as a bug.
      **Fix:** exact-match first; enumerate and refuse when more than one
      matches, listing them.

Two lower-severity items worth mentioning in the same report (not
vulnerabilities, but they weaken stated guarantees):

- [x] `clean_only` silently loses its verification signal in CSV and exported
      JSON — the "could not be verified" note is suppressed for those formats
      because rows are supposed to carry `explicit`, but the CSV field list and
      the JSON export key set both omit the column.
- [x] `SECURITY.md` documents a `secure_storage` preference that does not exist
      in the code, so a user following it believes they've hardened token
      storage and hasn't.

## Next

- [x] Note the advisory link / date here
- [ ] Give upstream a reasonable window (90 days is the usual default — i.e. by
      **2026-11-21** — shorter is fine if they respond or ship a fix sooner)
- [ ] Then push `hardened` and make the fork's fixes public
- [ ] Offer the fixes upstream as PRs where they apply cleanly — the URL
      validator and the traversal guard both port over as-is, and the ambiguity
      guard is a port of upstream's own web-rail behaviour to the native rail


---

# Other projects

## kennethreitz/mcp-applemusic

Reported **2026-08-23** as
[issue #8](https://github.com/kennethreitz/mcp-applemusic/issues/8).

That repo has no `SECURITY.md` and private vulnerability reporting is disabled,
so there was no private channel to use. Standard practice in that situation is a
minimal public issue that describes the class of problem and its impact, omits a
working payload, and asks the maintainer to open a private channel — which is
what was filed. A full write-up is offered privately (they list
`me@kennethreitz.org` on their GitHub profile) or via GitHub's private reporting
once enabled.

**Finding:** tool parameters are interpolated into AppleScript with no escaping
at all (`itunes_search`, `itunes_play_song`, `itunes_create_playlist`), so a
parameter containing a double quote breaks out of the string literal. Since
AppleScript reaches the shell, the ceiling is command execution as the user.
This is unrelated to our fork — noted here only because the comparison in the
README points readers at that project, and it would be wrong to recommend
looking at it without having told the maintainer first.

# Upstream disclosure status

This fork fixes three security issues that also affect
[epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp).
Publishing fixes for someone else's unreported vulnerabilities is a disclosure
event whether or not it's framed as one, so the status is tracked here.

**Status: NOT YET REPORTED — do not push the `hardened` branch or make this
repository's fixes public until the boxes below are ticked.**

Upstream's `SECURITY.md` asks for
[GitHub Security Advisories](https://github.com/epheterson/applemusic-mcp/security/advisories/new).

## To report

- [ ] **Apple Music URL validation is bypassable** (`applescript.py:1707`,
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

- [ ] **`exports://{filename}` traversal guard is ineffective**
      (`server.py:911`). `is_relative_to` on an unresolved path returns `True`
      for `cache_dir/../../x`, and the check runs *after* `.exists()`. Currently
      mitigated only by the MCP SDK's URI-template matching (`[^/]+`), which is
      an external control, and the branch is marked `pragma: no cover` as
      unreachable — so a change in the SDK would go unnoticed.
      **Fix:** `.resolve()` both sides before comparing; reject absolute paths
      and `..` components up front.

- [ ] **Destructive operations act on a substring guess.**
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

- [ ] `clean_only` silently loses its verification signal in CSV and exported
      JSON — the "could not be verified" note is suppressed for those formats
      because rows are supposed to carry `explicit`, but the CSV field list and
      the JSON export key set both omit the column.
- [ ] `SECURITY.md` documents a `secure_storage` preference that does not exist
      in the code, so a user following it believes they've hardened token
      storage and hasn't.

## After reporting

- [ ] Note the advisory link / date here
- [ ] Give upstream a reasonable window (90 days is the usual default; shorter
      is fine if they respond quickly or the fix ships)
- [ ] Then push `hardened` and make the fork's fixes public
- [ ] Offer the fixes upstream as PRs where they apply cleanly — the URL
      validator and the traversal guard both port over as-is, and the ambiguity
      guard is a port of upstream's own web-rail behaviour to the native rail

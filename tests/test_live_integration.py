"""The live pre-release gate: real mutations against a real Apple Music account.

Why this file exists at all
---------------------------
GitHub CI runs on Ubuntu with no Apple Music account and no signed helper, so it
can only test mocked logic. The paths that actually break in the field — adding
a catalog track to the library, attaching it to a playlist, rating it — are the
ones CI structurally cannot reach. RELEASING.md calls this gate mandatory.

It had stopped existing. Commit d4fc279 removed the amp-api rail and deleted the
previous version of this file, but left `scripts/preflight.sh` invoking it and
`scripts/check_live_env.py` importing the removed module. So `make preflight`
died with an ImportError at step 3 and would have found no tests at step 4 — a
mandatory gate that could not run on any machine, for months, while the rule
"don't bump the version without it" stayed in the docs.

What it covers now
------------------
The MusicKit rail, because that is the rail users are on: a signed helper
signing its own requests, with no credential stored anywhere. The developer
token is an optional rate-limit upgrade and is deliberately NOT required here.

Self-cleaning is a hard requirement, not an aspiration. Every mutation below is
undone by the test that made it, including the rating — which is why
`musickit.unrate_song` exists.

Running it
----------
    TEST_API=1 pytest -o addopts="" -m ui tests/test_live_integration.py

`make preflight` does this for you and refuses to read a SKIP as green.
"""

from __future__ import annotations

import os
import time

import pytest

from applemusic_mcp import musickit, server

pytestmark = pytest.mark.ui

# A real, stable catalog song used as the probe. Numeric catalog id, verified to
# resolve through the public iTunes lookup so a failure here means the account or
# the rail is wrong, not that the fixture rotted.
PROBE_QUERY = "Bohemian Rhapsody Queen"

# conftest sweeps playlists whose names start with these, so debris from a
# crashed run does not accumulate.
PLAYLIST_NAME = "_UI_TEST_musickit_gate"


def _require_live() -> None:
    """Skip only when genuinely not asked to run live; fail loudly otherwise.

    A gate that skips when half-configured is worse than no gate — it reads as
    green. So TEST_API=1 means "I intend to mutate a real account", and anything
    missing after that point is an ERROR, not a skip.
    """
    if os.environ.get("TEST_API") != "1":
        pytest.skip("live gate not requested (set TEST_API=1)")
    if not musickit.is_available():
        pytest.fail(
            "TEST_API=1 but no MusicKit helper is built. Run `make swift` with a "
            "Developer ID — this gate exists to exercise that rail."
        )
    status = musickit.authorization_status()
    if status != "authorized":
        pytest.fail(
            f"TEST_API=1 but the MusicKit helper reports {status!r}. Approve it "
            "once (config(action='signin')) — an unapproved helper cannot mutate."
        )


@pytest.fixture(autouse=True)
def _live_rail():
    """Undo the unit-test isolation for this file only.

    `tests/conftest.py` forces `musickit.is_available()` to False for every test,
    which is exactly right everywhere else — it is what stops the unit suite
    making signed calls against the developer's own account. This gate is the one
    place that genuinely wants the real helper, so it opts back in explicitly.
    """
    import importlib

    importlib.reload(musickit)
    _require_live()
    yield


def _probe_catalog_id() -> str:
    hits = server._catalog_search_itunes(PROBE_QUERY, 1)
    if not hits or not hits[0].get("id"):
        pytest.fail(
            f"Public catalog search could not resolve {PROBE_QUERY!r}. Catalog "
            "resolution is the basis of every add — nothing below can pass."
        )
    return hits[0]["id"]


class TestTheCredentialFreeWriteRail:
    def test_rating_roundtrip(self):
        """Rate a catalog song, then clear it.

        Named for `preflight.sh`, which greps for this exact test PASSING before
        it will call the gate green.
        """
        catalog_id = _probe_catalog_id()

        ok, msg = musickit.rate_song(catalog_id, "love")
        assert ok, f"MusicKit rate failed: {msg}"

        ok, msg = musickit.unrate_song(catalog_id)
        assert ok, (
            f"MusicKit unrate failed: {msg}. The rating IS still set on the "
            "account — clear it by hand before releasing."
        )

    def test_full_mutation_lifecycle(self):
        """Create a playlist, put a catalog track in it, verify, tear it down.

        This is the flow a user actually runs and the one CI cannot: catalog
        resolution over the public endpoint, the library add over MusicKit, the
        attach over Apple Events. Named for `preflight.sh`.
        """
        if not server.APPLESCRIPT_AVAILABLE:
            pytest.fail(
                "The lifecycle needs Music.app over Apple Events. Run this from "
                "an unlocked console session on macOS with Automation granted."
            )

        created = server._playlist_create(PLAYLIST_NAME)
        assert "Created playlist" in created, created

        try:
            result = server._playlist_add(
                playlist=PLAYLIST_NAME,
                track="Bohemian Rhapsody",
                artist="Queen",
                auto_add=True,
                verify=True,
            )
            assert "Error" not in result, result

            # Verify against Music.app rather than trusting the return value:
            # a claimed success that did not land is the failure mode this gate
            # exists to catch.
            deadline = time.monotonic() + 30
            landed = False
            while time.monotonic() < deadline:
                ok, exists = server.asc.track_exists_in_playlist(
                    PLAYLIST_NAME, "Bohemian Rhapsody", "Queen"
                )
                if ok and exists:
                    landed = True
                    break
                time.sleep(1)
            assert landed, f"track never appeared in {PLAYLIST_NAME!r}:\n{result}"
        finally:
            server._playlist_delete(PLAYLIST_NAME)

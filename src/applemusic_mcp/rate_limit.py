"""Rate-limit bookkeeping for Apple Music API calls.

This is what remains of upstream's ``amp_api`` module. Everything that TALKED to
``amp-api.music.apple.com`` — the web player's private host, driven by the
AMPWebPlay token scraped out of Apple's JS bundle plus a harvested
media-user-token cookie — was removed with the rest of the credential
harvesting. What survives is the part that needed no credential at all: the
sticky-429 marker that lets an empty result be reported as "rate limited"
instead of as "no such song".

Kept as its own module (rather than folded into server.py) because it is
stateful and the tests drive it directly.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# --- rate-limit state ------------------------------------------------------
#
# Apple throttles on a ROLLING 60-MINUTE window and returns no ``Retry-After``
# or ``X-Rate-Limit`` header on the web-player path, so remaining quota is
# unobservable — you only learn you're over by getting a 429 (#42). Two
# consequences this state exists to handle:
#
#   1. The reads below swallow non-200 and return empty, so a throttle looks
#      exactly like "no such song". Callers that would otherwise report a
#      false "not found" ask ``throttled_recently()`` instead.
#   2. Probing to find out (``session_status()``) costs a REQUEST, and every
#      request inside a rolling window pushes the recovery further out. So a
#      known-recent 429 short-circuits the probe rather than spending one.

_THROTTLE_STICKY_SECONDS = 120.0

# One rail remains: the official ``api.music.apple.com`` with a developer token.
# Upstream also tracked a ``web`` rail (amp-api + harvested token) separately,
# because the two hosts had independent quotas. That rail is gone.
API = "api"  # api.music.apple.com, developer token
_last_429_at: dict[str, float] = {API: 0.0}


def note_status(code: int, rail: str = API) -> None:
    """Record an HTTP status seen on ``rail``, so an empty result can later be
    attributed to a throttle rather than to "not found".

    The marker means "the last thing Apple told us on this rail was 429" — a
    success on the SAME rail clears it, so a genuine no-such-song is never
    mislabelled just because a throttle happened a minute ago."""
    if 200 <= code < 300:
        _last_429_at[rail] = 0.0
    elif code == 429:
        _last_429_at[rail] = time.monotonic()
        logger.warning(
            "Apple Music returned HTTP 429 (rate limited) on the %s rail. Apple's window "
            "is rolling and ~60 min long, and no Retry-After header is sent. Results may "
            "come back EMPTY rather than as errors until it clears. Resolve by ISRC to "
            "spend 25x fewer requests.",
            rail,
        )


def throttled_recently(
    within: float = _THROTTLE_STICKY_SECONDS, rail: Optional[str] = None
) -> bool:
    """True if a 429 was seen in the last ``within`` seconds — on ``rail`` if
    given, otherwise on either rail (the caller doesn't always know which rail
    produced the empty result it's trying to explain)."""
    now = time.monotonic()
    rails = [rail] if rail else list(_last_429_at)
    return any(_last_429_at[r] > 0 and (now - _last_429_at[r]) < within for r in rails)


def reset_throttle_state() -> None:
    """Clear the sticky 429 markers (tests, and after a confirmed-good call)."""
    for r in _last_429_at:
        _last_429_at[r] = 0.0

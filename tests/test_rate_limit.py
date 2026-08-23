"""Rate-limit bookkeeping — the credential-free remnant of upstream's amp_api.

Upstream's tests/test_amp_api.py covered ~79 cases against
``amp-api.music.apple.com``, the web player's private host, authenticated with
the scraped AMPWebPlay token plus a harvested media-user-token cookie. That
whole rail is gone, so those tests went with it. What remains — and what these
tests pin down — is the sticky-429 marker.

Why it still matters: every read on the API path swallows errors and returns
empty, so "throttled" and "no such song" are indistinguishable at the call
site. The marker is what lets the caller report the real reason. Getting it
wrong is a correctness bug with a safety edge: a clean_only filter that reports
"nothing explicit found" when it was actually rate-limited is claiming a
verification it never performed.
"""

from __future__ import annotations

import pytest

from applemusic_mcp import rate_limit


@pytest.fixture(autouse=True)
def _clean_state():
    rate_limit.reset_throttle_state()
    yield
    rate_limit.reset_throttle_state()


def test_429_sets_the_marker():
    rate_limit.note_status(429)
    assert rate_limit.throttled_recently() is True


def test_success_clears_the_marker():
    rate_limit.note_status(429)
    rate_limit.note_status(200)
    assert rate_limit.throttled_recently() is False


@pytest.mark.parametrize("code", [200, 201, 202, 204])
def test_any_2xx_clears(code):
    rate_limit.note_status(429)
    rate_limit.note_status(code)
    assert rate_limit.throttled_recently() is False


@pytest.mark.parametrize("code", [404, 500, 503])
def test_other_errors_do_not_set_or_clear(code):
    """A 500 is not a throttle, and must not clear a live throttle either."""
    rate_limit.note_status(code)
    assert rate_limit.throttled_recently() is False
    rate_limit.note_status(429)
    rate_limit.note_status(code)
    assert rate_limit.throttled_recently() is True


def test_window_is_respected():
    rate_limit.note_status(429)
    assert rate_limit.throttled_recently(within=60) is True
    assert rate_limit.throttled_recently(within=0) is False


def test_reset_clears():
    rate_limit.note_status(429)
    rate_limit.reset_throttle_state()
    assert rate_limit.throttled_recently() is False


def test_explicit_rail_argument():
    rate_limit.note_status(429, rate_limit.API)
    assert rate_limit.throttled_recently(rail=rate_limit.API) is True


def test_only_the_official_rail_remains():
    """Upstream tracked a second 'web' rail for amp-api. Its removal is the
    point: if a WEB rail reappears, an unofficial credential path came back."""
    assert not hasattr(rate_limit, "WEB")
    assert set(rate_limit._last_429_at) == {rate_limit.API}


def test_module_makes_no_network_calls():
    """The surviving module must be pure bookkeeping — no HTTP client at all.

    Checks imports and URLs rather than raw text: the docstring legitimately
    NAMES amp-api when explaining what was removed.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rate_limit))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "requests" not in imported
    assert "urllib" not in imported
    assert not hasattr(rate_limit, "requests")

    urls = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "http" in n.value
    ]
    assert urls == []

